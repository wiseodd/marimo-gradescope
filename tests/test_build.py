import json
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from mogs import __version__
from mogs import build as build_module
from mogs.build import (
    BuildError,
    BuildResult,
    QuestionScore,
    build,
    data_files,
    failure_report,
    find_notebook,
)
from mogs.cli import main, report
from mogs.config import ConfigError, read_config
from mogs.runner import FailureReason, Outcome

DEMO = Path(__file__).parent.parent / "examples" / "demo"
ZIP_RE = re.compile(r"hw-autograder-\d{4}_\d{2}_\d{2}T\d{2}_\d{2}_\d{2}_\d{6}\.zip")
ZIP_NAMES = ["bundle.json", "data/scores.csv", "requirements.txt", "run_autograder", "setup.sh"]


def demo_copy(target: Path) -> Path:
    shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("dist", "__marimo__"))
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> tuple[Path, BuildResult]:
    folder = demo_copy(tmp_path_factory.mktemp("build") / "demo")
    stale = folder / "dist" / "autograder" / "old-autograder.zip"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    return folder, build(folder)


# Demo build


@pytest.mark.network
def test_artifact_paths(demo):
    folder, result = demo

    assert result.student == folder / "dist" / "student" / "hw.zip"
    assert result.solved == folder / "dist" / "autograder" / "hw.py"
    assert result.zip.parent == folder / "dist" / "autograder"
    assert ZIP_RE.fullmatch(result.zip.name), result.zip.name


@pytest.mark.network
def test_zip_namelist_exact(demo):
    _, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        assert archive.namelist() == ZIP_NAMES


@pytest.mark.network
def test_zip_modes_and_timestamps(demo):
    _, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        infos = archive.infolist()

    modes = {info.filename: stat.S_IMODE(info.external_attr >> 16) for info in infos}

    assert modes == {
        name: 0o755 if name in ("setup.sh", "run_autograder") else 0o644 for name in ZIP_NAMES
    }
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}


@pytest.mark.network
def test_zip_contents(demo):
    folder, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        requirements = archive.read("requirements.txt").decode()
        data = archive.read("data/scores.csv")
        setup = archive.read("setup.sh").decode()

    assert requirements.endswith(f"\nmogs=={__version__}\n")
    assert data == (folder / "data" / "scores.csv").read_bytes()
    assert setup.startswith("#!")


@pytest.mark.network
def test_bundle_has_hidden_tests_and_student_copy_does_not(demo):
    _, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        bundle = json.loads(archive.read("bundle.json"))

    sources = "\n".join(cell["source"] for cell in bundle["cells"])

    with zipfile.ZipFile(result.student) as archive:
        student = archive.read("hw/hw.py").decode()

    for name in ("test_q1_negative", "test_q2_floats"):
        assert name in sources
        assert name not in student

    assert "def test_" not in student
    assert "# BEGIN SOLUTION" not in student
    assert "csv.DictReader" not in student
    assert f'"mogs[pdf]=={__version__}"' in student
    assert "mogs.assignment(" not in student


@pytest.mark.network
def test_dist_has_only_the_artifacts(demo):
    folder, result = demo

    assert tree(folder / "dist") == sorted(
        [
            f"autograder/{result.zip.name}",
            "autograder/data/scores.csv",
            "autograder/hw.py",
            "autograder/requirements.txt",
            "autograder/tests/q1.py",
            "autograder/tests/q2.py",
            "student/hw.zip",
        ]
    )
    source = [name for name in tree(DEMO) if not name.startswith(("dist/", "__marimo__/"))]
    assert tree(folder) == sorted(source + [f"dist/{name}" for name in tree(folder / "dist")])


@pytest.mark.network
def test_student_zip_holds_one_folder_with_the_notebook_tests_and_data(demo):
    folder, result = demo

    with zipfile.ZipFile(result.student) as archive:
        names = archive.namelist()
        infos = archive.infolist()
        data = archive.read("hw/data/scores.csv")

    assert names == ["hw/data/scores.csv", "hw/hw.py", "hw/tests/q1.py", "hw/tests/q2.py"]
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert data == (folder / "data" / "scores.csv").read_bytes()


@pytest.mark.network
def test_public_tests_sit_next_to_both_copies(demo):
    folder, result = demo

    with zipfile.ZipFile(result.student) as archive:
        student = archive.read("hw/tests/q1.py").decode()

    autograder = (folder / "dist" / "autograder" / "tests" / "q1.py").read_text(encoding="utf-8")

    for q1 in (student, autograder):
        assert "def test_q1_whole_numbers():" in q1
        assert "def test_q1_fractions():" in q1
        assert "test_q1_negative" not in q1


@pytest.mark.network
def test_solution_full_marks_and_blank_zero(demo):
    _, result = demo

    assert result.solution == (10.0, 10.0)
    assert result.blank == (0.0, 10.0)
    assert [
        (score.title, score.solution, score.blank, score.public, score.hidden)
        for score in result.scores
    ] == [
        ("Average", 4, 0, 2, 2),
        ("Reading scores", 6, 0, 2, 2),
        ("Empty lists", None, None, 0, 0),
    ]
    assert [score.prompts for score in result.scores] == [
        (),
        (),
        ("## Question 3: Empty lists",),
    ]
    assert not any("blank" in warning for warning in result.warnings)


@pytest.mark.network
def test_solved_copy_and_requirements_next_to_the_zip(demo):
    folder, result = demo
    solved = result.solved.read_text(encoding="utf-8")
    autograder = folder / "dist" / "autograder"

    with zipfile.ZipFile(result.zip) as archive:
        requirements = archive.read("requirements.txt").decode()

    assert (autograder / "requirements.txt").read_text(encoding="utf-8") == requirements
    assert (autograder / "data" / "scores.csv").read_bytes() == (
        folder / "data" / "scores.csv"
    ).read_bytes()
    assert "csv.DictReader" in solved
    assert "return sum(xs) / len(xs)" in solved
    assert "SOLUTION" not in solved
    assert "test_q1_negative" not in solved
    assert "def test_" not in solved
    assert "export_button = mogs.export_button()" in solved
    assert "mogs.assignment(" not in solved


@pytest.mark.network
def test_return_solution_line_grades_full_and_blank_zero(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    edit(
        folder / "hw.py",
        '    """Average of the numbers in `xs`."""\n    # BEGIN SOLUTION\n'
        "    return sum(xs) / len(xs)\n    # END SOLUTION\n",
        "    return sum(xs) / len(xs)  # SOLUTION\n",
    )

    result = build(folder)
    with zipfile.ZipFile(result.student) as archive:
        student = archive.read("hw/hw.py").decode()

    assert "def mean(xs):\n    return ...\n" in student
    assert result.solution == (10.0, 10.0)
    assert result.blank == (0.0, 10.0)


# Failures through the CLI


@pytest.mark.network
def test_broken_solution_fails_naming_the_test(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", "return sum(xs) / len(xs)", "return sum(xs) / (len(xs) + 1)")

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert "test_q1_whole_numbers" in err
    assert "test_q1_negative" in err
    assert "test_q2_reads_scores" not in err
    assert not (folder / "dist").exists()


@pytest.mark.network
def test_self_check_installs_the_header_dependencies(tmp_path, capsys):
    # `tabulate` is not in this test environment, only in the notebook's header.
    folder = demo_copy(tmp_path / "demo")
    edit(
        folder / "hw.py",
        '#     "mogs",\n',
        '#     "mogs",\n#     "tabulate",\n',
    )
    edit(
        folder / "hw.py",
        "    import mogs\n",
        "    import mogs\n    import tabulate\n",
    )

    code = main(["build", str(folder)])

    assert code == 0, capsys.readouterr().err


@pytest.mark.network
def test_blank_score_warning_names_passing_tests(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    edit(
        folder / "hw.py",
        "    def test_q1_fractions():",
        "    def test_q1_trivial():\n        assert True\n\n    def test_q1_fractions():",
    )

    code = main(["build", str(folder)])
    out = capsys.readouterr().out

    assert code == 0
    assert "test_q1_trivial" in out
    [archive] = (folder / "dist" / "autograder").glob("*.zip")
    assert f"Upload to Gradescope  : {archive}" in out
    assert f"Distribute to students: {folder / 'dist' / 'student' / 'hw.zip'}" in out
    assert "Average" in out
    assert "Reading scores" in out


def test_validation_errors_all_listed_and_no_dist(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", 'mogs.grade("q2", points=6', 'mogs.grade("q7", points=6')
    edit(folder / "hw.py", "def test_q1_single", "def test_q9_single")

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert "Question `q7` has no tests" in err
    assert "test_q2_reads_scores" in err
    assert "test_q9_single" in err
    assert not (folder / "dist").exists()


def test_two_candidates_listed(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    shutil.copy(folder / "hw.py", folder / "other.py")

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert "hw.py" in err
    assert "other.py" in err


def test_no_candidate(tmp_path):
    (tmp_path / "plain.py").write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(BuildError, match=r"No notebook.*`mogs init`"):
        find_notebook(tmp_path)


def test_find_notebook_ignores_other_scripts(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    (folder / "helper.py").write_text(
        "# /// script\n# dependencies = []\n# ///\n", encoding="utf-8"
    )

    assert find_notebook(folder) == folder / "hw.py"


def test_find_notebook_follows_renamed_imports(tmp_path):
    (tmp_path / "hw.py").write_text(
        "from mogs import assignment as configure, grade\n\nconfigure()\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text("def assignment():\n    pass\n", encoding="utf-8")

    assert find_notebook(tmp_path) == tmp_path / "hw.py"


def test_find_notebook_keeps_a_notebook_with_a_syntax_error(tmp_path):
    (tmp_path / "hw.py").write_text("import mogs\nmogs.assignment(\n", encoding="utf-8")

    assert find_notebook(tmp_path) == tmp_path / "hw.py"


def test_missing_dir_is_an_error(tmp_path):
    with pytest.raises(BuildError, match="does not exist"):
        find_notebook(tmp_path / "nope")


def test_file_instead_of_dir_is_an_error(tmp_path):
    (tmp_path / "hw.py").write_text("", encoding="utf-8")

    with pytest.raises(BuildError, match="not a folder"):
        find_notebook(tmp_path / "hw.py")


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ('["data/missing.csv"]', "data/missing.csv"),
        ('["../hw.py"]', "inside"),
        ('["/etc/passwd"]', "inside"),
        ('["data/scores.csv", "setup.sh"]', "setup.sh"),
        ('["hw.py"]', "hw.py"),
        ('["submission.py"]', "submission.py"),
        ('["tests"]', "tests"),
    ],
)
def test_bad_data_files(tmp_path, files, message):
    folder = demo_copy(tmp_path / "demo")
    (folder / "setup.sh").write_text("", encoding="utf-8")
    (folder / "tests").mkdir()
    (folder / "tests" / "x.txt").write_text("", encoding="utf-8")
    edit(folder / "hw.py", 'files=["data/scores.csv"]', f"files={files}")

    with pytest.raises(ConfigError, match=message):
        build(folder)

    assert not (folder / "dist").exists()


@pytest.mark.parametrize("name", ["Tests/x.txt", "HW.py", "Setup.sh"])
def test_data_files_cannot_take_reserved_names_in_any_case(tmp_path, name):
    # The files don't exist: the reserved names are checked first. Writing `HW.py` would
    # replace `hw.py` on a case-insensitive file system.
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", 'files=["data/scores.csv"]', f'files=["{name}"]')

    with pytest.raises(ConfigError, match="would replace"):
        build(folder)


def test_help_is_short(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "build" in capsys.readouterr().out


@pytest.mark.network
def test_failed_write_keeps_the_old_dist(tmp_path, monkeypatch, capsys):
    folder = demo_copy(tmp_path / "demo")
    old = folder / "dist" / "student" / "old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")

    def broken(path, entries):
        raise OSError("disk full")

    monkeypatch.setattr(build_module, "write_zip", broken)
    code = main(["build", str(folder)])

    assert code == 1
    assert "disk full" in capsys.readouterr().err
    assert tree(folder / "dist") == ["student/old.txt"]


def test_name_cannot_escape_dist(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", "mogs.assignment(", 'mogs.assignment(name="../../escaped", ')

    with pytest.raises(ConfigError, match="`name`"):
        build(folder)

    assert not (folder / "dist").exists()


def test_windows_is_refused(tmp_path, monkeypatch):
    folder = demo_copy(tmp_path / "demo")
    monkeypatch.setattr(build_module.sys, "platform", "win32")

    with pytest.raises(BuildError, match="Linux or macOS"):
        build(folder)


def test_cli_does_not_import_the_sandbox():
    code = "import sys, mogs.cli; print(sorted(sys.modules))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "mogs.build" in result.stdout
    assert "mogs.grader.sandbox" not in result.stdout
    assert "mogs.grader._process" not in result.stdout


def fake_result(dist: Path, **fields) -> BuildResult:
    return BuildResult(
        student=dist / "student" / "hw.zip",
        zip=dist / "autograder" / "hw-autograder-2026_09_30T12_30_41_890583.zip",
        solved=dist / "autograder" / "hw.py",
        solution=(4.0, 4.0),
        blank=(0.0, 4.0),
        warnings=[],
        **fields,
    )


def test_report_starts_with_what_to_do(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    lines = report(fake_result(tmp_path / "demo" / "dist", scores=())).splitlines()

    assert lines[:4] == [
        "All done!",
        "Upload to Gradescope  : demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip",
        "Distribute to students: demo/dist/student/hw.zip",
        "",
    ]


def test_report_shows_absolute_paths_outside_the_working_directory(tmp_path, monkeypatch):
    dist = tmp_path / "demo" / "dist"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    lines = report(fake_result(dist, scores=())).splitlines()

    assert lines[2] == f"Distribute to students: {dist / 'student' / 'hw.zip'}"


def test_report_shows_manual_questions(tmp_path):
    result = BuildResult(
        student=tmp_path / "dist/student/hw.zip",
        zip=tmp_path / "dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip",
        solved=tmp_path / "dist/autograder/hw.py",
        solution=(4.0, 4.0),
        blank=(0.0, 4.0),
        warnings=["careful"],
        scores=(
            QuestionScore("q1", "Average", 4, 4, 0),
            QuestionScore("q3", "Proof", 5, None, None),
        ),
    )

    text = report(result)

    assert "Proof" in text
    assert "graded by hand" in text
    assert "5 pts" in text
    assert "- careful" in text


def test_report_previews_the_text_of_written_questions(tmp_path):
    result = fake_result(
        tmp_path / "dist",
        scores=(
            QuestionScore("q3", "Proof", 5, None, None, prompts=("## Question 3", "(b) Then")),
            QuestionScore("q4", "Essay", 2, None, None),
        ),
    )

    text = report(result)

    assert (
        "- q3 Proof: 5 pts, graded by hand\n    ## Question 3\n    (b) Then\n"
        "- q4 Essay: 2 pts, graded by hand\n    no question text"
    ) in text


def test_report_says_a_zero_point_manual_question_is_not_graded(tmp_path):
    result = fake_result(
        tmp_path / "dist",
        scores=(
            QuestionScore("q1", "Average", 4, 4, 0),
            QuestionScore("q5", "Proof", 0, None, None),
        ),
    )

    text = report(result)

    assert "Not in the autograder total:\n- q5 Proof: collected in the PDF, not graded" in text
    assert "graded by hand" not in text
    assert "graded on Gradescope" not in text


def test_report_shows_public_and_hidden_test_counts(tmp_path):
    result = BuildResult(
        student=tmp_path / "dist/student/hw.zip",
        zip=tmp_path / "dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip",
        solved=tmp_path / "dist/autograder/hw.py",
        solution=(4.0, 4.0),
        blank=(0.0, 4.0),
        warnings=[],
        scores=(QuestionScore("q1", "Average", 4, 4, 0, public=2, hidden=3),),
    )

    lines = report(result).splitlines()
    header = next(line for line in lines if line.startswith("Question"))
    [row] = [line for line in lines if line.startswith("Average")]

    assert header.split() == ["Question", "Public", "Hidden", "Solution", "Blank"]
    assert row.split() == ["Average", "2", "3", "4/4", "0/4"]


def test_failure_report_merges_identical_messages():
    outcomes = [
        Outcome("test_q1_a", False, "not run: crash", FailureReason.NOT_RUN),
        Outcome("test_q1_b", False, "not run: crash", FailureReason.NOT_RUN),
        Outcome("test_q2_a", False, "boom\nline two", FailureReason.ERROR),
    ]

    assert failure_report(outcomes) == [
        "- test_q1_a, test_q1_b: not run: crash",
        "- test_q2_a: boom\n    line two",
    ]


@pytest.mark.network
def test_zero_point_manual_question_builds_without_warnings(tmp_path):
    # A written answer collected in the PDF but not graded.
    folder = demo_copy(tmp_path / "demo")
    edit(
        folder / "hw.py",
        'mogs.grade("q3", points=5, title="Empty lists")',
        'mogs.grade("q3", points=0, title="Empty lists")',
    )

    result = build(folder)

    assert result.warnings == []
    assert [(score.id, score.points) for score in result.scores][-1] == ("q3", 0)


def test_failure_report_names_questions_for_load_failures():
    setup = "the grader's setup code failed (ModuleNotFoundError: No module named 'torch')"
    outcomes = [
        *(Outcome(f"test_q1_{i}", False, setup, FailureReason.LOAD_FAILED) for i in range(20)),
        *(Outcome(f"test_q2_{i}", False, setup, FailureReason.LOAD_FAILED) for i in range(10)),
        Outcome("test_q3_a", False, "boom", FailureReason.ERROR),
    ]

    assert failure_report(outcomes) == [
        f"- 30 tests of q1, q2: {setup}",
        "- test_q3_a: boom",
    ]


def test_marimo_check_warnings_are_passed_through(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "warning: something odd", "")

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)
    notebook = tmp_path / "hw.py"
    notebook.write_text("", encoding="utf-8")

    assert build_module.marimo_check(notebook, "solved notebook", sys.executable) == [
        "`marimo check` on the solved notebook:\nwarning: something odd"
    ]


def test_data_files_skip_junk(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    for junk in ("data/__pycache__/x.pyc", "data/__marimo__/session.json", "data/.DS_Store"):
        (folder / junk).parent.mkdir(parents=True, exist_ok=True)
        (folder / junk).write_text("junk", encoding="utf-8")
    edit(folder / "hw.py", 'files=["data/scores.csv"]', 'files=["data"]')
    src = (folder / "hw.py").read_text(encoding="utf-8")

    files = data_files(folder, read_config(src, folder / "hw.py"))

    assert [str(rel) for rel in files] == ["data/scores.csv"]


def test_report_says_one_pt(tmp_path):
    result = fake_result(tmp_path / "dist", scores=(QuestionScore("q3", "Proof", 1, None, None),))

    text = report(result)

    assert "- q3 Proof: 1 pt, graded by hand" in text

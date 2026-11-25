import json
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from marimo_gradescope import __version__
from marimo_gradescope import build as build_module
from marimo_gradescope.build import (
    BuildError,
    BuildResult,
    QuestionScore,
    build,
    data_files,
    failure_report,
    find_notebook,
)
from marimo_gradescope.cli import main, report
from marimo_gradescope.config import AssignmentConfig, ConfigError, Question, read_config
from marimo_gradescope.runner import FailureReason, Outcome

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

    assert result.student == folder / "dist" / "student" / "hw.py"
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

    assert requirements.endswith(f"\nmarimo-gradescope=={__version__}\n")
    assert data == (folder / "data" / "scores.csv").read_bytes()
    assert setup.startswith("#!")


@pytest.mark.network
def test_bundle_has_hidden_tests_and_student_copy_does_not(demo):
    _, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        bundle = json.loads(archive.read("bundle.json"))

    sources = "\n".join(cell["source"] for cell in bundle["cells"])
    student = result.student.read_text(encoding="utf-8")

    for name in ("test_q1_negative", "test_q2_floats"):
        assert name in sources
        assert name not in student

    assert "def test_" not in student
    assert "# BEGIN SOLUTION" not in student
    assert "csv.DictReader" not in student
    assert f'"marimo-gradescope[pdf]=={__version__}"' in student
    assert "[tool.marimo-gradescope]" not in student


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
            "student/data/scores.csv",
            "student/hw.py",
            "student/tests/q1.py",
            "student/tests/q2.py",
        ]
    )
    source = [name for name in tree(DEMO) if not name.startswith(("dist/", "__marimo__/"))]
    assert tree(folder) == sorted(source + [f"dist/{name}" for name in tree(folder / "dist")])


@pytest.mark.network
def test_public_tests_sit_next_to_both_copies(demo):
    folder, _ = demo

    for part in ("student", "autograder"):
        q1 = (folder / "dist" / part / "tests" / "q1.py").read_text(encoding="utf-8")

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
    assert "export_button = mgs.export_button()" in solved
    assert "[tool.marimo-gradescope]" not in solved


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
        '#     "marimo-gradescope",\n',
        '#     "marimo-gradescope",\n#     "tabulate",\n',
    )
    edit(
        folder / "hw.py",
        "    import marimo_gradescope as mgs\n",
        "    import marimo_gradescope as mgs\n    import tabulate\n",
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
    assert f"Autograder zip:    {archive}" in out
    assert f"Solved notebook:   {folder / 'dist' / 'autograder' / 'hw.py'}" in out
    assert "Average" in out
    assert "Reading scores" in out


def test_validation_errors_all_listed_and_no_dist(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", "# [tool.marimo.runtime]\n# reactive_tests = false\n", "")
    edit(folder / "hw.py", "def test_q1_single", "def test_q9_single")

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert "reactive_tests = false" in err
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

    with pytest.raises(BuildError, match="No notebook"):
        find_notebook(tmp_path)


def test_find_notebook_ignores_other_scripts(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    (folder / "helper.py").write_text(
        "# /// script\n# dependencies = []\n# ///\n", encoding="utf-8"
    )

    assert find_notebook(folder) == folder / "hw.py"


def test_missing_dir_is_an_error(tmp_path):
    with pytest.raises(BuildError, match="not a folder"):
        find_notebook(tmp_path / "nope")


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
    edit(folder / "hw.py", '# files = ["data/scores.csv"]', f"# files = {files}")

    with pytest.raises(ConfigError, match=message):
        build(folder)

    assert not (folder / "dist").exists()


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
    edit(folder / "hw.py", "# files =", '# name = "../../escaped"\n# files =')

    with pytest.raises(ConfigError, match="`name`"):
        build(folder)

    assert not (folder / "dist").exists()


def test_windows_is_refused(tmp_path, monkeypatch):
    folder = demo_copy(tmp_path / "demo")
    monkeypatch.setattr(build_module.sys, "platform", "win32")

    with pytest.raises(BuildError, match="Linux or macOS"):
        build(folder)


def test_cli_does_not_import_the_sandbox():
    code = "import sys, marimo_gradescope.cli; print(sorted(sys.modules))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "marimo_gradescope.build" in result.stdout
    assert "marimo_gradescope.grader.sandbox" not in result.stdout
    assert "marimo_gradescope.grader._process" not in result.stdout


def test_report_shows_manual_questions():
    result = BuildResult(
        student=Path("dist/student/hw.py"),
        zip=Path("dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip"),
        solved=Path("dist/autograder/hw.py"),
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


def test_report_shows_public_and_hidden_test_counts():
    result = BuildResult(
        student=Path("dist/student/hw.py"),
        zip=Path("dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip"),
        solved=Path("dist/autograder/hw.py"),
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


def test_zero_point_manual_question_warns():
    questions = {
        "q1": Question("q1", "Mean", 0),
        "q2": Question("q2", "Proof", 0, manual=True),
        "q3": Question("q3", "Essay", 5, manual=True),
    }
    cfg = AssignmentConfig("hw", "hw.py", questions)

    [warning] = build_module.question_warnings(cfg)

    assert "q2" in warning
    assert "q1" not in warning
    assert "q3" not in warning
    assert "SOLUTION" in warning


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
    edit(folder / "hw.py", '# files = ["data/scores.csv"]', '# files = ["data"]')
    src = (folder / "hw.py").read_text(encoding="utf-8")

    files = data_files(folder, read_config(src, folder / "hw.py"))

    assert [str(rel) for rel in files] == ["data/scores.csv"]

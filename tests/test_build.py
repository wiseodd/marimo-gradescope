import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from mogs import __version__, _analysis, _native
from mogs._analysis import BuildError, data_files, find_notebook, source_notebook
from mogs._native import main
from mogs.config import ConfigError, read_config

DEMO = Path(__file__).parent.parent / "examples" / "demo"
ZIP_RE = re.compile(r"hw-autograder-\d{4}_\d{2}_\d{2}T\d{2}_\d{2}_\d{2}_\d{6}\.zip")
ZIP_NAMES = ["bundle.json", "data/scores.csv", "requirements.txt", "run_autograder", "setup.sh"]
SCORES = """\
Question        Public  Hidden   Solution      Blank
Average              2       2        4/4        0/4
Reading scores       2       2        6/6        0/6
Total                               10/10       0/10

Not in the autograder total:
- q3 Empty lists: 5 pts, graded by hand
    ## Question 3: Empty lists
"""


def demo_copy(target: Path) -> Path:
    shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("dist", "__marimo__"))
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


@dataclass(frozen=True, slots=True)
class Built:
    """A `mogs build` run: its exit code, what it printed, and where its artifacts are."""

    code: int
    out: str
    err: str
    student: Path
    solved: Path

    @property
    def zip(self) -> Path:
        [archive] = self.solved.parent.glob("*.zip")
        return archive


def build(folder: Path) -> Built:
    """Run `mogs build folder`."""

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["build", str(folder)])

    dist = folder / "dist"
    student = dist / "student" / "hw.zip"

    return Built(code, out.getvalue(), err.getvalue(), student, dist / "autograder" / "hw.py")


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> tuple[Path, Built]:
    folder = demo_copy(tmp_path_factory.mktemp("build") / "demo")
    stale = folder / "dist" / "autograder" / "old-autograder.zip"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    result = build(folder)
    assert result.code == 0, result.err

    return folder, result


# Demo build


@pytest.mark.network
def test_artifact_paths(demo):
    folder, result = demo

    assert result.student.is_file()
    assert result.solved.is_file()
    assert ZIP_RE.fullmatch(result.zip.name), result.zip.name
    assert result.out.splitlines()[:4] == [
        "All done!",
        f"Upload to Gradescope  : {result.zip}",
        f"Distribute to students: {folder / 'dist' / 'student' / 'hw.zip'}",
        "",
    ]
    assert result.err == ""


@pytest.mark.network
def test_report_paths_are_relative_to_the_working_directory(monkeypatch, tmp_path):
    folder = demo_copy(tmp_path / "demo")
    monkeypatch.chdir(tmp_path)

    result = build(folder)

    assert result.code == 0, result.err
    archive = result.zip
    assert result.out.splitlines()[1:3] == [
        f"Upload to Gradescope  : demo/dist/autograder/{archive.name}",
        "Distribute to students: demo/dist/student/hw.zip",
    ]


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
    assert {info.create_system for info in infos} == {3}
    assert {info.compress_type for info in infos} == {zipfile.ZIP_DEFLATED}


@pytest.mark.network
def test_zip_contents(demo):
    folder, result = demo

    with zipfile.ZipFile(result.zip) as archive:
        requirements = archive.read("requirements.txt").decode()
        data = archive.read("data/scores.csv")
        setup = archive.read("setup.sh").decode()
        run_autograder = archive.read("run_autograder").decode()

    assert requirements.endswith(f"\nmogs=={__version__}\n")
    assert data == (folder / "data" / "scores.csv").read_bytes()
    assert setup.startswith("#!")
    assert run_autograder == (
        "#!/usr/bin/env bash\nset -euo pipefail\nexec /opt/mogs-venv/bin/mogs autograde\n"
    )


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
    assert f'"mogs=={__version__}"' in student
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

    assert result.out.split("\n", 4)[4] == SCORES
    assert "blank" not in result.out


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
def test_uv_installer_url_exists(demo):
    # curl, as in setup.sh; the CDN refuses urllib's default user agent.
    _, result = demo
    with zipfile.ZipFile(result.zip) as archive:
        setup = archive.read("setup.sh").decode()

    [url] = re.findall(r"https://astral\.sh/uv/[^/]+/install\.sh", setup)
    command = ["curl", "-fsSLI", "-o", "/dev/null", "-w", "%{http_code}", url]
    found = subprocess.run(command, capture_output=True, text=True, timeout=60)

    assert found.returncode == 0, found.stderr
    assert found.stdout == "200"


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
    assert "Total                               10/10       0/10\n" in result.out


# Failures through the CLI


@pytest.mark.network
def test_broken_solution_fails_naming_the_test(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", "return sum(xs) / len(xs)", "return sum(xs) / (len(xs) + 1)")

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert err.startswith(
        "Build failed.\nThe solved copy of hw.py does not get full marks. Failing tests:\n- "
    )
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
def test_self_check_grades_with_this_package(tmp_path, capsys):
    # A hidden test checks the version of mogs that grades the solved copy.
    folder = demo_copy(tmp_path / "demo")
    edit(
        folder / "hw.py",
        "    def test_q1_negative():\n",
        "    def test_q1_negative():\n        import importlib.metadata\n\n"
        f"        assert importlib.metadata.version('mogs') == {__version__!r}\n",
    )

    code = main(["build", str(folder)])

    assert code == 0, capsys.readouterr().err


@pytest.mark.network
def test_failed_install_names_the_requirements(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    (folder / "requirements.txt").write_text("no-such-package-mogs-test==0.0.1\n", encoding="utf-8")
    edit(folder / "hw.py", "mogs.assignment(", 'mogs.assignment(requirements="requirements.txt", ')

    code = main(["build", str(folder)])
    err = capsys.readouterr().err

    assert code == 1
    assert err.startswith(
        "Build failed.\n`uv` could not install the requirements in the build's grading "
        "environment:\n"
    )
    assert "no-such-package-mogs-test" in err
    assert not (folder / "dist").exists()


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
    assert out.endswith(
        "\n\nWarnings:\n- These tests pass on the blank student notebook, so they give points "
        "for no work: test_q1_trivial\n"
    )
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
    assert err.startswith(f"Build failed.\n{folder / 'hw.py'} has ")
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
    assert err == (
        f"Build failed.\nSeveral notebooks in {folder} call `mogs.assignment(...)`; keep "
        "exactly one:\n  hw.py\n  other.py\n"
    )


def test_no_candidate(tmp_path):
    (tmp_path / "plain.py").write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(BuildError, match=r"No notebook.*`mogs init`"):
        find_notebook(tmp_path)


def test_missing_folder_fails_the_build(tmp_path, capsys):
    code = main(["build", str(tmp_path / "nope")])

    assert code == 1
    assert capsys.readouterr().err == f"Build failed.\n{tmp_path / 'nope'} does not exist.\n"


# What `marimo convert` makes of an otter-assign notebook's raw cells.
CONVERTED_OTTER = """import marimo

app = marimo.App()


@app.cell
def _(hw01):
    # ASSIGNMENT CONFIG
    _name: hw01
    return


@app.cell
def _(q1):
    # BEGIN QUESTION
    _name: q1
    points: 2


if __name__ == "__main__":
    app.run()
"""


def test_no_candidate_names_otter_syntax(tmp_path):
    (tmp_path / "hw.py").write_text(CONVERTED_OTTER, encoding="utf-8")

    with pytest.raises(BuildError) as raised:
        find_notebook(tmp_path)

    message = str(raised.value)
    assert f"{tmp_path / 'hw.py'}:8 " in message
    assert "`# ASSIGNMENT CONFIG`" in message
    assert "otter-assign" in message
    assert "https://mogs.kristia.de/porting" in message


@pytest.mark.parametrize(
    "marker",
    [
        "# BEGIN QUESTION",
        "# END QUESTION",
        "# BEGIN TESTS",
        "# END TESTS",
        '""" # BEGIN PROMPT',
        "# BEGIN TEST CONFIG",
    ],
)
def test_no_candidate_names_each_otter_marker(tmp_path, marker):
    text = f'x = 1\n\n\n@app.cell\ndef _():\n    """\n    {marker}\n    """\n'
    (tmp_path / "hw.py").write_text(text, encoding="utf-8")

    with pytest.raises(BuildError, match=r"hw\.py:7 .*otter-assign"):
        find_notebook(tmp_path)


def test_no_candidate_names_otter_syntax_in_an_unparsable_cell(tmp_path):
    text = 'app._unparsable_cell(\n    r"""\n    # BEGIN QUESTION\n    name: q1\n    """,\n)\n'
    (tmp_path / "hw.py").write_text(text, encoding="utf-8")

    with pytest.raises(BuildError, match=r"hw\.py:3 .*`# BEGIN QUESTION`"):
        find_notebook(tmp_path)


def test_no_candidate_names_otter_syntax_in_an_ipynb(tmp_path):
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Homework 1\n"]},
        {"cell_type": "raw", "metadata": {}, "source": ["# BEGIN QUESTION\n", "name: q1\n"]},
    ]
    notebook = {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (tmp_path / "hw.ipynb").write_text(json.dumps(notebook, indent=1), encoding="utf-8")

    with pytest.raises(BuildError) as raised:
        find_notebook(tmp_path)

    message = str(raised.value)
    assert f"{tmp_path / 'hw.ipynb'}:" in message
    assert "`# BEGIN QUESTION`" in message
    assert "https://mogs.kristia.de/porting" in message


def test_no_candidate_ignores_solution_markers(tmp_path):
    text = "def f():\n    # BEGIN SOLUTION\n    return 1\n    # END SOLUTION\n"
    (tmp_path / "hw.py").write_text(text, encoding="utf-8")

    with pytest.raises(BuildError) as raised:
        find_notebook(tmp_path)

    assert "otter" not in str(raised.value)


def test_source_notebook_names_otter_syntax(tmp_path):
    path = tmp_path / "hw.py"
    path.write_text(CONVERTED_OTTER, encoding="utf-8")

    with pytest.raises(BuildError, match=r"hw\.py:8 .*otter-assign.*/porting"):
        source_notebook(path)


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

    result = build(folder)

    assert result.code == 1
    assert result.err.startswith("Build failed.\nmogs.assignment() `files`")
    assert message in result.err
    assert not (folder / "dist").exists()


@pytest.mark.parametrize("name", ["Tests/x.txt", "HW.py", "Setup.sh"])
def test_data_files_cannot_take_reserved_names_in_any_case(tmp_path, name):
    # The files don't exist: the reserved names are checked first. Writing `HW.py` would
    # replace `hw.py` on a case-insensitive file system.
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", 'files=["data/scores.csv"]', f'files=["{name}"]')

    result = build(folder)

    assert result.code == 1
    assert "would replace" in result.err


def test_help_is_short(capsys):
    assert main(["--help"]) == 0
    assert "build" in capsys.readouterr().out


@pytest.mark.network
def test_failed_self_check_removes_the_old_dist(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    old = folder / "dist" / "student" / "old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    (folder / "dist" / "autograder").mkdir()
    (folder / "dist" / "notes.txt").write_text("mine", encoding="utf-8")
    edit(folder / "hw.py", "return sum(xs) / len(xs)", "return sum(xs) / (len(xs) + 1)")

    code = main(["build", str(folder)])

    assert code == 1
    assert "does not get full marks" in capsys.readouterr().err
    assert tree(folder / "dist") == ["notes.txt"]
    assert not (folder / "dist" / "autograder").exists()


def test_invalid_notebook_removes_the_old_dist(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    old = folder / "dist" / "autograder" / "old.zip"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    edit(folder / "hw.py", "def test_q1_single", "def test_q9_single")

    code = main(["build", str(folder)])

    assert code == 1
    assert not old.parent.exists()


def test_name_cannot_escape_dist(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    edit(folder / "hw.py", "mogs.assignment(", 'mogs.assignment(name="../../escaped", ')

    result = build(folder)

    assert result.code == 1
    assert "`name`" in result.err
    assert not (folder / "dist").exists()


def test_bad_python_version_is_refused(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    (folder / "requirements.txt").write_text("", encoding="utf-8")
    edit(
        folder / "hw.py",
        "mogs.assignment(",
        'mogs.assignment(requirements="requirements.txt", python="latest", ',
    )

    result = build(folder)

    assert result.code == 1
    assert result.err == (
        'Build failed.\nmogs.assignment() `python` must be a version such as "3.13", got '
        "'latest'.\n"
    )
    assert not (folder / "dist").exists()


def test_windows_is_refused(tmp_path, monkeypatch, capsys):
    folder = demo_copy(tmp_path / "demo")
    monkeypatch.setattr(sys, "platform", "win32")

    code = main(["build", str(folder)])

    assert code == 1
    assert capsys.readouterr().err == (
        "Build failed.\nGrading needs Linux or macOS (WSL on Windows).\n"
    )


def test_analysis_and_cli_import_nothing_posix_only():
    # `edit` and the assignment summary use `_analysis` on Windows too.
    code = (
        "import sys, mogs._analysis\n"
        "from mogs._native import main\n"
        "main(['--help'])\n"
        "print(sorted(sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "'mogs._analysis'" in result.stdout
    assert "'mogs.grader.child'" not in result.stdout
    assert "'resource'" not in result.stdout


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

    assert result.code == 0, result.err
    assert "Warnings:" not in result.out
    assert result.out.endswith(
        "Not in the autograder total:\n- q3 Empty lists: collected in the PDF, not graded\n"
        "    ## Question 3: Empty lists\n"
    )


def requirements_file_copy(target: Path, requirements: str) -> Path:
    """The demo, with its requirements from a file instead of the header, so no `uv export`."""

    folder = demo_copy(target)
    (folder / "requirements.txt").write_text(requirements, encoding="utf-8")
    edit(folder / "hw.py", "mogs.assignment(", 'mogs.assignment(requirements="requirements.txt", ')

    return folder


def test_tool_requirement_comes_from_the_environment(tmp_path, monkeypatch):
    folder = requirements_file_copy(tmp_path / "demo", "numpy==2.0\n")
    monkeypatch.setenv("MOGS_TOOL_REQUIREMENT", "mogs @ file:///wheels/mogs.whl")

    inputs = _analysis.build_inputs(_analysis.read_assignment(folder))

    assert inputs["requirements"] == "numpy==2.0\nmogs @ file:///wheels/mogs.whl\n"
    assert inputs["grading_requirements"] == "numpy==2.0\n"


@pytest.mark.parametrize("value", [None, ""])
def test_tool_requirement_defaults_to_this_version(tmp_path, monkeypatch, value):
    folder = requirements_file_copy(tmp_path / "demo", "numpy==2.0\n")
    if value is None:
        monkeypatch.delenv("MOGS_TOOL_REQUIREMENT", raising=False)
    else:
        monkeypatch.setenv("MOGS_TOOL_REQUIREMENT", value)

    inputs = _analysis.build_inputs(_analysis.read_assignment(folder))

    assert inputs["requirements"] == f"numpy==2.0\nmogs=={__version__}\n"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root reads any file")
def test_bad_python_version_comes_before_an_unreadable_data_file(tmp_path):
    folder = requirements_file_copy(tmp_path / "demo", "")
    edit(folder / "hw.py", "mogs.assignment(", 'mogs.assignment(python="latest", ')
    (folder / "data" / "scores.csv").chmod(0)

    result = build(folder)

    assert result.code == 1
    assert result.err == (
        'Build failed.\nmogs.assignment() `python` must be a version such as "3.13", got '
        "'latest'.\n"
    )


@pytest.mark.network
def test_os_error_names_dist_as_python_shows_it(tmp_path, monkeypatch):
    # `dist` is a file, so it cannot be made: v0.7 named it `dist`, not `./dist`.
    folder = demo_copy(tmp_path / "demo")
    (folder / "dist").write_text("", encoding="utf-8")
    monkeypatch.chdir(folder)

    result = build(Path("."))

    assert result.code == 1
    assert result.err == "Build failed.\n[Errno 17] File exists: 'dist'\n"


def test_build_inputs_give_the_prompts_of_written_questions(tmp_path):
    folder = requirements_file_copy(tmp_path / "demo", "")

    inputs = _analysis.build_inputs(_analysis.read_assignment(folder))

    assert inputs["prompts"] == {"q3": ["## Question 3: Empty lists"]}
    assert [rel for rel, _ in inputs["data"]] == ["data/scores.csv"]
    assert [rel for rel, _ in inputs["tests"]] == ["tests/q1.py", "tests/q2.py"]


def test_written_question_without_prompt_text_says_so(tmp_path, monkeypatch):
    folder = requirements_file_copy(tmp_path / "demo", "")
    monkeypatch.setattr(_analysis, "prompt_previews", lambda nb, qid: ())

    inputs = _analysis.build_inputs(_analysis.read_assignment(folder))

    assert inputs["prompts"] == {"q3": ["no question text"]}


@pytest.mark.skipif(sys.platform != "linux", reason="needs a file system of bytes")
def test_missing_data_file_names_a_folder_that_is_not_utf_8(tmp_path):
    root = Path(os.fsdecode(bytes(tmp_path) + b"/\xff"))
    try:
        root.mkdir()
    except OSError as error:
        pytest.skip(f"the file system refuses the name: {error}")

    with pytest.raises(ConfigError) as error:
        _native.data_files(root, ["nope.csv"])

    assert str(error.value) == (
        f"mogs.assignment() `files`: data file 'nope.csv' not found in {root}."
    )


def test_data_files_skip_junk(tmp_path):
    folder = demo_copy(tmp_path / "demo")
    for junk in ("data/__pycache__/x.pyc", "data/__marimo__/session.json", "data/.DS_Store"):
        (folder / junk).parent.mkdir(parents=True, exist_ok=True)
        (folder / junk).write_text("junk", encoding="utf-8")
    edit(folder / "hw.py", 'files=["data/scores.csv"]', 'files=["data"]')
    src = (folder / "hw.py").read_text(encoding="utf-8")

    files = data_files(folder, read_config(src, folder / "hw.py"))

    assert [str(rel) for rel in files] == ["data/scores.csv"]

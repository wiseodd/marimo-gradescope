import os
import re
import subprocess
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path

import pytest

from mogs._analysis import read_assignment
from mogs._native import main
from mogs.source import CellKind

SCRIPT = Path(sysconfig.get_path("scripts")) / ("mogs.exe" if os.name == "nt" else "mogs")


def init(folder: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """Run `mogs init folder` and return the notebook it created."""

    code = main(["init", str(folder)])
    out = capsys.readouterr().out

    assert code == 0, out
    assert out.startswith("Created ")

    return Path(out.splitlines()[0].removeprefix("Created "))


def test_init_writes_one_notebook_named_after_the_folder(tmp_path, capsys):
    path = init(tmp_path / "A12", capsys)

    assert path == tmp_path / "A12" / "A12.py"
    assert [p.name for p in (tmp_path / "A12").iterdir()] == ["A12.py"]


def test_init_notebook_is_a_valid_assignment(tmp_path, capsys):
    init(tmp_path / "A12", capsys)

    assignment = read_assignment(tmp_path / "A12")

    assert assignment.cfg.name == "A12"
    assert [(q.id, q.points, q.manual) for q in assignment.cfg.questions.values()] == [
        ("q1", 4, False),
        ("q2", 5, True),
    ]


def test_init_notebook_has_a_guide_for_the_instructor(tmp_path, capsys):
    text = init(tmp_path / "A12", capsys).read_text(encoding="utf-8")

    assert "def instructor_guide():" in text
    assert "examples" in text
    assert "https://mogs.kristia.de/instructors" in text


def test_init_notebook_imports_the_package_as_mogs(tmp_path, capsys):
    text = init(tmp_path / "A12", capsys).read_text(encoding="utf-8")

    assert "    import mogs\n" in text
    assert "mgs." not in text.replace("mogs.", "")
    assert 'mogs.assignment(name="A12")' in text


def test_init_notebook_leaves_titles_to_their_default(tmp_path, capsys):
    text = init(tmp_path / "A12", capsys).read_text(encoding="utf-8")

    assert "title" not in text
    assert 'mogs.grade("q1", points=4)' in text
    assert 'mogs.grade("q2", points=5)' in text


def test_init_notebook_names_only_the_guide(tmp_path, capsys):
    init(tmp_path / "A12", capsys)

    cells = read_assignment(tmp_path / "A12").nb.cells
    named = [cell.name for cell in cells if cell.name not in ("_", "setup")]

    assert named == ["instructor_guide", "mean"]


def test_init_notebook_tests_are_cells_as_in_the_docs(tmp_path, capsys):
    init(tmp_path / "A12", capsys)

    cells = read_assignment(tmp_path / "A12").nb.cells
    tests = [(cell.kind, cell.tests, cell.hidden) for cell in cells if cell.tests]

    assert tests == [
        (CellKind.CELL, ("test_q1_basic",), False),
        (CellKind.CELL, ("test_q1_single",), True),
    ]


def test_init_notebook_hidden_marker_opens_its_cell(tmp_path, capsys):
    text = init(tmp_path / "A12", capsys).read_text(encoding="utf-8")

    assert "@app.cell\ndef _():\n    # HIDDEN\n    def test_q1_single():\n" in text


def test_init_notebook_names_the_installed_marimo(tmp_path, capsys):
    text = init(tmp_path / "A12", capsys).read_text(encoding="utf-8")

    assert f'__generated_with = "{version("marimo")}"\n' in text


def test_init_notebook_has_the_line_endings_of_text_files(tmp_path, capsys):
    data = init(tmp_path / "A12", capsys).read_bytes()

    assert (b"\r\n" in data) == (os.linesep == "\r\n")
    assert data.endswith(b"    app.run()" + os.linesep.encode())


def test_init_quotes_the_name(tmp_path, capsys):
    init(tmp_path / "Bob's HW", capsys)

    assert read_assignment(tmp_path / "Bob's HW").cfg.name == "Bob's HW"


def test_init_dot_takes_the_name_of_the_current_folder(tmp_path, monkeypatch, capsys):
    (tmp_path / "hw").mkdir()
    monkeypatch.chdir(tmp_path / "hw")

    path = init(Path("."), capsys)

    assert path == Path("hw.py")
    assert read_assignment(tmp_path / "hw").cfg.name == "hw"


def test_init_names_the_folder_after_its_resolved_path(tmp_path, monkeypatch, capsys):
    (tmp_path / "hw").mkdir()
    monkeypatch.chdir(tmp_path / "hw")

    path = init(Path("new") / "..", capsys)

    assert path == Path("new") / ".." / "hw.py"
    assert read_assignment(tmp_path / "hw").cfg.name == "hw"


def test_init_into_an_empty_folder(tmp_path, capsys):
    (tmp_path / "A12").mkdir()

    assert init(tmp_path / "A12", capsys).exists()


def test_init_makes_the_missing_parents(tmp_path, capsys):
    assert init(tmp_path / "course" / "A12", capsys) == tmp_path / "course" / "A12" / "A12.py"


def test_init_refuses_a_folder_with_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "A12").mkdir()
    (tmp_path / "A12" / "notes.txt").write_text("mine", encoding="utf-8")

    code = main(["init", "A12"])

    assert code == 1
    assert capsys.readouterr().err == (
        "Init failed.\nA12 is not empty. Pick a new folder for the assignment.\n"
    )
    assert (tmp_path / "A12" / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_init_refuses_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "A12").write_text("", encoding="utf-8")

    code = main(["init", "A12"])

    assert code == 1
    assert capsys.readouterr().err == "Init failed.\nA12 is not a folder.\n"


def test_init_refuses_a_name_unfit_for_windows(tmp_path, capsys):
    code = main(["init", str(tmp_path / "con")])

    assert code == 1
    assert capsys.readouterr().err.startswith(
        "Init failed.\nAssignment name: `name` 'con' is not a valid file name on Windows."
    )
    assert not (tmp_path / "con").exists()


def test_init_names_a_dangling_link_after_its_target(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    try:
        Path("hw").symlink_to("con")
    except OSError as error:
        pytest.skip(f"cannot make a symlink: {error}")

    code = main(["init", "hw"])

    assert code == 1
    assert capsys.readouterr().err.startswith(
        "Init failed.\nAssignment name: `name` 'con' is not a valid file name on Windows."
    )


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_init_reports_a_folder_it_cannot_look_at(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "locked").mkdir(mode=0o000)

    try:
        with pytest.raises(OSError) as python:
            Path("locked", "hw").exists()

        code = main(["init", str(Path("locked", "hw"))])
    finally:
        (tmp_path / "locked").chmod(0o755)

    assert code == 1
    assert capsys.readouterr().err == f"Init failed.\n{python.value}\n"


def test_init_reports_a_folder_it_cannot_make(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f").write_text("", encoding="utf-8")

    with pytest.raises(OSError) as python:
        Path("f", "sub").mkdir(parents=True, exist_ok=True)

    code = main(["init", str(Path("f", "sub"))])

    assert code == 1
    assert capsys.readouterr().err == f"Init failed.\n{python.value}\n"


def test_cli_init_says_what_to_do_next(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    code = main(["init", "A12"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.splitlines() == [
        f"Created {Path('A12', 'A12.py')}",
        "Edit it  : mogs edit A12",
        "Build it : mogs build A12",
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="needs a file system of bytes")
@pytest.mark.parametrize("folder", [b"p\xff/hw", b"h\xffw"])
def test_cli_init_keeps_bytes_that_are_not_utf_8(folder, tmp_path):
    try:
        os.mkdir(bytes(tmp_path) + b"/\xff")
    except OSError as error:
        pytest.skip(f"the file system refuses the name: {error}")

    name = folder.rsplit(b"/", 1)[-1]
    env = dict(os.environ, PYTHONIOENCODING="utf-8:surrogateescape")

    result = subprocess.run(
        [SCRIPT, "init", os.fsdecode(folder)], cwd=tmp_path, env=env, capture_output=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        b"Created " + folder + b"/" + name + b".py",
        b"Edit it  : mogs edit '" + folder + b"'",
        b"Build it : mogs build '" + folder + b"'",
    ]
    assert os.path.isfile(bytes(tmp_path) + b"/" + folder + b"/" + name + b".py")


def test_cli_init_shows_paths_as_python_does(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    code = main(["init", "./Bob's HW/"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.splitlines() == [
        f"Created {Path("Bob's HW", "Bob's HW.py")}",
        "Edit it  : mogs edit 'Bob'\"'\"'s HW'",
        "Build it : mogs build 'Bob'\"'\"'s HW'",
    ]


@pytest.mark.network
@pytest.mark.skipif(sys.platform == "win32", reason="build needs Linux or macOS")
def test_init_notebook_builds(tmp_path, capsys):
    init(tmp_path / "A12", capsys)

    code = main(["build", str(tmp_path / "A12")])
    out = capsys.readouterr().out

    assert code == 0
    assert re.search(r"^Total +4/4 +0/4$", out, re.MULTILINE), out
    assert "Warnings:" not in out

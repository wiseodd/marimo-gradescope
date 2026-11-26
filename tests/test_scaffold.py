from importlib.metadata import version
from pathlib import Path

import pytest

from mogs.build import build, read_assignment
from mogs.cli import main
from mogs.config import ConfigError
from mogs.scaffold import InitError, init
from mogs.source import CellKind


def test_init_writes_one_notebook_named_after_the_folder(tmp_path):
    path = init(tmp_path / "A12")

    assert path == tmp_path / "A12" / "A12.py"
    assert [p.name for p in (tmp_path / "A12").iterdir()] == ["A12.py"]


def test_init_notebook_is_a_valid_assignment(tmp_path):
    init(tmp_path / "A12")

    assignment = read_assignment(tmp_path / "A12")

    assert assignment.cfg.name == "A12"
    assert [(q.id, q.points, q.manual) for q in assignment.cfg.questions.values()] == [
        ("q1", 4, False),
        ("q2", 5, True),
    ]


def test_init_notebook_has_a_guide_for_the_instructor(tmp_path):
    text = init(tmp_path / "A12").read_text(encoding="utf-8")

    assert "def instructor_guide():" in text
    assert "examples" in text
    assert "https://mogs.kristia.de/instructors" in text


def test_init_notebook_imports_the_package_as_mogs(tmp_path):
    text = init(tmp_path / "A12").read_text(encoding="utf-8")

    assert "    import mogs\n" in text
    assert "mgs." not in text.replace("mogs.", "")
    assert 'mogs.assignment(name="A12")' in text


def test_init_notebook_leaves_titles_to_their_default(tmp_path):
    text = init(tmp_path / "A12").read_text(encoding="utf-8")

    assert "title" not in text
    assert 'mogs.grade("q1", points=4)' in text
    assert 'mogs.grade("q2", points=5)' in text


def test_init_notebook_names_only_the_guide(tmp_path):
    init(tmp_path / "A12")

    cells = read_assignment(tmp_path / "A12").nb.cells
    named = [cell.name for cell in cells if cell.name not in ("_", "setup")]

    assert named == ["instructor_guide", "mean"]


def test_init_notebook_tests_are_cells_as_in_the_docs(tmp_path):
    init(tmp_path / "A12")

    cells = read_assignment(tmp_path / "A12").nb.cells
    tests = [(cell.kind, cell.tests, cell.hidden) for cell in cells if cell.tests]

    assert tests == [
        (CellKind.CELL, ("test_q1_basic",), False),
        (CellKind.CELL, ("test_q1_single",), True),
    ]


def test_init_notebook_hidden_marker_opens_its_cell(tmp_path):
    text = init(tmp_path / "A12").read_text(encoding="utf-8")

    assert "@app.cell\ndef _():\n    # HIDDEN\n    def test_q1_single():\n" in text


def test_init_notebook_names_the_installed_marimo(tmp_path):
    text = init(tmp_path / "A12").read_text(encoding="utf-8")

    assert f'__generated_with = "{version("marimo")}"\n' in text


def test_init_quotes_the_name(tmp_path):
    init(tmp_path / "Bob's HW")

    assert read_assignment(tmp_path / "Bob's HW").cfg.name == "Bob's HW"


def test_init_dot_takes_the_name_of_the_current_folder(tmp_path, monkeypatch):
    (tmp_path / "hw").mkdir()
    monkeypatch.chdir(tmp_path / "hw")

    path = init(Path("."))

    assert path == Path("hw.py")
    assert read_assignment(tmp_path / "hw").cfg.name == "hw"


def test_init_into_an_empty_folder(tmp_path):
    (tmp_path / "A12").mkdir()

    assert init(tmp_path / "A12").exists()


def test_init_refuses_a_folder_with_files(tmp_path):
    (tmp_path / "A12").mkdir()
    (tmp_path / "A12" / "notes.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(InitError, match="not empty"):
        init(tmp_path / "A12")

    assert (tmp_path / "A12" / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_init_refuses_a_file(tmp_path):
    (tmp_path / "A12").write_text("", encoding="utf-8")

    with pytest.raises(InitError, match="not a folder"):
        init(tmp_path / "A12")


def test_init_refuses_a_name_unfit_for_windows(tmp_path):
    with pytest.raises(ConfigError, match="Windows"):
        init(tmp_path / "con")

    assert not (tmp_path / "con").exists()


def test_cli_init_says_what_to_do_next(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    code = main(["init", "A12"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.splitlines() == [
        "Created A12/A12.py",
        "Edit it  : mogs edit A12",
        "Build it : mogs build A12",
    ]


def test_cli_init_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "A12").mkdir()
    (tmp_path / "A12" / "x.py").write_text("", encoding="utf-8")

    code = main(["init", "A12"])

    assert code == 1
    assert "not empty" in capsys.readouterr().err


@pytest.mark.network
def test_init_notebook_builds(tmp_path):
    init(tmp_path / "A12")

    result = build(tmp_path / "A12")

    assert result.solution == (4, 4)
    assert result.blank == (0, 4)
    assert result.warnings == []

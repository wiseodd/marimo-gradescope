import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from marimogs import cli

SOLUTION = Path(__file__).parent / "fixtures" / "solution.py"


class FakeMarimo:
    """Stands in for `subprocess.Popen`; records the command and plays back `wait` results."""

    def __init__(self, *waits: int | type[BaseException]) -> None:
        self.waits = list(waits)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> "FakeMarimo":
        self.commands.append(command)
        return self

    def wait(self) -> int:
        result = self.waits.pop(0)

        if isinstance(result, int):
            return result

        raise result


@pytest.fixture
def assignment(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hw").mkdir()
    shutil.copy(SOLUTION, tmp_path / "hw" / "hw.py")

    return Path("hw")


def test_edit_runs_marimo_from_its_own_python(assignment, monkeypatch):
    marimo = FakeMarimo(0)
    monkeypatch.setattr(subprocess, "Popen", marimo)

    code = cli.main(["edit", str(assignment)])

    assert code == 0
    assert marimo.commands == [
        [sys.executable, "-m", "marimo", "edit", "--sandbox", str(assignment / "hw.py")]
    ]


def test_edit_returns_marimo_exit_code(assignment, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", FakeMarimo(3))

    assert cli.main(["edit", str(assignment)]) == 3


def test_edit_leaves_ctrl_c_to_marimo(assignment, monkeypatch):
    marimo = FakeMarimo(KeyboardInterrupt, KeyboardInterrupt, 0)
    monkeypatch.setattr(subprocess, "Popen", marimo)

    code = cli.main(["edit", str(assignment)])

    assert code == 0
    assert marimo.waits == []


def test_edit_without_a_notebook_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hw").mkdir()
    marimo = FakeMarimo()
    monkeypatch.setattr(subprocess, "Popen", marimo)

    code = cli.main(["edit", "hw"])

    assert code == 1
    assert marimo.commands == []
    assert "No notebook in hw" in capsys.readouterr().err


def test_edit_takes_the_notebook_itself(assignment, monkeypatch):
    marimo = FakeMarimo(0)
    monkeypatch.setattr(subprocess, "Popen", marimo)

    code = cli.main(["edit", str(assignment / "hw.py")])

    assert code == 0
    assert marimo.commands[0][-1] == str(assignment / "hw.py")


def test_edit_refuses_a_file_that_is_not_a_source_notebook(assignment, monkeypatch, capsys):
    (assignment / "helper.py").write_text("print('hi')\n", encoding="utf-8")
    marimo = FakeMarimo()
    monkeypatch.setattr(subprocess, "Popen", marimo)

    code = cli.main(["edit", str(assignment / "helper.py")])

    assert code == 1
    assert marimo.commands == []
    assert "helper.py does not call `mogs.assignment(...)`" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["nope", "nope.py"])
def test_edit_names_a_missing_path(missing, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", FakeMarimo())

    code = cli.main(["edit", missing])

    assert code == 1
    assert f"{missing} does not exist" in capsys.readouterr().err

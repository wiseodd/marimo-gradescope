import _thread
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mogs._native import main

SOLUTION = Path(__file__).parent / "fixtures" / "solution.py"

# Stands in for marimo: `python -m marimo` finds it first on PYTHONPATH. It records its
# command line, then, if told to, waits for the test to let it go.
FAKE_MARIMO = """\
import json
import os
import sys
import time
from pathlib import Path

folder = Path(os.environ["FAKE_MARIMO"])
# On macOS a venv's python starts the framework binary, which then is `orig_argv[0]`, but
# `sys.executable` stays the venv's python that was started.
command = [sys.executable, *sys.orig_argv[1:]]
(folder / "command.json").write_text(json.dumps(command), encoding="utf-8")

if (folder / "hold").exists():
    (folder / "started").touch()
    deadline = time.monotonic() + 60
    while not (folder / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.01)

sys.exit(int((folder / "exit").read_text(encoding="utf-8")))
"""


class FakeMarimo:
    """The folder of the fake marimo, which records its command and exits with `code`."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def exit_with(self, code: int) -> None:
        (self.folder / "exit").write_text(str(code), encoding="utf-8")

    def hold(self) -> None:
        (self.folder / "hold").touch()

    @property
    def commands(self) -> list[list[str]]:
        command = self.folder / "command.json"

        return [json.loads(command.read_text(encoding="utf-8"))] if command.exists() else []


@pytest.fixture
def marimo(tmp_path, monkeypatch) -> FakeMarimo:
    package = tmp_path / "fake" / "marimo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(FAKE_MARIMO, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    monkeypatch.setenv("FAKE_MARIMO", str(package))
    fake = FakeMarimo(package)
    fake.exit_with(0)

    return fake


@pytest.fixture
def assignment(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hw").mkdir()
    shutil.copy(SOLUTION, tmp_path / "hw" / "hw.py")
    (tmp_path / "hw" / "requirements.txt").write_text("numpy\n", encoding="utf-8")

    return Path("hw")


def test_edit_runs_marimo_from_its_own_python(assignment, marimo):
    code = main(["edit", str(assignment)])

    assert code == 0
    assert marimo.commands == [
        [sys.executable, "-m", "marimo", "edit", "--sandbox", str(assignment / "hw.py")]
    ]


def test_edit_returns_marimo_exit_code(assignment, marimo):
    marimo.exit_with(3)

    assert main(["edit", str(assignment)]) == 3


def test_edit_leaves_ctrl_c_to_marimo(assignment, marimo):
    marimo.hold()

    def press_ctrl_c_twice() -> None:
        deadline = time.monotonic() + 60
        while not (marimo.folder / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        for _ in range(2):
            _thread.interrupt_main()
            time.sleep(0.2)

        (marimo.folder / "go").touch()

    keys = threading.Thread(target=press_ctrl_c_twice)
    keys.start()
    code = main(["edit", str(assignment)])
    keys.join()

    assert code == 0
    assert len(marimo.commands) == 1


def test_edit_without_a_notebook_fails(tmp_path, monkeypatch, capsys, marimo):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hw").mkdir()

    code = main(["edit", "hw"])

    assert code == 1
    assert marimo.commands == []
    assert "No notebook in hw" in capsys.readouterr().err


def test_edit_takes_the_notebook_itself(assignment, marimo):
    code = main(["edit", str(assignment / "hw.py")])

    assert code == 0
    assert marimo.commands[0][-1] == str(assignment / "hw.py")


def test_edit_refuses_a_file_that_is_not_a_source_notebook(assignment, marimo, capsys):
    (assignment / "helper.py").write_text("print('hi')\n", encoding="utf-8")

    code = main(["edit", str(assignment / "helper.py")])

    assert code == 1
    assert marimo.commands == []
    assert "helper.py does not call `mogs.assignment(...)`" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["nope", "nope.py"])
def test_edit_names_a_missing_path(missing, tmp_path, monkeypatch, capsys, marimo):
    monkeypatch.chdir(tmp_path)

    code = main(["edit", missing])

    assert code == 1
    assert capsys.readouterr().err == f"Edit failed.\n{missing} does not exist.\n"


def test_edit_reports_a_failed_launch(assignment, tmp_path, monkeypatch, capsys):
    missing = str(tmp_path / "no-python")

    with pytest.raises(OSError) as python:
        subprocess.Popen([missing])

    monkeypatch.setattr(sys, "executable", missing)
    code = main(["edit", str(assignment)])

    assert code == 1
    assert capsys.readouterr().err == f"Edit failed.\n{python.value}\n"


def test_edit_writes_the_header_from_the_requirements_first(assignment, marimo, capsys):
    notebook = assignment / "hw.py"
    text = notebook.read_text(encoding="utf-8")
    text = text.replace('#     "mogs",\n', '#     "mogs",\n#     "scipy",\n')
    notebook.write_text(text, encoding="utf-8")

    code = main(["edit", str(assignment)])

    assert code == 0
    assert notebook.read_text(encoding="utf-8").startswith(
        "# /// script\n"
        '# requires-python = "==3.13.*"\n'
        "# dependencies = [\n"
        '#     "numpy",\n'
        '#     "mogs",\n'
        "# ]\n"
        "# ///\n"
    )
    assert capsys.readouterr().err == (
        f"Warning: The header of {notebook} listed 'scipy', which "
        f"{assignment / 'requirements.txt'} does not. Add them to "
        f"{assignment / 'requirements.txt'}; mogs rewrites the header from it.\n"
    )
    assert len(marimo.commands) == 1


def test_edit_without_a_requirements_file_fails(assignment, marimo, capsys):
    (assignment / "requirements.txt").unlink()

    code = main(["edit", str(assignment)])

    assert code == 1
    assert marimo.commands == []
    assert "requirements.txt" in capsys.readouterr().err

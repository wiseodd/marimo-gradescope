import subprocess
import sys
from importlib.metadata import version

import pytest

from mogs.cli import main


def test_help_names_the_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    assert capsys.readouterr().out.startswith("usage: mogs ")


def test_module_runs_the_command():
    result = subprocess.run(
        [sys.executable, "-m", "mogs.cli", "--version"], capture_output=True, text=True
    )

    assert result.returncode == 0
    assert result.stdout == f"mogs {version('mogs')}\n"

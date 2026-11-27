import os
import subprocess
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path

from mogs import _native

SCRIPT = Path(sysconfig.get_path("scripts")) / ("mogs.exe" if os.name == "nt" else "mogs")


def mogs(*args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([SCRIPT, *args], capture_output=True, text=True, cwd=cwd)


def test_version_matches_package():
    assert _native.version() == version("mogs")


def test_main_returns_the_exit_code(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing-dir"
    monkeypatch.setattr(sys, "argv", ["mogs", "build", str(missing)])

    assert _native.main() == 1
    assert capsys.readouterr().err == f"Build failed.\n{missing} does not exist.\n"


def test_command_prints_version():
    result = mogs("--version")

    assert result.returncode == 0
    assert result.stdout == f"mogs {version('mogs')}\n"


def test_command_runs_the_cli(tmp_path):
    result = mogs("build", "missing-dir", cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Build failed.\nmissing-dir does not exist.\n"


def test_command_through_symlink(tmp_path):
    link = tmp_path / SCRIPT.name
    link.symlink_to(SCRIPT)

    result = subprocess.run([link, "--version"], capture_output=True, text=True)

    assert result.returncode == 0
    assert result.stdout == f"mogs {version('mogs')}\n"


def test_command_ignores_modules_in_working_directory(tmp_path):
    (tmp_path / "packaging.py").write_text("raise SystemExit('shadowed')\n")

    result = mogs("--version", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"mogs {version('mogs')}\n"

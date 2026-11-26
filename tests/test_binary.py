import os
import subprocess
import sysconfig
from importlib.metadata import version

import pytest

from mogs._binary import BinaryError, binary, run


def test_binary_is_in_scripts():
    assert binary().name == ("mogs.exe" if os.name == "nt" else "mogs")


def test_binary_version_matches_package():
    result = run(["--version"])

    assert result.returncode == 0
    assert result.stdout == "mogs 0.7.0\n"
    assert result.stdout == f"mogs {version('mogs')}\n"


def test_binary_forwards_to_python_cli(tmp_path):
    missing = tmp_path / "missing-dir"

    result = run(["build", str(missing)])

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"Build failed.\n{missing} does not exist.\n"


def test_binary_found_through_symlink(tmp_path):
    link = tmp_path / binary().name
    link.symlink_to(binary())

    result = subprocess.run([link, "--version"], capture_output=True, text=True)

    assert result.returncode == 0
    assert result.stdout == f"mogs {version('mogs')}\n"


def test_binary_missing_names_paths(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    user = tmp_path / "user"
    paths = {None: scripts, f"{os.name}_user": user}
    monkeypatch.setattr(sysconfig, "get_path", lambda name, scheme=None: str(paths[scheme]))

    with pytest.raises(BinaryError) as error:
        binary()

    assert str(scripts) in str(error.value)
    assert str(user) in str(error.value)


def test_binary_is_a_file_not_a_folder(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    (scripts / binary().name).mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_path", lambda name, scheme=None: str(scripts))

    with pytest.raises(BinaryError):
        binary()


def test_binary_ignores_modules_in_working_directory(tmp_path):
    (tmp_path / "packaging.py").write_text("raise SystemExit('shadowed')\n")

    result = subprocess.run([binary(), "--version"], capture_output=True, text=True, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"mogs {version('mogs')}\n"

import shutil
import subprocess
import sys
from importlib.metadata import metadata, version
from pathlib import Path

import pytest

from mogs import _native

REPO = Path(__file__).parent.parent


def mogs(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([script, *args], capture_output=True, text=True, cwd=cwd)


def test_version_matches_package(capsys):
    assert _native.main(["--version"]) == 0
    assert capsys.readouterr().out == f"mogs {version('mogs')}\n"


def test_metadata_has_the_readme_links_and_platforms():
    meta = metadata("mogs")

    assert meta.json.get("description_content_type") == "text/markdown; charset=UTF-8; variant=GFM"
    assert "mogs.kristia.de" in str(meta.json.get("description"))
    assert sorted(meta.get_all("Project-URL") or []) == [
        "Documentation, https://mogs.kristia.de",
        "Issues, https://github.com/wiseodd/mogs/issues",
        "Repository, https://github.com/wiseodd/mogs",
    ]

    systems = [c for c in meta.get_all("Classifier") or [] if c.startswith("Operating System")]
    assert sorted(systems) == [
        "Operating System :: MacOS",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX :: Linux",
    ]


def test_main_returns_the_exit_code(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing-dir"
    monkeypatch.setattr(sys, "argv", ["mogs", "edit", str(missing)])

    assert _native.main() == 1
    assert capsys.readouterr().err == f"Edit failed.\n{missing} does not exist.\n"


def test_command_prints_version(mogs_script):
    result = mogs(mogs_script, "--version")

    assert result.returncode == 0
    assert result.stdout == f"mogs {version('mogs')}\n"


def test_command_runs_the_cli(mogs_script, tmp_path):
    result = mogs(mogs_script, "edit", "missing-dir", cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Edit failed.\nmissing-dir does not exist.\n"


def test_command_ignores_modules_in_working_directory(mogs_script, tmp_path):
    (tmp_path / "packaging.py").write_text("raise SystemExit('shadowed')\n")

    result = mogs(mogs_script, "--version", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"mogs {version('mogs')}\n"


@pytest.mark.slow
def test_wheel_ships_typst_licenses(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None

    subprocess.run([uv, "build", "--wheel", "-q", "-o", str(tmp_path)], check=True, cwd=REPO)
    wheel = next(tmp_path.glob("*.whl"))
    names = subprocess.run(
        [sys.executable, "-m", "zipfile", "-l", str(wheel)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # The templates of `setup.sh` and `run_autograder` are in the extension module.
    assert "templates/" not in names

    # The Typst packages the PDF of written answers needs are in the extension module; their
    # licenses ship with the wheel's metadata.
    assert "mogs/_typst/" not in names
    for package in ("cmarker/0.1.10", "mitex/0.2.7"):
        assert f"crates/mogs-pdf/typst/preview/{package}/LICENSE" in names

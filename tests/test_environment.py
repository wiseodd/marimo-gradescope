import json
import subprocess
from pathlib import Path

import pytest

from mogs import __version__, environment
from mogs.environment import grading_python, tool_install_args
from mogs.requirements import RequirementsError


def direct_url(**fields: object) -> str:
    return json.dumps({"url": "file:///src/mogs"} | fields)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, [f"mogs=={__version__}"]),
        (direct_url(dir_info={"editable": True}), ["--editable", "/src/mogs"]),
        (direct_url(dir_info={}), ["/src/mogs"]),
        (
            json.dumps({"url": "file:///w/mogs.whl", "archive_info": {}}),
            ["mogs @ file:///w/mogs.whl"],
        ),
        (
            json.dumps(
                {
                    "url": "https://github.com/o/mogs",
                    "vcs_info": {"vcs": "git", "commit_id": "abc123"},
                }
            ),
            ["mogs @ git+https://github.com/o/mogs@abc123"],
        ),
    ],
)
def test_tool_install_args(text, expected):
    assert tool_install_args(text) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"url": "file:///src/my%20project", "dir_info": {"editable": True}},
            ["--editable", "/src/my project"],
        ),
        ({"url": "file:///src/my%20project", "dir_info": {}}, ["/src/my project"]),
        (
            {"url": "file:///src/repo", "dir_info": {}, "subdirectory": "pkg"},
            ["/src/repo/pkg"],
        ),
        (
            {"url": "https://x.test/repo.tar.gz", "archive_info": {}, "subdirectory": "pkg"},
            ["mogs @ https://x.test/repo.tar.gz#subdirectory=pkg"],
        ),
        (
            {
                "url": "https://github.com/o/repo",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
                "subdirectory": "pkg",
            },
            ["mogs @ git+https://github.com/o/repo@abc123#subdirectory=pkg"],
        ),
    ],
)
def test_tool_install_args_decodes_paths_and_keeps_subdirectory(fields, expected):
    assert tool_install_args(json.dumps(fields)) == expected


def test_tool_install_args_rejects_unknown_direct_url():
    with pytest.raises(RequirementsError, match="direct_url.json"):
        tool_install_args(json.dumps({"url": "file:///x"}))


def test_uv_missing_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)

    with pytest.raises(RequirementsError, match="`uv` is not on PATH"):
        grading_python("", tmp_path / "env", "3.13")


@pytest.mark.network
def test_failed_install_names_the_requirements(tmp_path):
    with pytest.raises(RequirementsError, match="no-such-package-mogs-test"):
        grading_python("no-such-package-mogs-test==0.0.1\n", tmp_path / "env", "3.13")


@pytest.mark.network
def test_environment_has_the_requirements_and_this_package(tmp_path):
    requirements = "tabulate==0.9.0\nmogs==0.0.0\n"
    python = grading_python(requirements, tmp_path / "env", "3.13")
    code = (
        "import sys, tabulate, mogs; "
        "print(sys.version_info[:2], tabulate.__version__, mogs.__version__)"
    )

    result = subprocess.run([python, "-c", code], capture_output=True, text=True, check=True)

    assert result.stdout.split(maxsplit=2)[0] == "(3,"
    assert result.stdout.split()[1] == "13)"
    assert result.stdout.split()[2:] == ["0.9.0", __version__]


def test_install_resolves_like_pip(tmp_path, monkeypatch):
    # Gradescope installs with pip, which takes each package's best version from any index.
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1] == "venv":
            Path(command[-1]).mkdir()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    grading_python("--extra-index-url https://x.test/simple\ntorch==1\n", tmp_path / "env", "3.13")

    [install] = [command for command in commands if command[1:3] == ["pip", "install"]]
    assert install[install.index("--index-strategy") + 1] == "unsafe-best-match"

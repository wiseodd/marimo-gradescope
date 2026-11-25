import json
import subprocess
from pathlib import Path

import pytest

from marimo_gradescope import __version__, environment
from marimo_gradescope.environment import grading_python, tool_install_args
from marimo_gradescope.requirements import RequirementsError


def direct_url(**fields: object) -> str:
    return json.dumps({"url": "file:///src/mgs"} | fields)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, [f"marimo-gradescope=={__version__}"]),
        (direct_url(dir_info={"editable": True}), ["--editable", "/src/mgs"]),
        (direct_url(dir_info={}), ["/src/mgs"]),
        (
            json.dumps({"url": "file:///w/mgs.whl", "archive_info": {}}),
            ["marimo-gradescope @ file:///w/mgs.whl"],
        ),
        (
            json.dumps(
                {
                    "url": "https://github.com/o/mgs",
                    "vcs_info": {"vcs": "git", "commit_id": "abc123"},
                }
            ),
            ["marimo-gradescope @ git+https://github.com/o/mgs@abc123"],
        ),
    ],
)
def test_tool_install_args(text, expected):
    assert tool_install_args(text) == expected


def test_tool_install_args_rejects_unknown_direct_url():
    with pytest.raises(RequirementsError, match="direct_url.json"):
        tool_install_args(json.dumps({"url": "file:///x"}))


def test_uv_missing_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)

    with pytest.raises(RequirementsError, match="`uv` is not on PATH"):
        grading_python("", tmp_path / "env", "3.13")


@pytest.mark.network
def test_failed_install_names_the_requirements(tmp_path):
    with pytest.raises(RequirementsError, match="no-such-package-mgs-test"):
        grading_python("no-such-package-mgs-test==0.0.1\n", tmp_path / "env", "3.13")


@pytest.mark.network
def test_environment_has_the_requirements_and_this_package(tmp_path):
    requirements = "tabulate==0.9.0\nmarimo-gradescope==0.0.0\n"
    python = grading_python(requirements, tmp_path / "env", "3.13")
    code = (
        "import sys, tabulate, marimo_gradescope; "
        "print(sys.version_info[:2], tabulate.__version__, marimo_gradescope.__version__)"
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

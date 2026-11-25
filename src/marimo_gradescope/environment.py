"""The Python environment the build grades in.

It holds what Gradescope installs: the pinned requirements and marimo-gradescope. The
marimo-gradescope in it is the one that runs the build, so a build from a source checkout grades
with that checkout.
"""

import importlib.metadata
import json
import shutil
import subprocess
from pathlib import Path

from marimo_gradescope import __version__
from marimo_gradescope._env import clean_env
from marimo_gradescope.config import TOOL, normalized_name
from marimo_gradescope.requirements import RequirementsError


def grading_python(requirements: str, folder: Path, python_version: str) -> Path:
    """Create a virtual environment in `folder` and install the grading requirements.

    Args:
        requirements: Text of Gradescope's `requirements.txt`. Its marimo-gradescope line is
            replaced by the running marimo-gradescope.
        folder: Where to create the environment; must not exist.
        python_version: Python version for the environment, as on Gradescope.

    Returns:
        The environment's interpreter.

    Raises:
        RequirementsError: If `uv` is missing, or the environment cannot be created or the
            requirements cannot be installed.
    """

    uv = shutil.which("uv")
    if uv is None:
        raise RequirementsError("`uv` is not on PATH; it is needed to install the requirements.")

    _uv([uv, "venv", "--quiet", "--python", python_version, str(folder)], "create")

    python = folder / "bin" / "python"
    lines = [line for line in requirements.splitlines() if normalized_name(line) != TOOL]
    listed = folder / "requirements.txt"
    listed.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    # Gradescope installs with pip, which takes each package's best version from any index.
    command = [uv, "pip", "install", "--quiet", "--python", str(python), "-r", str(listed)]
    command += ["--index-strategy", "unsafe-best-match"]
    _uv(command + tool_install_args(_direct_url()), "install the requirements in")

    return python


def tool_install_args(direct_url: str | None) -> list[str]:
    """`uv pip install` arguments that install the running marimo-gradescope.

    Args:
        direct_url: Its `direct_url.json` (PEP 610), present when it was installed from a
            folder, an archive or a repository; `None` when it came from an index.

    Raises:
        RequirementsError: If `direct_url` describes none of these.
    """

    match None if direct_url is None else json.loads(direct_url):
        case None:
            return [f"{TOOL}=={__version__}"]
        case {"url": str(url), "dir_info": {"editable": True}}:
            return ["--editable", url.removeprefix("file://")]
        case {"url": str(url), "dir_info": dict()}:
            return [url.removeprefix("file://")]
        case {"url": str(url), "archive_info": dict()}:
            return [f"{TOOL} @ {url}"]
        case {"url": str(url), "vcs_info": {"vcs": str(vcs), "commit_id": str(commit)}}:
            return [f"{TOOL} @ {vcs}+{url}@{commit}"]
        case other:
            raise RequirementsError(
                f"Cannot tell where {TOOL} was installed from; its direct_url.json is {other!r}."
            )


def _direct_url() -> str | None:
    return importlib.metadata.distribution(TOOL).read_text("direct_url.json")


def _uv(command: list[str], action: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=clean_env())

    if result.returncode != 0:
        raise RequirementsError(
            f"`uv` could not {action} the build's grading environment:\n{result.stderr.strip()}"
        )

"""Gradescope's `setup.sh` and `run_autograder`, rendered from the files in this folder."""

import re
from importlib.resources import files
from string import Template

from mogs.config import ASSIGNMENT, AssignmentConfig, ConfigError
from mogs.grader._user import STUDENT_USER

# The uv that `setup.sh` installs on Gradescope.
UV_VERSION = "0.11.21"
PYTHON_RE = re.compile(r"3\.\d+(\.\d+)?")


def setup_script(cfg: AssignmentConfig) -> str:
    """Render `setup.sh` for the assignment.

    Raises:
        ConfigError: If `cfg.python` is not a version such as `3.13` or `3.13.5`.
    """

    if not PYTHON_RE.fullmatch(cfg.python):
        raise ConfigError(
            f'{ASSIGNMENT} `python` must be a version such as "3.13", got {cfg.python!r}.'
        )

    template = Template(_read("setup.sh"))
    return template.substitute(uv_version=UV_VERSION, python=cfg.python, user=STUDENT_USER)


def run_autograder_script() -> str:
    """The `run_autograder` script."""

    return _read("run_autograder")


def _read(name: str) -> str:
    return files(__name__).joinpath(name).read_text(encoding="utf-8")

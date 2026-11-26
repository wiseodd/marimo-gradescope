"""Environment of the processes this package starts, and the flag they read."""

import os

GRADING = "MOGS_GRADING"


def clean_env() -> dict[str, str]:
    """The current environment without `PYTEST_CURRENT_TEST`.

    marimo acts as if under test when that variable is set.
    """

    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def grading_env() -> dict[str, str]:
    """`clean_env()` plus the flag that turns `check` and `export` into no-ops."""

    return clean_env() | {GRADING: "1"}


def grading() -> bool:
    """Whether this process runs tests for a grader or a self-check, not for a student."""

    return os.environ.get(GRADING) == "1"

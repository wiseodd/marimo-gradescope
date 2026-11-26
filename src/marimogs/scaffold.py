"""`marimo-gradescope init`: a new assignment folder with a notebook to start from."""

from importlib.metadata import version
from pathlib import Path
from string import Template

from marimogs.config import check_name
from marimogs.transform import python_literal

NOTEBOOK = Template('''\
# /// script
# requires-python = ">=3.13"
# dependencies = ["marimo", "marimo-gradescope"]
# ///

import marimo

__generated_with = $marimo
app = marimo.App()

with app.setup:
    import marimo as mo
    import marimogs as mogs


@app.cell
def _():
    mogs.assignment(name=$name)


@app.cell(hide_code=True)
def instructor_guide():
    mo.md(r"""
    ## How to write this assignment

    See docs: https://marimo-gradescope.kristia.de/instructors

    The two questions below are examples. Change them into your own questions or delete
    them. Each question ends with a `mogs.grade("<id>", points=...)` cell.

    **Auto-graded question `q1`:**

    1. The question text, as Markdown.
    2. The function students write. Put your solution between `# BEGIN SOLUTION` and
       `# END SOLUTION`, or by putting an inline comment `# SOLUTION`.
    3. Public tests: functions named `test_q1_<anything>`.
    4. Hidden tests, optional: the same, with `# HIDDEN` as the first line.
    5. `mogs.grade("q1", points=4)`

    **Written question `q2`:**

    1. The question text, as Markdown.
    2. Your answer, as Markdown between `<!-- BEGIN SOLUTION -->` and
       `<!-- END SOLUTION -->`.
    3. `mogs.grade("q2", points=5)`
    """)


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Question 1

    Write `mean(xs)`, the average of a list of numbers.
    """)


@app.function
def mean(xs):
    # BEGIN SOLUTION
    return sum(xs) / len(xs)
    # END SOLUTION


@app.function
def test_q1_basic():
    assert mean([1, 2, 3]) == 2


@app.function
# HIDDEN
def test_q1_single():
    assert mean([7]) == 7


@app.cell
def _():
    mogs.grade("q1", points=4)


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Question 2

    What does `mean([])` do?
    """)


@app.cell
def _():
    mo.md(r"""
    <!-- BEGIN SOLUTION -->
    It divides by zero and raises `ZeroDivisionError`.
    <!-- END SOLUTION -->
    """)


@app.cell
def _():
    mogs.grade("q2", points=5)


if __name__ == "__main__":
    app.run()
''')


class InitError(Exception):
    """The assignment folder cannot be created."""


def init(folder: Path) -> Path:
    """Create `folder` with one notebook, `<folder name>.py`, to start an assignment from.

    The notebook holds the assignment cell, a guide for the instructor and two example
    questions. It builds as it is.

    Args:
        folder: The assignment folder. It may exist if it is empty.

    Returns:
        The notebook's path.

    Raises:
        InitError: If `folder` is a file or a folder with files in it.
        ConfigError: If the folder name does not work as an assignment name.
    """

    name = check_name(folder.resolve().name)

    if folder.exists() and not folder.is_dir():
        raise InitError(f"{folder} is not a folder.")

    if folder.is_dir() and any(folder.iterdir()):
        raise InitError(f"{folder} is not empty. Pick a new folder for the assignment.")

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.py"
    text = NOTEBOOK.substitute(name=python_literal(name), marimo=python_literal(version("marimo")))
    path.write_text(text, encoding="utf-8")

    return path

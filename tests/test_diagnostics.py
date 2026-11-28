"""Located errors: the structured fields of `UsageError`, and how `mogs` draws them."""

import io
import sys
from pathlib import Path

import pytest

from mogs._analysis import read_assignment
from mogs._native import main
from mogs.config import ConfigError, Span, read_config
from mogs.source import SourceError, parse_notebook

NB = Path("hw/hw.py")


def notebook(grade: str = 'mogs.grade("q1", points=10)', assignment: str = 'name="hw"') -> str:
    """A small source notebook: the `grade` call is on line 17, indented by 4."""

    return f"""\
import marimo

app = marimo.App()

with app.setup:
    import mogs


@app.cell
def _():
    mogs.assignment({assignment})
    return


@app.cell
def _():
    {grade}
    return
"""


def config_error(src: str) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        read_config(src, NB)

    return caught.value


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, src: str) -> tuple[int, str]:
    """Run `mogs build hw` in `tmp_path` on `src`, without a terminal or `NO_COLOR`."""

    (tmp_path / "hw").mkdir()
    (tmp_path / "hw" / "hw.py").write_text(src, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)

    code = main(["build", "hw"])

    return code, err.getvalue()


# Structured fields


def test_located_config_error_keeps_its_text():
    error = config_error(notebook('mogs.grade("q1", points="10")'))

    assert str(error) == "mogs.grade() (line 17): `points` must be a number >= 0, got '10'."
    assert error.message == "`points` must be a number >= 0, got '10'."
    assert error.span == Span(None, 17, 28, 17, 32)
    assert error.label == "a string"
    assert error.help == "drop the quotes: points=10"


def test_error_without_location_has_no_span():
    error = config_error(notebook().replace('mogs.assignment(name="hw")', "pass"))

    assert str(error).startswith("The notebook has no `mogs.assignment()` call.")
    assert error.message == str(error)
    assert error.span is None
    assert error.label is None
    assert error.help is None


def test_syntax_error_has_line_and_column():
    error = config_error(notebook("x = (1 +"))

    assert str(error) == "The notebook is not valid Python (line 17): '(' was never closed"
    assert error.message == "The notebook is not valid Python: '(' was never closed"
    assert error.span == Span(None, 17, 8, 17, 9)


def test_syntax_error_column_counts_bytes():
    error = config_error(notebook('s = "é" +'))

    assert error.span is not None
    assert (error.span.line, error.span.col) == (17, 14)


def test_source_error_points_at_the_decorator():
    src = notebook() + "\n\n@app.cel\ndef _():\n    return\n"

    with pytest.raises(SourceError) as caught:
        parse_notebook(src)

    assert str(caught.value) == "line 21: unexpected `@app.cel` here."
    assert caught.value.message == "unexpected `@app.cel` here."
    assert caught.value.span == Span(None, 21, 1, 21, 8)


def test_read_assignment_names_the_notebook(tmp_path):
    (tmp_path / "hw.py").write_text(notebook('mogs.grade("q1", points="10")'), encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        read_assignment(tmp_path)

    assert str(caught.value) == "mogs.grade() (line 17): `points` must be a number >= 0, got '10'."
    assert caught.value.span == Span(tmp_path / "hw.py", 17, 28, 17, 32)


# Help lines


@pytest.mark.parametrize(
    ("grade", "assignment", "text", "help"),
    [
        (
            'mogs.grade("q1", points="10")',
            'name="hw"',
            "mogs.grade() (line 17): `points` must be a number >= 0, got '10'.",
            "drop the quotes: points=10",
        ),
        (
            'mogs.grade("q1", points=10)',
            'name="hw", timeout="30"',
            "mogs.assignment() (line 11): `timeout` must be a number of seconds > 0, got '30'.",
            "drop the quotes: timeout=30",
        ),
        (
            'mogs.grade("q1", pionts=10)',
            'name="hw"',
            "mogs.grade() (line 17): unknown argument `pionts`. It takes points, title.",
            "did you mean `points`?",
        ),
        (
            'mogs.grade("q1", points=10); mogs.grade("q1", points=5)',
            'name="hw"',
            "mogs.grade() (line 17): question `q1` is declared more than once (first at line 17).",
            'the first `mogs.grade("q1", ...)` is at line 17; give each question its own id',
        ),
        (
            "mogs.grade(q1, points=10)",
            'name="hw"',
            "mogs.grade() (line 17): needs the question id as a string literal first argument, "
            'e.g. mogs.grade("q1", points=10).',
            'quote the id: "q1"',
        ),
        (
            'mogs.grade("q1", 10)',
            'name="hw"',
            "mogs.grade() (line 17): pass `points` and `title` as keywords, e.g. "
            'mogs.grade("q1", points=10).',
            "write it as a keyword: points=10",
        ),
        (
            'mogs.grade("q_1", points=10)',
            'name="hw"',
            "mogs.grade() (line 17): question id `q_1` must be letters and digits, starting with "
            "a letter (no `_`, since tests are named `test_<id>_<name>`).",
            "try `q1`",
        ),
    ],
)
def test_help_line(grade, assignment, text, help):
    error = config_error(notebook(grade, assignment))

    assert str(error) == text
    assert error.help == help


def test_second_assignment_call_has_help():
    src = notebook().replace(
        '    mogs.assignment(name="hw")',
        '    mogs.assignment(name="hw")\n    mogs.assignment(name="x")',
    )
    error = config_error(src)

    assert str(error) == "mogs.assignment() (line 12): the notebook calls it more than once."
    assert error.help == "keep one `mogs.assignment(...)` call, with all the settings"


def test_no_help_without_a_close_name():
    error = config_error(notebook('mogs.grade("q1", points=10, colour="red")'))

    assert error.help is None

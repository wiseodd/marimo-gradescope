"""Located errors: the structured fields of `UsageError`, and how `mogs` draws them."""

import io
import sys
from pathlib import Path

import pytest

from mogs._analysis import read_assignment
from mogs._native import main
from mogs.config import ConfigError, Span, UsageError, read_config
from mogs.source import SourceError, parse_notebook
from mogs.transform import student_notebook

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


# A notebook that passes `validate`, with a `# SOLUTION` comment that ends no statement.
MARKER = """\
import marimo

app = marimo.App()

with app.setup:
    import mogs


@app.cell
def _():
    mogs.assignment(name="hw")
    return


@app.function
def answer():
    if True:  # SOLUTION
        return 1


@app.cell
def _():
    def test_q1_answer():
        assert answer() == 1

    return


@app.cell
def _():
    mogs.grade("q1", points=10)
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
    assert error.diagnostic.message == "`points` must be a number >= 0, got '10'."
    assert error.diagnostic.span == Span(None, 17, 28, 17, 32)
    assert error.diagnostic.label == "a string"
    assert error.diagnostic.help == "drop the quotes: points=10"


def test_error_without_location_has_no_span():
    error = config_error(notebook().replace('mogs.assignment(name="hw")', "pass"))

    assert str(error).startswith("The notebook has no `mogs.assignment()` call.")
    assert error.diagnostic.message == str(error)
    assert error.diagnostic.span is None
    assert error.diagnostic.label is None
    assert error.diagnostic.help is None


def test_syntax_error_has_line_and_column():
    error = config_error(notebook("x = (1 +"))

    assert str(error) == "The notebook is not valid Python (line 17): '(' was never closed"
    assert error.diagnostic.message == "The notebook is not valid Python: '(' was never closed"
    assert error.diagnostic.span == Span(None, 17, 8, 17, 9)


def test_syntax_error_column_counts_bytes():
    error = config_error(notebook('s = "é" +'))

    assert error.diagnostic.span is not None
    assert (error.diagnostic.span.line, error.diagnostic.span.col) == (17, 14)


def test_source_error_points_at_the_decorator():
    src = notebook() + "\n\n@app.cel\ndef _():\n    return\n"

    with pytest.raises(SourceError) as caught:
        parse_notebook(src)

    assert str(caught.value) == "line 21: unexpected `@app.cel` here."
    assert caught.value.diagnostic.message == "unexpected `@app.cel` here."
    assert caught.value.diagnostic.span == Span(None, 21, 1, 21, 8)


def test_read_assignment_names_the_notebook(tmp_path):
    (tmp_path / "hw.py").write_text(notebook('mogs.grade("q1", points="10")'), encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        read_assignment(tmp_path)

    assert str(caught.value) == "mogs.grade() (line 17): `points` must be a number >= 0, got '10'."
    assert caught.value.diagnostic.span == Span(tmp_path / "hw.py", 17, 28, 17, 32)


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
            "give each question its own id",
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
            'mogs.grade("q1", "Norms")',
            'name="hw"',
            "mogs.grade() (line 17): pass `points` and `title` as keywords, e.g. "
            'mogs.grade("q1", points=10).',
            "write it as a keyword: title='Norms'",
        ),
        (
            'mogs.grade("q1", True)',
            'name="hw"',
            "mogs.grade() (line 17): pass `points` and `title` as keywords, e.g. "
            'mogs.grade("q1", points=10).',
            None,
        ),
        (
            'mogs.grade("q1", points=10)',
            'name="hw", memory="4096"',
            "mogs.assignment() (line 11): `memory` must be a whole number of MB >= 2048, "
            "got '4096'.",
            "drop the quotes: memory=4096",
        ),
        (
            'mogs.grade("q1", points=10)',
            'name="hw", memory="1.5"',
            "mogs.assignment() (line 11): `memory` must be a whole number of MB >= 2048, "
            "got '1.5'.",
            None,
        ),
        (
            'mogs.grade("q1", points=10)',
            'name="hw", timeout="0"',
            "mogs.assignment() (line 11): `timeout` must be a number of seconds > 0, got '0'.",
            None,
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
    assert error.diagnostic.help == help


def test_second_assignment_call_has_help():
    src = notebook().replace(
        '    mogs.assignment(name="hw")',
        '    mogs.assignment(name="hw")\n    mogs.assignment(name="x")',
    )
    error = config_error(src)

    assert str(error) == "mogs.assignment() (line 12): the notebook calls it more than once."
    assert error.diagnostic.help == "keep one `mogs.assignment(...)` call, with all the settings"


def test_no_help_without_a_close_name():
    error = config_error(notebook('mogs.grade("q1", points=10, colour="red")'))

    assert error.diagnostic.help is None


# The command line


def test_cli_draws_a_located_config_error(tmp_path, monkeypatch):
    code, err = build(tmp_path, monkeypatch, notebook('mogs.grade("q1", points="10")'))

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: `points` must be a number >= 0, got '10'.
  --> hw/hw.py:17:29
   |
17 |     mogs.grade("q1", points="10")
   |                             ^^^^ a string
   |
help: drop the quotes: points=10
"""
    )


def test_cli_draws_a_syntax_error(tmp_path, monkeypatch):
    code, err = build(tmp_path, monkeypatch, notebook("x = (1 +"))

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: The notebook is not valid Python: '(' was never closed
  --> hw/hw.py:17:9
   |
17 |     x = (1 +
   |         ^
"""
    )


def test_cli_draws_a_located_source_error(tmp_path, monkeypatch):
    src = notebook() + "\n\n@app.cel\ndef _():\n    return\n"

    code, err = build(tmp_path, monkeypatch, src)

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: unexpected `@app.cel` here.
  --> hw/hw.py:21:2
   |
21 | @app.cel
   |  ^^^^^^^
"""
    )


def test_cli_draws_a_solution_marker_error(tmp_path, monkeypatch):
    code, err = build(tmp_path, monkeypatch, MARKER)

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: `# SOLUTION` must end a statement.
  --> hw/hw.py:17:5
   |
17 |     if True:  # SOLUTION
   |     ^^^^^^^^^^^^^^^^^^^^
"""
    )


def test_cli_suggests_title_for_a_positional_string(tmp_path, monkeypatch):
    code, err = build(tmp_path, monkeypatch, notebook('mogs.grade("q1", "Norms")'))

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: pass `points` and `title` as keywords, e.g. mogs.grade("q1", points=10).
  --> hw/hw.py:17:22
   |
17 |     mogs.grade("q1", "Norms")
   |                      ^^^^^^^
   |
help: write it as a keyword: title='Norms'
"""
    )


def test_cli_lines_up_the_caret_after_a_tab_and_non_ascii(tmp_path, monkeypatch):
    grade = 'mogs.grade("q1", title="é",\tpoints="10")'
    code, err = build(tmp_path, monkeypatch, notebook(grade))

    assert code == 1
    assert (
        err
        == """\
Build failed.
error: `points` must be a number >= 0, got '10'.
  --> hw/hw.py:17:40
   |
17 |     mogs.grade("q1", title="é",    points="10")
   |                                           ^^^^ a string
   |
help: drop the quotes: points=10
"""
    )


def test_cli_keeps_the_plain_form_without_a_location(tmp_path, monkeypatch):
    src = notebook().replace('mogs.grade("q1", points=10)', "pass")

    code, err = build(tmp_path, monkeypatch, src)

    assert code == 1
    assert err == (
        "Build failed.\nThe notebook has no questions. End each one with a "
        '`mogs.grade("q1", points=...)` cell.\n'
    )


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cli_colors_only_a_terminal_without_no_color(tmp_path, monkeypatch):
    src = notebook('mogs.grade("q1", points="10")')
    (tmp_path / "hw").mkdir()
    (tmp_path / "hw" / "hw.py").write_text(src, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    assert main(["build", "hw"]) == 1
    colored = terminal.getvalue()

    monkeypatch.setenv("NO_COLOR", "1")
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    assert main(["build", "hw"]) == 1
    plain = terminal.getvalue()

    assert colored.startswith("Build failed.\n\x1b[")
    assert "\x1b[" not in plain
    assert plain.startswith("Build failed.\nerror: `points` must be")


def test_span_of_line_skips_indent_and_trailing_blanks():
    assert Span.of_line(["x", "\t é = 1  "], 2) == Span(None, 2, 2, 2, 8)
    assert Span.of_line(["   "], 1) == Span(None, 1, 3, 1, 3)


def test_body_on_the_def_line_points_at_it():
    src = notebook() + "\n\n@app.cell\ndef _(): x = 1\n"
    nb = parse_notebook(src)

    with pytest.raises(SourceError) as caught:
        nb.cell_body_source(nb.cells[-1])

    assert str(caught.value) == "line 22: put the cell body on its own lines."
    assert caught.value.diagnostic.span == Span(None, 22, 0, 22, 14)


def test_student_notebook_places_source_line_errors_in_path():
    nb = parse_notebook(MARKER)
    cfg = read_config(MARKER, NB)

    with pytest.raises(SourceError) as caught:
        student_notebook(MARKER, nb, cfg, "mogs", path=NB)

    assert str(caught.value) == "line 17: `# SOLUTION` must end a statement."
    assert caught.value.diagnostic.message == "`# SOLUTION` must end a statement."
    assert caught.value.diagnostic.span == Span(NB, 17, 4, 17, 24)


def test_at_keeps_the_error_and_its_notes(tmp_path):
    error = UsageError("x", span=Span(None, 1, 0, 1, 1))
    error.add_note("note")

    assert error.at(tmp_path) is error
    assert error.diagnostic.span == Span(tmp_path, 1, 0, 1, 1)
    assert error.__notes__ == ["note"]

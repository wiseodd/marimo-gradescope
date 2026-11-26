"""The summary that `assignment()` shows in the source notebook."""

import html
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mogs._env import grading
from mogs.check import (
    FAIL,
    ITEM_STYLE,
    LIST_STYLE,
    NOTE_STYLE,
    PANEL_STYLE,
    PASS,
    SHADE,
    TITLE_STYLE,
    format_points,
    locate,
)
from mogs.config import AssignmentConfig, ConfigError, read_config
from mogs.source import (
    NO_TEXT,
    Notebook,
    SourceError,
    parse_notebook,
    prompt_previews,
    validate,
)

if TYPE_CHECKING:
    import marimo as mo

CELL_STYLE = "padding: 0.1em 1em 0.1em 0; vertical-align: top;"
UNSAVED = "Save the notebook to see the summary of the assignment."


def assignment(
    *,
    name: str | None = None,
    files: list[str] | tuple[str, ...] = (),
    timeout: float = 60,
    budget: float = 540,
    memory: int = 4096,
    python: str = "3.13",
    requirements: str | None = None,
) -> "mo.Html":
    """Declare the assignment's settings, and show a summary of the saved notebook.

    The build reads the arguments from the source without running it, so they must be
    literals. The build also removes the cell with this call from the student notebook.

    The summary reads the saved notebook file, not the arguments: its questions, their
    points and tests, and any problem the build would report. It is as fresh as the last run
    of the cell. While grading (`MGS_GRADING=1`), it returns an empty panel.

    Args:
        name: Names the student zip and the autograder zip; defaults to the notebook's name.
        files: Data files and folders, relative to the notebook, shipped with it.
        timeout: Seconds per test.
        budget: Seconds for the whole grading run.
        memory: MB per grading process, at least 2048. Linux only.
        python: Python version on Gradescope.
        requirements: A requirements file for Gradescope, relative to the notebook. It is used
            instead of the header's `dependencies`.

    Returns:
        The summary, as marimo HTML.
    """

    import marimo as mo

    del name, files, timeout, budget, memory, python, requirements

    if grading():
        return mo.Html("")

    path = locate(sys._getframe(1).f_globals)

    try:
        if path is None:
            raise FileNotFoundError
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return mo.Html(_panel(SHADE, "Assignment", f"<div>{html.escape(UNSAVED)}</div>"))

    try:
        cfg = read_config(src, path)
        nb = parse_notebook(src)
    except (ConfigError, SourceError) as error:
        return mo.Html(_problems(path.stem, [str(error)], path))

    problems = validate(nb, cfg) + _data_problems(path, cfg)
    if problems:
        return mo.Html(_problems(cfg.name, problems, path))

    return mo.Html(summary(cfg, nb, path))


def summary(cfg: AssignmentConfig, nb: Notebook, path: Path) -> str:
    """HTML for a valid assignment: one row per question, then how to build it.

    A written question's row also has the first line of each of its prompt cells.
    """

    total = sum(question.points for question in cfg.questions.values())
    count = len(cfg.questions)
    title = f"{cfg.name} · {count} question{'s' if count != 1 else ''} · {format_points(total)}"
    tests = nb.tests_by_question()
    rows = []

    for qid, question in cfg.questions.items():
        public, hidden = tests.get(qid, ([], []))

        # The kind cell is HTML; a written question lists its prompts under it.
        if question.manual:
            kind = "written" + "".join(
                f'<div style="{NOTE_STYLE}">{html.escape(line)}</div>'
                for line in prompt_previews(nb, qid) or (NO_TEXT,)
            )
        else:
            kind = html.escape(f"auto-graded: {len(public)} public, {len(hidden)} hidden")

        texts = (qid, question.title, format_points(question.points))
        cells = [*(html.escape(text) for text in texts), kind]
        rows.append(
            "<tr>" + "".join(f'<td style="{CELL_STYLE}">{cell}</td>' for cell in cells) + "</tr>"
        )

    body = f'<table style="border-collapse: collapse;">{"".join(rows)}</table>'

    return _panel(PASS, title, body + _footer(path))


def _problems(name: str, problems: list[str], path: Path) -> str:
    """HTML listing what the build would refuse."""

    count = len(problems)
    title = f"{name} · {count} problem{'s' if count != 1 else ''}"
    items = "".join(f'<li style="{ITEM_STYLE}">{html.escape(problem)}</li>' for problem in problems)
    body = f'<ul style="{LIST_STYLE}">{items}</ul>'

    return _panel(FAIL, title, body + _footer(path))


def _data_problems(path: Path, cfg: AssignmentConfig) -> list[str]:
    """The build's complaint about the data files, if any."""

    from mogs.build import data_files

    try:
        data_files(path.parent, cfg)
    except ConfigError as error:
        return [str(error)]

    return []


def _footer(path: Path) -> str:
    note = (
        f"Read from {path.name} when this cell ran; run it again after you save changes. "
        f"Build with: mogs build {shlex.quote(str(path.parent))}"
    )
    return f'<div style="{NOTE_STYLE}">{html.escape(note)}</div>'


def _panel(accent: str, title: str, body: str) -> str:
    return (
        f'<div style="{PANEL_STYLE.format(accent=accent, shade=SHADE)}">'
        f'<div style="{TITLE_STYLE}">{html.escape(title)}</div>{body}</div>'
    )

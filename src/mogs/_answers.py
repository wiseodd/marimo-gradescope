"""The written answers of a notebook, as the questions of their PDF.

Per question the PDF has a heading, then the `prompt_<qid>` and `answer_<qid>` cells in
notebook order, so each answer follows its own prompt. A cell that is just `mo.md("...")`
shows as Markdown with LaTeX math; any other cell shows its source. This module builds the
questions from the notebook; `mogs._native.render_pdf` renders them with Typst
(`crates/mogs-pdf`).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from copy import replace
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mogs import _native
from mogs.runner import exception_summary
from mogs.source import (
    Cell,
    CellKind,
    Notebook,
    SourceError,
    answer_text,
    cell_source,
    parse_notebook,
    unanswered,
)

NO_ANSWER = "No answer."

type Status = Callable[[str], None]


class PdfUnavailable(Exception):
    """No PDF could be made. The message says why and what to try."""


class Role(StrEnum):
    """What a part of the PDF is."""

    PROMPT = "prompt"
    ANSWER = "answer"


class Style(StrEnum):
    """How a part of the PDF is rendered."""

    MARKDOWN = "markdown"
    PLAIN = "plain"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Part:
    """One prompt or answer cell, as it goes into the PDF.

    Attributes:
        text: Markdown, or plain text for `Style.PLAIN` and `Style.NOTE`.
        role: Whether it is a prompt or an answer.
        style: How `text` is rendered.
    """

    text: str
    role: Role
    style: Style = Style.MARKDOWN


@dataclass(frozen=True, slots=True)
class Question:
    """One question's page of the PDF.

    Attributes:
        title: The heading.
        parts: The prompt and answer cells in notebook order, or a note that there is no
            answer cell.
    """

    title: str
    parts: tuple[Part, ...]


def render_manual_pdf(
    notebook: Path,
    qids: Sequence[str],
    titles: Mapping[str, str],
    out: Path,
    *,
    placeholders: Mapping[str, str] | None = None,
    status: Status | None = None,
) -> Path:
    """Render the prompts and answers of `qids` in `notebook` to a PDF at `out`.

    Nothing is written besides `out`. Typst runs with the GIL released; Ctrl+C stops it.

    Args:
        notebook: The student's notebook file.
        qids: Questions to include, in order.
        titles: Heading per question id; the id itself when absent.
        out: Where to write the PDF.
        placeholders: The text each answer cell starts with, by cell name. An answer still
            equal to it, or blank, shows as "No answer."
        status: Called with a short message before slow steps.

    Returns:
        `out`.

    Raises:
        PdfUnavailable: If the notebook cannot be read, `out` cannot be written, or Typst
            fails even on the plain-text document.
    """

    try:
        nb = parse_notebook(notebook.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SourceError) as exc:
        raise PdfUnavailable(f"the notebook cannot be read ({exc})") from exc

    # A generator: Rust shows the status message before it builds the first question.
    questions = (
        _valid(manual_question(nb, qid, titles.get(qid, qid), placeholders or {})) for qid in qids
    )

    # Typst errors are left only after every fallback; the export then falls back to Markdown.
    try:
        data = _native.render_pdf(questions, status or quiet)
    except _native.TypstError as exc:
        raise PdfUnavailable(f"Typst failed ({exception_summary(exc)})") from exc

    # Python writes the file, so the error text is Python's on every platform.
    try:
        out.write_bytes(data)
    except OSError as exc:
        raise PdfUnavailable(f"writing the PDF failed ({exception_summary(exc)})") from exc

    return out


def manual_question(
    nb: Notebook, qid: str, title: str, placeholders: Mapping[str, str] | None = None
) -> Question:
    """A question's prompt and answer cells in notebook order; a note when it has no answer.

    Each answer follows the prompts just before it in the notebook. A notebook built by an
    older release names all prompts before the first answer, and shows them the same way.
    An answer cell that is blank or still its text in `placeholders` shows as "No answer."
    """

    def part(cell: Cell, role: Role) -> Part:
        match role:
            case Role.ANSWER if unanswered(
                answer_text(nb, cell), (placeholders or {}).get(cell.name)
            ):
                return Part(NO_ANSWER, role, Style.NOTE)
            case _:
                return Part(cell_markdown(nb, cell), role)

    prompts = [(cell, Role.PROMPT) for cell in _plain(nb.prompt_cells(qid))]
    answers = [(cell, Role.ANSWER) for cell in _plain(nb.answer_cells(qid))]
    parts = tuple(
        part(cell, role) for cell, role in sorted(prompts + answers, key=lambda pair: pair[0].start)
    )

    if not answers:
        parts += (Part(f"*No answer cell `answer_{qid}` found.*", Role.ANSWER),)

    match parts:
        case (Part(role=Role.PROMPT, style=Style.MARKDOWN) as first, *rest):
            parts = (replace(first, text=without_title_heading(first.text, title)), *rest)

    return Question(title, parts)


def without_title_heading(text: str, title: str) -> str:
    """`text` without a leading Markdown heading whose words `title` already has.

    `## Derive the slope` goes under the title `Question 4: Derive the slope`; `## Question 4:
    Derive the slope` stays under the title `Question 4`, since it says more.
    """

    match = re.match(r"\s*#{1,6}[ \t]+(.+?)[ \t#]*(?:\n|$)", text)

    if match is None or _words(match.group(1)) not in _words(title):
        return text

    return text[match.end() :].lstrip("\n")


def _words(text: str) -> str:
    """The words and symbols of `text` in lower case, spaced: `x + y.` → ` x + y `.

    Sentence punctuation is dropped, so `Question 4:` matches `Question 4`; other symbols, such
    as `+` and `-` in math, are kept.
    """

    return f" {' '.join(re.findall(r'\w+|[^\w\s.,:;!?]', text.casefold()))} "


def cell_markdown(nb: Notebook, cell: Cell) -> str:
    """The Markdown of a cell that is just `mo.md("...")`; else its source as a code block."""

    match answer_text(nb, cell):
        case None:
            return _code_block(cell_source(nb, cell).rstrip())
        case text:
            return text


def _valid(question: Question) -> Question:
    """`question` with lone surrogates, which Rust strings cannot hold, made `?`."""

    def valid(text: str) -> str:
        return text.encode("utf-8", "replace").decode("utf-8")

    parts = tuple(replace(part, text=valid(part.text)) for part in question.parts)

    return Question(valid(question.title), parts)


def _plain(cells: tuple[Cell, ...]) -> list[Cell]:
    """Plain cells only: a function or class cell has no output to show."""

    return [cell for cell in cells if cell.kind is CellKind.CELL]


def quiet(message: str) -> None:
    """Status callback that shows nothing."""


def _code_block(text: str) -> str:
    """A fenced code block holding `text` verbatim; the fence outgrows every backtick run."""

    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    fence = "`" * max(3, longest + 1)

    return f"{fence}\n{text}\n{fence}"

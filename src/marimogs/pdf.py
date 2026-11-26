"""PDF of the written answers, for questions graded by hand.

Typst renders the PDF in-process, from the `typst` package of the `pdf` extra. Per question
the PDF has a heading, then the `prompt_<qid>` and `answer_<qid>` cells in notebook order, so
each answer follows its own prompt. A page break separates questions. A cell that is just
`mo.md("...")` shows as Markdown with LaTeX math (through the vendored Typst packages cmarker
and mitex); any other cell shows its source.

The cell text reaches Typst as data in `sys.inputs`, never as Typst source. Typst cannot
recover from an error inside a document, so a cell that breaks the document is rendered again
with less: first with each failing formula shown as LaTeX source, then as plain text. A note
under the cell says so.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import replace
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from marimogs.grader.results import code_block
from marimogs.runner import exception_summary
from marimogs.source import (
    Cell,
    CellKind,
    Notebook,
    SourceError,
    answer_text,
    cell_source,
    parse_notebook,
)

if TYPE_CHECKING:
    import typst

# Holds `preview/<name>/<version>/` for each vendored Typst package.
PACKAGES = Path(__file__).parent / "_typst"

# mitex evaluates the Typst it converts LaTeX to, so a formula that could run Typst code shows
# as source: `\iftypst` passes Typst through, `\hspace`, `\vspace` and `\raisebox` evaluate
# their first argument (only a plain length is let through), and a `#` left in the converted
# formula starts code. mitex itself writes escaped `\#` and the calls in `ALIASES` (from
# `\text`, `\textbf`, `\textcolor`, `\color`, `$...$` inside `\text`, ...), which are dropped
# before the check. Other calls it writes (`\section`, `\footnote`, `\ref`, ...) stay
# flagged, so those formulas show as source.
ALIASES = (
    "textmath|textbf|textit|textrm|texttt|textsf|textup|textmd|textnormal|emph|colortext"
    "|mitexcolor|mitexcolorbox|math\\\\.equation"
)
PREAMBLE = r"""
#import "@preview/cmarker:0.1.10"
#import "@preview/mitex:0.2.7": mitex, mitex-convert

#let aliases = "#(ALIASES)\\b"
#let spacing = "\\\\(hspace|vspace|raisebox)\\*?"
#let length = regex("^\\s*-?(\\d+(\\.\\d*)?|\\.\\d+)\\s*(pt|mm|cm|in|em)\\s*$")

#let runs-code(src) = {
  if src.contains("iftypst") { return true }

  let commands = src.matches(regex(spacing))
  let lengths = src
    .matches(regex(spacing + "\\s*\\{([^{}]*)\\}"))
    .filter(m => m.captures.at(1).match(length) != none)
  if commands.len() != lengths.len() { return true }

  mitex-convert(src).replace(regex("\\\\."), "").replace(regex(aliases), "").contains("#")
}

#let safe-math(src, block: false) = {
  if runs-code(src) { raw(src, block: block) } else { mitex(src, block: block) }
}
""".replace("ALIASES", ALIASES)

# The document. `sys.inputs.doc` is JSON: a list of questions, each `{title, parts}`, where a
# part is `{text, role, style, note}`. Images are not loaded: they show as their
# alt text.
TEMPLATE = (
    PREAMBLE
    + """\
#set page(paper: "us-letter", margin: 2cm)

// Typst's embedded fonts; system fonts only fill in glyphs these lack, such as CJK.
#set text(font: "Libertinus Serif", size: 11pt)
#show math.equation: set text(font: "New Computer Modern Math")
#show raw: set text(font: "DejaVu Sans Mono")

#let image-stub(source, alt: none, ..args) = {
  let label = if alt == none or alt == "" { "no description" } else { alt }
  emph("[Image: " + label + "]")
}

// A link to an anchor in the answer shows just its text: a missing label would stop Typst.
#let link-stub(dest, body) = if type(dest) == label { body } else { link(dest, body) }

#let markdown(text) = cmarker.render(
  text,
  math: safe-math,
  h1-level: 2,
  raw-typst: false,
  set-document-title: false,
  scope: (image: image-stub, link: link-stub),
)

#let show-part(part) = block(width: 100%, {
  if part.style == "markdown" { markdown(part.text) } else { raw(part.text, block: true) }
  if part.note != none { block(above: 0.8em, text(size: 8pt, fill: gray, part.note)) }
})

#for (index, question) in json(bytes(sys.inputs.doc)).enumerate() {
  if index > 0 { pagebreak() }
  heading(level: 1, question.title)

  let parts = question.parts
  for (at, part) in parts.enumerate() {
    show-part(part)

    // A rule between a prompt and the answer after it.
    let next = parts.at(at + 1, default: none)
    if part.role == "prompt" and next != none and next.role == "answer" {
      line(length: 100%, stroke: 0.5pt + gray)
    }
  }
}
"""
)

# One formula alone, as cmarker hands it to mitex.
FORMULA = PREAMBLE + '#safe-math(sys.inputs.formula, block: sys.inputs.display == "true")\n'

SOURCE_NOTE = "Some math in this cell could not be rendered and is shown as LaTeX source."
PLAIN_NOTE = "This cell could not be rendered and is shown as plain text."

FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
BLANK_LINE = re.compile(r"\n[ \t]*\n")

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


@dataclass(frozen=True, slots=True)
class Part:
    """One prompt or answer cell, as it goes into the PDF.

    Attributes:
        text: Markdown, or plain text for `Style.PLAIN`.
        role: Whether it is a prompt or an answer.
        style: How `text` is rendered.
        note: A small note under the part, or `None`.
    """

    text: str
    role: Role
    style: Style = Style.MARKDOWN
    note: str | None = None


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


@dataclass(frozen=True, slots=True)
class Formula:
    """A `$...$` or `$$...$$` formula in Markdown.

    Attributes:
        start: Offset of the opening dollar sign.
        end: Offset just past the closing dollar sign.
        source: The LaTeX between the dollar signs.
        display: Whether it is a `$$` formula.
    """

    start: int
    end: int
    source: str
    display: bool


def render_manual_pdf(
    notebook: Path,
    qids: Sequence[str],
    titles: Mapping[str, str],
    out: Path,
    *,
    status: Status | None = None,
) -> Path:
    """Render the prompts and answers of `qids` in `notebook` to a PDF at `out`.

    Nothing is written besides `out`.

    Args:
        notebook: The student's notebook file.
        qids: Questions to include, in order.
        titles: Heading per question id; the id itself when absent.
        out: Where to write the PDF.
        status: Called with a short message before slow steps.

    Returns:
        `out`.

    Raises:
        PdfUnavailable: If `typst` is not installed, the notebook cannot be read, `out`
            cannot be written, or Typst fails even on the plain-text document.
    """

    try:
        import typst  # noqa: F401  (only checks that the pdf extra is installed)
    except ImportError as exc:
        raise PdfUnavailable(
            "the PDF tool is not installed (typst). Add "
            '"marimo-gradescope[pdf]" to the notebook\'s dependencies, or run '
            '`pip install "marimo-gradescope[pdf]"`'
        ) from exc

    try:
        nb = parse_notebook(notebook.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SourceError) as exc:
        raise PdfUnavailable(f"the notebook cannot be read ({exc})") from exc

    (status or quiet)("Creating the PDF of your written answers…")
    questions = [manual_question(nb, qid, titles.get(qid, qid)) for qid in qids]

    # Any error left after the fallbacks is unexpected; the export then falls back to Markdown.
    try:
        data = _render(questions)
    except Exception as exc:
        raise PdfUnavailable(f"Typst failed ({exception_summary(exc)})") from exc

    try:
        out.write_bytes(data)
    except OSError as exc:
        raise PdfUnavailable(f"writing the PDF failed ({exception_summary(exc)})") from exc

    return out


def _render(questions: list[Question]) -> bytes:
    """The PDF of `questions`; parts that break the document are rendered with less."""

    import typst

    try:
        return _document(questions)
    except typst.TypstError:
        pass

    rescued = [_each_part(question, _rescue) for question in questions]

    # Cells can also break the document only together, as with two headings of the same name
    # and a link to them.
    try:
        return _document(rescued)
    except typst.TypstError:
        return _document([_each_part(question, _as_plain) for question in questions])


def _each_part(question: Question, change: Callable[[Part], Part]) -> Question:
    return replace(question, parts=tuple(change(part) for part in question.parts))


def manual_question(nb: Notebook, qid: str, title: str) -> Question:
    """A question's prompt and answer cells in notebook order; a note when it has no answer.

    Each answer follows the prompts just before it in the notebook. A notebook built by an
    older release names all prompts before the first answer, and shows them the same way.
    """

    prompts = [(cell, Role.PROMPT) for cell in _plain(nb.prompt_cells(qid))]
    answers = [(cell, Role.ANSWER) for cell in _plain(nb.answer_cells(qid))]
    parts = tuple(
        Part(cell_markdown(nb, cell), role)
        for cell, role in sorted(prompts + answers, key=lambda pair: pair[0].start)
    )

    if not answers:
        parts += (Part(f"*No answer cell `answer_{qid}` found.*", Role.ANSWER),)

    return Question(title, parts)


def cell_markdown(nb: Notebook, cell: Cell) -> str:
    """The Markdown of a cell that is just `mo.md("...")`; else its source as a code block."""

    match answer_text(nb, cell):
        case None:
            return code_block(cell_source(nb, cell).rstrip())
        case text:
            return text


def find_formulas(text: str) -> list[Formula]:
    r"""The `$...$` and `$$...$$` formulas in Markdown, in order.

    Code spans, fenced code blocks and `\$` are skipped. A formula does not span a blank
    line; a dollar sign without a partner is text.
    """

    formulas = []

    for start, end in _prose(text):
        index = start

        while index < end:
            match text[index]:
                case "\\":
                    index += 2
                case "`":
                    run = len(text[index:end]) - len(text[index:end].lstrip("`"))
                    blank = BLANK_LINE.search(text, index, end)
                    limit = blank.start() if blank else end
                    close = re.compile(rf"(?<!`)`{{{run}}}(?!`)").search(text, index + run, limit)
                    index = close.end() if close else index + run
                case "$":
                    width = 2 if text.startswith("$$", index) else 1
                    close = _closing(text, index + width, end, "$" * width)

                    if close is None:
                        index += width
                        continue

                    source = text[index + width : close]
                    formulas.append(Formula(index, close + width, source, width == 2))
                    index = close + width
                case _:
                    index += 1

    return formulas


def _prose(text: str) -> list[tuple[int, int]]:
    """Offsets of the runs of lines outside fenced code blocks."""

    runs = []
    fence = None
    start = 0
    offset = 0

    for line in text.splitlines(keepends=True):
        opened = FENCE.match(line)
        stripped = line.strip()

        if fence is None and opened is not None:
            runs.append((start, offset))
            fence = opened.group(1)
        elif fence is not None and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
            fence = None
            start = offset + len(line)

        offset += len(line)

    if fence is None:
        runs.append((start, len(text)))

    return [(begin, end) for begin, end in runs if begin < end]


def _closing(text: str, start: int, end: int, delimiter: str) -> int | None:
    """Offset of the `delimiter` that closes a formula opened before `start`, if any."""

    index = start

    while index < end:
        if text[index] == "\\":
            index += 2
        elif text.startswith(delimiter, index):
            return index
        elif BLANK_LINE.match(text, index):
            return None
        else:
            index += 1

    return None


def _rescue(part: Part) -> Part:
    """`part`, or a version of it that Typst can render, with a note when it changed."""

    if _renders(Question("", (part,))):
        return part

    text = part.text
    for formula in reversed(find_formulas(text)):
        if not _formula_renders(formula):
            text = text[: formula.start] + _as_code(text, formula) + text[formula.end :]

    shown = replace(part, text=text, style=Style.MARKDOWN, note=SOURCE_NOTE)
    if text != part.text and _renders(Question("", (shown,))):
        return shown

    return _as_plain(part)


def _as_plain(part: Part) -> Part:
    return replace(part, style=Style.PLAIN, note=PLAIN_NOTE)


def _as_code(text: str, formula: Formula) -> str:
    """The formula's source with its dollar signs, as a code block or a code span."""

    source = text[formula.start : formula.end]

    if formula.display:
        return f"\n\n{code_block(source)}\n\n"

    longest = max((len(run) for run in re.findall(r"`+", source)), default=0)
    fence = "`" * (longest + 1)

    return f"{fence} {' '.join(source.split())} {fence}"


def _renders(question: Question) -> bool:
    import typst

    try:
        _document([question])
    except typst.TypstError:
        return False

    return True


def _formula_renders(formula: Formula) -> bool:
    import typst

    inputs = {"formula": formula.source, "display": str(formula.display).lower()}

    try:
        _compile(FORMULA, inputs)
    except typst.TypstError:
        return False

    return True


def _document(questions: list[Question]) -> bytes:
    """The PDF of `questions`."""

    def parts(parts: tuple[Part, ...]) -> list[dict[str, str | None]]:
        return [
            {"text": _valid(p.text), "role": p.role, "style": p.style, "note": p.note}
            for p in parts
        ]

    doc = [{"title": _valid(q.title), "parts": parts(q.parts)} for q in questions]

    return _compile(TEMPLATE, {"doc": json.dumps(doc, ensure_ascii=False)})


def _valid(text: str) -> str:
    """`text` with lone surrogates, which JSON for Typst cannot carry, made `?`."""

    return text.encode("utf-8", "replace").decode("utf-8")


def _compile(source: str, inputs: dict[str, str]) -> bytes:
    """Compile Typst `source` to PDF with the vendored packages and Typst's own fonts.

    System fonts are added only when the inputs hold a script the embedded fonts lack: a
    system font with an embedded font's family name would replace it.
    """

    import typst

    system = any(_needs_system_fonts(value) for value in inputs.values())

    return typst.compile(
        source.encode(),
        package_path=str(PACKAGES),
        font_paths=_fonts(system=system),
        sys_inputs=inputs,
    )


def _needs_system_fonts(text: str) -> bool:
    """Whether `text` has characters from scripts Typst's embedded fonts do not cover.

    A heuristic by Unicode block: Latin, Greek, Cyrillic, punctuation, arrows and math are
    covered; Hebrew to the Indic and South-East Asian scripts, CJK and emoji are not.
    """

    return any(
        0x0590 <= point < 0x1D00 or (point >= 0x2E80 and not 0x1D400 <= point < 0x1D800)
        for point in map(ord, text)
    )


@cache
def _fonts(*, system: bool) -> "typst.Fonts":
    """Typst's embedded fonts, plus the system's when `system`; found once per process."""

    import typst

    return typst.Fonts(include_system_fonts=system, include_embedded_fonts=True)


def _plain(cells: tuple[Cell, ...]) -> list[Cell]:
    """Plain cells only: a function or class cell has no output to show."""

    return [cell for cell in cells if cell.kind is CellKind.CELL]


def quiet(message: str) -> None:
    """Status callback that shows nothing."""

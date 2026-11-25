"""The export button: check the saved notebook and package it for Gradescope.

The build appends two cells to the student notebook:

    export_button = mgs.export_button()
    export_button

    mgs.export(export_button, name="hw", manual=["q3"], titles={"q3": "Question 3"},
               placeholders={"q3": "*Write your answer here.*"})

On a click, `export` works from the notebook file on disk, not the kernel's state. It reads
the file once, runs the public tests on the file in a subprocess, makes a PDF of the written
answers from the snapshot (or a Markdown file when the PDF fails), and offers a zip of both
as a download. A save during the export gets a warning. Nothing is written next to the
notebook.
"""

import hashlib
import html
import io
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from marimo_gradescope._env import grading
from marimo_gradescope._run import run_capped
from marimo_gradescope.check import (
    FAIL,
    ITEM_STYLE,
    LIST_STYLE,
    MARK_STYLE,
    NOTE_STYLE,
    PANEL_STYLE,
    PASS,
    SHADE,
    WARN,
)
from marimo_gradescope.pdf import PdfUnavailable, cell_markdown, quiet, render_manual_pdf
from marimo_gradescope.runner import exception_summary, shorten
from marimo_gradescope.source import Notebook, SourceError, answer_text, parse_notebook

if TYPE_CHECKING:
    import marimo as mo

SELFCHECK_TIMEOUT = 300.0
BUTTON_LABEL = "Export submission"
NOTE = "rgba(127, 127, 127, 0.6)"
CHANGED = "The notebook changed during the export; export again."
NOT_FOUND = (
    "Cannot find this notebook's file, so it cannot be exported. Save the notebook, then open "
    "it with `marimo edit <your notebook>.py`."
)

type Status = Callable[[str], None]


class Mark(StrEnum):
    """The symbol in front of a panel line."""

    OK = "✓"
    WARN = "⚠"
    FAIL = "✗"
    DOWNLOAD = "⬇"
    NOTE = "→"


ACCENTS = {
    Mark.OK: PASS,
    Mark.WARN: WARN,
    Mark.FAIL: FAIL,
    Mark.DOWNLOAD: PASS,
    Mark.NOTE: NOTE,
}


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the export panel.

    Attributes:
        mark: Symbol in front.
        text: Plain text; escaped when rendered.
    """

    mark: Mark
    text: str


@dataclass(frozen=True, slots=True)
class Package:
    """An exported submission.

    Attributes:
        lines: What the panel reports, in order.
        filename: Name of the zip.
        data: The zip.
    """

    lines: tuple[Line, ...]
    filename: str
    data: bytes


def export_button() -> "mo.ui.run_button":
    """The button that starts an export."""

    import marimo as mo

    return mo.ui.run_button(label=BUTTON_LABEL)


def export(
    button: "mo.ui.run_button",
    *,
    name: str,
    manual: Sequence[str],
    titles: Mapping[str, str],
    placeholders: Mapping[str, str] | None = None,
) -> "mo.Html":
    """Export the notebook when `button` was clicked; otherwise say which file it would export.

    The notebook file is the caller's `__file__`, or else the file of marimo's running
    notebook. When neither is known, the panel says so. An unexpected error shows as a failed
    panel; `KeyboardInterrupt` stops the export. While grading (`MGS_GRADING=1`) it does
    nothing and returns an empty panel.

    Args:
        button: The button from `export_button()`.
        name: Base name of the files in the zip, e.g. `hw` for `hw_submission.zip`.
        manual: Ids of the questions graded by hand.
        titles: Title per manual question, used as headings.
        placeholders: The answer text each manual question starts with; an answer still
            equal to it (ignoring whitespace) gets a warning.

    Returns:
        The export panel, with the download when the export ran.
    """

    import marimo as mo

    if grading():
        return mo.Html("")

    namespace = sys._getframe(1).f_globals

    return export_panel(button.value, namespace, name, manual, titles, placeholders or {})


def export_panel(
    clicked: bool,
    namespace: Mapping[str, object],
    name: str,
    manual: Sequence[str],
    titles: Mapping[str, str],
    placeholders: Mapping[str, str],
) -> "mo.Html":
    """`export`, with the button's state and the caller's globals passed in."""

    import marimo as mo

    notebook = locate(namespace)

    if notebook is None:
        return mo.Html(panel((Line(Mark.FAIL, NOT_FOUND),)))

    if not clicked:
        note = f"Click “{BUTTON_LABEL}” to check and package {notebook}. Save your changes first."
        return mo.Html(panel((Line(Mark.NOTE, note),)))

    if not notebook.is_file():
        message = f"{notebook} does not exist. Save the notebook, then export again."
        return mo.Html(panel((Line(Mark.FAIL, message),)))

    try:
        with mo.status.spinner(title="Exporting your submission…") as spinner:
            result = package(
                notebook,
                name=name,
                manual=manual,
                titles=titles,
                placeholders=placeholders,
                status=lambda message: spinner.update(title=message),
            )
    except Exception as exc:
        return mo.Html(panel((Line(Mark.FAIL, f"Export failed: {exception_summary(exc)}"),)))

    download = mo.download(
        data=result.data, filename=result.filename, mimetype="application/zip", label="Download"
    )

    return mo.Html(panel(result.lines, download=download.text))


def locate(namespace: Mapping[str, object]) -> Path | None:
    """The notebook file: `__file__` in `namespace`, else marimo's running notebook, else `None`.

    The second lookup uses marimo's private runtime context and gives `None` when that fails.
    """

    match namespace.get("__file__"):
        case str() as file if file:
            return Path(file).resolve()

    try:
        from marimo._runtime.context import get_context

        filename = get_context().filename
    except Exception:
        return None

    return Path(filename).resolve() if filename else None


def package(
    notebook: Path,
    *,
    name: str,
    manual: Sequence[str],
    titles: Mapping[str, str],
    placeholders: Mapping[str, str] | None = None,
    status: Status | None = None,
) -> Package:
    """Check the notebook file, make the answers file, and zip both.

    The file is read once; the PDF is made from a copy of that snapshot in a temporary folder
    and the zip holds the snapshot. The public tests run on the real file, as the grader runs
    them next to the data files. If the file's bytes differ from the snapshot after the
    tests and the PDF, a warning asks to export again. A `KeyboardInterrupt` stops the
    export: it propagates and no zip is made.

    Args:
        notebook: The notebook file.
        name: Base name of the files in the zip.
        manual: Ids of the questions graded by hand.
        titles: Title per manual question.
        placeholders: The answer text each manual question starts with.
        status: Called with a short message before slow steps.

    Returns:
        The panel lines and the zip.

    Raises:
        OSError: If the notebook cannot be read.
    """

    report = status or quiet

    with notebook.open("rb") as file:
        modified = os.fstat(file.fileno()).st_mtime
        data = file.read()

    saved = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
    lines = [Line(Mark.OK, f"Using {notebook.name} (saved {saved})")]
    entries = {notebook.name: data}

    with tempfile.TemporaryDirectory(prefix="mgs-export-", ignore_cleanup_errors=True) as folder:
        snapshot = Path(folder) / notebook.name
        snapshot.write_bytes(data)

        report("Running the public tests…")
        lines.append(tests_line(notebook, notebook.parent))

        if manual:
            nb = _parse(data)

            try:
                out = Path(folder) / f"{name}.pdf"
                render_manual_pdf(snapshot, manual, titles, out, status=report)
                entries[out.name] = out.read_bytes()
                lines.append(Line(Mark.OK, f"Written answers → {name}.pdf"))
            except PdfUnavailable as exc:
                answers = answers_markdown(nb, name, manual, titles)
                entries[f"{name}_answers.md"] = answers.encode()
                lines.append(
                    Line(Mark.WARN, f"PDF not created: {exc}; answers included as Markdown")
                )

            lines += [
                Line(Mark.WARN, f"answer_{qid} is missing or empty")
                for qid in manual
                if nb is None or not _answered(nb, qid, (placeholders or {}).get(qid))
            ]

    if _digest_of(notebook) != hashlib.sha256(data).hexdigest():
        lines.append(Line(Mark.WARN, CHANGED))

    filename = f"{name}_submission.zip"
    lines.append(Line(Mark.DOWNLOAD, f"{filename} — upload this to Gradescope"))

    return Package(tuple(lines), filename, build_zip(entries))


def tests_line(notebook: Path, folder: Path) -> Line:
    """Run the public tests of a notebook file in a subprocess and summarize them.

    The line counts the failures per question. The check under each question names them.

    Args:
        notebook: The notebook file.
        folder: Working directory for the tests: the folder of the student's notebook.
    """

    result = run_capped(
        [sys.executable, "-m", "marimo_gradescope.selfcheck", str(notebook), str(folder)],
        cwd=folder,
        timeout=SELFCHECK_TIMEOUT,
    )

    if result is None:
        return Line(Mark.WARN, f"Public tests: the check timed out after {SELFCHECK_TIMEOUT:g} s")

    summary = _summary(result.stdout) if result.returncode == 0 else None

    if summary is None:
        errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        reason = shorten(errors[-1]) if errors else f"exit status {result.returncode}"
        return Line(Mark.WARN, f"Public tests could not run: {reason}")

    passed = sum(passed for passed, _, _ in summary.values())
    total = sum(total for _, total, _ in summary.values())
    failed = ", ".join(f"{qid} ({len(names)})" for qid, (_, _, names) in summary.items() if names)
    text = f"Public tests: {passed}/{total} passed"

    if not failed:
        return Line(Mark.OK, text)

    return Line(Mark.WARN, f"{text}. Failing: {failed}")


def answers_markdown(
    nb: Notebook | None, name: str, manual: Sequence[str], titles: Mapping[str, str]
) -> str:
    """The written answers as Markdown, for when the PDF cannot be made."""

    parts = [f"# Written answers: {name}"]

    for qid in manual:
        parts.append(f"## {titles.get(qid, qid)}")

        if nb is None:
            parts.append("*The notebook does not parse, so no answer could be read.*")
            continue

        cells = nb.answer_cells(qid)
        if not cells:
            parts.append(f"*No answer cell `answer_{qid}` found.*")

        parts += [cell_markdown(nb, cell) for cell in cells]

    return "\n\n".join(parts) + "\n"


def build_zip(entries: Mapping[str, bytes]) -> bytes:
    """A zip of `entries`, built in memory."""

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)

    return buffer.getvalue()


def panel(lines: Sequence[Line], download: str = "") -> str:
    """HTML for the export panel; `download` is trusted HTML placed after the lines."""

    marks = {line.mark for line in lines}
    accent = FAIL if Mark.FAIL in marks else WARN if Mark.WARN in marks else PASS

    if marks <= {Mark.NOTE}:
        accent = NOTE

    items = "".join(
        f'<li style="{ITEM_STYLE}">'
        f'<span style="{MARK_STYLE.format(accent=ACCENTS[line.mark])}">{line.mark}</span> '
        f"{html.escape(line.text)}</li>"
        for line in lines
    )
    after = (
        f'<div style="margin-top: 0.5em;">{download}</div>'
        f'<div style="{NOTE_STYLE}">Export again after further changes.</div>'
        if download
        else ""
    )

    return (
        f'<div style="{PANEL_STYLE.format(accent=accent, shade=SHADE)}">'
        f'<ul style="{LIST_STYLE}">{items}</ul>{after}</div>'
    )


def _summary(stdout: str) -> dict[str, tuple[int, int, list[str]]] | None:
    """The selfcheck summary: the last stdout line that is a valid one, else `None`."""

    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue

        match data:
            case dict():
                summary = {}
                for qid, entry in data.items():
                    match entry:
                        case {"passed": int() as passed, "total": int() as total, "failed": list()}:
                            names = [str(name) for name in entry["failed"]]
                            summary[str(qid)] = (passed, total, names)
                        case _:
                            return None
                return summary

    return None


def _parse(data: bytes) -> Notebook | None:
    try:
        return parse_notebook(data.decode("utf-8"))
    except (UnicodeDecodeError, SourceError):
        return None


def _answered(nb: Notebook, qid: str, placeholder: str | None) -> bool:
    """Whether `qid` has an answer cell that is neither blank nor its starting text."""

    start = None if placeholder is None else _words(placeholder)
    texts = [answer_text(nb, cell) for cell in nb.answer_cells(qid)]

    return any(text is None or (text.strip() and _words(text) != start) for text in texts)


def _digest_of(path: Path) -> str | None:
    """SHA-256 of the file's bytes, or `None` when it cannot be read."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _words(text: str) -> str:
    """`text` with each run of whitespace made one space."""

    return " ".join(text.split())

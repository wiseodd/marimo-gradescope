"""The Rust side of mogs, built from crates/mogs."""

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath

from mogs._answers import Question

class TypstError(Exception):
    """Typst failed, even on the plain-text document. The message is Typst's."""

def main(argv: list[str] | None = None) -> int:
    """Run the `mogs` command line on `argv`, `sys.argv[1:]` when `None`.

    Returns:
        Exit code: 0 for success, `--help` and `--version`, 1 when the command fails, 2 for a
        usage error, and marimo's exit code for `mogs edit`.
    """

def data_files(root: Path, files: Sequence[str]) -> dict[PurePosixPath, Path]:
    """The data files to ship, by path relative to `root`, with folders expanded.

    Files in junk folders (`__pycache__`, `__marimo__`, `__MACOSX`, `.ipynb_checkpoints`), and
    `.DS_Store` files, are left out of a folder.

    Returns:
        Source path by relative destination, in the order of `files`, folders sorted.

    Raises:
        ValueError: If a path is absolute, has a `..` part, is empty, starts with
            `submission.py`, or does not exist. The message names the path.
    """

def render_pdf(questions: Iterable[Question], status: Callable[[str], None]) -> bytes:
    """The PDF of `questions`, one page or more per question, after calling `status`.

    A Markdown part shows with LaTeX math, through the vendored Typst packages cmarker and
    mitex. A part that breaks the document is rendered again with less: first with each
    failing formula shown as LaTeX source, then as plain text, with a note under it. Typst
    runs with the GIL released; Ctrl+C stops it and raises `KeyboardInterrupt`.

    Raises:
        TypstError: If Typst fails even on the plain-text document.
        TypeError: If a question or part lacks a field, or a field has the wrong type. A
            role or style outside `Role` or `Style` is a `TypeError` caused by a `ValueError`
            that names the value.
    """

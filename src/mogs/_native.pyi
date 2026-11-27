"""The Rust side of mogs, built from crates/mogs."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, final

from mogs._answers import Question
from mogs.grader.bundle import GradingBundle
from mogs.runner import Outcome

STUDENT_USER: str
"""The user that runs student code on Gradescope."""

class Row(TypedDict):
    """One entry of `tests` in `results.json`."""

    name: str
    number: NotRequired[str]
    score: float
    max_score: float
    output: str
    output_format: str
    visibility: str
    status: NotRequired[str]

class Results(TypedDict):
    """The content of `results.json`.

    `score` is set only when `tests` is empty, since Gradescope then has no rows to add up.
    `output` is the score summary for students: Gradescope hides the total when a row is hidden.
    """

    tests: list[Row]
    stdout_visibility: str
    score: NotRequired[float]
    output: NotRequired[str]
    output_format: NotRequired[str]

@final
class Submission:
    """The files found in an upload.

    Attributes:
        py: The notebook to grade, or `None` when there is none or it is ambiguous.
        answers: The written answers: a PDF, else the export's Markdown fallback
            `<notebook stem>_answers.md`, else `None`. Only tells whether the student sent
            any; graders see every uploaded file.
        error: Why `py` is `None`, ready to show the student.
    """

    @property
    def py(self) -> Path | None: ...
    @property
    def answers(self) -> Path | None: ...
    @property
    def error(self) -> str | None: ...

@final
class Graded:
    """What `grade` found.

    Attributes:
        outcomes: One outcome per test of the bundle: public tests first, then hidden ones.
        load_error: Why the notebook, or the grader's setup code, did not load, if it did
            not in some phase. A hidden helper or test cell that fails does not count.
    """

    @property
    def outcomes(self) -> dict[str, Outcome]: ...
    @property
    def load_error(self) -> str | None: ...

class TypstError(Exception):
    """Typst failed, even on the plain-text document. The message is Typst's."""

def version() -> str:
    """The version of mogs."""

def main() -> int:
    """Run the `mogs` command line on `sys.argv` and return its exit code."""

def find_submission(sub_dir: Path, notebook: str, name: str | None = None) -> Submission:
    """Find the notebook and the written answers anywhere under `sub_dir`.

    Skips `__MACOSX`, `__pycache__`, dot files and dot folders, and never follows or picks a
    symlink. Public tests, the `.py` files in a `tests/` folder next to another `.py` file, are
    never the notebook. Of the rest, a file named `notebook` wins; otherwise the only `.py` file
    is taken. Any PDF counts as the written answers. Of Markdown files, only the export's
    fallback `<name>_answers.md` (any case) does, so a stray `README.md` does not.

    Args:
        sub_dir: The unpacked upload.
        notebook: File name of the student notebook.
        name: Assignment name, which the export names its files after. Defaults to the
            notebook's stem.
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

def build_results(
    bundle: GradingBundle, outcomes: Mapping[str, Outcome], notices: Sequence[str]
) -> Results:
    """Gradescope's `results.json` content.

    Each auto-graded question gets a hidden row worth `points × passed / total` that lists all
    its tests. A visible 0-point "Public tests" row lists the public tests. Notices go first, in
    a 0-point "Submission" row. Manual questions get no row; with no rows at all, the results
    carry a top-level score of 0.

    Raises:
        KeyError: If a test has no outcome.
        ValueError: If an auto-graded question has no tests.
    """

def failure_results(bundle: GradingBundle | None, error: BaseException) -> Results:
    """Results for a grader crash: every question at 0, or one row without a bundle.

    Only the class name of `error` is shown, since its text may quote hidden test source.
    """

def grade(
    bundle: GradingBundle,
    submission: Path,
    root_workdir: Path,
    *,
    user: str | None,
    files_root: Path | None = None,
    files: Sequence[str] = (),
    python: str | None = None,
) -> Graded:
    """Run the public phase, then the hidden phase, within one time budget. Linux and macOS.

    Each phase gets a fresh `root_workdir/<phase>/` with a copy of the submission (as
    `submission.py`) and of each data file `files_root/<rel>` at `<rel>`. With a `user`, the
    folder belongs to that user, and every process of the user is killed after each phase.
    The caller must make sure that `user` can enter `root_workdir` but not write to it.
    Grading releases the GIL; Ctrl+C kills the running child and raises `KeyboardInterrupt`.

    Args:
        bundle: The full bundle; each phase gets its own part.
        submission: The student's notebook, a regular file.
        root_workdir: Existing folder for the phase folders.
        user: Dedicated grading user to run student code as, or `None` to keep the current one.
        files_root: Folder the data files are relative to. Required when `files` is given.
        files: Data files or folders for the notebook, as relative paths.
        python: Interpreter for the children, `sys.executable` by default. It must have mogs
            and the notebook's dependencies.

    Raises:
        ValueError: If `user` is root or the user running the grader, `submission` is not a
            regular file, or a data path is absolute, leaves its root or is `submission.py`.
        KeyError: If there is no such `user`.
        OSError: If a phase folder exists or a file cannot be copied.
    """

def autograde(source: Path, submission: Path, results: Path, workdir: Path) -> None:
    """Grade `submission` against `source/bundle.json` and write `results`, whatever happens.

    A failure gives results that name only its exception class, and a log on stderr. Linux and
    macOS.

    Args:
        source: Folder with `bundle.json` and the data files.
        submission: The unpacked upload.
        results: The `results.json` to write.
        workdir: Folder for the phase folders; replaced if it exists.
    """

def render_pdf(questions: Iterable[Question], status: Callable[[str], None]) -> bytes:
    """The PDF of `questions`, one page or more per question, after calling `status`.

    A Markdown part shows with LaTeX math, through the vendored Typst packages cmarker and
    mitex. A part that breaks the document is rendered again with less: first with each
    failing formula shown as LaTeX source, then as plain text, with a note under it. Typst
    runs with the GIL released; Ctrl+C stops it and raises `KeyboardInterrupt`.

    Raises:
        TypstError: If Typst fails even on the plain-text document.
        ValueError: If a part's role or style is not one of `Role` or `Style`.
    """

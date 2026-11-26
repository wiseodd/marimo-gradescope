"""Find the uploaded files and turn test outcomes into Gradescope's `results.json`.

Students see the score summary, the public test results and the notices. Each question's row,
which also lists its hidden tests, is hidden from them. Text a student controls (failure
messages, file names) is capped and quoted so it cannot change the layout around it.
"""

import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from mogs.grader.bundle import GradingBundle
from mogs.runner import Outcome, cap

SKIPPED_DIRS = {"__MACOSX", "__pycache__"}
NOTICE_ROW = "Submission"
PUBLIC_ROW = "Public tests"
ALL_PASSED = "All tests passed"


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


@dataclass(frozen=True, slots=True)
class Submission:
    """The files found in an upload.

    Attributes:
        py: The notebook to grade, or `None` when there is none or it is ambiguous.
        answers: The written answers: a PDF, else the export's Markdown fallback
            `<notebook stem>_answers.md`, else `None`. Only tells whether the student sent
            any; graders see every uploaded file.
        error: Why `py` is `None`, ready to show the student.
    """

    py: Path | None
    answers: Path | None
    error: str | None


def find_submission(sub_dir: Path, notebook: str) -> Submission:
    """Find the notebook and the written answers anywhere under `sub_dir`.

    Skips `__MACOSX`, `__pycache__`, dot files and dot folders, and never follows or picks a
    symlink. A file named `notebook` wins; otherwise the only `.py` file is taken. Any PDF
    counts as the written answers. Of Markdown files, only the export's fallback
    `<notebook stem>_answers.md` (any case) does, so a stray `README.md` does not.

    Args:
        sub_dir: The unpacked upload.
        notebook: File name of the student notebook.

    Returns:
        The files found, with an error message when the notebook is missing or ambiguous.
    """

    files = sorted(_regular_files(sub_dir))
    scripts = [path for path in files if path.suffix == ".py"]
    exact = [path for path in scripts if path.name == notebook]
    pdfs = [path for path in files if path.suffix.lower() == ".pdf"]
    fallback = f"{Path(notebook).stem}_answers.md".casefold()
    markdown = [path for path in files if path.name.casefold() == fallback]
    answers = next(iter(pdfs or markdown), None)

    match exact or scripts:
        case [py]:
            return Submission(py, answers, None)
        case []:
            error = f"No `.py` file found in your upload. Upload {inline_code(notebook)}."
        case several:
            names = ", ".join(inline_code(str(path.relative_to(sub_dir))) for path in several)
            error = (
                f"Found several notebooks: {names}. Upload only {inline_code(notebook)}, "
                "or a single `.py` file."
            )

    return Submission(None, answers, error)


def _regular_files(root: Path) -> list[Path]:
    """Regular files under `root`, skipping junk folders, dot names and symlinks."""

    found = []

    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in SKIPPED_DIRS]

        for name in names:
            path = Path(folder, name)

            if not name.startswith(".") and stat.S_ISREG(path.lstat().st_mode):
                found.append(path)

    return found


def build_results(
    bundle: GradingBundle, outcomes: Mapping[str, Outcome], notices: Sequence[str]
) -> Results:
    """Gradescope's `results.json` content.

    Each auto-graded question gets a row worth `points × passed / total`, hidden from students,
    that lists all its tests with their failure messages. A visible 0-point "Public tests" row,
    marked failed when one of them fails, lists the public tests. The top-level output gives the
    total and each question's score.
    Notices go first, in a 0-point "Submission" row marked failed, so Gradescope does not show
    it as passed. Manual questions get no row; with no rows at all, the results carry a
    top-level score of 0.

    Args:
        bundle: The full bundle.
        outcomes: One outcome per test of the bundle.
        notices: Messages about the upload, in Markdown.

    Returns:
        The results, ready for `json.dump`.

    Raises:
        KeyError: If a test has no outcome.
        ValueError: If an auto-graded question has no tests.
    """

    public_parts = []
    public_failed = False
    question_rows = []

    for question in bundle.config.questions.values():
        if question.manual:
            continue

        cells = [cell for cell in bundle.cells if cell.qid == question.id]
        public = [outcomes[name] for cell in cells if not cell.hidden for name in cell.tests]
        hidden = [outcomes[name] for cell in cells if cell.hidden for name in cell.tests]
        total = len(public) + len(hidden)

        if total == 0:
            raise ValueError(f"question {question.id} has no tests")

        if public:
            public_parts.append(f"**{question.title}**\n\n{_output(public, [])}")
            public_failed |= not all(outcome.passed for outcome in public)

        passed = sum(outcome.passed for outcome in public + hidden)
        score = round(question.points * passed / total, 4)
        output = _output(public, hidden)
        question_rows.append(
            row(question.title, score, question.points, output, question.id, hidden=True)
        )

    rows = [notice_row(notices)] if notices else []

    if public_parts:
        rows.append(row(PUBLIC_ROW, 0, 0, "\n\n".join(public_parts), failed=public_failed))

    rows += question_rows

    if not question_rows:
        return {"tests": rows, "stdout_visibility": "hidden", "score": 0.0}

    return {
        "tests": rows,
        "stdout_visibility": "hidden",
        "output": _summary(question_rows),
        "output_format": "md",
    }


def _summary(rows: Sequence[Row]) -> str:
    """The total and each question's score, in Markdown."""

    score = sum(row["score"] for row in rows)
    max_score = sum(row["max_score"] for row in rows)
    lines = [f"Total: {_fraction(score, max_score)}", ""]
    lines += [f"- {row['name']}: {_fraction(row['score'], row['max_score'])}" for row in rows]

    return "\n".join(lines)


def _fraction(score: float, max_score: float) -> str:
    """`18.75 / 25`."""

    return f"{round(score, 4):g} / {max_score:g}"


def failure_results(bundle: GradingBundle | None, error: BaseException) -> Results:
    """Results for a grader crash: every question at 0, or one row without a bundle.

    Every row is marked failed. Only the exception type is shown, since its text may quote
    hidden test source.
    """

    message = (
        f"The autograder failed ({type(error).__name__}), so nothing was graded. "
        "Please tell your instructor."
    )

    if bundle is None:
        rows = [row("Autograder", 0, 0, message, failed=True)]
    else:
        rows = [
            row(q.title, 0, q.points, message, q.id, failed=True)
            for q in bundle.config.questions.values()
            if not q.manual
        ]

    return {"tests": rows, "stdout_visibility": "hidden"}


def notice_row(notices: Sequence[str]) -> Row:
    """The 0-point row that shows messages about the upload."""

    return row(NOTICE_ROW, 0, 0, "\n\n".join(cap(notice) for notice in notices), failed=True)


def row(
    name: str,
    score: float,
    max_score: float,
    output: str,
    number: str | None = None,
    *,
    failed: bool = False,
    hidden: bool = False,
) -> Row:
    """One entry of `tests` in `results.json`.

    `failed` sets its status to `failed`, and `hidden` hides the row from students.
    """

    entry = Row(
        name=name,
        score=score,
        max_score=max_score,
        output=output,
        output_format="md",
        visibility="hidden" if hidden else "visible",
    )

    if number is not None:
        entry["number"] = number

    if failed:
        entry["status"] = "failed"

    return entry


def _output(public: list[Outcome], hidden: list[Outcome]) -> str:
    """Markdown for tests: `All tests passed`, or each test with its failure message."""

    if all(outcome.passed for outcome in public + hidden):
        return ALL_PASSED

    parts = []
    labeled = [(outcome, "") for outcome in public] + [(o, " (hidden)") for o in hidden]

    for outcome, label in labeled:
        mark = "✓" if outcome.passed else "✗"
        parts.append(f"{mark} {inline_code(outcome.name)}{label}")

        if not outcome.passed and outcome.message:
            parts.append(code_block(cap(outcome.message)))

    return "\n\n".join(parts)


def code_block(text: str) -> str:
    """A fenced code block holding `text` verbatim; the fence outgrows every backtick run."""

    fence = "`" * max(3, _longest_backtick_run(text) + 1)

    return f"{fence}\n{text}\n{fence}"


def inline_code(text: str) -> str:
    """`text` as one line of inline code that its own backticks cannot end.

    Control characters, newlines included, become `?`.
    """

    text = re.sub(r"[\x00-\x1f\x7f]", "?", text)
    fence = "`" * (_longest_backtick_run(text) + 1)

    return f"{fence} {text} {fence}" if "`" in text else f"{fence}{text}{fence}"


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall("`+", text)), default=0)

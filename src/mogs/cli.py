"""The `build` and `grade` commands, which `mogs._native.main` runs."""

import sys
from copy import replace
from pathlib import Path

from mogs.build import BuildError, BuildResult, build, failure_report
from mogs.check import format_points
from mogs.config import ConfigError
from mogs.regrade import GradeResult, grade_submission
from mogs.requirements import RequirementsError
from mogs.source import NO_TEXT, SourceError


def build_command(folder: Path) -> int:
    """Run `mogs build folder`: build, then print the report.

    Returns:
        Exit code: 0 on success, 1 when the build fails.
    """

    try:
        result = build(folder)
    except (BuildError, ConfigError, SourceError, RequirementsError, OSError) as error:
        print(f"Build failed.\n{error}", file=sys.stderr)
        return 1

    print(report(result))
    return 0


def grade_command(folder: Path, submission: Path) -> int:
    """Run `mogs grade folder submission`: grade, then print the scores.

    Returns:
        Exit code: 0 on success, 1 when grading fails or the submission does not load.
    """

    try:
        result = grade_submission(folder, submission)
    except (BuildError, ConfigError, SourceError, RequirementsError, OSError) as error:
        print(f"Grading failed.\n{error}", file=sys.stderr)
        return 1

    print(grade_report(result))

    if result.load_error is not None:
        print(
            f"Grading failed.\n{result.submission} failed to load: {result.load_error}",
            file=sys.stderr,
        )
        return 1

    return 0


def grade_report(result: GradeResult) -> str:
    """Per-question scores and the failing tests, hidden ones marked, as text."""

    lines = [f"Graded:  {result.submission}", ""]
    rows = [("Question", "Score")]
    titles = {q.id: q.title for q in result.bundle.config.questions.values()}

    for row in result.results["tests"]:
        if "number" in row:
            rows.append((titles[row["number"]], _fraction(row["score"], row["max_score"])))

    rows.append(("Total", _fraction(*result.score)))
    width = max(len(row[0]) for row in rows)
    lines += [f"{name:<{width}}  {score:>9}" for name, score in rows]

    hidden = {name for cell in result.bundle.cells if cell.hidden for name in cell.tests}
    failed = [
        replace(outcome, name=f"{outcome.name} (hidden)") if outcome.name in hidden else outcome
        for outcome in result.outcomes.values()
        if not outcome.passed
    ]

    if failed:
        lines += ["", "Failing tests:", *failure_report(failed, count_load_failures=False)]

    return "\n".join(lines)


def report(result: BuildResult) -> str:
    """The files to upload and distribute, then the self-check scores and the warnings.

    Each written question is listed with the first line of each of its prompt cells, so the
    instructor can check which text goes into the PDF.
    """

    lines = [
        "All done!",
        f"Upload to Gradescope  : {_shown(result.zip)}",
        f"Distribute to students: {_shown(result.student)}",
        "",
    ]

    rows = [("Question", "Public", "Hidden", "Solution", "Blank")]
    manual = []

    for score in result.scores:
        if score.solution is None or score.blank is None:
            how = (
                f"{format_points(score.points)}, graded by hand"
                if score.points
                else "collected in the PDF, not graded"
            )
            manual.append(f"- {score.id} {score.title}: {how}")
            manual += [f"    {line}" for line in score.prompts or (NO_TEXT,)]
            continue

        rows.append(
            (
                score.title,
                str(score.public),
                str(score.hidden),
                _fraction(score.solution, score.points),
                _fraction(score.blank, score.points),
            )
        )

    rows.append(("Total", "", "", _fraction(*result.solution), _fraction(*result.blank)))
    width = max(len(row[0]) for row in rows)
    lines += [
        f"{name:<{width}}  {public:>6}  {hidden:>6}  {solution:>9}  {blank:>9}"
        for name, public, hidden, solution, blank in rows
    ]

    if manual:
        lines += ["", "Not in the autograder total:"]
        lines += manual

    if result.warnings:
        lines += ["", "Warnings:"]
        lines += [f"- {warning}" for warning in result.warnings]

    return "\n".join(lines)


def _shown(path: Path) -> Path:
    """`path` relative to the working directory when it is inside it, else as it is."""

    cwd = Path.cwd()

    return path.relative_to(cwd) if path.is_relative_to(cwd) else path


def _fraction(score: float, points: float) -> str:
    return f"{score:g}/{points:g}"

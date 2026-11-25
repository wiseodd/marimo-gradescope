"""The `marimo-gradescope` command."""

import argparse
import sys
from copy import replace
from pathlib import Path

from marimo_gradescope import __version__
from marimo_gradescope.build import BuildError, BuildResult, build, failure_report
from marimo_gradescope.config import ConfigError
from marimo_gradescope.grade import GradeResult, grade_submission
from marimo_gradescope.requirements import RequirementsError
from marimo_gradescope.source import SourceError


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments; `sys.argv[1:]` when `None`.

    Returns:
        Exit code: 0 on success, 1 when the build fails.
    """

    args = _parser().parse_args(argv)

    match args.command:
        case "build":
            return _build(args.dir)
        case "grade":
            return _grade(args.dir, args.submission)
        case _:
            raise AssertionError(f"unhandled command {args.command!r}")


def _build(folder: Path) -> int:
    try:
        result = build(folder)
    except (BuildError, ConfigError, SourceError, RequirementsError, OSError) as error:
        print(f"Build failed.\n{error}", file=sys.stderr)
        return 1

    print(report(result))
    return 0


def _grade(folder: Path, submission: Path) -> int:
    try:
        result = grade_submission(folder, submission)
    except (BuildError, ConfigError, SourceError, RequirementsError, OSError) as error:
        print(f"Grading failed.\n{error}", file=sys.stderr)
        return 1

    print(grade_report(result))
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
        lines += ["", "Failing tests:", *failure_report(failed)]

    return "\n".join(lines)


def report(result: BuildResult) -> str:
    """Artifact paths, per-question test counts and self-check scores, and warnings, as text."""

    lines = [
        f"Student notebook:  {result.student}",
        f"Autograder zip:    {result.zip}",
        f"Solved notebook:   {result.solved}",
        "",
    ]

    rows = [("Question", "Public", "Hidden", "Solution", "Blank")]
    manual = []

    for score in result.scores:
        if score.solution is None or score.blank is None:
            manual.append(f"{score.id} {score.title}: {score.points:g} pts, graded by hand")
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
        lines += ["", "Not in the total (graded on Gradescope):"]
        lines += [f"- {line}" for line in manual]

    if result.warnings:
        lines += ["", "Warnings:"]
        lines += [f"- {warning}" for warning in result.warnings]

    return "\n".join(lines)


def _fraction(score: float, points: float) -> str:
    return f"{score:g}/{points:g}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marimo-gradescope",
        description="Build Gradescope autograders from marimo notebooks.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser(
        "build",
        help="build the student notebook and the autograder zip",
        description=(
            "Build DIR/dist/student/ and DIR/dist/autograder/ (the autograder zip and the "
            "solved student notebook) from the notebook in DIR, then grade the solved and the "
            "blank student copy."
        ),
    )
    build_parser.add_argument(
        "dir", type=Path, metavar="DIR", help="assignment folder with the source notebook"
    )

    grade_parser = commands.add_parser(
        "grade",
        help="grade one submission locally",
        description=(
            "Grade SUBMISSION against the source notebook in DIR with the real grader, as "
            "Gradescope would, and list every failing test, hidden ones included."
        ),
    )
    grade_parser.add_argument(
        "dir", type=Path, metavar="DIR", help="assignment folder with the source notebook"
    )
    grade_parser.add_argument(
        "submission",
        type=Path,
        metavar="SUBMISSION",
        help="student notebook, or a folder with one (such as a downloaded upload)",
    )

    return parser

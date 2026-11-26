"""The `marimogs` command."""

import argparse
import shlex
import subprocess
import sys
from copy import replace
from pathlib import Path

from marimogs import __version__
from marimogs.build import BuildError, BuildResult, build, failure_report, source_notebook
from marimogs.config import ConfigError
from marimogs.regrade import GradeResult, grade_submission
from marimogs.requirements import RequirementsError
from marimogs.scaffold import InitError, init
from marimogs.source import SourceError


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments; `sys.argv[1:]` when `None`.

    Returns:
        Exit code: 0 on success, 1 when the command fails.
    """

    args = _parser().parse_args(argv)

    match args.command:
        case "init":
            return _init(args.dir)
        case "edit":
            return _edit(args.path)
        case "build":
            return _build(args.dir)
        case "grade":
            return _grade(args.dir, args.submission)
        case _:
            raise AssertionError(f"unhandled command {args.command!r}")


def _init(folder: Path) -> int:
    try:
        path = init(folder)
    except (InitError, ConfigError, OSError) as error:
        print(f"Init failed.\n{error}", file=sys.stderr)
        return 1

    print(f"Created {path}")
    print(f"Edit it  : marimogs edit {shlex.quote(str(folder))}")
    print(f"Build it : marimogs build {shlex.quote(str(folder))}")
    return 0


def _edit(path: Path) -> int:
    """Open the source notebook at `path`, a file or its folder, with `marimo edit --sandbox`.

    marimo runs on this package's Python, which has pytest. The editor server compiles the
    test cells there, and without pytest it logs a warning for each of them.
    """

    try:
        notebook = source_notebook(path)
    except (BuildError, OSError) as error:
        print(f"Edit failed.\n{error}", file=sys.stderr)
        return 1

    marimo = subprocess.Popen([sys.executable, "-m", "marimo", "edit", "--sandbox", str(notebook)])

    # Ctrl+C reaches marimo too, and marimo asks whether to quit. Wait until it exits.
    while True:
        try:
            return marimo.wait()
        except KeyboardInterrupt:
            continue


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
        lines += ["", "Failing tests:", *failure_report(failed, count_load_failures=False)]

    return "\n".join(lines)


def report(result: BuildResult) -> str:
    """The files to upload and distribute, then the self-check scores and the warnings."""

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
                f"{score.points:g} pts, graded by hand"
                if score.points
                else "collected in the PDF, not graded"
            )
            manual.append(f"{score.id} {score.title}: {how}")
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
        lines += [f"- {line}" for line in manual]

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marimogs",
        description="Build Gradescope autograders from marimo notebooks.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser(
        "init",
        help="start a new assignment",
        description=(
            "Create DIR with one notebook, DIR/<name of DIR>.py, holding the assignment cell, "
            "a guide and two example questions."
        ),
    )
    init_parser.add_argument("dir", type=Path, metavar="DIR", help="new assignment folder")

    edit_parser = commands.add_parser(
        "edit",
        help="open the source notebook in marimo's editor",
        description=(
            "Run `marimo edit --sandbox` on the source notebook PATH, or the one in the "
            "assignment folder PATH, with the marimo installed next to marimo-gradescope."
        ),
    )
    edit_parser.add_argument(
        "path",
        type=Path,
        metavar="PATH",
        help="source notebook, or the assignment folder with it",
    )

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

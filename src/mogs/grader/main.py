"""Gradescope entry point: grade `/autograder/submission` and write `results.json`.

`run_autograder` calls `python -m mogs.grader.main`. It always writes
`results.json` and exits with code 0, since Gradescope treats any other code as a crash and
shows no results. Logs go to stderr, which students do not see.
"""

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from mogs.grader._user import STUDENT_USER
from mogs.grader.bundle import GradingBundle
from mogs.grader.results import (
    Results,
    build_results,
    failure_results,
    find_submission,
    inline_code,
)
from mogs.grader.sandbox import grade
from mogs.runner import FailureReason, Outcome

AUTOGRADER = Path("/autograder")
WORKDIR = Path("/tmp/mogs")

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    """Grade the submission and write `results.json`, whatever happens.

    Args:
        argv: Command-line arguments; `sys.argv[1:]` when `None`. The defaults are
            Gradescope's paths.
    """

    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    bundle = None

    try:
        bundle = GradingBundle.from_json((args.source / "bundle.json").read_text(encoding="utf-8"))
        results = run(bundle, args.source, args.submission, args.workdir)
    except Exception as error:
        logger.exception("the autograder failed")
        results = failure_results(bundle, error)

    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(results, indent=2), encoding="utf-8")


def run(bundle: GradingBundle, source: Path, submission: Path, workdir: Path) -> Results:
    """Find the upload, grade it in the sandbox and build the results.

    Args:
        bundle: The grading bundle.
        source: Folder with the data files, at their paths relative to the notebook.
        submission: The unpacked upload.
        workdir: Folder for the sandbox's phase folders; replaced if it exists.

    Returns:
        The results.
    """

    config = bundle.config
    found = find_submission(submission, config.notebook)
    notices = [] if found.error is None else [found.error]

    if any(question.manual for question in config.questions.values()):
        notices += _answers_notices(found.answers)

    if found.py is None:
        tests = [name for cell in bundle.cells for name in cell.tests]
        outcomes = {
            name: Outcome(name, False, "not run: no notebook", FailureReason.NOT_RUN)
            for name in tests
        }
    else:
        _fresh_workdir(workdir)
        user = STUDENT_USER if os.geteuid() == 0 else None
        outcomes = grade(
            bundle, found.py, workdir, user=user, files_root=source, files=config.files
        ).outcomes

    return build_results(bundle, outcomes, notices)


def _answers_notices(answers: Path | None) -> list[str]:
    """Warnings about the written answers for manual questions."""

    if answers is None:
        return [
            "No PDF of your written answers found. Questions graded by hand need it: upload "
            "the zip from the notebook's export button."
        ]

    if answers.suffix.lower() == ".md":
        return [
            f"No PDF of your written answers found; graders will read {inline_code(answers.name)}"
            " instead."
        ]

    return []


def _fresh_workdir(workdir: Path) -> None:
    """Create an empty `workdir` that others can enter but not write to."""

    if workdir.exists():
        shutil.rmtree(workdir)

    workdir.mkdir(parents=True)
    workdir.chmod(0o755)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=AUTOGRADER / "source")
    parser.add_argument("--submission", type=Path, default=AUTOGRADER / "submission")
    parser.add_argument("--results", type=Path, default=AUTOGRADER / "results" / "results.json")
    parser.add_argument("--workdir", type=Path, default=WORKDIR)

    return parser


if __name__ == "__main__":
    main()

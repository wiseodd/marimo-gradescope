"""Gradescope entry point: grade `/autograder/submission` and write `results.json`.

`run_autograder` calls `python -m mogs.grader.main`. It always writes
`results.json` and exits with code 0, since Gradescope treats any other code as a crash and
shows no results. Logs go to stderr, which students do not see.
"""

import argparse
import logging
import sys
from pathlib import Path

from mogs._native import autograde

AUTOGRADER = Path("/autograder")
WORKDIR = Path("/tmp/mogs")


def main(argv: list[str] | None = None) -> None:
    """Grade the submission and write `results.json`, whatever happens.

    Args:
        argv: Command-line arguments; `sys.argv[1:]` when `None`. The defaults are
            Gradescope's paths.
    """

    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    autograde(args.source, args.submission, args.results, args.workdir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=AUTOGRADER / "source")
    parser.add_argument("--submission", type=Path, default=AUTOGRADER / "submission")
    parser.add_argument("--results", type=Path, default=AUTOGRADER / "results" / "results.json")
    parser.add_argument("--workdir", type=Path, default=WORKDIR)

    return parser


if __name__ == "__main__":
    main()

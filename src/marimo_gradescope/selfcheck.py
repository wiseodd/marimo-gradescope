"""Run the public tests of a saved notebook and print a summary as JSON.

Usage: `python -m marimo_gradescope.selfcheck NOTEBOOK [FOLDER]`. The export button runs this
in a subprocess on the saved file, so the tests see the file on disk, not the kernel. It runs
from FOLDER, by default the notebook's folder, as marimo does, so relative paths in the
notebook work.

Tests are found statically and run the way the grader's public phase runs them: the setup
cell first, then the student names bound from the notebook, then each test cell compiled with
assertion rewriting. A student copy has no test cells; its grade calls name the files that
hold its public tests (`test_file`), and those run the same way. A file that cannot be read
counts as one failed test named after it. There is no per-test timer here; the caller caps
the whole run.

The last line of stdout is the JSON summary, `{qid: {"passed": k, "total": n, "failed":
[names]}}`. Prints from the notebook go to stderr. On a notebook that cannot be read or
parsed, the exit status is 1 and stderr says why.
"""

import contextlib
import json
import os
import sys
from pathlib import Path

from marimo_gradescope.config import AssignmentConfig
from marimo_gradescope.grader.bundle import Phase, TestCell, bundle_from_notebook
from marimo_gradescope.loader import bind
from marimo_gradescope.runner import (
    FailureReason,
    Outcome,
    cap,
    exception_summary,
    run_test_source,
)
from marimo_gradescope.source import (
    Notebook,
    SourceError,
    cell_names,
    module_tests,
    parse_notebook,
)

type Summary = dict[str, dict[str, object]]


def public_results(path: Path) -> Summary:
    """Run the public tests of the notebook at `path` in this process.

    Raises:
        OSError: If the file cannot be read.
        UnicodeDecodeError: If it is not UTF-8.
        SourceError: If it does not parse as a notebook.
    """

    src = path.read_text(encoding="utf-8")
    nb = parse_notebook(src)

    # The bundle keeps the config but does not read it; a student copy has none.
    cfg = AssignmentConfig(name=path.stem, notebook=path.name, questions={})
    bundle = bundle_from_notebook(nb, cfg).for_phase(Phase.PUBLIC)
    # Each cell with the name it reports a load failure under.
    cells = [(cell, f"{cell.qid}-tests") for cell in bundle.cells]
    outcomes: dict[str, list[Outcome]] = {}
    students = nb.student_names()

    for qid, relative in test_files(nb):
        try:
            source = (path.parent / relative).read_text(encoding="utf-8")
            names = module_tests(source)
            uses = tuple(sorted(cell_names(source)[1] & students))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            message = cap(f"cannot read {relative} ({exception_summary(exc)})")
            outcome = Outcome(relative, False, message, FailureReason.LOAD_FAILED)
            outcomes.setdefault(qid, []).append(outcome)
            continue

        cells.append((TestCell(qid, False, source, names, uses), relative))

    base: dict[str, object] = {"__name__": "__mgs_selfcheck__"}
    setup_error = None

    try:
        exec(compile(bundle.setup_code, "<mgs-setup>", "exec", dont_inherit=True), base)
    except Exception as exc:
        setup_error = cap(f"the setup cell failed ({exception_summary(exc)})")

    if setup_error is None:
        uses = sorted({name for cell, _ in cells for name in cell.uses})
        base.update(bind(path, uses))

    for cell, label in cells:
        results = outcomes.setdefault(cell.qid, [])

        if setup_error is not None:
            reason = FailureReason.LOAD_FAILED
            results += [Outcome(name, False, setup_error, reason) for name in cell.tests]
            continue

        results += run_test_source(cell.source, label, cell.tests, dict(base))

    return {
        qid: {
            "passed": sum(outcome.passed for outcome in results),
            "total": len(results),
            "failed": [outcome.name for outcome in results if not outcome.passed],
        }
        for qid, results in outcomes.items()
    }


def test_files(nb: Notebook) -> list[tuple[str, str]]:
    """Question id and `test_file` of each grade call that names one, without repeats."""

    calls = [cell for cell in nb.cells if cell.grade_qid is not None and not cell.hidden]
    pairs = [(cell.grade_qid, nb.grade_test_file(cell)) for cell in calls]

    return list(
        dict.fromkeys((qid, file) for qid, file in pairs if qid is not None and file is not None)
    )


def main(path: str | Path, folder: str | Path | None = None) -> int:
    """Print the public-test summary of the notebook at `path` as one JSON line.

    Args:
        path: The notebook file.
        folder: Working directory for the tests; the notebook's folder when `None`.

    Returns:
        The exit status: 0, or 1 when the notebook cannot be read or parsed.
    """

    sys.dont_write_bytecode = True
    notebook = Path(path).resolve()

    try:
        os.chdir(notebook.parent if folder is None else folder)
        with contextlib.redirect_stdout(sys.stderr):
            summary = public_results(notebook)
    except (OSError, UnicodeDecodeError, SourceError) as exc:
        print(f"Cannot check {notebook.name}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    match sys.argv[1:]:
        case [path]:
            sys.exit(main(path))
        case [path, folder]:
            sys.exit(main(path, folder))
        case _:
            sys.exit("Usage: python -m marimo_gradescope.selfcheck NOTEBOOK [FOLDER]")

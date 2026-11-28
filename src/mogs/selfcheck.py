"""Run the tests of a saved notebook and print a summary as JSON.

Usage: `python -m mogs.selfcheck NOTEBOOK [FOLDER]`. The export button runs this
in a subprocess on the saved file, so the tests see the file on disk, not the kernel. It runs
from FOLDER, by default the notebook's folder, as marimo does, so relative paths in the
notebook work.

Usage: `python -m mogs.selfcheck --bundle BUNDLE NOTEBOOK`. `mogs build` runs this on the
solved copy: every test of the grading bundle in the file BUNDLE, public and hidden, from the
notebook's folder. The last line of stdout is then `{"outcomes": [outcome, ...]}`, one per
test in bundle order. The hidden helpers run as in the grader.

Tests are found statically and run the way the grader's public phase runs them: the setup
cell first, then the student names bound from the notebook, then each test cell compiled with
assertion rewriting. A student copy has no test cells; its grade calls name the files that
hold its public tests (`test_file`), and those run the same way. A file that cannot be read
counts as one failed test named after it. There is no per-test timer here; the caller caps
the whole run.

The last line of stdout is the JSON summary, `{"questions": {qid: {"passed": k, "total": n,
"failed": [names]}}, "unavailable": {name: reason}}`. `unavailable` holds each name the tests
use that the notebook could not provide, such as a name lost to a failed notebook run,
with the reason. Prints from the notebook go to stderr. On a notebook that cannot be read or
parsed, the exit status is 1 and stderr says why.
"""

import contextlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from mogs.config import AssignmentConfig
from mogs.grader.bundle import (
    GradingBundle,
    HiddenHelper,
    Phase,
    TestCell,
    bundle_from_notebook,
)
from mogs.grader.helpers import Failure, Helpers, Namespace
from mogs.loader import bind, load, unavailable
from mogs.runner import (
    FailureReason,
    Outcome,
    cap,
    exception_summary,
    run_test_source,
)
from mogs.source import (
    Notebook,
    SourceError,
    cell_names,
    module_tests,
    parse_notebook,
)


class Summary(TypedDict):
    """The JSON summary: results per question, and the reason for each unavailable name."""

    questions: dict[str, dict[str, object]]
    unavailable: dict[str, str]


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
    cells = list(bundle.cells)
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

        cells.append(TestCell(qid, relative, False, source, names, uses))

    ran, missing = _run_cells(path, bundle.setup_code, cells, ())

    for qid, outcome in ran:
        outcomes.setdefault(qid, []).append(outcome)

    questions: dict[str, dict[str, object]] = {
        qid: {
            "passed": sum(outcome.passed for outcome in results),
            "total": len(results),
            "failed": [outcome.name for outcome in results if not outcome.passed],
        }
        for qid, results in outcomes.items()
    }

    return {"questions": questions, "unavailable": missing}


def bundle_results(path: Path, bundle: GradingBundle) -> list[Outcome]:
    """Run every test of `bundle`, public and hidden, on the notebook at `path`, in this process.

    `mogs build` checks the solved copy with this. The tests run as in the grader, in bundle
    order, with the hidden helpers they need, but in one process and without timers or limits;
    the caller caps the whole run. A notebook that does not import fails every test with
    `LOAD_FAILED`, as in the grader.
    """

    _, import_error = load(path)

    if import_error is not None:
        reason = FailureReason.LOAD_FAILED
        return [
            Outcome(name, False, import_error, reason)
            for cell in bundle.cells
            for name in cell.tests
        ]

    ran, _ = _run_cells(path, bundle.setup_code, bundle.cells, bundle.helpers)

    return [outcome for _, outcome in ran]


def _run_cells(
    path: Path, setup_code: str, cells: Sequence[TestCell], helpers: Sequence[HiddenHelper]
) -> tuple[list[tuple[str, Outcome]], dict[str, str]]:
    """Run the setup code, bind the names the cells use, then run each cell's tests.

    Returns:
        Each outcome with its question id, in cell order, and the reason for each name the
        notebook could not provide.
    """

    base: Namespace = {"__name__": "__mogs_selfcheck__"}

    try:
        exec(compile(setup_code, "<mogs-setup>", "exec", dont_inherit=True), base)
    except Exception as exc:
        error = cap(f"the setup cell failed ({exception_summary(exc)})")
        reason = FailureReason.LOAD_FAILED
        failed = [
            (cell.qid, Outcome(name, False, error, reason)) for cell in cells for name in cell.tests
        ]
        return failed, {}

    uses = sorted({name for part in [*helpers, *cells] for name in part.uses})
    bound = bind(path, uses)
    base.update(bound)
    missing = {
        name: reason for name, value in bound.items() if (reason := unavailable(value)) is not None
    }
    provider = Helpers(tuple(helpers), base, _load)
    ran: list[tuple[str, Outcome]] = []

    for cell in cells:
        match provider.provide(cell.needs):
            case Failure(message, reason):
                results = [Outcome(name, False, cap(message), reason) for name in cell.tests]
            case provided:
                namespace = {**base, **provided}
                results = run_test_source(cell.source, cell.label, cell.tests, namespace)

        ran += [(cell.qid, outcome) for outcome in results]

    return ran, missing


def _load(source: str, label: str, namespace: Namespace) -> tuple[str, FailureReason] | None:
    """Run a hidden helper cell into `namespace`, without a timer."""

    from mogs._assertrewrite import compile_tests

    try:
        exec(compile_tests(source, label), namespace)
    except BaseException as exc:
        return f"failed to load ({exception_summary(exc)})", FailureReason.LOAD_FAILED

    return None


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


def main_bundle(bundle: str | Path, path: str | Path) -> int:
    """Print the outcome of every test in the bundle file `bundle` on the notebook at `path`.

    The tests run from the notebook's folder.

    Returns:
        The exit status: 0, or 1 when the bundle or the notebook cannot be read.
    """

    sys.dont_write_bytecode = True
    notebook = Path(path).resolve()

    try:
        loaded = GradingBundle.from_json(Path(bundle).read_text(encoding="utf-8"))
        os.chdir(notebook.parent)
        with contextlib.redirect_stdout(sys.stderr):
            outcomes = bundle_results(notebook, loaded)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Cannot check {notebook.name}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"outcomes": [outcome.to_dict() for outcome in outcomes]}))
    return 0


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["--bundle", bundle, path]:
            sys.exit(main_bundle(bundle, path))
        case [path]:
            sys.exit(main(path))
        case [path, folder]:
            sys.exit(main(path, folder))
        case _:
            sys.exit(
                "Usage: python -m mogs.selfcheck NOTEBOOK [FOLDER]\n"
                "       python -m mogs.selfcheck --bundle BUNDLE NOTEBOOK"
            )

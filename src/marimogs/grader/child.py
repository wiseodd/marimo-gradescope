"""Grading child: runs one phase's tests on a submission and reports events.

Usage: `python -m marimogs.grader.child EVENT_FD`. The parent passes a writable file
descriptor `EVENT_FD` (e.g. with `pass_fds`) and writes a `Job` to stdin; the child writes
events to `EVENT_FD`, as `grader.protocol` describes. Stdout and stderr belong to the
student's code.

Linux and macOS: each test runs under a `SIGALRM` timer of `config.timeout` seconds.
"""

import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType

from marimogs._assertrewrite import compile_tests
from marimogs.grader.bundle import HiddenHelper, TestCell
from marimogs.grader.protocol import Event, Job, Loaded, Loading, Result, Start, encode
from marimogs.loader import bind, load
from marimogs.runner import (
    FailureReason,
    Outcome,
    cap,
    exception_summary,
    run_test,
)

type Emit = Callable[[Event], None]
type Namespace = dict[str, object]


class _Timeout(BaseException):
    """Raised by the timer inside the running test."""


class _Alarm:
    """One-shot `SIGALRM` timer that raises `_Timeout` and remembers that it fired.

    It fires at most once, so code that catches `_Timeout` around the `with` block sees every
    expiry, even one that lands while the block exits.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.fired = False

    def __enter__(self) -> None:
        signal.signal(signal.SIGALRM, self._expire)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, *exc: object) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

    def _expire(self, signum: int, frame: FrameType | None) -> None:
        self.fired = True
        raise _Timeout


def main(argv: list[str] | None = None) -> int:
    """Read the job from stdin, run it, and write events to the descriptor in `argv`."""

    [fd] = sys.argv[1:] if argv is None else argv
    events = int(fd)

    if events <= 2:
        raise SystemExit(f"EVENT_FD must not be stdin, stdout or stderr (0-2); got {events}.")

    # Student code may start processes; they must not inherit the event channel.
    os.set_inheritable(events, False)

    job = Job.from_json(sys.stdin.read())
    _close_stdin()

    with os.fdopen(events, "w", encoding="utf-8") as stream:

        def emit(event: Event) -> None:
            stream.write(encode(event))
            stream.flush()

        run(job, emit)

    return 0


def run(job: Job, emit: Emit) -> None:
    """Set up the namespace, then run every test not in `job.skip`, emitting events.

    The base namespace holds the setup code's names and the bound student names. Each test
    cell runs in its own copy of it, plus the names of the hidden helpers it needs. If the
    setup code fails or the notebook does not import, every test fails with `LOAD_FAILED`.
    """

    bundle = job.bundle
    timeout = bundle.config.timeout
    base: Namespace = {"__name__": "__mgs_tests__"}

    try:
        exec(compile(bundle.setup_code, "<mgs-setup>", "exec", dont_inherit=True), base)
    except BaseException as exc:
        error = cap(f"the grader's setup code failed ({exception_summary(exc)})")
        emit(Loaded(error))

        for cell in bundle.cells:
            _fail_cell(cell, job.skip, error, FailureReason.LOAD_FAILED, emit)

        return

    _, import_error = load(job.submission)

    if import_error is not None:
        emit(Loaded(import_error))

        for cell in bundle.cells:
            _fail_cell(cell, job.skip, import_error, FailureReason.LOAD_FAILED, emit)

        return

    # One call: a second `bind` could give different objects after an `app.run()` fallback.
    parts = [*bundle.helpers, *bundle.cells]
    uses = sorted({name for part in parts for name in part.uses})
    base.update(bind(job.submission, uses, run_failed=job.run_failed))
    emit(Loaded(None))

    helpers = _Helpers(bundle.helpers, base, timeout)

    for cell in bundle.cells:
        _run_cell(cell, base, helpers, job.skip, timeout, emit)


@dataclass(frozen=True, slots=True)
class _Failure:
    """Why a hidden helper, or one it needs, did not load."""

    message: str
    reason: FailureReason


class _Helpers:
    """Runs hidden helpers on first need, each at most once, and keeps what they define.

    A helper runs in a copy of the base namespace plus the names of the helpers it needs, which
    run first. A failure is kept too, so it reaches every cell that needs the helper and no
    other cell.
    """

    def __init__(self, helpers: tuple[HiddenHelper, ...], base: Namespace, timeout: float) -> None:
        self._helpers = helpers
        self._base = base
        self._timeout = timeout
        self._done: dict[int, Namespace | _Failure] = {}
        self._running: set[int] = set()

    def provide(self, needs: tuple[str, ...]) -> Namespace | _Failure:
        """The values of `needs`, or the failure of a helper that defines one of them."""

        names: Namespace = {}

        for index, helper in enumerate(self._helpers):
            if not set(helper.defines) & set(needs):
                continue

            match self._run(index):
                case _Failure() as failure:
                    return failure
                case defined:
                    names.update(defined)

        return names

    def _run(self, index: int) -> Namespace | _Failure:
        if index in self._done:
            return self._done[index]

        helper = self._helpers[index]
        label = ", ".join(f"`{name}`" for name in helper.defines)

        if index in self._running:
            message = f"the hidden helper cell defining {label} needs itself through other cells"
            return _Failure(message, FailureReason.LOAD_FAILED)

        self._running.add(index)

        match self.provide(helper.needs):
            case _Failure() as failure:
                result: Namespace | _Failure = failure
            case names:
                namespace = {**self._base, **names}
                match _load(helper.source, "hidden-helper", namespace, self._timeout):
                    case (message, reason):
                        result = _Failure(
                            f"the hidden helper cell defining {label} {message}", reason
                        )
                    case None:
                        result = {
                            name: namespace[name] for name in helper.defines if name in namespace
                        }

        self._running.discard(index)
        self._done[index] = result

        return result


def _load(
    source: str, label: str, namespace: Namespace, timeout: float
) -> tuple[str, FailureReason] | None:
    """Run cell source into `namespace` under the timer.

    Returns:
        `None` on success, else the end of a message (after "the cell") and the reason.
    """

    alarm = _Alarm(timeout)
    error = None

    try:
        with alarm:
            exec(compile_tests(source, label), namespace)
    except BaseException as exc:
        error = exc

    if alarm.fired:
        return f"took longer than {timeout:g} s to load", FailureReason.TIMEOUT

    if error is not None:
        return f"failed to load ({exception_summary(error)})", FailureReason.LOAD_FAILED

    return None


def _run_cell(
    cell: TestCell,
    base: Namespace,
    helpers: _Helpers,
    skip: tuple[str, ...],
    timeout: float,
    emit: Emit,
) -> None:
    """Run one cell's body in a copy of `base` and its helpers' names, then its tests."""

    tests = tuple(name for name in cell.tests if name not in skip)

    if not tests:
        return

    # The parent blames these tests if the process dies or hangs while the cell loads.
    emit(Loading(tests))

    match helpers.provide(cell.needs):
        case _Failure(message, reason):
            _fail_cell(cell, skip, message, reason, emit)
            return
        case provided:
            pass

    namespace = {**base, **provided}

    match _load(cell.source, f"{cell.qid}-tests", namespace, timeout):
        case (message, reason):
            _fail_cell(cell, skip, f"the test cell {message}", reason, emit)
            return

    for name in tests:
        emit(Start(name))
        emit(Result(_run_one(name, namespace.get(name), timeout)))


def _run_one(name: str, test: object, timeout: float) -> Outcome:
    """Run one test under the timer."""

    if not callable(test):
        return Outcome(
            name, False, f"`{name}` is not defined by its test cell", FailureReason.ERROR
        )

    alarm = _Alarm(timeout)
    outcome = None

    try:
        with alarm:
            outcome = run_test(name, test)
    except _Timeout:
        pass

    if alarm.fired or outcome is None:
        message = f"the test took longer than {timeout:g} s"
        return Outcome(name, False, message, FailureReason.TIMEOUT, timeout)

    return outcome


def _fail_cell(
    cell: TestCell, skip: tuple[str, ...], message: str, reason: FailureReason, emit: Emit
) -> None:
    """Fail every test of `cell` not in `skip` with the same message."""

    for name in cell.tests:
        if name in skip:
            continue

        emit(Start(name))
        emit(Result(Outcome(name, False, cap(message), reason)))


def _close_stdin() -> None:
    """Point fd 0 at `/dev/null`, closing the parent's pipe; student reads see end of file."""

    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)


if __name__ == "__main__":
    sys.exit(main())

"""Grade a submission in child processes that the parent watches and restarts.

Each phase runs in its own child, as an unprivileged user when one is given. The parent
enforces every deadline itself, since student code can defeat the child's own timer and C code
ignores it. When a child dies or misses a deadline, the parent kills it and everything it
started, fails the tests it was working on, and starts a new child for the rest, at most
`MAX_RESPAWNS` times. Every test gets exactly one outcome. Linux and macOS; a grading user
and its process cap are for Gradescope (Linux).

A child that dies or hangs while it loads the notebook may be stuck in the whole-notebook run
(`loader.bind`). The parent then starts one more child that skips that run, so names that
need no run still get graded. Later children and the hidden phase skip it too. If that child
also fails to load, every test of the phase fails.

The parent keeps the first result per test and ignores results for tests the child has not
announced. Code in the child can still write forged events to the channel; the threat model
accepts that.
"""

import logging
import pwd
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from enum import Enum, auto
from pathlib import Path, PurePosixPath

from marimo_gradescope.grader._process import (
    Child,
    check_user,
    describe_exit,
    kill_user,
    launch_options,
)
from marimo_gradescope.grader.bundle import GradingBundle, Phase
from marimo_gradescope.grader.datafiles import SUBMISSION, data_files
from marimo_gradescope.grader.protocol import (
    Event,
    Job,
    Loaded,
    Loading,
    Result,
    Start,
)
from marimo_gradescope.runner import FailureReason, Outcome, cap

MAX_RESPAWNS = 3
GRACE = 5.0
EXIT_GRACE = 2.0

logger = logging.getLogger(__name__)


def grade(
    bundle: GradingBundle,
    submission: Path,
    root_workdir: Path,
    *,
    user: str | None,
    files_root: Path | None = None,
    files: Sequence[str] = (),
) -> dict[str, Outcome]:
    """Run the public phase, then the hidden phase, within one time budget.

    Each phase gets a fresh `root_workdir/<phase>/` with a copy of the submission (as
    `submission.py`) and of each data file `files_root/<rel>` at `<rel>`. With a `user`, the
    directory belongs to that user, and every process of the user is killed after each phase.

    The caller must make sure that `user` can enter `root_workdir` but not write to it.

    Args:
        bundle: The full bundle; each phase gets its own part.
        submission: The student's notebook, a regular file.
        root_workdir: Existing directory for the phase directories.
        user: Dedicated grading user to run student code as, or `None` to keep the current one.
        files_root: Directory the data files are relative to. Required when `files` is given.
        files: Data files or directories for the notebook, as relative paths.

    Returns:
        One outcome per test of the bundle: public tests first, then hidden ones.

    Raises:
        ValueError: If `user` is root or the user running the grader, `submission` is not a
            regular file, or a data path is absolute, leaves its root or is `submission.py`.
    """

    if user is not None:
        check_user(user)

    if not stat.S_ISREG(submission.lstat().st_mode):
        raise ValueError(f"the submission must be a regular file: {submission}")

    data = _data_paths(files_root, files)
    deadline = time.monotonic() + bundle.config.budget
    results: dict[str, Outcome] = {}
    run_failed = None

    for phase in (Phase.PUBLIC, Phase.HIDDEN):
        workdir = _prepare(root_workdir / phase.value, submission, data, user)
        outcomes, run_failed = _run_phase(
            bundle, phase, workdir / SUBMISSION, workdir, user, deadline, run_failed
        )
        results |= outcomes

        if user is not None:
            kill_user(user)

    return results


def run_phase(
    bundle: GradingBundle,
    phase: Phase,
    submission: Path,
    workdir: Path,
    *,
    user: str | None,
    deadline: float,
) -> dict[str, Outcome]:
    """Run one phase's tests in children started in `workdir`.

    Args:
        bundle: The full bundle; only `phase`'s part reaches the child.
        phase: The phase.
        submission: The student's notebook.
        workdir: Existing working directory for the children.
        user: User to run the children as, or `None` to keep the current one.
        deadline: `time.monotonic()` value at which the time budget runs out.

    Returns:
        One outcome per test of the phase, in bundle order.
    """

    outcomes, _ = _run_phase(bundle, phase, submission, workdir, user, deadline, None)
    return outcomes


def _run_phase(
    bundle: GradingBundle,
    phase: Phase,
    submission: Path,
    workdir: Path,
    user: str | None,
    deadline: float,
    run_failed: str | None,
) -> tuple[dict[str, Outcome], str | None]:
    """`run_phase`, starting from and returning why the whole-notebook run failed, if it did."""

    bundle = bundle.for_phase(phase)
    tests = [name for cell in bundle.cells for name in cell.tests]
    done: dict[str, Outcome] = {}
    respawns = 0

    while pending := [name for name in tests if name not in done]:
        if respawns > MAX_RESPAWNS:
            message = f"not run: the grader restarted {MAX_RESPAWNS} times after crashes"
            _fail(done, pending, message, FailureReason.NOT_RUN)
            break

        if time.monotonic() >= deadline:
            _fail(done, pending, _budget_message(bundle), FailureReason.BUDGET)
            break

        job = Job(bundle, submission, skip=tuple(done), run_failed=run_failed)
        options = launch_options(workdir, user, bundle.config.memory_mb)
        child = Child(job.to_json(), options)
        watch = _Watch(bundle, pending, done, deadline, retry_load=run_failed is None)

        try:
            ending = watch.run(child)
        finally:
            output = child.close()
            if output:
                logger.info("grading child output (%s phase, end):\n%s", phase, output)

        match ending:
            case _Ending.STOP:
                break
            case _Ending.RETRY_WITHOUT_RUN:
                run_failed = watch.load_failure
            case _Ending.RESPAWN:
                respawns += 1

    return {name: done[name] for name in tests}, run_failed


class _Ending(Enum):
    """What `run_phase` does after a child ends."""

    STOP = auto()
    RESPAWN = auto()
    RETRY_WITHOUT_RUN = auto()


class _Stage(Enum):
    """What the child is doing, as far as its events tell."""

    SETUP = auto()
    LOADING = auto()
    TEST = auto()
    IDLE = auto()
    FINISHING = auto()


class _Watch:
    """Follows one child's events, enforces deadlines and fails the tests it is blamed for.

    Every test it fails or finishes goes into `done`, which the caller shares across children.
    With `retry_load`, a child that dies or hangs before it has loaded the notebook fails no
    test: the watch keeps why in `load_failure` and asks for a retry without the notebook run.
    """

    def __init__(
        self,
        bundle: GradingBundle,
        pending: list[str],
        done: dict[str, Outcome],
        deadline: float,
        *,
        retry_load: bool,
    ) -> None:
        self._timeout = bundle.config.timeout
        self._budget = bundle.config.budget
        self._budget_message = _budget_message(bundle)
        self._pending = pending
        self._done = done
        self._deadline = deadline
        self._started: set[str] = set()
        self._stage = _Stage.SETUP
        self._unit: tuple[str, ...] = ()
        self._since = time.monotonic()
        self._due = self._since + self._timeout + GRACE
        self._retry_load = retry_load
        self.load_failure: str | None = None

    def run(self, child: Child) -> _Ending:
        """Watch `child` until it exits or misses a deadline."""

        while True:
            until = min(self._due, self._deadline)

            for event in child.wait(until):
                self._handle(event)

            if child.error is not None:
                logger.warning("grading child broke the protocol: %s", child.error)
                return self._crashed("the grader received a malformed event")

            if child.returncode is not None:
                return self._exited(child.returncode)

            now = time.monotonic()

            if now >= self._deadline:
                if self._stage is _Stage.FINISHING:
                    return _Ending.STOP
                stopped = f"stopped: grading ran past its {self._budget:g} s budget"
                self._fail_unit(stopped, FailureReason.BUDGET)
                self._fail_rest(self._budget_message, FailureReason.BUDGET)
                return _Ending.STOP

            if now >= self._due:
                return self._overdue(now)

    def _handle(self, event: Event) -> None:
        match event:
            case Loaded():
                if self._stage is _Stage.SETUP:
                    self._enter(_Stage.IDLE, ())
            case Loading(tests):
                self._enter(_Stage.LOADING, tuple(t for t in tests if self._open(t)))
            case Start(test) if self._open(test):
                self._started.add(test)
                self._enter(_Stage.TEST, (test,))
            case Result(outcome) if outcome.name in self._started and self._open(outcome.name):
                self._done[outcome.name] = outcome
                self._enter(_Stage.IDLE, ())

        if all(name in self._done for name in self._pending):
            self._enter(_Stage.FINISHING, ())

    def _open(self, test: str) -> bool:
        """Whether `test` belongs to this phase and has no outcome yet."""

        return test in self._pending and test not in self._done

    def _enter(self, stage: _Stage, unit: tuple[str, ...]) -> None:
        self._stage = stage
        self._unit = unit
        self._since = time.monotonic()
        grace = EXIT_GRACE if stage is _Stage.FINISHING else self._timeout + GRACE
        self._due = self._since + grace

    def _exited(self, returncode: int) -> _Ending:
        if self._stage is _Stage.FINISHING:
            return _Ending.STOP

        return self._crashed(describe_exit(returncode))

    def _crashed(self, status: str) -> _Ending:
        """Fail what the child was doing when it died; respawn, or retry setup without the run."""

        match self._stage:
            case _Stage.SETUP if self._retry_load:
                self.load_failure = status
                return _Ending.RETRY_WITHOUT_RUN
            case _Stage.SETUP:
                message = f"the grader stopped while loading your notebook: {status}"
                self._fail_rest(message, FailureReason.LOAD_FAILED)
                return _Ending.STOP
            case _Stage.LOADING:
                message = f"the grader stopped while loading the test cell: {status}"
                self._fail_unit(message, FailureReason.KILLED)
            case _Stage.TEST:
                message = f"the grader stopped during this test: {status}"
                self._fail_unit(message, FailureReason.KILLED)
            case _Stage.IDLE | _Stage.FINISHING:
                pass

        return _Ending.RESPAWN

    def _overdue(self, now: float) -> _Ending:
        """Fail what the child is stuck on; respawn, or retry setup without the run."""

        took = f"it took longer than {self._timeout:g} s"

        match self._stage:
            case _Stage.SETUP if self._retry_load:
                self.load_failure = took
                return _Ending.RETRY_WITHOUT_RUN
            case _Stage.SETUP:
                message = f"the grader stopped while loading your notebook: {took}"
                self._fail_rest(message, FailureReason.TIMEOUT)
                return _Ending.STOP
            case _Stage.LOADING:
                message = f"the grader stopped while loading the test cell: {took}"
                self._fail_unit(message, FailureReason.TIMEOUT)
            case _Stage.TEST:
                message = f"the test took longer than {self._timeout:g} s"
                self._fail_unit(message, FailureReason.TIMEOUT, now - self._since)
            case _Stage.FINISHING:
                return _Ending.STOP
            case _Stage.IDLE:
                pass

        return _Ending.RESPAWN

    def _fail_unit(self, message: str, reason: FailureReason, seconds: float = 0.0) -> None:
        _fail(self._done, self._unit, message, reason, seconds)

    def _fail_rest(self, message: str, reason: FailureReason) -> None:
        _fail(self._done, self._pending, message, reason)


def _fail(
    done: dict[str, Outcome],
    names: Sequence[str],
    message: str,
    reason: FailureReason,
    seconds: float = 0.0,
) -> None:
    """Give each of `names` without an outcome a failed one."""

    for name in names:
        if name not in done:
            done[name] = Outcome(name, False, cap(message), reason, seconds)


def _budget_message(bundle: GradingBundle) -> str:
    return f"not run: grading took longer than its {bundle.config.budget:g} s budget"


def _data_paths(files_root: Path | None, files: Sequence[str]) -> dict[PurePosixPath, Path]:
    """Data files by relative destination; see `datafiles.data_files`.

    Raises:
        ValueError: If a path is bad or missing, or `files` is given without `files_root`.
    """

    if not files:
        return {}

    if files_root is None:
        raise ValueError("data files need a files_root")

    return data_files(files_root, files)


def _prepare(
    workdir: Path, submission: Path, files: Mapping[PurePosixPath, Path], user: str | None
) -> Path:
    """Create `workdir` with copies of the submission and the data files.

    Raises:
        FileExistsError: If `workdir` exists; each phase needs a fresh one.
    """

    workdir.mkdir(mode=0o700)
    shutil.copyfile(submission, workdir / SUBMISSION, follow_symlinks=False)

    for rel, source in files.items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    if user is not None:
        _hand_over(workdir, user)

    return workdir


def _hand_over(workdir: Path, user: str) -> None:
    """Make `user` the owner of `workdir` and everything in it."""

    group = pwd.getpwnam(user).pw_gid

    for path in [workdir, *workdir.rglob("*")]:
        shutil.chown(path, user, group)

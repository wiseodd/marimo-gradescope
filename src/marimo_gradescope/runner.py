"""Run test functions and record outcomes with short failure messages.

The notebook `check()` panel and the Gradescope grader share this runner.
"""

import linecache
import textwrap
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

MAX_LINES = 25
MAX_BYTES = 4096
MAX_STATEMENT_LINES = 5
MAX_SOURCE_WIDTH = 200
TRUNCATED = "... (truncated)"
UNAVAILABLE = "<failure details unavailable>"

type TestFunction = Callable[[], object]


class FailureReason(StrEnum):
    """Why a test did not pass."""

    ASSERTION = "assertion"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    OUT_OF_MEMORY = "out_of_memory"
    BUDGET = "budget"
    LOAD_FAILED = "load_failed"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class Outcome:
    """Result of one test.

    Attributes:
        name: Test name.
        passed: Whether the test passed.
        message: Failure message, empty when passed.
        reason: Why the test failed, `None` when passed.
        seconds: Wall time spent in the test.
    """

    name: str
    passed: bool
    message: str = ""
    reason: FailureReason | None = None
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """JSON-ready fields; `reason` becomes its string value."""

        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "reason": None if self.reason is None else self.reason.value,
            "seconds": self.seconds,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        """Inverse of `to_dict`.

        Raises:
            ValueError: If a field is missing or has the wrong type, or `reason` is unknown.
        """

        match data:
            case {
                "name": str() as name,
                "passed": bool() as passed,
                "message": str() as message,
                "reason": None | str() as reason,
                "seconds": int() | float() as seconds,
            } if not isinstance(seconds, bool):
                kind = None if reason is None else FailureReason(reason)
                return cls(name, passed, message, kind, float(seconds))

        raise ValueError(f"not an outcome: {data!r}")


def run_test(name: str, fn: TestFunction) -> Outcome:
    """Call one test function and record its outcome.

    Never raises: every `BaseException`, including `SystemExit` and `KeyboardInterrupt`, becomes
    a failed outcome. `AssertionError` gives `ASSERTION`, `MemoryError` gives `OUT_OF_MEMORY`,
    anything else gives `ERROR`.

    Args:
        name: Test name for the outcome.
        fn: The test, called without arguments.

    Returns:
        The outcome.
    """

    start = time.perf_counter()

    try:
        fn()
    except BaseException as exc:
        seconds = time.perf_counter() - start

        # A hostile exception can break any step of describing it.
        try:
            return Outcome(name, False, failure_message(exc), failure_reason(exc), seconds)
        except BaseException:
            return Outcome(name, False, UNAVAILABLE, FailureReason.ERROR, seconds)

    return Outcome(name, True, seconds=time.perf_counter() - start)


def failure_reason(exc: BaseException) -> FailureReason:
    """Classify an exception raised by a test."""

    # `type()` and `issubclass` ignore an instance's own `__class__` attribute.
    kind = type(exc)

    if issubclass(kind, AssertionError):
        return FailureReason.ASSERTION

    if issubclass(kind, MemoryError):
        return FailureReason.OUT_OF_MEMORY

    return FailureReason.ERROR


def failure_message(exc: BaseException) -> str:
    """Describe a test failure in at most `MAX_LINES` lines and `MAX_BYTES` bytes.

    The message has the failing statement in the test function, then, when the error came from
    deeper code, the statement there, then `ExcType: message`.
    """

    summary = exception_summary(exc)

    try:
        location = failure_location(exc)
    except BaseException:
        location = []

    return cap("\n".join([*location, summary]))


def failure_location(exc: BaseException) -> list[str]:
    """Lines naming the failing statement in the test and, if different, the innermost one."""

    # The first traceback entry is `run_test` itself; the next is the test function.
    tb = exc.__traceback__
    tb = tb.tb_next if tb is not None else None

    if tb is None:
        return []

    first = traceback.extract_tb(tb, limit=1)[0]
    last = traceback.extract_tb(tb, limit=-1)[0]
    depth = sum(1 for _ in traceback.walk_tb(tb))

    lines = frame_lines(first)

    if depth > 1:
        lines += ["..."] if depth > 2 else []
        lines += frame_lines(last)

    return lines


def frame_lines(frame: traceback.FrameSummary) -> list[str]:
    """The header and statement lines for one traceback entry."""

    header = f"In {frame.name}, line {frame.lineno}:"

    if frame.lineno is None:
        return [header]

    end = frame.end_lineno or frame.lineno
    end = min(end, frame.lineno + MAX_STATEMENT_LINES - 1)
    statement = "".join(linecache.getline(frame.filename, n) for n in range(frame.lineno, end + 1))

    if not statement.strip():
        return [header]

    # Short lines keep the exception summary inside the byte cap.
    source = textwrap.dedent(statement).rstrip().splitlines()

    return [header, *(f"    {shorten(line)}" for line in source)]


def shorten(line: str) -> str:
    """Cut a source line to `MAX_SOURCE_WIDTH` characters."""

    return line if len(line) <= MAX_SOURCE_WIDTH else line[: MAX_SOURCE_WIDTH - 3] + "..."


def exception_summary(exc: BaseException) -> str:
    """`ExcType: message`, or `ExcType` alone when the message is empty."""

    try:
        kind = str.__str__(type(exc).__name__)
    except BaseException:
        kind = "<unnamed exception>"

    try:
        # `__str__` may return a `str` subclass; make it a plain `str`.
        text = str.__str__(str(exc))
    except BaseException:
        return f"{kind}: <str() of the exception failed>"

    return f"{kind}: {text}" if text else kind


def cap(message: str) -> str:
    """Cut a message to `MAX_LINES` lines and `MAX_BYTES` UTF-8 bytes.

    Characters UTF-8 cannot encode, such as lone surrogates, become `?`.
    """

    data = message.encode(errors="replace")
    message = data.decode()

    if len(message.splitlines()) <= MAX_LINES and len(data) <= MAX_BYTES:
        return message

    # Keep a prefix that leaves room for the marker line in both limits.
    room = MAX_BYTES - len(TRUNCATED.encode()) - 1
    head = data[:room].decode(errors="ignore").splitlines()[: MAX_LINES - 1]

    return "\n".join([*head, TRUNCATED])

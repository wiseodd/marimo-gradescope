"""Messages between the grader parent and a grading child.

The parent writes one `Job` as JSON to the child's stdin. The child writes events, one JSON
object per line, to a separate file descriptor whose number is its first argument. Each event
starts with `"schema": SCHEMA`, the format version:

- `{"event": "loaded", "error": str | None, "unavailable": {name: reason}}` once, after setup
  and binding;
- `{"event": "loading", "tests": [name, ...]}` before loading each test cell (and the hidden
  helpers it needs), naming the cell's tests that will run;
- `{"event": "start", "test": name}` before each test;
- `{"event": "result", **outcome}` after each test, with the fields of `Outcome.to_dict`.

A job or event of another format version is a `ProtocolError`.

Student code writes to stdout and stderr freely; neither carries events.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from mogs.grader.bundle import GradingBundle
from mogs.grader.wire import SCHEMA, check_schema
from mogs.runner import Outcome


class ProtocolError(Exception):
    """A line or job that does not follow the protocol."""


@dataclass(frozen=True, slots=True)
class Job:
    """What one child runs.

    Attributes:
        bundle: The bundle of one phase.
        submission: The student's notebook.
        skip: Tests already done, e.g. by an earlier child that died.
        run_failed: Why an earlier child died while running the whole notebook. When set,
            the child does not run it again; see `loader.bind`.
        alarm: Whether the child times each test itself with an alarm. The child does not
            read it yet.
    """

    bundle: GradingBundle
    submission: Path
    skip: tuple[str, ...] = ()
    run_failed: str | None = None
    alarm: bool = True

    def to_json(self) -> str:
        """The job as JSON text."""

        return json.dumps(
            {
                "schema": SCHEMA,
                "bundle": self.bundle.to_dict(),
                "submission": str(self.submission),
                "skip": list(self.skip),
                "run_failed": self.run_failed,
                "alarm": self.alarm,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Inverse of `to_json`.

        Raises:
            ProtocolError: If the text is not a job of version `SCHEMA`.
        """

        try:
            data = json.loads(text)
            check_schema(data)

            match data:
                case {
                    "bundle": bundle,
                    "submission": str() as submission,
                    "skip": list() as skip,
                    "run_failed": None | str() as run_failed,
                    "alarm": bool() as alarm,
                } if all(isinstance(name, str) for name in skip):
                    bundle = GradingBundle.from_dict(bundle)
                    return cls(bundle, Path(submission), tuple(skip), run_failed, alarm)
        except ValueError as e:
            raise ProtocolError(f"bad job: {e}") from e

        raise ProtocolError("bad job: expected bundle, submission, skip, run_failed and alarm")


@dataclass(frozen=True, slots=True)
class Loaded:
    """The child is ready to run tests, or failed to set up (`error`).

    Attributes:
        error: Why setup failed, `None` when it worked.
        unavailable: `(name, reason)` for each name the tests use that the submission could
            not provide, in the order found. The child leaves it empty for now.
    """

    error: str | None
    unavailable: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Loading:
    """The child is about to load the test cell of `tests`."""

    tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Start:
    """The child is about to run `test`."""

    test: str


@dataclass(frozen=True, slots=True)
class Result:
    """A test finished."""

    outcome: Outcome


type Event = Loaded | Loading | Start | Result


def encode(event: Event) -> str:
    """One event as a JSON line, ending in a newline."""

    match event:
        case Loaded(error, unavailable):
            data: dict[str, object] = {
                "event": "loaded",
                "error": error,
                "unavailable": dict(unavailable),
            }
        case Loading(tests):
            data = {"event": "loading", "tests": list(tests)}
        case Start(test):
            data = {"event": "start", "test": test}
        case Result(outcome):
            data = {"event": "result", **outcome.to_dict()}

    data = {"schema": SCHEMA, **data}

    # `json.dumps` escapes newlines inside strings, so the line stays one line.
    return json.dumps(data) + "\n"


def decode(line: str) -> Event:
    """Parse one event line.

    The line may come from hostile code, so any failure to parse or validate it, such as a
    `RecursionError` from deeply nested JSON, becomes a `ProtocolError`.

    Raises:
        ProtocolError: If the line is not an event.
    """

    try:
        return _decode(line)
    except ProtocolError:
        raise
    except Exception as e:
        raise ProtocolError(f"bad event ({type(e).__name__}): {_excerpt(line)}") from e


def _decode(line: str) -> Event:
    try:
        data = json.loads(line)
    except ValueError as e:
        raise ProtocolError(f"not JSON: {_excerpt(line)}") from e

    try:
        check_schema(data)
    except ValueError as e:
        raise ProtocolError(str(e)[:200]) from e

    match data:
        case {
            "event": "loaded",
            "error": None | str() as error,
            "unavailable": dict() as unavailable,
        } if all(isinstance(reason, str) for reason in unavailable.values()):
            return Loaded(error, tuple(unavailable.items()))
        case {"event": "loading", "tests": list() as tests} if all(
            isinstance(test, str) for test in tests
        ):
            return Loading(tuple(tests))
        case {"event": "start", "test": str() as test}:
            return Start(test)
        case {"event": "result", "schema": _, **fields}:
            try:
                return Result(Outcome.from_dict(fields))
            except ValueError as e:
                raise ProtocolError(str(e)[:200]) from e

    raise ProtocolError(f"not an event: {_excerpt(line)}")


def _excerpt(line: str) -> str:
    """The start of `line`, short enough for an error message."""

    return repr(line[:200]) + ("..." if len(line) > 200 else "")

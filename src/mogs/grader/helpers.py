"""Hidden helper cells, run on first need, for the grading child and the build's check.

Works on Linux, macOS and Windows: the caller's `Load` decides how a cell runs, with a timer
or without.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from mogs.grader.bundle import HiddenHelper
from mogs.runner import FailureReason

Namespace: TypeAlias = dict[str, object]
# Run cell source, compiled under a label, into a namespace. Gives `None` on success, else
# the end of a message (after "the cell") and the reason.
Load: TypeAlias = Callable[[str, str, Namespace], tuple[str, FailureReason] | None]


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a hidden helper, or one it needs, did not load."""

    message: str
    reason: FailureReason


class Helpers:
    """Runs hidden helpers on first need, each at most once, and keeps what they define.

    A helper runs in a copy of the base namespace plus the names of the helpers it needs, which
    run first. A failure is kept too, so it reaches every cell that needs the helper and no
    other cell.
    """

    def __init__(self, helpers: tuple[HiddenHelper, ...], base: Namespace, load: Load) -> None:
        self._helpers = helpers
        self._base = base
        self._load = load
        self._done: dict[int, Namespace | Failure] = {}
        self._running: set[int] = set()

    def provide(self, needs: tuple[str, ...]) -> Namespace | Failure:
        """The values of `needs`, or the failure of a helper that defines one of them."""

        names: Namespace = {}

        for index, helper in enumerate(self._helpers):
            if not set(helper.defines) & set(needs):
                continue

            match self._run(index):
                case Failure() as failure:
                    return failure
                case defined:
                    names.update(defined)

        return names

    def _run(self, index: int) -> Namespace | Failure:
        if index in self._done:
            return self._done[index]

        helper = self._helpers[index]
        label = ", ".join(f"`{name}`" for name in helper.defines)

        if index in self._running:
            message = f"the hidden helper cell defining {label} needs itself through other cells"
            return Failure(message, FailureReason.LOAD_FAILED)

        self._running.add(index)

        match self.provide(helper.needs):
            case Failure() as failure:
                result: Namespace | Failure = failure
            case names:
                namespace = {**self._base, **names}
                match self._load(helper.source, "hidden-helper", namespace):
                    case (message, reason):
                        result = Failure(
                            f"the hidden helper cell defining {label} {message}", reason
                        )
                    case None:
                        result = {
                            name: namespace[name] for name in helper.defines if name in namespace
                        }

        self._running.discard(index)
        self._done[index] = result

        return result

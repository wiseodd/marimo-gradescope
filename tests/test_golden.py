"""Replay the golden corpus that `make golden` records from the test suite (tests/golden)."""

import hashlib
import json

import pytest
from golden.record import BLOBS, CASES, FUNCTIONS, PYFMT, VERSION, read_cases, replay

import mogs
from mogs import transform

MODULES = sorted([*FUNCTIONS, PYFMT])


@pytest.mark.parametrize("module", MODULES)
def test_python_replays_its_corpus(module: str) -> None:
    cases = read_cases(module)
    assert cases, f"no cases for {module}; run `make golden`"

    mismatches = []

    for case in cases:
        expected = {key: value for key, value in case.items() if key in ("ok", "error")}
        actual = replay(module, case)

        if json.dumps(actual) != json.dumps(expected):
            mismatches.append(f"{case['function']}({case['args']}): {actual} != {expected}")

    assert not mismatches, f"{len(mismatches)} of {len(cases)} cases differ:\n" + "\n".join(
        mismatches[:5]
    )


@pytest.mark.parametrize("module", sorted(FUNCTIONS))
def test_corpus_has_a_case_per_function(module: str) -> None:
    recorded = {case["function"] for case in read_cases(module)}
    listed = {fn.__name__ for fn in FUNCTIONS[module]}

    assert listed - recorded == set()


def test_blobs_are_named_by_their_hash() -> None:
    blobs = sorted(BLOBS.glob("*.txt"))
    assert blobs, "no blobs; run `make golden`"

    for blob in blobs:
        assert hashlib.sha256(blob.read_bytes()).hexdigest()[:16] == blob.stem


def test_corpus_holds_no_copy_of_the_running_version() -> None:
    files = [*CASES.glob("*.jsonl"), *BLOBS.glob("*.txt")]
    assert files, "no corpus; run `make golden`"

    holding = [path.name for path in files if mogs.__version__ in path.read_text("utf-8")]

    assert holding == []


# A version no recorded test uses; the suite itself pins `mogs==9.9.9` in places.
OTHER_VERSION = "42.0.1"


def _pins_this_package(case: dict) -> bool:
    match case:
        case {"function": "pinned_requirement"}:
            return True
        case {"function": "student_notebook", "args": {"tool_requirement": str() as pin}}:
            return pin == f"mogs=={VERSION}"

    return False


@pytest.mark.parametrize("function", ["pinned_requirement", "student_notebook"])
def test_replay_follows_the_running_version(function: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(
        case
        for case in read_cases("transform")
        if case["function"] == function and _pins_this_package(case)
    )
    expected = {key: value for key, value in case.items() if key in ("ok", "error")}
    assert OTHER_VERSION not in json.dumps(case)

    monkeypatch.setattr(mogs, "__version__", OTHER_VERSION)
    monkeypatch.setattr(transform, "__version__", OTHER_VERSION)

    assert json.dumps(replay("transform", case)) == json.dumps(expected)

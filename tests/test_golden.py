"""Replay the golden corpus that `make golden` records from the test suite (tests/golden)."""

import hashlib
import json

import pytest
from golden.record import BLOBS, FUNCTIONS, PYFMT, read_cases, replay

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

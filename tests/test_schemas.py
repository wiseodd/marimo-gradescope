import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from mogs.config import read_config
from mogs.grader.bundle import GradingBundle, Phase, bundle_from_notebook
from mogs.grader.protocol import Job, ProtocolError, decode
from mogs.grader.results import build_results, failure_results
from mogs.runner import FailureReason, Outcome
from mogs.source import parse_notebook

SCHEMAS = Path(__file__).parents[1] / "schemas"
SOLUTION = Path(__file__).parent / "fixtures" / "solution.py"


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate(instance: object, name: str) -> None:
    jsonschema.validate(instance, schema(name), cls=jsonschema.Draft202012Validator)


def solution_bundle() -> GradingBundle:
    src = SOLUTION.read_text(encoding="utf-8")
    return bundle_from_notebook(parse_notebook(src), read_config(src, SOLUTION))


def test_child_events_match_schema(tmp_path):
    submission = tmp_path / "submission.py"
    shutil.copy(SOLUTION, submission)
    events = tmp_path / "events.jsonl"
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}

    for phase in Phase:
        job = Job(solution_bundle().for_phase(phase), submission)

        with events.open("w") as sink:
            result = subprocess.run(
                [sys.executable, "-m", "mogs.grader.child", str(sink.fileno())],
                input=job.to_json(),
                pass_fds=(sink.fileno(),),
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp_path,
                timeout=120,
            )

        assert result.returncode == 0, result.stderr
        lines = events.read_text(encoding="utf-8").splitlines()
        kinds = {json.loads(line)["event"] for line in lines}

        assert kinds == {"loaded", "loading", "start", "result"}

        for line in lines:
            validate(json.loads(line), "event")


def test_job_matches_schema(tmp_path):
    job = Job(solution_bundle(), tmp_path / "s.py", ("test_q1_add",), run_failed="exit code 3")
    data = json.loads(job.to_json())

    validate(data, "job")
    assert list(data) == ["schema", "bundle", "submission", "skip", "run_failed", "alarm"]
    assert data["alarm"] is True


def test_bundle_json_matches_schema():
    bundle = solution_bundle()

    for part in [bundle, bundle.for_phase(Phase.PUBLIC), bundle.for_phase(Phase.HIDDEN)]:
        validate(json.loads(part.to_json()), "bundle")


def test_results_match_schema():
    bundle = solution_bundle()
    names = [name for cell in bundle.cells for name in cell.tests]
    outcomes = {name: Outcome(name, False, "boom", FailureReason.ASSERTION) for name in names}

    validate(build_results(bundle, outcomes, ["No PDF found."]), "results")
    validate(failure_results(bundle, RuntimeError()), "results")
    validate(failure_results(None, RuntimeError()), "results")


def other_version(text: str) -> str:
    return json.dumps({**json.loads(text), "schema": 2})


def test_decode_rejects_other_version(tmp_path):
    bundle = solution_bundle()
    job = Job(bundle, tmp_path / "s.py")
    event = '{"schema": 1, "event": "start", "test": "test_q1_add"}'

    with pytest.raises(ProtocolError, match="schema version 2, expected 1"):
        Job.from_json(other_version(job.to_json()))
    with pytest.raises(ProtocolError, match="schema version 2, expected 1"):
        decode(other_version(event))
    with pytest.raises(ValueError, match="schema version 2, expected 1"):
        GradingBundle.from_json(other_version(bundle.to_json()))
    with pytest.raises(ProtocolError, match="missing schema version, expected 1"):
        decode('{"event": "start", "test": "test_q1_add"}')

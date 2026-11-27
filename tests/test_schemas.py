import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from mogs._native import main
from mogs.config import read_config
from mogs.grader.bundle import GradingBundle, HiddenHelper, Phase, bundle_from_notebook
from mogs.grader.protocol import (
    Job,
    Loaded,
    Loading,
    ProtocolError,
    Result,
    Start,
    decode,
    encode,
)
from mogs.grader.wire import SCHEMA, check_schema
from mogs.runner import Outcome
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


def autograde(folder: Path, bundle: str, notebook: str, workdir: Path | None = None) -> object:
    """The `results.json` that `mogs autograde` writes for `notebook`, with no PDF."""

    source, submission, results = folder / "source", folder / "submission", folder / "results.json"
    source.mkdir(parents=True)
    submission.mkdir()
    (source / "bundle.json").write_text(bundle, encoding="utf-8")
    (submission / "solution.py").write_text(notebook, encoding="utf-8")
    argv = ["--source", str(source), "--submission", str(submission), "--results", str(results)]

    assert main(["autograde", *argv, "--workdir", str(workdir or folder / "work")]) == 0
    return json.loads(results.read_text(encoding="utf-8"))


@pytest.mark.skipif(sys.platform == "win32", reason="grading needs Linux or macOS")
def test_results_match_schema(tmp_path):
    bundle = solution_bundle().to_json()
    (tmp_path / "file").write_text("", encoding="utf-8")

    # Failed tests with a notice, a crash with a bundle, and one without.
    failed = autograde(tmp_path / "failed", bundle, "def broken(:\n")
    crashed = autograde(tmp_path / "crashed", bundle, "x = 1\n", tmp_path / "file" / "work")
    unreadable = autograde(tmp_path / "unreadable", "{not json", "x = 1\n")

    for results in (failed, crashed, unreadable):
        validate(results, "results")
    assert "No PDF" in json.dumps(failed)
    assert "The autograder failed (NotADirectoryError)" in json.dumps(crashed)
    assert "The autograder failed (JSONDecodeError)" in json.dumps(unreadable)


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


def test_check_schema_lives_in_wire():
    check_schema({"schema": SCHEMA})

    with pytest.raises(ValueError, match="schema version 2, expected 1"):
        check_schema({"schema": 2})


def with_extra_key(data: dict, path: tuple[str | int, ...]) -> dict:
    data = copy.deepcopy(data)
    target = data

    for step in path:
        target = target[step]

    target["bogus"] = 1
    return data


@pytest.mark.skipif(sys.platform == "win32", reason="grading needs Linux or macOS")
def test_schemas_reject_unknown_keys(tmp_path):
    bundle = solution_bundle()
    job = json.loads(Job(bundle, tmp_path / "s.py").to_json())
    solution = SOLUTION.read_text(encoding="utf-8")
    results = autograde(tmp_path / "graded", bundle.to_json(), solution)
    assert isinstance(results, dict)
    events = [
        {"schema": 1, "event": "loaded", "error": None, "unavailable": {}},
        {"schema": 1, "event": "loading", "tests": []},
        {"schema": 1, "event": "start", "test": "t"},
        {"schema": 1, "event": "result", **Outcome("t", True).to_dict()},
    ]
    cases = [
        *[(job, path, "job") for path in [(), ("bundle",), ("bundle", "config")]],
        (job, ("bundle", "config", "questions", 0), "job"),
        (job, ("bundle", "cells", 0), "job"),
        (json.loads(bundle.to_json()), (), "bundle"),
        *[(event, (), "event") for event in events],
        (results, (), "results"),
        (results, ("tests", 0), "results"),
    ]

    for data, path, name in cases:
        validate(data, name)

        with pytest.raises(jsonschema.ValidationError, match="bogus"):
            validate(with_extra_key(data, path), name)


def test_python_readers_reject_unknown_keys(tmp_path):
    helper = HiddenHelper("secret = 1\n", ("secret",), (), ())
    bundle = copy.replace(solution_bundle(), helpers=(helper,))
    job = json.loads(Job(bundle, tmp_path / "s.py").to_json())
    events = [
        json.loads(encode(event))
        for event in [Loaded(None), Loading(("t",)), Start("t"), Result(Outcome("t", True))]
    ]
    job_paths = [
        (),
        ("bundle",),
        ("bundle", "config"),
        ("bundle", "config", "questions", 0),
        ("bundle", "cells", 0),
        ("bundle", "helpers", 0),
    ]

    for path in job_paths:
        assert Job.from_json(json.dumps(job)) == Job(bundle, tmp_path / "s.py")

        with pytest.raises(ProtocolError, match="unknown field `bogus`"):
            Job.from_json(json.dumps(with_extra_key(job, path)))

        if path:
            with pytest.raises(ValueError, match="unknown field `bogus`"):
                GradingBundle.from_dict(with_extra_key(job, path)["bundle"])

    for event in events:
        assert decode(json.dumps(event)) is not None

        with pytest.raises(ProtocolError, match="unknown field `bogus`"):
            decode(json.dumps(with_extra_key(event, ())))


@pytest.mark.parametrize("key", ["score", "output", "output_format"])
def test_results_schema_rejects_null(key):
    with pytest.raises(jsonschema.ValidationError):
        validate({"tests": [], "stdout_visibility": "hidden", key: None}, "results")

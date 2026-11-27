import copy
import errno
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
from mogs.grader.wire import SCHEMA, check_schema
from mogs.runner import FailureReason, Outcome, exception_summary
from mogs.source import module_tests, parse_notebook

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


def test_schemas_reject_unknown_keys(tmp_path):
    bundle = solution_bundle()
    job = json.loads(Job(bundle, tmp_path / "s.py").to_json())
    passed = {name: Outcome(name, True) for cell in bundle.cells for name in cell.tests}
    results = dict(build_results(bundle, passed, []))
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


@pytest.mark.parametrize("key", ["score", "output", "output_format"])
def test_results_schema_rejects_null(key):
    with pytest.raises(jsonschema.ValidationError):
        validate({"tests": [], "stdout_visibility": "hidden", key: None}, "results")


# What `mogs inspect` writes for a test file it cannot read is the data of the exception, which
# each call site turns back into the exception Python raises. These check that the recipe in
# `crates/mogs-schema/src/inspect.rs` gives back the same text.

SYNTAX_CLASSES: dict[type[SyntaxError], str] = {
    SyntaxError: "syntax_error",
    IndentationError: "indentation_error",
    TabError: "tab_error",
}


def wire_error(exc: BaseException) -> dict:
    """The `TestFileError` a producer writes for `exc`."""

    match exc:
        case FileNotFoundError():
            return {"kind": "missing"}
        case OSError(errno=int() as number):
            return {"kind": "unreadable", "errno": number}
        case UnicodeDecodeError(start=start, end=end, object=data, reason=reason):
            data = list(data[start:end])
            reason = reason.replace(" ", "_")
            return {"kind": "not_utf8", "start": start, "bytes": data, "reason": reason}
        case SyntaxError(lineno=int() as line, msg=message):
            kind = SYNTAX_CLASSES[type(exc)]
            return {"kind": "syntax", "class": kind, "line": line, "message": message}

    raise AssertionError(f"no wire error for {exc!r}")


def rebuilt(error: dict, file: Path) -> BaseException:
    """The exception that `error` stands for, as the doc comments say to rebuild it."""

    match error:
        case {"kind": "missing"}:
            return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(file))
        case {"kind": "unreadable", "errno": int() as number}:
            return OSError(number, os.strerror(number), str(file))
        case {"kind": "not_utf8", "start": int() as start, "bytes": list() as data}:
            reason = error["reason"].replace("_", " ")
            padded = bytes(start) + bytes(data)
            return UnicodeDecodeError("utf-8", padded, start, start + len(data), reason)
        case {"kind": "syntax", "class": str() as name, "line": int() as line}:
            kind = {value: key for key, value in SYNTAX_CLASSES.items()}[name]
            return kind(error["message"], ("<unknown>", line, None, None))

    raise AssertionError(f"not a test file error: {error!r}")


def read_test_file(path: Path) -> BaseException:
    """The exception `selfcheck` and `check` get reading and parsing the test file at `path`."""

    try:
        module_tests(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return exc

    raise AssertionError(f"{path} reads fine")


def assert_rebuilds(path: Path) -> dict:
    real = read_test_file(path)
    error = wire_error(real)
    copy_of = rebuilt(error, path)
    definitions = schema("inspect")["$defs"]

    jsonschema.validate(
        error,
        {"$ref": "#/$defs/TestFileError", "$defs": definitions},
        cls=jsonschema.Draft202012Validator,
    )
    assert type(copy_of) is type(real)
    assert str(copy_of) == str(real)
    assert exception_summary(copy_of) == exception_summary(real)

    return error


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"abc\xff", "invalid_start_byte"),
        (b"abc\xe2(", "invalid_continuation_byte"),
        (b"abc\xe2\x82", "unexpected_end_of_data"),
        (b"abc\xf0\x9f(", "invalid_continuation_byte"),
    ],
)
def test_not_utf8_rebuilds_the_decode_error(tmp_path, data, reason):
    path = tmp_path / "q1.py"
    path.write_bytes(data)

    assert assert_rebuilds(path)["reason"] == reason


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("def test_q1_a(:\n", "syntax_error"),
        ("def test_q1_a():\nreturn 1\n", "indentation_error"),
        ("if True:\n\tx = 1\n        y = 2\n", "tab_error"),
    ],
)
def test_syntax_rebuilds_each_class(tmp_path, source, kind):
    path = tmp_path / "q1.py"
    path.write_text(source, encoding="utf-8")

    assert assert_rebuilds(path)["class"] == kind


def test_missing_rebuilds_the_os_error(tmp_path):
    assert assert_rebuilds(tmp_path / "q1.py") == {"kind": "missing"}


def test_unreadable_rebuilds_the_os_error(tmp_path):
    folder = tmp_path / "q1.py"
    folder.mkdir()

    assert assert_rebuilds(folder)["kind"] == "unreadable"

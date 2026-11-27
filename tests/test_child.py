import copy
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mogs.config import AssignmentConfig, Question, read_config
from mogs.grader.bundle import (
    GradingBundle,
    HiddenHelper,
    Phase,
    TestCell,
    bundle_from_notebook,
)
from mogs.grader.protocol import (
    Event,
    Job,
    Loaded,
    Loading,
    ProtocolError,
    Result,
    Start,
    decode,
    encode,
)
from mogs.runner import FailureReason, Outcome
from mogs.source import SourceError, parse_notebook

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"

PUBLIC_TESTS = ["test_q1_add", "test_q1_add_negative", "test_q2_count"]
HIDDEN_TESTS = ["test_q1_add_large", "test_q2_upper", "test_q2_empty"]


# A hidden cell without tests holds data that a hidden test cell uses.
HELPER_NOTEBOOK = textwrap.dedent(
    """\
    import marimo

    app = marimo.App()


    @app.function
    def add(a, b):
        return a + b


    @app.cell
    def _():
        def test_q1_public():
            assert add(1, 1) == 2

        return


    @app.cell
    def _():
        # HIDDEN
        secret_data = [(20, 22, 42), (-5, 5, 0)]
        secret_sum = add(40, 2)
        return secret_data, secret_sum


    @app.cell
    def _():
        # HIDDEN
        def test_q1_hidden():
            for a, b, total in secret_data:
                assert add(a, b) == total

        def test_q1_secret_sum():
            assert secret_sum == 42

        return
    """
)


def helper_bundle() -> GradingBundle:
    return bundle_from_notebook(parse_notebook(HELPER_NOTEBOOK), config())


def solution_bundle() -> GradingBundle:
    src = SOLUTION.read_text(encoding="utf-8")
    return bundle_from_notebook(parse_notebook(src), read_config(src, SOLUTION))


def config(timeout: float = 60) -> AssignmentConfig:
    return AssignmentConfig("A", "a.py", {"q1": Question("q1", "Question 1", 1)}, timeout=timeout)


def cell(source: str, tests: tuple[str, ...], uses: tuple[str, ...] = ()) -> TestCell:
    return TestCell("q1", "q1-tests", False, textwrap.dedent(source), tests, uses)


def submission(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "submission.py"
    header = "import marimo\n\napp = marimo.App()\n\n"
    path.write_text(header + textwrap.dedent(body), encoding="utf-8")
    return path


def solution_submission(tmp_path: Path) -> Path:
    path = tmp_path / "submission.py"
    shutil.copy(SOLUTION, path)
    return path


def run_child(tmp_path: Path, job: Job) -> list[Event]:
    """Run the child on `job`; events go to a file, so no pipe can fill up."""

    events = tmp_path / "events.jsonl"
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}

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
    return [decode(line) for line in events.read_text(encoding="utf-8").splitlines()]


def outcomes(events: list[Event]) -> list[Outcome]:
    return [event.outcome for event in events if isinstance(event, Result)]


def by_name(events: list[Event]) -> dict[str, Outcome]:
    return {outcome.name: outcome for outcome in outcomes(events)}


# Bundle


def test_bundle_cells_from_notebook():
    bundle = solution_bundle()

    assert [(c.qid, c.hidden, c.tests) for c in bundle.cells] == [
        ("q1", False, ("test_q1_add", "test_q1_add_negative")),
        ("q1", True, ("test_q1_add_large",)),
        ("q2", False, ("test_q2_count",)),
        ("q2", True, ("test_q2_upper", "test_q2_empty")),
    ]
    assert "import mogs" in bundle.setup_code
    assert bundle.cells[1].source.startswith("# HIDDEN\ndef big(n):")


def test_bundle_uses_are_free_student_names():
    bundle = solution_bundle()

    # `big` is defined in the hidden cell itself, so only `add` is needed.
    assert [c.uses for c in bundle.cells] == [
        ("add",),
        ("add",),
        ("count_vowels",),
        ("count_vowels",),
    ]


def test_bundle_uses_respect_local_scopes():
    src = textwrap.dedent(
        """\
        import marimo

        app = marimo.App()


        @app.function
        def add(a, b):
            return a + b


        @app.function
        def sub(a, b):
            return a - b


        @app.cell
        def _():
            def test_q1_local():
                add = lambda a, b: 0
                assert add(1, 2) == 0

            def test_q1_global():
                assert sub(2, 1) == 1

            return
        """
    )
    bundle = bundle_from_notebook(parse_notebook(src), config())

    assert bundle.cells[0].uses == ("sub",)


def test_bundle_rejects_cell_with_tests_of_two_questions():
    src = textwrap.dedent(
        """\
        import marimo

        app = marimo.App()


        @app.cell
        def _():
            def test_q1_a():
                pass

            def test_q2_a():
                pass

            return
        """
    )

    with pytest.raises(SourceError, match="test_q1_a.*test_q2_a"):
        bundle_from_notebook(parse_notebook(src), config())


def test_bundle_json_round_trip():
    bundle = solution_bundle()

    assert GradingBundle.from_json(bundle.to_json()) == bundle


def test_bundle_json_holds_no_host_paths(tmp_path):
    bundle = solution_bundle()
    cfg = copy.replace(bundle.config, requirements=tmp_path / "requirements.txt")
    text = copy.replace(bundle, config=cfg).to_json()

    assert str(tmp_path) not in text
    assert "requirements" not in json.loads(text)["config"]
    assert GradingBundle.from_json(text).config.requirements is None


def test_bundle_labels_each_cell_after_its_question():
    bundle = solution_bundle()

    assert [(c.qid, c.label) for c in bundle.cells] == [
        (c.qid, f"{c.qid}-tests") for c in bundle.cells
    ]
    assert {c.qid for c in bundle.cells} == {"q1", "q2"}
    assert json.loads(bundle.to_json())["cells"][0]["label"] == "q1-tests"


def test_for_phase_splits_cells():
    bundle = solution_bundle()

    public = bundle.for_phase(Phase.PUBLIC)
    hidden = bundle.for_phase(Phase.HIDDEN)

    assert [name for c in public.cells for name in c.tests] == PUBLIC_TESTS
    assert [name for c in hidden.cells for name in c.tests] == HIDDEN_TESTS
    assert public.config == hidden.config == bundle.config


@pytest.mark.parametrize(
    ("make", "needles"),
    [
        (solution_bundle, ["big(", "AEIOU", *HIDDEN_TESTS]),
        (helper_bundle, ["secret_data", "secret_sum", "test_q1_hidden", "test_q1_secret_sum"]),
    ],
)
def test_public_json_has_no_hidden_source(make, needles):
    bundle = make()
    text = bundle.for_phase(Phase.PUBLIC).to_json()

    hidden = [c.source for c in bundle.cells if c.hidden] + [h.source for h in bundle.helpers]
    assert hidden

    for source in hidden:
        assert source not in text
    for needle in ["# HIDDEN", *needles]:
        assert needle not in text


def test_bundle_keeps_hidden_helpers_for_the_hidden_phase():
    bundle = helper_bundle()

    assert bundle.helpers == (
        HiddenHelper(
            "# HIDDEN\nsecret_data = [(20, 22, 42), (-5, 5, 0)]\nsecret_sum = add(40, 2)\n",
            defines=("secret_data", "secret_sum"),
            uses=("add",),
            needs=(),
        ),
    )
    assert [c.needs for c in bundle.cells] == [(), ("secret_data", "secret_sum")]
    assert bundle.for_phase(Phase.HIDDEN).helpers == bundle.helpers
    assert bundle.for_phase(Phase.PUBLIC).helpers == ()
    assert GradingBundle.from_json(bundle.to_json()) == bundle


def test_bundle_uses_scopes_of_comprehensions_closures_and_classes():
    src = textwrap.dedent(
        """\
        import marimo

        app = marimo.App()


        @app.function
        def add(a, b):
            return a + b


        @app.function
        def sub(a, b):
            return a - b


        @app.function
        def mul(a, b):
            return a * b


        @app.cell
        def _():
            def test_q1_comprehension():
                assert [add for add in range(3)] == [0, 1, 2]

            def test_q1_closure():
                def inner():
                    return sub(2, 1)

                assert inner() == 1

            class Expected:
                value = mul(2, 3)

            def test_q1_class():
                assert Expected.value == 6

            return
        """
    )
    bundle = bundle_from_notebook(parse_notebook(src), config())

    assert bundle.cells[0].uses == ("mul", "sub")


# Serialization


def test_outcome_dict_round_trip():
    failed = Outcome("test_q1_a", False, "boom", FailureReason.TIMEOUT, 1.5)
    passed = Outcome("test_q1_b", True, seconds=0.25)

    assert failed.to_dict()["reason"] == "timeout"
    assert json.loads(json.dumps(failed.to_dict())) == failed.to_dict()
    assert Outcome.from_dict(failed.to_dict()) == failed
    assert Outcome.from_dict(passed.to_dict()) == passed


def test_outcome_from_bad_dict_raises():
    with pytest.raises(ValueError, match="outcome"):
        Outcome.from_dict({"name": "x", "passed": "yes"})


def test_event_encoding():
    outcome = Outcome("test_q1_a", False, "AssertionError", FailureReason.ASSERTION, 0.5)

    loaded = {"schema": 1, "event": "loaded", "error": None, "unavailable": {"x": "it raised"}}
    start = {"schema": 1, "event": "start", "test": "test_q1_a"}
    loading = {"schema": 1, "event": "loading", "tests": ["a", "b"]}
    result = {"schema": 1, "event": "result", **outcome.to_dict()}

    assert json.loads(encode(Loaded(None, {"x": "it raised"}))) == loaded
    assert json.loads(encode(Loaded(None)))["unavailable"] == {}
    assert json.loads(encode(Start("test_q1_a"))) == start
    assert json.loads(encode(Loading(("a", "b")))) == loading
    assert json.loads(encode(Result(outcome))) == result
    assert list(json.loads(encode(Start("t")))) == ["schema", "event", "test"]
    assert encode(Loaded("x")).endswith("\n")
    assert encode(Loaded("x")).count("\n") == 1

    events = [Loaded(None), Loaded("bad", {"x": "y"}), Loading(("t",)), Start("t"), Result(outcome)]

    for event in events:
        assert decode(encode(event)) == event


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[]",
        '{"event": "nope"}',
        '{"event": "start"}',
        '{"event": "loading", "tests": "t"}',
        '{"event": "loading", "tests": [1]}',
        "[" * 200_000 + "]" * 200_000,
        '{"schema": 1, "event": "result", "name": "t", "passed": true, "message": "", '
        '"reason": "x", "seconds": 0}',
        '{"schema": 1, "event": "loaded", "error": null}',
        '{"schema": 1, "event": "loaded", "error": null, "unavailable": {"x": 1}}',
        '{"schema": true, "event": "start", "test": "t"}',
    ],
)
def test_decode_rejects_bad_lines(line):
    with pytest.raises(ProtocolError):
        decode(line)


def test_job_json_round_trip(tmp_path):
    job = Job(solution_bundle(), tmp_path / "s.py", ("test_q1_add",))

    assert Job.from_json(job.to_json()) == job


def test_job_json_round_trip_with_run_failed(tmp_path):
    job = Job(solution_bundle(), tmp_path / "s.py", run_failed="exit code 3")

    assert Job.from_json(job.to_json()) == job


def test_job_alarm_defaults_to_true_and_round_trips(tmp_path):
    job = Job(solution_bundle(), tmp_path / "s.py")

    assert job.alarm
    assert Job.from_json(copy.replace(job, alarm=False).to_json()).alarm is False


def test_job_without_alarm_is_rejected(tmp_path):
    data = json.loads(Job(solution_bundle(), tmp_path / "s.py").to_json())
    del data["alarm"]

    with pytest.raises(ProtocolError, match="alarm"):
        Job.from_json(json.dumps(data))


# Child process


@pytest.mark.parametrize(
    ("phase", "names"), [(Phase.PUBLIC, PUBLIC_TESTS), (Phase.HIDDEN, HIDDEN_TESTS)]
)
def test_solution_passes_every_test(tmp_path, phase, names):
    job = Job(solution_bundle().for_phase(phase), solution_submission(tmp_path))
    events = run_child(tmp_path, job)

    assert [o.name for o in outcomes(events)] == names
    assert all(o.passed for o in outcomes(events)), outcomes(events)


def test_event_order(tmp_path):
    job = Job(solution_bundle().for_phase(Phase.PUBLIC), solution_submission(tmp_path))
    events = run_child(tmp_path, job)

    expected: list[object] = [
        Loaded(None),
        Loading(("test_q1_add", "test_q1_add_negative")),
        Start("test_q1_add"),
        "test_q1_add",
        Start("test_q1_add_negative"),
        "test_q1_add_negative",
        Loading(("test_q2_count",)),
        Start("test_q2_count"),
        "test_q2_count",
    ]

    shown = [e.outcome.name if isinstance(e, Result) else e for e in events]
    assert shown == expected


def test_skip_list_is_honored(tmp_path):
    job = Job(
        solution_bundle().for_phase(Phase.PUBLIC),
        solution_submission(tmp_path),
        skip=("test_q1_add", "test_q2_count"),
    )
    events = run_child(tmp_path, job)

    assert events == [
        Loaded(None),
        Loading(("test_q1_add_negative",)),
        Start("test_q1_add_negative"),
        events[3],
    ]
    assert [o.name for o in outcomes(events)] == ["test_q1_add_negative"]


def test_failures_are_reported(tmp_path):
    path = submission(
        tmp_path,
        """
        @app.function
        def add(a, b):
            return a - b
        """,
    )
    job = Job(solution_bundle().for_phase(Phase.PUBLIC), path)
    results = by_name(run_child(tmp_path, job))

    assert results["test_q1_add"].reason is FailureReason.ASSERTION
    assert "assert" in results["test_q1_add"].message

    # `count_vowels` is missing, so the test calls a placeholder that explains why.
    assert results["test_q2_count"].reason is FailureReason.ERROR
    assert "count_vowels" in results["test_q2_count"].message


def test_cell_that_fails_to_load_fails_only_its_tests(tmp_path):
    bundle = GradingBundle(
        config(),
        "",
        (
            cell("raise ValueError('boom')\ndef test_q1_a():\n    pass\n", ("test_q1_a",)),
            cell("def test_q1_b():\n    pass\n", ("test_q1_b",)),
            cell("def test_q1_c(:\n", ("test_q1_c",)),
        ),
    )
    events = run_child(tmp_path, Job(bundle, submission(tmp_path)))
    results = by_name(events)

    assert [type(e) for e in events] == [Loaded, *[Loading, Start, Result] * 3]
    assert results["test_q1_a"].reason is FailureReason.LOAD_FAILED
    assert "ValueError: boom" in results["test_q1_a"].message
    assert results["test_q1_b"].passed
    assert results["test_q1_c"].reason is FailureReason.LOAD_FAILED
    assert "SyntaxError" in results["test_q1_c"].message


def test_cell_compiles_under_its_label(tmp_path):
    broken = TestCell("q1", "tests/q1.py", False, "def test_q1_a(:\n", ("test_q1_a",), ())
    bundle = GradingBundle(config(), "", (broken,))
    results = by_name(run_child(tmp_path, Job(bundle, submission(tmp_path))))

    assert results["test_q1_a"].reason is FailureReason.LOAD_FAILED
    assert "q1.py" in results["test_q1_a"].message


def test_test_missing_from_its_cell_fails(tmp_path):
    bundle = GradingBundle(config(), "", (cell("x = 1\n", ("test_q1_a",)),))
    results = by_name(run_child(tmp_path, Job(bundle, submission(tmp_path))))

    assert results["test_q1_a"].reason is FailureReason.ERROR
    assert "test_q1_a" in results["test_q1_a"].message


def test_cells_get_separate_namespaces(tmp_path):
    bundle = GradingBundle(
        config(),
        "BASE = 10\n",
        (
            cell(
                "_helper = 1\ndef test_q1_a():\n    assert _helper + BASE == 11\n", ("test_q1_a",)
            ),
            cell(
                "_helper = 2\ndef test_q1_b():\n    assert _helper + BASE == 12\n", ("test_q1_b",)
            ),
            cell("def test_q1_c():\n    assert '_helper' not in globals()\n", ("test_q1_c",)),
        ),
    )
    results = by_name(run_child(tmp_path, Job(bundle, submission(tmp_path))))

    assert all(o.passed for o in results.values()), results


def test_setup_failure_fails_every_test(tmp_path):
    bundle = GradingBundle(
        config(),
        "import no_such_module_mgs\n",
        (cell("def test_q1_a():\n    pass\n", ("test_q1_a",)),),
    )
    events = run_child(tmp_path, Job(bundle, submission(tmp_path)))

    match events[0]:
        case Loaded(error=str() as error):
            assert "no_such_module_mgs" in error
        case other:
            pytest.fail(f"expected a load error, got {other!r}")

    [outcome] = outcomes(events)
    assert outcome.reason is FailureReason.LOAD_FAILED
    assert "no_such_module_mgs" in outcome.message


def test_notebook_that_fails_to_import_fails_every_test(tmp_path):
    bundle = GradingBundle(
        config(),
        "",
        (cell("def test_q1_a():\n    pass\n", ("test_q1_a",)),),
    )
    events = run_child(tmp_path, Job(bundle, submission(tmp_path, "def broken(:\n")))

    match events[0]:
        case Loaded(error=str() as error):
            assert "failed to import" in error
        case other:
            pytest.fail(f"expected a load error, got {other!r}")

    [outcome] = outcomes(events)
    assert outcome.reason is FailureReason.LOAD_FAILED
    assert "SyntaxError" in outcome.message


def test_timeout_fails_only_the_slow_test(tmp_path):
    bundle = GradingBundle(
        config(timeout=0.5),
        "",
        (
            cell(
                """\
                def test_q1_slow():
                    while True:
                        pass

                def test_q1_swallow():
                    try:
                        while True:
                            pass
                    except BaseException:
                        pass

                def test_q1_fast():
                    pass
                """,
                ("test_q1_slow", "test_q1_swallow", "test_q1_fast"),
            ),
        ),
    )
    results = by_name(run_child(tmp_path, Job(bundle, submission(tmp_path))))

    assert results["test_q1_slow"].reason is FailureReason.TIMEOUT
    assert results["test_q1_swallow"].reason is FailureReason.TIMEOUT
    assert "0.5" in results["test_q1_slow"].message
    assert results["test_q1_fast"].passed


def test_student_streams_are_not_the_event_channel(tmp_path):
    fake = json.dumps({"schema": 1, "event": "result", **Outcome("test_q1_a", True).to_dict()})
    path = submission(
        tmp_path,
        f"""
        @app.function
        def noisy():
            print({fake!r})
            return 1
        """,
    )
    bundle = GradingBundle(
        config(),
        "",
        (
            cell(
                """\
                import os, sys

                def test_q1_a():
                    assert noisy() == 2

                def test_q1_fd_not_inherited():
                    assert not os.get_inheritable(int(sys.argv[1]))

                def test_q1_stdin_closed():
                    assert sys.stdin.read() == ""
                """,
                ("test_q1_a", "test_q1_fd_not_inherited", "test_q1_stdin_closed"),
                ("noisy",),
            ),
        ),
    )
    results = by_name(run_child(tmp_path, Job(bundle, path)))

    assert not results["test_q1_a"].passed
    assert results["test_q1_fd_not_inherited"].passed, results["test_q1_fd_not_inherited"]
    assert results["test_q1_stdin_closed"].passed, results["test_q1_stdin_closed"]


def test_hidden_helpers_feed_hidden_tests(tmp_path):
    path = submission(
        tmp_path,
        """
        @app.function
        def add(a, b):
            return a + b
        """,
    )
    job = Job(helper_bundle().for_phase(Phase.HIDDEN), path)
    events = run_child(tmp_path, job)

    assert events[0] == Loaded(None)
    assert [o.name for o in outcomes(events)] == ["test_q1_hidden", "test_q1_secret_sum"]
    assert all(o.passed for o in outcomes(events)), outcomes(events)


@pytest.mark.parametrize("fd", ["0", "1", "2"])
def test_child_rejects_standard_descriptors(tmp_path, fd):
    job = Job(GradingBundle(config(), "", ()), submission(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "mogs.grader.child", fd],
        input=job.to_json(),
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"},
        timeout=60,
    )

    assert result.returncode != 0
    assert "EVENT_FD" in result.stderr
    assert result.stdout == ""


# Hidden helpers: lazy, run once, scoped to the cells that need them

TWO_QUESTIONS = AssignmentConfig(
    "A",
    "a.py",
    {"q1": Question("q1", "Question 1", 1), "q2": Question("q2", "Question 2", 1)},
    timeout=1,
)

STUDENT_CELLS = """\
import marimo

app = marimo.App()


@app.function
def add(a, b):
    return a + b


@app.function
def upper(s):
    return s.upper()

"""


def student(tmp_path: Path) -> Path:
    path = tmp_path / "submission.py"
    path.write_text(STUDENT_CELLS, encoding="utf-8")
    return path


def hidden_bundle(cells: str) -> GradingBundle:
    src = STUDENT_CELLS + textwrap.dedent(cells)
    return bundle_from_notebook(parse_notebook(src), TWO_QUESTIONS).for_phase(Phase.HIDDEN)


def hidden(body: str) -> str:
    """A `# HIDDEN` plain cell with `body`."""

    lines = textwrap.indent(textwrap.dedent(body).strip(), "    ")
    return f"\n@app.cell\ndef _():\n    # HIDDEN\n{lines}\n    return\n\n"


CROSS_QUESTION = (
    hidden("secret_sum = add(40, 2)")
    + hidden(
        """
    def test_q1_h():
        assert secret_sum == 42
    """
    )
    + hidden(
        """
    def test_q2_h():
        assert upper("a") == "A"
    """
    )
)


@pytest.mark.parametrize(
    ("add", "reason", "detail"),
    [
        ("", FailureReason.LOAD_FAILED, "`add` is unavailable"),
        (
            "@app.function\ndef add(a, b):\n    raise ValueError('todo')\n",
            FailureReason.LOAD_FAILED,
            "ValueError: todo",
        ),
        (
            "@app.function\ndef add(a, b):\n    while True:\n        pass\n",
            FailureReason.TIMEOUT,
            "longer than 1 s",
        ),
    ],
    ids=["missing", "raising", "looping"],
)
def test_broken_helper_fails_only_its_question(tmp_path, add, reason, detail):
    path = submission(tmp_path, add + "\n@app.function\ndef upper(s):\n    return s.upper()\n")
    results = by_name(run_child(tmp_path, Job(hidden_bundle(CROSS_QUESTION), path)))

    assert results["test_q2_h"].passed, results["test_q2_h"]

    q1 = results["test_q1_h"]
    assert q1.reason is reason
    assert "hidden helper cell defining `secret_sum`" in q1.message
    assert detail in q1.message


def test_helpers_run_in_dependency_order(tmp_path):
    cells = (
        hidden("doubled = [x * 2 for x in base_data]")
        + hidden(
            """
            def scaled(x):
                return x * factor
            """
        )
        + hidden(
            """
            def test_q1_h():
                assert doubled == [2, 4]
                assert scaled(2) == 6
            """
        )
        + hidden("base_data = [1, 2]")
        + hidden("factor = len(doubled) + 1")
    )
    bundle = hidden_bundle(cells)

    assert [h.needs for h in bundle.helpers] == [("base_data",), ("factor",), (), ("doubled",)]

    results = by_name(run_child(tmp_path, Job(bundle, student(tmp_path))))

    assert results["test_q1_h"].passed, results["test_q1_h"]


def test_failed_helper_fails_only_its_dependents(tmp_path):
    cells = (
        hidden("good = 1")
        + hidden("bad = 1 / 0")
        + hidden("worse = bad + 1")
        + hidden(
            """
            def test_q1_good():
                assert good == 1
            """
        )
        + hidden(
            """
            def test_q1_bad():
                assert worse == 2
            """
        )
        + hidden(
            """
            def test_q2_plain():
                assert upper("a") == "A"
            """
        )
    )
    results = by_name(run_child(tmp_path, Job(hidden_bundle(cells), student(tmp_path))))

    assert results["test_q1_good"].passed
    assert results["test_q2_plain"].passed

    bad = results["test_q1_bad"]
    assert bad.reason is FailureReason.LOAD_FAILED
    assert "hidden helper cell defining `bad`" in bad.message
    assert "ZeroDivisionError" in bad.message


def test_helper_runs_once_for_many_cells(tmp_path):
    cells = (
        hidden(
            """
            import pathlib

            with pathlib.Path("helper_runs.txt").open("a") as log:
                log.write("ran\\n")

            shared = [1, 2, 3]
            """
        )
        + hidden(
            """
            def test_q1_a():
                assert len(shared) == 3
            """
        )
        + hidden(
            """
            def test_q2_b():
                assert sum(shared) == 6
            """
        )
        + hidden(
            """
            def test_q2_c():
                assert upper("x") == "X"
            """
        )
    )
    results = by_name(run_child(tmp_path, Job(hidden_bundle(cells), student(tmp_path))))

    assert all(o.passed for o in results.values()), results
    assert (tmp_path / "helper_runs.txt").read_text(encoding="utf-8") == "ran\n"


def test_helper_needed_by_no_cell_never_runs(tmp_path):
    cells = hidden("unused = 1 / 0") + hidden(
        """
        def test_q1_a():
            assert add(1, 1) == 2
        """
    )
    results = by_name(run_child(tmp_path, Job(hidden_bundle(cells), student(tmp_path))))

    assert results["test_q1_a"].passed

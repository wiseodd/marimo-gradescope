import contextlib
import getpass
import json
import logging
import os
import pwd
import resource
import signal
import subprocess
import sys
import textwrap
import time
from copy import replace
from pathlib import Path

import pytest

from mogs.config import AssignmentConfig, Question
from mogs.grader import _process, sandbox
from mogs.grader.bundle import GradingBundle, HiddenHelper, Phase, TestCell
from mogs.grader.sandbox import grade, run_phase
from mogs.runner import FailureReason, Outcome

# The sandbox also runs on macOS, but these tests rely on Linux-only limits: the memory bomb
# fixture needs RLIMIT_AS, and the process cap and `resource` checks are Linux values.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux only: the memory-bomb fixture and the process limits need Linux rlimits",
)

FIXTURES = Path(__file__).parent / "fixtures"
OOM = "killed, probably out of memory: raise the Gradescope container memory"

GOOD = ["test_q1_before", "test_q1_after", "test_q2_other"]
ALL = ["test_q1_before", "test_q1_bad", "test_q1_after", "test_q2_other"]

Q1 = """\
def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    assert misbehave() is True

def test_q1_after():
    assert add(2, 2) == 4
"""

Q2 = """\
def test_q2_other():
    assert add(0, 0) == 0
"""


def config(timeout: float = 1, budget: float = 60, memory_mb: int = 1024) -> AssignmentConfig:
    questions = {qid: Question(qid, qid, 1) for qid in ["q1", "q2"]}
    return AssignmentConfig(
        "A", "a.py", questions, timeout=timeout, budget=budget, memory_mb=memory_mb
    )


def cell(qid: str, source: str, *, hidden: bool = False, uses=("add", "misbehave")) -> TestCell:
    source = textwrap.dedent(source)
    tests = tuple(
        line[4:].split("(")[0] for line in source.splitlines() if line.startswith("def test_")
    )
    return TestCell(qid, hidden, source, tests, tuple(uses))


def bundle(*cells: TestCell, **kwargs) -> GradingBundle:
    return GradingBundle(config(**kwargs), "", cells or (cell("q1", Q1), cell("q2", Q2)))


def run(
    tmp_path: Path, submission: Path, grading: GradingBundle, phase: Phase = Phase.PUBLIC
) -> dict[str, Outcome]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    deadline = time.monotonic() + grading.config.budget

    return run_phase(grading, phase, submission, workdir, user=None, deadline=deadline)


def fixture(name: str) -> Path:
    return FIXTURES / f"{name}.py"


def assert_only_bad_fails(results: dict[str, Outcome]) -> Outcome:
    assert list(results) == ALL

    for name in GOOD:
        assert results[name].passed, results[name]

    assert not results["test_q1_bad"].passed
    return results["test_q1_bad"]


def notebook(tmp_path: Path, body: str, name: str = "submission.py") -> Path:
    path = tmp_path / name
    header = "import marimo\n\napp = marimo.App()\n\n\n@app.function\ndef add(a, b):\n"
    path.write_text(header + "    return a + b\n\n" + textwrap.dedent(body), encoding="utf-8")
    return path


# Misbehaving submissions cost only their own test


@pytest.mark.parametrize(
    ("name", "reason", "detail"),
    [
        ("infinite_loop", FailureReason.TIMEOUT, "longer than 1 s"),
        ("c_loop", FailureReason.TIMEOUT, "longer than 1 s"),
        ("segfault", FailureReason.KILLED, "SIGSEGV"),
        ("exits", FailureReason.KILLED, "exited with code 3"),
        ("memory_bomb", FailureReason.OUT_OF_MEMORY, "MemoryError"),
    ],
)
def test_misbehaving_test_fails_alone(tmp_path, name, reason, detail):
    start = time.monotonic()
    bad = assert_only_bad_fails(run(tmp_path, fixture(name), bundle()))

    assert bad.reason is reason, bad
    assert detail in bad.message
    assert time.monotonic() - start < 15


def test_stdout_flood_finishes_and_keeps_only_the_tail(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="mogs.grader.sandbox")

    start = time.monotonic()
    results = run(tmp_path, fixture("stdout_flood"), bundle())

    assert time.monotonic() - start < 30
    assert assert_only_bad_fails(results).reason is FailureReason.ASSERTION

    [record] = caplog.records
    output = record.args[-1]
    assert output.endswith("x" * 1000 + "\n")
    assert len(output) == _process.OUTPUT_LIMIT


def test_daemonizer_does_not_hang(tmp_path):
    start = time.monotonic()

    try:
        results = run(tmp_path, fixture("daemonizer"), bundle())
    finally:
        pid_file = tmp_path / "work" / "grandchild.pid"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)

    assert time.monotonic() - start < 1 + 10
    assert assert_only_bad_fails(results).reason is FailureReason.ASSERTION


def test_printed_events_do_not_flip_results(tmp_path):
    bad = assert_only_bad_fails(run(tmp_path, fixture("forge_print"), bundle()))

    assert bad.reason is FailureReason.ASSERTION


def test_parent_keeps_first_result_and_ignores_unstarted_tests(tmp_path):
    source = """\
    import json, os, sys

    def send(name, passed):
        result = {"schema": 1, "event": "result", "name": name, "passed": passed}
        result |= {"message": "forged", "reason": None if passed else "error", "seconds": 0.0}
        os.write(int(sys.argv[1]), (json.dumps(result) + "\\n").encode())

    def test_q1_before():
        pass

    def test_q1_bad():
        send("test_q1_before", False)
        send("test_q1_after", False)
        send("test_q2_other", False)

    def test_q1_after():
        pass
    """
    grading = bundle(cell("q1", source, uses=()), cell("q2", Q2))
    results = run(tmp_path, fixture("infinite_loop"), grading)

    assert all(outcome.passed for outcome in results.values()), results


# Deadlines, crashes outside tests, and limits on restarts


def test_budget_exhaustion_marks_the_rest_budget(tmp_path):
    source = "import time\n\n" + "".join(
        f"def test_q1_s{i}():\n    time.sleep(0.6)\n\n" for i in range(8)
    )
    grading = bundle(cell("q1", source, uses=()), budget=2.5)

    start = time.monotonic()
    results = run(tmp_path, fixture("infinite_loop"), grading)

    assert time.monotonic() - start < 2.5 + 2
    assert list(results) == [f"test_q1_s{i}" for i in range(8)]
    assert results["test_q1_s0"].passed
    assert results["test_q1_s7"].reason is FailureReason.BUDGET

    reasons = [outcome.reason for outcome in results.values()]
    first = reasons.index(FailureReason.BUDGET)
    assert all(reason is None for reason in reasons[:first])
    assert all(reason is FailureReason.BUDGET for reason in reasons[first:])

    # The budget ran out during a test: that one was stopped, the rest never started.
    outcomes = list(results.values())
    assert outcomes[first].message.startswith("stopped:"), outcomes[first]
    assert all(o.message.startswith("not run:") for o in outcomes[first + 1 :])


@pytest.mark.parametrize(
    ("name", "reason", "detail"),
    [
        ("segfault", FailureReason.KILLED, "SIGSEGV"),
        ("c_loop", FailureReason.TIMEOUT, "longer than 1 s"),
    ],
)
def test_crash_while_loading_a_cell_fails_its_tests(tmp_path, name, reason, detail):
    source = "_value = misbehave()\n\n" + Q1
    results = run(tmp_path, fixture(name), bundle(cell("q1", source), cell("q2", Q2)))

    assert results["test_q2_other"].passed, results["test_q2_other"]

    for test in ["test_q1_before", "test_q1_bad", "test_q1_after"]:
        assert results[test].reason is reason, results[test]
        assert "loading the test cell" in results[test].message
        assert detail in results[test].message


NEEDS_RUN = """\
def test_q2_other():
    assert nothing_here() is None
"""


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("import ctypes\n\nctypes.string_at(0)", "SIGSEGV"),
        ("import os\n\nos._exit(3)", "exited with code 3"),
        ("while True:\n    pass", "longer than 1 s"),
    ],
)
def test_failed_notebook_run_costs_only_the_names_that_need_it(tmp_path, body, detail):
    path = notebook(
        tmp_path,
        "@app.cell\ndef _():\n" + textwrap.indent(body, "    ") + "\n    return\n",
    )
    grading = bundle(
        cell("q1", Q1, uses=("add", "nothing_here")), cell("q2", NEEDS_RUN, uses=("nothing_here",))
    )

    start = time.monotonic()
    results = run(tmp_path, path, grading)

    assert time.monotonic() - start < 20
    assert list(results) == ALL
    assert results["test_q1_before"].passed, results["test_q1_before"]
    assert results["test_q1_after"].passed, results["test_q1_after"]
    assert not results["test_q1_bad"].passed

    other = results["test_q2_other"]
    assert not other.passed
    assert "`nothing_here` is unavailable" in other.message
    assert "needs the whole notebook to run, which failed" in other.message
    assert detail in other.message


def test_crash_while_importing_fails_everything_after_one_retry(tmp_path):
    path = tmp_path / "submission.py"
    path.write_text(
        "import marimo\n\napp = marimo.App()\n\nwith app.setup:\n"
        "    import ctypes\n\n    ctypes.string_at(0)\n",
        encoding="utf-8",
    )

    start = time.monotonic()
    results = run(tmp_path, path, bundle())

    assert time.monotonic() - start < 10
    assert list(results) == ALL

    for outcome in results.values():
        assert outcome.reason is FailureReason.LOAD_FAILED, outcome
        assert "loading your notebook" in outcome.message
        assert "SIGSEGV" in outcome.message


def test_slow_check_cell_does_not_stall_the_notebook_run(tmp_path):
    path = tmp_path / "submission.py"
    path.write_text(
        textwrap.dedent(
            """\
            import marimo

            app = marimo.App()

            with app.setup:
                import time

                import mogs


            @app.cell
            def _():
                factor = 2
                return (factor,)


            @app.cell
            def double(factor):
                def double(x):
                    return factor * x

                return (double,)


            @app.cell
            def _():
                def slow():
                    time.sleep(30)

                mogs.grade("q1", tests=[slow])
                return
            """
        ),
        encoding="utf-8",
    )
    source = "def test_q1_double():\n    assert double(2) == 4\n"
    grading = bundle(cell("q1", source, uses=("double",)))

    start = time.monotonic()
    results = run(tmp_path, path, grading)

    assert time.monotonic() - start < 15
    assert results["test_q1_double"].passed, results["test_q1_double"]


def test_failed_notebook_run_is_not_retried_in_the_hidden_phase(tmp_path, monkeypatch):
    jobs = []
    real = sandbox.Child

    def recording(job, options):
        jobs.append(json.loads(job))
        return real(job, options)

    monkeypatch.setattr(sandbox, "Child", recording)
    path = notebook(tmp_path, "@app.cell\ndef _():\n    import os\n\n    os._exit(3)\n")
    grading = bundle(
        cell("q1", Q1, uses=("add", "nothing_here")),
        cell("q2", NEEDS_RUN, hidden=True, uses=("nothing_here",)),
    )
    root = tmp_path / "root"
    root.mkdir()

    results = grade(grading, path, root, user=None).outcomes

    assert results["test_q1_before"].passed
    assert "exited with code 3" in results["test_q2_other"].message
    assert [job["run_failed"] for job in jobs] == [None, "exited with code 3", "exited with code 3"]


def test_respawns_are_capped(tmp_path):
    source = "".join(f"def test_q1_c{i}():\n    misbehave()\n\n" for i in range(6))
    results = run(tmp_path, fixture("segfault"), bundle(cell("q1", source)))

    reasons = [outcome.reason for outcome in results.values()]
    assert reasons == [FailureReason.KILLED] * 4 + [FailureReason.NOT_RUN] * 2


# Two phases


def test_grade_runs_both_phases_in_fresh_workdirs(tmp_path):
    source = tmp_path / "source"
    (source / "data").mkdir(parents=True)
    (source / "data" / "x.txt").write_text("42", encoding="utf-8")

    path = notebook(tmp_path, "")
    public = """\
    from pathlib import Path

    def test_q1_public():
        Path("public_was_here").touch()
        assert Path("data/x.txt").read_text() == "42"
        assert Path("submission.py").exists()
    """
    hidden = """\
    # HIDDEN
    from pathlib import Path

    def test_q2_hidden():
        assert not Path("public_was_here").exists()
        assert Path("data/x.txt").read_text() == "42"
    """
    grading = GradingBundle(
        config(),
        "",
        (cell("q1", public, uses=()), cell("q2", hidden, hidden=True, uses=())),
    )

    root = tmp_path / "root"
    root.mkdir()
    results = grade(
        grading, path, root, user=None, files_root=source, files=["data/x.txt"]
    ).outcomes

    assert list(results) == ["test_q1_public", "test_q2_hidden"]
    assert all(outcome.passed for outcome in results.values()), results
    assert (root / "public" / "public_was_here").exists()
    assert not (root / "hidden" / "public_was_here").exists()


def test_grade_reports_why_the_notebook_did_not_load(tmp_path):
    path = tmp_path / "submission.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()

    graded = grade(bundle(), path, root, user=None)

    assert graded.load_error is not None
    assert "failed to import" in graded.load_error
    assert all(o.reason is FailureReason.LOAD_FAILED for o in graded.outcomes.values())


def test_failing_hidden_helper_is_not_a_notebook_load_failure(tmp_path):
    helper = HiddenHelper("EXPECTED = missing()\n", ("EXPECTED",), ("missing",), ())
    hidden = cell("q1", "# HIDDEN\ndef test_q1_hidden():\n    assert EXPECTED\n", hidden=True)
    grading = GradingBundle(config(), "", (replace(hidden, needs=("EXPECTED",)),), (helper,))
    root = tmp_path / "root"
    root.mkdir()

    graded = grade(grading, notebook(tmp_path, ""), root, user=None)

    assert graded.outcomes["test_q1_hidden"].reason is FailureReason.LOAD_FAILED
    assert graded.load_error is None


def test_grade_leaves_junk_out_of_data_folders(tmp_path):
    source = tmp_path / "source"
    for name in ("data/x.txt", "data/__pycache__/m.pyc", "data/__marimo__/c", "data/.DS_Store"):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("42", encoding="utf-8")

    test = """\
    from pathlib import Path

    def test_q1_tree():
        found = sorted(str(p) for p in Path("data").rglob("*") if p.is_file())
        assert found == ["data/x.txt"], found
    """
    grading = GradingBundle(config(), "", (cell("q1", test, uses=()),))
    root = tmp_path / "root"
    root.mkdir()

    results = grade(
        grading, notebook(tmp_path, ""), root, user=None, files_root=source, files=["data"]
    ).outcomes

    assert results["test_q1_tree"].passed, results


def test_grade_rejects_a_missing_data_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="nope.csv"):
        grade(
            bundle(),
            notebook(tmp_path, ""),
            root,
            user=None,
            files_root=tmp_path,
            files=["nope.csv"],
        )


@pytest.mark.parametrize("name", ["/etc/passwd", "../outside.txt", "data/../../x.txt"])
def test_grade_rejects_data_paths_outside_the_workdir(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="data file"):
        grade(bundle(), notebook(tmp_path, ""), root, user=None, files_root=tmp_path, files=[name])


def test_grade_rejects_a_symlinked_submission(tmp_path):
    link = tmp_path / "link.py"
    link.symlink_to(notebook(tmp_path, ""))
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        grade(bundle(), link, root, user=None)


def test_grade_shares_the_budget(tmp_path):
    slow = "import time\n\ndef test_{qid}_slow():\n    time.sleep({seconds})\n"
    grading = GradingBundle(
        config(timeout=10, budget=3),
        "",
        (
            cell("q1", slow.format(qid="q1", seconds=0.2), uses=()),
            cell("q2", "# HIDDEN\n" + slow.format(qid="q2", seconds=5), hidden=True, uses=()),
        ),
    )
    root = tmp_path / "root"
    root.mkdir()

    results = grade(grading, notebook(tmp_path, ""), root, user=None).outcomes

    assert results["test_q1_slow"].passed
    assert results["test_q2_slow"].reason is FailureReason.BUDGET


# Process setup


def test_exit_status_messages():
    assert _process.describe_exit(-signal.SIGKILL) == OOM
    assert _process.describe_exit(-signal.SIGSEGV) == "killed by signal SIGSEGV"
    assert _process.describe_exit(3) == "exited with code 3"
    assert _process.describe_exit(0) == "exited with code 0"


def test_limits():
    memory = 512 * 1024 * 1024

    assert _process.limits(512, None) == [(resource.RLIMIT_AS, memory)]
    assert _process.limits(512, "grader") == [
        (resource.RLIMIT_AS, memory),
        (resource.RLIMIT_NPROC, 512),
    ]


def test_limits_on_macos_have_no_memory_cap(monkeypatch):
    # macOS rejects RLIMIT_AS in setrlimit.
    monkeypatch.setattr(_process.sys, "platform", "darwin")

    assert _process.limits(512, None) == []
    assert _process.limits(512, "grader") == [(resource.RLIMIT_NPROC, 512)]


def test_child_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("MOGS_KEPT", "1")

    env = _process.child_env(tmp_path)

    assert "PYTEST_CURRENT_TEST" not in env
    assert env["MOGS_KEPT"] == "1"
    assert env["MPLBACKEND"] == "Agg"
    assert env["OMP_NUM_THREADS"] == "4"
    assert env["HOME"] == env["MPLCONFIGDIR"] == str(tmp_path)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["MOGS_GRADING"] == "1"


def test_launch_without_user(tmp_path):
    options = _process.launch_options(tmp_path, None, 1024, "/env/bin/python")

    assert options.cwd == tmp_path
    assert options.python == "/env/bin/python"
    assert options.env == _process.child_env(tmp_path)
    assert options.user is options.group is options.extra_groups is None
    assert options.limits == _process.limits(1024, None)
    assert _process.child_command(7, "/env/bin/python") == [
        "/env/bin/python",
        "-m",
        "mogs.grader.child",
        "7",
    ]


def other_user() -> str:
    """An existing user that is neither root nor the current user."""

    for entry in pwd.getpwall():
        if entry.pw_uid not in (0, os.getuid()):
            return entry.pw_name

    pytest.skip("no other user on this machine")


def test_launch_with_user(tmp_path):
    user = other_user()
    options = _process.launch_options(tmp_path, user, 1024, sys.executable)

    assert options.user == user
    assert options.group == pwd.getpwnam(user).pw_gid
    assert options.extra_groups == []
    assert options.limits == _process.limits(1024, user)


@pytest.mark.parametrize(
    ("user", "match"),
    [(pwd.getpwuid(0).pw_name, "root"), (getpass.getuser(), "user running the grader")],
)
def test_root_and_own_user_are_refused(tmp_path, user, match):
    if os.getuid() == 0 and match != "root":
        pytest.skip("running as root")

    with pytest.raises(ValueError, match=match):
        _process.launch_options(tmp_path, user, 1024, sys.executable)

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match=match):
        grade(bundle(), notebook(tmp_path, ""), root, user=user)

    assert list(root.iterdir()) == []


def test_kill_user_repeats_until_no_process_is_left(monkeypatch):
    calls = []
    left = iter([0, 0, 1])

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, next(left) if argv[0] == "pgrep" else 0)

    monkeypatch.setattr(_process.subprocess, "run", run)

    _process.kill_user("grader")

    assert calls == [["pkill", "-KILL", "-u", "grader"], ["pgrep", "-u", "grader"]] * 3


def test_kill_user_gives_up_after_a_few_passes(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_process.subprocess, "run", run)

    _process.kill_user("grader")

    assert len(calls) == 2 * _process.KILL_PASSES


def test_limits_never_exceed_the_hard_limit():
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

    assert _process.clamped(resource.RLIMIT_NOFILE, hard + 1) == (hard, hard)
    assert _process.clamped(resource.RLIMIT_NOFILE, 16) == (16, 16)


# Hostile event lines


def test_events_before_a_bad_line_still_count(tmp_path):
    source = """\
import os, sys
from mogs.grader.protocol import Result, encode
from mogs.runner import Outcome

def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    forged = encode(Result(Outcome("test_q1_bad", False, "forged")))
    os.write(int(sys.argv[1]), (forged + "\\nnot json\\n").encode())
    import time
    time.sleep(5)

def test_q1_after():
    assert add(2, 2) == 4
"""
    results = run(tmp_path, fixture("infinite_loop"), bundle(cell("q1", source), cell("q2", Q2)))

    bad = assert_only_bad_fails(results)
    assert bad.message == "forged"


@pytest.mark.parametrize(
    "line", ["not json", "[" * 200_000 + "]" * 200_000], ids=["malformed", "nested"]
)
def test_bad_event_line_fails_only_the_writer(tmp_path, line):
    source = f"""\
import os, sys

def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    os.write(int(sys.argv[1]), ({line!r} + "\\n").encode())
    import time
    time.sleep(5)

def test_q1_after():
    assert add(2, 2) == 4
"""
    results = run(tmp_path, fixture("infinite_loop"), bundle(cell("q1", source), cell("q2", Q2)))

    bad = assert_only_bad_fails(results)
    assert "malformed event" in bad.message


# Killing the process group


def child_of(process: subprocess.Popen[bytes]) -> _process.Child:
    """A `Child` around `process`, without the grading setup."""

    child = object.__new__(_process.Child)
    child._process = process
    return child


def refuse(pid: int, sig: int) -> None:
    raise PermissionError(1, "Operation not permitted")


def test_kill_group_ignores_eperm_once_the_child_has_ended(tmp_path, monkeypatch):
    # macOS answers EPERM for a group whose only member is the unreaped (zombie) child.
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    child = child_of(process)
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    monkeypatch.setattr(_process.os, "killpg", refuse)

    try:
        child._kill_group()
    finally:
        process.wait()


def test_kill_group_raises_eperm_while_the_child_runs(tmp_path, monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    child = child_of(process)
    monkeypatch.setattr(_process.os, "killpg", refuse)

    try:
        with pytest.raises(PermissionError):
            child._kill_group()
    finally:
        process.kill()
        process.wait()

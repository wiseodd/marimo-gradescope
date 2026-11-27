import contextlib
import getpass
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import textwrap
import threading
import time
from copy import replace
from pathlib import Path

import pytest

from mogs._native import grade
from mogs.config import AssignmentConfig, Question
from mogs.grader.bundle import GradingBundle, HiddenHelper, TestCell
from mogs.runner import FailureReason, Outcome

# The sandbox also runs on macOS, but these tests rely on Linux-only limits: the memory bomb
# fixture needs RLIMIT_AS.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux only: the memory-bomb fixture and the process limits need Linux rlimits",
)

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUT_LIMIT = 64 * 1024

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
    return TestCell(qid, f"{qid}-tests", hidden, source, tests, tuple(uses))


def bundle(*cells: TestCell, **kwargs) -> GradingBundle:
    return GradingBundle(config(**kwargs), "", cells or (cell("q1", Q1), cell("q2", Q2)))


def run(tmp_path: Path, submission: Path, grading: GradingBundle) -> dict[str, Outcome]:
    """Grade with the public cells of `grading`, as `tmp_path/work/public`."""

    workdir = tmp_path / "work"
    workdir.mkdir()

    return grade(grading, submission, workdir, user=None).outcomes


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
    assert len(output) == OUTPUT_LIMIT


def test_daemonizer_does_not_hang(tmp_path):
    start = time.monotonic()

    try:
        results = run(tmp_path, fixture("daemonizer"), bundle())
    finally:
        pid_file = tmp_path / "work" / "public" / "grandchild.pid"
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


def recording_python(tmp_path: Path) -> tuple[Path, Path]:
    """An interpreter that keeps each job it reads in `jobs/`, then runs as `sys.executable`."""

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    script = tmp_path / "python"
    script.write_text(
        "#!/bin/sh\n"
        f'job="{jobs}/$(ls {jobs} | wc -l).json"\n'
        'cat > "$job"\n'
        f'exec {sys.executable} "$@" < "$job"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)

    return script, jobs


def test_failed_notebook_run_is_not_retried_in_the_hidden_phase(tmp_path):
    python, jobs = recording_python(tmp_path)
    path = notebook(tmp_path, "@app.cell\ndef _():\n    import os\n\n    os._exit(3)\n")
    grading = bundle(
        cell("q1", Q1, uses=("add", "nothing_here")),
        cell("q2", NEEDS_RUN, hidden=True, uses=("nothing_here",)),
    )
    root = tmp_path / "root"
    root.mkdir()

    results = grade(grading, path, root, user=None, python=str(python)).outcomes

    assert results["test_q1_before"].passed
    assert "exited with code 3" in results["test_q2_other"].message
    found = [json.loads((jobs / f"{i}.json").read_text()) for i in range(len(list(jobs.iterdir())))]
    assert [job["run_failed"] for job in found] == [
        None,
        "exited with code 3",
        "exited with code 3",
    ]


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


def other_user() -> str:
    """An existing user that is neither root nor the current user."""

    for entry in pwd.getpwall():
        if entry.pw_uid not in (0, os.getuid()):
            return entry.pw_name

    pytest.skip("no other user on this machine")


@pytest.mark.parametrize(
    ("user", "match"),
    [(pwd.getpwuid(0).pw_name, "root"), (getpass.getuser(), "user running the grader")],
)
def test_root_and_own_user_are_refused(tmp_path, user, match):
    if os.getuid() == 0 and match != "root":
        pytest.skip("running as root")

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match=match):
        grade(bundle(), notebook(tmp_path, ""), root, user=user)

    assert list(root.iterdir()) == []


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
    "data",
    [
        'b"not json\\n"',
        'b"[" * 200_000 + b"]" * 200_000 + b"\\n"',
        'b"\\xff\\xfe not utf-8\\n"',
        'b"x" * (2 * 1024 * 1024) + b"\\n"',
        'b"x" * (2 * 1024 * 1024)',
    ],
    ids=["malformed", "nested", "not-utf-8", "2-mib-line", "2-mib-unended"],
)
def test_bad_event_line_fails_only_the_writer(tmp_path, data):
    source = f"""\
import os, sys

def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    os.write(int(sys.argv[1]), {data})
    import time
    time.sleep(5)

def test_q1_after():
    assert add(2, 2) == 4
"""
    results = run(tmp_path, fixture("infinite_loop"), bundle(cell("q1", source), cell("q2", Q2)))

    bad = assert_only_bad_fails(results)
    assert "malformed event" in bad.message


# Hostile children


def hostile(body: str) -> TestCell:
    """Q1 with `body` as the test `test_q1_bad`."""

    source = Q1.replace("    assert misbehave() is True\n", textwrap.indent(body, "    "))
    return cell("q1", source)


def test_output_that_is_not_utf8_is_kept(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="mogs.grader.sandbox")
    body = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe tail')\n"
        "sys.stdout.flush()\n"
        "assert False\n"
    )

    results = run(tmp_path, fixture("infinite_loop"), bundle(hostile(body), cell("q2", Q2)))

    assert assert_only_bad_fails(results).reason is FailureReason.ASSERTION
    [record] = caplog.records
    assert "\ufffd\ufffd tail" in record.args[-1]


def test_child_that_closes_its_event_channel_fails_alone(tmp_path):
    body = "import os, sys\nos.close(int(sys.argv[1]))\n"

    results = run(tmp_path, fixture("infinite_loop"), bundle(hostile(body), cell("q2", Q2)))

    assert assert_only_bad_fails(results).reason is FailureReason.KILLED


def test_child_that_ignores_sigterm_is_killed(tmp_path):
    body = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    pass\n"

    start = time.monotonic()
    results = run(tmp_path, fixture("infinite_loop"), bundle(hostile(body), cell("q2", Q2)))

    assert time.monotonic() - start < 15
    assert assert_only_bad_fails(results).reason is FailureReason.TIMEOUT


def test_processes_a_test_forks_are_killed_with_it(tmp_path):
    # A bounded fork storm: without a grading user there is no process cap.
    body = textwrap.dedent(
        """\
        import os, time
        pids = []
        for _ in range(100):
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            pids.append(pid)
        with open("forked.txt", "w") as f:
            f.write(" ".join(map(str, pids)))
        while True:
            pass
        """
    )

    results = run(tmp_path, fixture("infinite_loop"), bundle(hostile(body), cell("q2", Q2)))

    assert assert_only_bad_fails(results).reason is FailureReason.TIMEOUT
    pids = [int(pid) for pid in (tmp_path / "work" / "public" / "forked.txt").read_text().split()]
    assert len(pids) == 100

    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# Ctrl+C and the GIL


def test_ctrl_c_stops_grading_and_kills_the_child(tmp_path):
    source = textwrap.dedent(
        """\
        import os, time
        from pathlib import Path

        def test_q1_wait():
            Path("pid").write_text(str(os.getpid()))
            time.sleep(60)
        """
    )
    grading = GradingBundle(config(timeout=60), "", (cell("q1", source, uses=()),))
    (tmp_path / "bundle.json").write_text(grading.to_json(), encoding="utf-8")
    (tmp_path / "work").mkdir()
    submission = notebook(tmp_path, "")
    script = textwrap.dedent(
        f"""\
        from pathlib import Path
        from mogs._native import grade
        from mogs.grader.bundle import GradingBundle

        bundle = GradingBundle.from_json(Path("bundle.json").read_text())
        grade(bundle, Path({str(submission)!r}), Path.cwd() / "work", user=None)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stderr=subprocess.PIPE, text=True, cwd=tmp_path
    )
    pid_file = tmp_path / "work" / "public" / "pid"

    try:
        for _ in range(300):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)

        child = int(pid_file.read_text())
        start = time.monotonic()
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=10)
    finally:
        process.kill()

    assert time.monotonic() - start < 2
    assert "KeyboardInterrupt" in stderr
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_grading_does_not_hold_the_gil(tmp_path):
    body = textwrap.dedent(
        """\
        import sys, time
        end = time.monotonic() + 3
        while time.monotonic() < end:
            sys.stdout.write("x" * 1000 + "\\n")
        """
    )
    grading = bundle(hostile(body), cell("q2", Q2), timeout=10)
    finished = []
    thread = threading.Thread(
        target=lambda: finished.append(run(tmp_path, fixture("infinite_loop"), grading))
    )
    gaps = []

    thread.start()
    last = time.monotonic()

    while thread.is_alive():
        time.sleep(0.01)
        now = time.monotonic()
        gaps.append(now - last)
        last = now

    thread.join()

    assert all(outcome.passed for outcome in finished[0].values()), finished
    assert sum(gaps) > 2.5
    assert max(gaps) < 0.5


# Python values and errors from Rust


def test_grade_returns_outcomes_and_load_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    graded = grade(bundle(), notebook(tmp_path, ""), root, user=None)

    assert graded.load_error is None
    assert list(graded.outcomes) == ALL
    assert all(type(outcome) is Outcome for outcome in graded.outcomes.values())
    before = graded.outcomes["test_q1_before"]
    assert before == Outcome("test_q1_before", True, seconds=before.seconds)
    assert type(graded.outcomes["test_q1_bad"].reason) is FailureReason


def test_grade_errors_keep_their_python_class_and_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "public").mkdir()
    missing = tmp_path / "missing.py"

    with pytest.raises(FileNotFoundError) as error:
        grade(bundle(), missing, root, user=None)
    assert str(error.value) == f"[Errno 2] No such file or directory: {str(missing)!r}"

    with pytest.raises(FileExistsError) as error:
        grade(bundle(), notebook(tmp_path, ""), root, user=None)
    assert str(error.value) == f"[Errno 17] File exists: {str(root / 'public')!r}"

    with pytest.raises(KeyError, match="no-such-mogs-user"):
        grade(bundle(), notebook(tmp_path, ""), root, user="no-such-mogs-user")

    with pytest.raises(ValueError, match="^data files need a files_root$"):
        grade(bundle(), notebook(tmp_path, ""), root, user=None, files=["x"])

import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mogs._native import main
from mogs.config import AssignmentConfig, Question, read_config
from mogs.grader.bundle import GradingBundle, TestCell, bundle_from_notebook
from mogs.source import parse_notebook

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="grading needs Linux or macOS")

SOLUTION = Path(__file__).parent / "fixtures" / "solution.py"
HIDDEN = ["test_q1_add_large", "test_q2_upper", "test_q2_empty"]


class Paths:
    def __init__(self, tmp_path: Path) -> None:
        self.source = tmp_path / "source"
        self.submission = tmp_path / "submission"
        self.results = tmp_path / "results" / "results.json"
        self.workdir = tmp_path / "work"

        self.source.mkdir()
        self.submission.mkdir()

        src = SOLUTION.read_text(encoding="utf-8")
        bundle = bundle_from_notebook(parse_notebook(src), read_config(src, SOLUTION))
        (self.source / "bundle.json").write_text(bundle.to_json(), encoding="utf-8")

    def argv(self) -> list[str]:
        return [
            "--source",
            str(self.source),
            "--submission",
            str(self.submission),
            "--results",
            str(self.results),
            "--workdir",
            str(self.workdir),
        ]

    def add(self, name: str, content: str | None = None) -> None:
        path = self.submission / name
        path.parent.mkdir(parents=True, exist_ok=True)

        if content is None:
            shutil.copy(SOLUTION, path)
        else:
            path.write_text(content, encoding="utf-8")

    def run(self) -> dict:
        assert main(["autograde", *self.argv()]) == 0
        return json.loads(self.results.read_text(encoding="utf-8"))


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path)


def rows(results: dict) -> dict[str, dict]:
    return {row.get("number", row["name"]): row for row in results["tests"]}


def test_nested_upload_with_macosx_junk_gets_full_marks(paths):
    paths.add("my folder/solution.py")
    paths.add("__MACOSX/my folder/._solution.py", "\x00\x05garbage")
    paths.add("my folder/solution.pdf", "%PDF")

    results = paths.run()

    by_number = rows(results)
    assert list(by_number) == ["Public tests", "q1", "q2"]
    assert by_number["q1"]["score"] == 4
    assert by_number["q2"]["score"] == 6
    assert by_number["q2"]["name"] == "Vowels"
    assert results["stdout_visibility"] == "hidden"

    text = paths.results.read_text(encoding="utf-8")
    for name in HIDDEN:
        assert name not in text


def test_workdir_is_traversable_but_not_writable_by_others(paths):
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    paths.run()

    mode = stat.S_IMODE(paths.workdir.stat().st_mode)
    assert mode == 0o755


def test_missing_pdf_gives_a_notice(paths):
    paths.add("solution.py")

    results = paths.run()

    first = results["tests"][0]
    assert first["name"] == "Submission"
    assert first["max_score"] == 0
    assert "PDF" in first["output"]
    assert rows(results)["q1"]["score"] == 4


def test_markdown_answers_are_accepted_with_a_notice(paths):
    paths.add("solution.py")
    paths.add("Fixture_answers.md", "Because.")

    results = paths.run()

    first = results["tests"][0]
    assert first["name"] == "Submission"
    assert "Fixture_answers.md" in first["output"]
    assert rows(results)["q2"]["score"] == 6


def test_two_notebooks_are_both_named_and_nothing_is_graded(paths):
    paths.add("first.py")
    paths.add("second.py")
    paths.add("a.pdf", "%PDF")

    results = paths.run()

    first = results["tests"][0]
    assert "first.py" in first["output"]
    assert "second.py" in first["output"]
    assert rows(results)["q1"]["score"] == 0
    assert rows(results)["q2"]["score"] == 0


def test_infinite_budget_grades_everything(paths):
    bundle = json.loads((paths.source / "bundle.json").read_text(encoding="utf-8"))
    bundle["config"]["budget"] = float("inf")
    (paths.source / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    assert rows(results)["q1"]["score"] == 4
    assert rows(results)["q2"]["score"] == 6


def test_panic_in_the_grader_still_writes_results_and_exits_0(paths):
    # Python's JSON reads NaN, which the grader's deadline cannot hold.
    bundle = json.loads((paths.source / "bundle.json").read_text(encoding="utf-8"))
    bundle["config"]["budget"] = float("nan")
    (paths.source / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    by_number = rows(results)
    assert by_number["q1"]["score"] == 0
    assert "The autograder failed (PanicException)" in by_number["q1"]["output"]


def test_infinite_points_reach_results_json(paths):
    bundle = json.loads((paths.source / "bundle.json").read_text(encoding="utf-8"))
    bundle["config"]["questions"][0]["points"] = float("inf")
    (paths.source / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    assert rows(results)["q1"]["score"] == float("inf")
    assert rows(results)["q1"]["max_score"] == float("inf")
    assert '"score": Infinity' in paths.results.read_text(encoding="utf-8")


def test_unreadable_bundle_still_writes_results(paths):
    (paths.source / "bundle.json").write_text("{not json", encoding="utf-8")
    paths.add("solution.py")

    results = paths.run()

    [row] = results["tests"]
    assert row["score"] == 0
    assert "The autograder failed" in row["output"]
    assert results["stdout_visibility"] == "hidden"


def test_internal_error_zeroes_every_row_without_leaking_its_text(paths, tmp_path):
    # The work folder cannot be made under a file: the error names its path.
    (tmp_path / "def test_q1_add_large(): secret").write_text("", encoding="utf-8")
    paths.workdir = tmp_path / "def test_q1_add_large(): secret" / "work"
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    by_number = rows(results)
    assert by_number["q1"]["score"] == 0
    assert by_number["q2"]["score"] == 0
    assert "The autograder failed (NotADirectoryError)" in by_number["q1"]["output"]
    assert "secret" not in paths.results.read_text(encoding="utf-8")


def test_hostile_submission_still_writes_results_and_exits_0(paths, mogs_script):
    paths.add(
        "solution.py",
        "import marimo\n\napp = marimo.App()\n\nwith app.setup:\n"
        "    import ctypes, sys\n\n    sys.stdout.buffer.write(b'\\xff' * 100_000)\n"
        "    ctypes.string_at(0)\n",
    )
    paths.add("solution.pdf", "%PDF")

    result = subprocess.run([mogs_script, "autograde", *paths.argv()], capture_output=True)

    assert result.returncode == 0, result.stderr
    results = json.loads(paths.results.read_text(encoding="utf-8"))
    assert rows(results)["q1"]["score"] == 0
    assert "loading your notebook" in rows(results)["q1"]["output"]


@pytest.mark.skipif(sys.platform != "linux", reason="APFS refuses names that are not UTF-8")
def test_workdir_that_is_not_utf8_still_writes_results_and_exits_0(paths, tmp_path, mogs_script):
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")
    workdir = os.fsdecode(bytes(tmp_path) + b"/work-\xff")
    argv = [*paths.argv()[:-1], workdir]

    result = subprocess.run([mogs_script, "autograde", *argv], capture_output=True)

    assert result.returncode == 0, result.stderr
    results = json.loads(paths.results.read_text(encoding="utf-8"))
    assert "The autograder failed (ValueError)" in rows(results)["q1"]["output"]


def test_error_that_is_not_an_os_error_zeroes_every_row(paths):
    # A data path outside the source folder fails inside grading with a `ValueError`.
    bundle = json.loads((paths.source / "bundle.json").read_text(encoding="utf-8"))
    bundle["config"]["files"] = ["../secret.csv"]
    (paths.source / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    by_number = rows(results)
    assert by_number["q1"]["score"] == 0
    assert "The autograder failed (ValueError)" in by_number["q1"]["output"]
    assert "secret" not in paths.results.read_text(encoding="utf-8")


def test_symlink_to_the_bundle_is_not_graded(paths):
    (paths.submission / "solution.py").symlink_to(paths.source / "bundle.json")

    results = paths.run()

    assert rows(results)["q1"]["score"] == 0
    assert "Submission" in rows(results)


# Ctrl+C and the GIL


def only_bundle(paths: Paths, source: str, timeout: float) -> None:
    """Replace the bundle with one question whose test cell is `source`."""

    tests = tuple(line[4:].split("(")[0] for line in source.splitlines() if line.startswith("def "))
    cfg = AssignmentConfig("A", "solution.py", {"q1": Question("q1", "Q1", 1)}, timeout=timeout)
    grading = GradingBundle(cfg, "", (TestCell("q1", "q1-tests", False, source, tests, ()),))
    (paths.source / "bundle.json").write_text(grading.to_json(), encoding="utf-8")


def test_ctrl_c_stops_grading_and_kills_the_child(paths, mogs_script):
    source = (
        "import os, time\nfrom pathlib import Path\n\n"
        "def test_q1_wait():\n    Path('pid').write_text(str(os.getpid()))\n    time.sleep(60)\n"
    )
    only_bundle(paths, source, timeout=60)
    paths.add("solution.py")
    process = subprocess.Popen(
        [mogs_script, "autograde", *paths.argv()], stderr=subprocess.PIPE, text=True
    )
    pid_file = paths.workdir / "public" / "pid"

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


def test_grading_does_not_hold_the_gil(paths, caplog):
    caplog.set_level(logging.INFO, logger="mogs.grader.sandbox")
    source = (
        "import sys, time\n\n"
        "def test_q1_flood():\n    end = time.monotonic() + 3\n"
        "    while time.monotonic() < end:\n        sys.stdout.write('x' * 1000 + '\\n')\n"
    )
    only_bundle(paths, source, timeout=10)
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")
    finished = []
    thread = threading.Thread(target=lambda: finished.append(paths.run()))
    gaps = []

    thread.start()
    last = time.monotonic()

    while thread.is_alive():
        time.sleep(0.01)
        now = time.monotonic()
        gaps.append(now - last)
        last = now

    thread.join()

    assert rows(finished[0])["q1"]["score"] == 1, finished
    assert sum(gaps) > 2.5
    assert max(gaps) < 0.5

    # The child's output reaches the grader's log, its end only.
    [record] = [r for r in caplog.records if r.name == "mogs.grader.sandbox"]
    assert record.levelno == logging.INFO
    assert record.msg == "grading child output (%s phase, end):\n%s"
    phase, output = record.args
    assert phase == "public"
    assert output.endswith("x" * 1000 + "\n")

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from mogs.config import read_config
from mogs.grader import main as main_module
from mogs.grader.bundle import bundle_from_notebook
from mogs.grader.main import main
from mogs.source import parse_notebook

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the grader is Linux only")

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
        main(self.argv())
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
    assert list(by_number) == ["q1", "q2"]
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


def test_unreadable_bundle_still_writes_results(paths):
    (paths.source / "bundle.json").write_text("{not json", encoding="utf-8")
    paths.add("solution.py")

    results = paths.run()

    [row] = results["tests"]
    assert row["score"] == 0
    assert "The autograder failed" in row["output"]
    assert results["stdout_visibility"] == "hidden"


def test_internal_error_zeroes_every_row_without_leaking_its_text(paths, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("def test_q1_add_large(): secret source")

    monkeypatch.setattr(main_module, "grade", broken)
    paths.add("solution.py")
    paths.add("solution.pdf", "%PDF")

    results = paths.run()

    by_number = rows(results)
    assert by_number["q1"]["score"] == 0
    assert by_number["q2"]["score"] == 0
    assert "The autograder failed" in by_number["q1"]["output"]
    assert "secret" not in paths.results.read_text(encoding="utf-8")


def test_symlink_to_the_bundle_is_not_graded(paths):
    (paths.submission / "solution.py").symlink_to(paths.source / "bundle.json")

    results = paths.run()

    assert rows(results)["q1"]["score"] == 0
    assert "Submission" in rows(results)

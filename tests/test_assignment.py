import html
import re
from pathlib import Path
from typing import Any

import pytest

from marimo_gradescope import assignment

FIXTURE = Path(__file__).parent / "fixtures" / "solution.py"


def summary(path: Path | None, call: str = "assignment()") -> str:
    """Visible text of the summary `call` gives when run from the notebook at `path`."""

    namespace: dict[str, Any] = {"assignment": assignment}
    if path is not None:
        namespace["__file__"] = str(path)

    exec(f"panel = {call}", namespace)

    return html.unescape(re.sub(r"<[^>]+>", " ", namespace["panel"].text))


def words(text: str) -> str:
    return " ".join(text.split())


def test_summary_lists_the_questions_and_the_total(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    text = words(summary(notebook))

    assert "Fixture · 3 questions · 15 pts" in text
    assert "q1 Question 1 4 pts auto-graded: 2 public, 1 hidden" in text
    assert "q2 Vowels 6 pts auto-graded: 1 public, 2 hidden" in text
    assert "q3 Question 3 5 pts written" in text
    assert "marimo-gradescope build" in text


def test_summary_lists_the_problems(tmp_path):
    notebook = tmp_path / "hw.py"
    src = FIXTURE.read_text(encoding="utf-8").replace("def test_q2_count", "def test_q9_count")
    notebook.write_text(src, encoding="utf-8")

    text = words(summary(notebook))

    assert "1 problem" in text
    assert "test_q9_count" in text


def test_summary_shows_a_config_error(tmp_path):
    notebook = tmp_path / "hw.py"
    src = FIXTURE.read_text(encoding="utf-8").replace("points=4", 'points="four"')
    notebook.write_text(src, encoding="utf-8")

    text = words(summary(notebook))

    assert "`points` must be a number" in text


def test_summary_names_missing_data_files(tmp_path):
    notebook = tmp_path / "hw.py"
    src = FIXTURE.read_text(encoding="utf-8").replace(
        'mogs.assignment(name="Fixture")', 'mogs.assignment(name="Fixture", files=["data.csv"])'
    )
    notebook.write_text(src, encoding="utf-8")

    assert "data.csv" in summary(notebook)


def test_summary_without_a_saved_notebook():
    assert "Save the notebook" in summary(None)


def test_summary_of_a_missing_file(tmp_path):
    assert "Save the notebook" in summary(tmp_path / "gone.py")


def test_assignment_is_a_no_op_while_grading(monkeypatch, tmp_path):
    monkeypatch.setenv("MGS_GRADING", "1")

    assert summary(FIXTURE) == ""


@pytest.mark.parametrize(
    "call",
    [
        'assignment(name="x", files=["a"], timeout=1, budget=2, memory=4096, python="3.13", '
        'requirements="r.txt")'
    ],
)
def test_assignment_takes_the_config_arguments(call):
    assert "Fixture" in summary(FIXTURE, call)


def test_summary_names_the_whole_folder_to_build(tmp_path):
    folder = tmp_path / "my course" / "hw 1"
    folder.mkdir(parents=True)
    notebook = folder / "hw.py"
    notebook.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    assert f"marimo-gradescope build '{folder}'" in words(summary(notebook))

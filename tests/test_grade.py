import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from marimo_gradescope.build import BuildError, build
from marimo_gradescope.cli import main
from marimo_gradescope.grade import GradeResult, grade_submission

DEMO = Path(__file__).parent.parent / "examples" / "demo"
TOTAL = 10


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    folder = demo_copy(tmp_path_factory.mktemp("grade") / "demo")
    build(folder)

    return folder


def edited(source: Path, target: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(old, new), encoding="utf-8")

    return target


@pytest.mark.network
def test_solved_copy_gets_full_marks(demo):
    result = grade_submission(demo, demo / "dist" / "autograder" / "hw.py")

    assert isinstance(result, GradeResult)
    assert result.score == (TOTAL, TOTAL)
    assert all(outcome.passed for outcome in result.outcomes.values())


@pytest.mark.network
def test_report_marks_hidden_failures(demo, tmp_path, capsys):
    submission = edited(
        demo / "dist" / "autograder" / "hw.py",
        tmp_path / "hw.py",
        "return sum(xs) / len(xs)",
        "return sum(xs) / len(xs) if all(x >= 0 for x in xs) else 0",
    )

    code = main(["grade", str(demo), str(submission)])
    out = capsys.readouterr().out

    assert code == 0
    assert "test_q1_negative (hidden)" in out
    assert "test_q1_whole_numbers" not in out
    assert "Average" in out
    assert f"/{TOTAL}" in out


@pytest.mark.network
def test_folder_submission_finds_the_notebook(demo, tmp_path):
    upload = tmp_path / "upload"
    upload.mkdir()
    with zipfile.ZipFile(demo / "dist" / "student" / "hw.zip") as archive:
        (upload / "hw.py").write_bytes(archive.read("hw/hw.py"))
    (upload / "hw_answers.pdf").write_bytes(b"%PDF-1.4")

    result = grade_submission(demo, upload)

    assert result.submission == upload / "hw.py"
    assert result.score == (0, TOTAL)


def demo_copy(target: Path) -> Path:
    shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("dist", "__marimo__"))
    return target


def test_folder_without_notebook_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload"
    upload.mkdir()

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert "No `.py` file found" in capsys.readouterr().err


def test_missing_submission_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")

    code = main(["grade", str(folder), str(tmp_path / "nope.py")])

    assert code == 1
    assert "nope.py" in capsys.readouterr().err


def test_windows_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(BuildError, match="Linux or macOS"):
        grade_submission(tmp_path, tmp_path / "hw.py")


@pytest.mark.network
def test_load_failures_keep_test_names_and_hidden_marks(demo, tmp_path, capsys):
    submission = edited(
        demo / "dist" / "autograder" / "hw.py",
        tmp_path / "hw.py",
        "    import marimo_gradescope as mgs\n",
        "    import marimo_gradescope as mgs\n    import os\n\n    os._exit(3)\n",
    )

    code = main(["grade", str(demo), str(submission)])
    out = capsys.readouterr().out

    assert code == 0
    assert "while loading your notebook" in out
    assert "test_q1_whole_numbers" in out
    assert "test_q1_negative (hidden)" in out


@pytest.mark.network
def test_symlinked_submission_is_graded(demo, tmp_path):
    link = tmp_path / "hw.py"
    link.symlink_to(demo / "dist" / "autograder" / "hw.py")

    result = grade_submission(demo, link)

    assert result.score == (TOTAL, TOTAL)


def test_missing_data_file_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    (folder / "data" / "scores.csv").unlink()

    code = main(["grade", str(folder), str(folder / "hw.py")])

    assert code == 1
    assert "`files`" in capsys.readouterr().err

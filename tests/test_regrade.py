import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from mogs import regrade
from mogs._native import main
from mogs.build import BuildError, build
from mogs.regrade import GradeResult, grade_submission

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
        "    import mogs\n",
        "    import mogs\n    import os\n\n    os._exit(3)\n",
    )

    code = main(["grade", str(demo), str(submission)])
    captured = capsys.readouterr()
    out = captured.out

    assert code == 1
    assert "failed to load" in captured.err
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


@pytest.mark.network
def test_zip_submission_is_unpacked_and_graded(demo, tmp_path, capsys):
    upload = tmp_path / "hw_submission.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.write(demo / "dist" / "autograder" / "hw.py", "hw.py")
        archive.writestr("hw_answers.pdf", b"%PDF-1.4")

    code = main(["grade", str(demo), str(upload)])
    out = capsys.readouterr().out

    assert code == 0
    assert f"Graded:  {upload / 'hw.py'}" in out
    assert f"{TOTAL}/{TOTAL}" in out


@pytest.mark.network
def test_notebook_that_fails_to_import_exits_nonzero(demo, tmp_path, capsys):
    submission = tmp_path / "hw.py"
    submission.write_text("def broken(:\n", encoding="utf-8")

    code = main(["grade", str(demo), str(submission)])
    captured = capsys.readouterr()

    assert code == 1
    assert "failed to load" in captured.err
    assert "test_q1_whole_numbers" in captured.out


HIDDEN_ONLY = """\
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "mogs",
# ]
# ///

import marimo

app = marimo.App()

with app.setup:
    import mogs


@app.cell
def _():
    mogs.assignment()
    return


@app.function
def double(x):
    # BEGIN SOLUTION
    return 2 * x
    # END SOLUTION


@app.cell
def _():
    # HIDDEN
    EXPECTED = double(2)
    return (EXPECTED,)


@app.cell
def _(EXPECTED):
    # HIDDEN
    def test_q1_double():
        assert EXPECTED == 4

    return


@app.cell
def _():
    mogs.grade("q1", points=1)
    return


if __name__ == "__main__":
    app.run()
"""


@pytest.mark.network
def test_failing_hidden_helper_is_a_normal_result(tmp_path, capsys):
    folder = tmp_path / "hidden"
    folder.mkdir()
    (folder / "hw.py").write_text(HIDDEN_ONLY, encoding="utf-8")
    build(folder)
    submission = edited(
        folder / "dist" / "autograder" / "hw.py",
        tmp_path / "hw.py",
        "def double(x):",
        "def triple(x):",
    )

    code = main(["grade", str(folder), str(submission)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "failed to load" not in captured.err
    assert "test_q1_double (hidden)" in captured.out


def test_zip_with_path_outside_is_refused(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("../escape.py", "x = 1\n")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert "../escape.py" in capsys.readouterr().err
    assert not (tmp_path / "escape.py").exists()


def test_broken_zip_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    upload.write_bytes(b"not a zip")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert "upload.zip" in capsys.readouterr().err


def test_zip_without_notebook_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("notes.txt", "hello")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert "No `.py` file found" in capsys.readouterr().err


def test_other_file_type_is_refused(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "hw.txt"
    upload.write_text("x = 1\n", encoding="utf-8")

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert "hw.txt" in err
    assert ".zip" in err


def test_zip_with_too_many_members_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(regrade, "MAX_ZIP_MEMBERS", 3)
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        for index in range(4):
            archive.writestr(f"file{index}.txt", "x")

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert "upload.zip" in err
    assert "4 files" in err


def test_zip_that_unpacks_too_large_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(regrade, "MAX_ZIP_BYTES", 1000)
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("hw.py", "x = 1\n")
        archive.writestr("big.txt", "0" * 2000)

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert "upload.zip" in err
    assert "1000 bytes" in err


def test_zip_with_lying_sizes_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(regrade, "MAX_ZIP_BYTES", 1000)
    monkeypatch.setattr(regrade, "_CHUNK", 100)
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.txt", "0" * 5000)
    upload.write_bytes(patched(upload.read_bytes(), local=22, central=24, value=10, size=4))

    target = tmp_path / "upload"
    with pytest.raises(BuildError, match="upload.zip"):
        regrade._unpack(upload, target)

    assert sum(path.stat().st_size for path in target.rglob("*") if path.is_file()) <= 1000


@pytest.mark.parametrize(
    ("offsets", "value", "message"),
    [((6, 8), 0x1, "encrypted"), ((8, 10), 99, "compression")],
    ids=["password", "unsupported-compression"],
)
def test_unreadable_zip_member_is_an_error(tmp_path, capsys, offsets, value, message):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("hw.py", "x = 1\n")
    local, central = offsets
    upload.write_bytes(patched(upload.read_bytes(), local=local, central=central, value=value))

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert "upload.zip" in err
    assert message in err


def patched(data: bytes, *, local: int, central: int, value: int, size: int = 2) -> bytes:
    """`data`, a zip of one member, with a field of its local and central headers set."""

    field = value.to_bytes(size, "little")
    start = data.index(b"PK\x01\x02")
    data = data[:local] + field + data[local + size :]

    return data[: start + central] + field + data[start + central + size :]

import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from mogs import _analysis
from mogs._analysis import BuildError
from mogs._native import main

DEMO = Path(__file__).parent.parent / "examples" / "demo"
TOTAL = 10
FULL_MARKS = """
Question            Score
Average               4/4
Reading scores        6/6
Total               10/10
"""


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    folder = demo_copy(tmp_path_factory.mktemp("grade") / "demo")
    assert main(["build", str(folder)]) == 0

    return folder


def edited(source: Path, target: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(old, new), encoding="utf-8")

    return target


@pytest.mark.network
def test_solved_copy_gets_full_marks(demo, capsys):
    submission = demo / "dist" / "autograder" / "hw.py"

    code = main(["grade", str(demo), str(submission)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out == f"Graded:  {submission}\n{FULL_MARKS}"
    assert captured.err == ""


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
    assert out == (
        f"Graded:  {submission}\n"
        "\n"
        "Question            Score\n"
        "Average               3/4\n"
        "Reading scores        6/6\n"
        "Total                9/10\n"
        "\n"
        "Failing tests:\n"
        "- test_q1_negative (hidden): In test_q1_negative, line 6:\n"
        "        assert mean([-4, 4, -6]) == -2\n"
        "    AssertionError: assert 0 == -2\n"
        "     +  where 0 = mean([-4, 4, -6])\n"
    )


@pytest.mark.network
def test_folder_submission_finds_the_notebook(demo, tmp_path, capsys):
    upload = tmp_path / "upload"
    upload.mkdir()
    with zipfile.ZipFile(demo / "dist" / "student" / "hw.zip") as archive:
        (upload / "hw.py").write_bytes(archive.read("hw/hw.py"))
    (upload / "hw_answers.pdf").write_bytes(b"%PDF-1.4")

    code = main(["grade", str(demo), str(upload)])
    out = capsys.readouterr().out

    assert code == 0
    assert out.startswith(f"Graded:  {upload / 'hw.py'}\n")
    assert f"Total                0/{TOTAL}\n" in out


def demo_copy(target: Path) -> Path:
    shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("dist", "__marimo__"))
    return target


def test_folder_without_notebook_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload"
    upload.mkdir()

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert capsys.readouterr().err.startswith(f"Grading failed.\n{upload}: No `.py` file found")


def test_missing_submission_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")

    code = main(["grade", str(folder), str(tmp_path / "nope.py")])

    assert code == 1
    assert capsys.readouterr().err == (
        f"Grading failed.\n{tmp_path / 'nope.py'} is not a file or folder.\n"
    )


def test_invalid_source_notebook_is_an_error(tmp_path, capsys):
    code = main(["grade", str(tmp_path / "nope"), str(tmp_path / "hw.py")])

    assert code == 1
    assert capsys.readouterr().err == f"Grading failed.\n{tmp_path / 'nope'} does not exist.\n"


def test_windows_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")

    code = main(["grade", str(tmp_path), str(tmp_path / "hw.py")])

    assert code == 1
    assert capsys.readouterr().err == (
        "Grading failed.\nGrading needs Linux or macOS (WSL on Windows).\n"
    )


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
    assert captured.err.startswith(f"Grading failed.\n{submission} failed to load: ")
    assert "while loading your notebook" in out
    assert "test_q1_whole_numbers" in out
    assert "test_q1_negative (hidden)" in out


@pytest.mark.network
def test_symlinked_submission_is_graded(demo, tmp_path, capsys):
    link = tmp_path / "hw.py"
    link.symlink_to(demo / "dist" / "autograder" / "hw.py")

    code = main(["grade", str(demo), str(link)])
    out = capsys.readouterr().out

    assert code == 0
    assert out == f"Graded:  {demo / 'dist' / 'autograder' / 'hw.py'}\n{FULL_MARKS}"


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
    assert out == f"Graded:  {upload / 'hw.py'}\n{FULL_MARKS}"


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
    assert main(["build", str(folder)]) == 0
    submission = edited(
        folder / "dist" / "autograder" / "hw.py",
        tmp_path / "hw.py",
        "def double(x):",
        "def triple(x):",
    )
    capsys.readouterr()

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
    assert capsys.readouterr().err == (
        f"Grading failed.\n{upload}: ../escape.py would unpack outside the folder.\n"
    )
    assert not (tmp_path / "escape.py").exists()


def test_zip_with_absolute_path_is_refused(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr(f"{tmp_path}/escape.py", "x = 1\n")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert "would unpack outside the folder." in capsys.readouterr().err
    assert not (tmp_path / "escape.py").exists()


def test_broken_zip_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    upload.write_bytes(b"not a zip")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert capsys.readouterr().err == (
        f"Grading failed.\n{upload} is not a readable zip (File is not a zip file).\n"
    )


def test_zip_without_notebook_is_an_error(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("notes.txt", "hello")

    code = main(["grade", str(folder), str(upload)])

    assert code == 1
    assert capsys.readouterr().err.startswith(f"Grading failed.\n{upload}: No `.py` file found")


def test_other_file_type_is_refused(tmp_path, capsys):
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "hw.txt"
    upload.write_text("x = 1\n", encoding="utf-8")

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert err == (
        f"Grading failed.\n{upload} is not a notebook. Pass a `.py` notebook, a `.zip` of the "
        "submission, or a folder with one.\n"
    )


def test_zip_with_too_many_members_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_analysis, "MAX_ZIP_MEMBERS", 3)
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        for index in range(4):
            archive.writestr(f"file{index}.txt", "x")

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert err == f"Grading failed.\n{upload} holds 4 files; the limit is 3.\n"


def test_zip_that_unpacks_too_large_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_analysis, "MAX_ZIP_BYTES", 1000)
    folder = demo_copy(tmp_path / "demo")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("hw.py", "x = 1\n")
        archive.writestr("big.txt", "0" * 2000)

    code = main(["grade", str(folder), str(upload)])
    err = capsys.readouterr().err

    assert code == 1
    assert err == f"Grading failed.\n{upload} unpacks to more than 1000 bytes.\n"


def test_zip_with_lying_sizes_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(_analysis, "MAX_ZIP_BYTES", 1000)
    monkeypatch.setattr(_analysis, "_CHUNK", 100)
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.txt", "0" * 5000)
    upload.write_bytes(patched(upload.read_bytes(), local=22, central=24, value=10, size=4))

    target = tmp_path / "upload"
    with pytest.raises(BuildError, match="upload.zip"):
        _analysis.unpack(upload, target)

    assert sum(path.stat().st_size for path in target.rglob("*") if path.is_file()) <= 1000


@pytest.mark.parametrize(
    ("offsets", "value", "message"),
    [((6, 8), 0x1, "encrypted"), ((8, 10), 99, "That compression method is not supported")],
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
    assert err.startswith(f"Grading failed.\n{upload} is not a readable zip (")
    assert message in err


def patched(data: bytes, *, local: int, central: int, value: int, size: int = 2) -> bytes:
    """`data`, a zip of one member, with a field of its local and central headers set."""

    field = value.to_bytes(size, "little")
    start = data.index(b"PK\x01\x02")
    data = data[:local] + field + data[local + size :]

    return data[: start + central] + field + data[start + central + size :]

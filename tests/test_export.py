import _thread
import ast
import importlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

import marimogs
from marimogs._run import run_capped
from marimogs.config import read_config
from marimogs.export import (
    Download,
    Line,
    Mark,
    export,
    export_button,
    locate,
    package,
    panel,
)
from marimogs.pdf import PdfUnavailable
from marimogs.source import parse_notebook
from marimogs.transform import public_test_files, student_notebook

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"
# The package's `export` attribute is the function, so take the module from `sys.modules`.
export_module = importlib.import_module("marimogs.export")
TITLES = {"q1": "Question 1", "q2": "Vowels", "q3": "Question 3"}
ANSWER = "Because the order of the terms does not change the sum."
# The student copy's answer cell is the source's, unchanged: its text is the placeholder.
PLACEHOLDERS = {"answer_q3": ANSWER}
ONE_LINER = '@app.cell\ndef answer_q3(): mo.md("Because."); return\n'
ANSWER_CELL = (
    '@app.cell\ndef answer_q3():\n    mo.md(r"""\n    Because the order of the terms does not '
    'change the sum.\n    """)\n    return\n'
)


def student_copy(path: Path = SOLUTION) -> str:
    src = path.read_text(encoding="utf-8")
    return student_notebook(src, parse_notebook(src), read_config(src, path), "marimo-gradescope")


def export_placeholders(text: str) -> dict[str, str]:
    """The `placeholders=` argument of the notebook's export call."""

    [call] = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "export"
    ]

    return next(ast.literal_eval(k.value) for k in call.keywords if k.arg == "placeholders")


def solved_copy() -> str:
    """The student copy with both functions written."""

    return (
        student_copy()
        .replace(
            '    """Sum of two numbers."""\n    ...\n',
            '    """Sum of two numbers."""\n    return a + b\n',
        )
        .replace(
            '    """Number of vowels in `s`."""\n    ...\n',
            '    """Number of vowels in `s`."""\n    return sum(c in "aeiou" for c in s.lower())\n',
        )
    )


def write_tests(folder: Path, q2_extra: str = "") -> None:
    """Write the public test files next to a student copy in `folder`.

    `q2_extra` goes in front of the first q2 test.
    """

    src = SOLUTION.read_text(encoding="utf-8")

    for rel, text in public_test_files(parse_notebook(src)).items():
        text = text.replace("def test_q2_count():\n", q2_extra + "def test_q2_count():\n")
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")


def texts(lines: tuple[Line, ...]) -> list[str]:
    return [f"{line.mark} {line.text}" for line in lines]


def item_texts(line: Line) -> list[str]:
    return texts(line.items)


def zip_names(data: bytes) -> list[str]:
    return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())


def zip_bytes(data: bytes, name: str) -> bytes:
    return zipfile.ZipFile(io.BytesIO(data)).read(name)


def zip_text(data: bytes, name: str) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read(name).decode()


@pytest.fixture
def no_pdf(monkeypatch):
    """Make every PDF attempt fail fast."""

    def fail(*args, **kwargs):
        raise PdfUnavailable("typst is not installed")

    monkeypatch.setattr(export_module, "render_manual_pdf", fail)


# Package surface


def test_exported_from_package():
    assert marimogs.export is export
    assert marimogs.export_button is export_button


def test_export_button_label():
    button = export_button()

    assert button.value is False
    assert "Export submission" in button.text


# Locating the notebook


def test_locate_prefers_dunder_file(tmp_path):
    notebook = tmp_path / "hw.py"

    assert locate({"__file__": str(notebook)}) == notebook.resolve()


def test_locate_without_file_outside_marimo():
    assert locate({}) is None


def test_unclicked_panel_names_the_file_and_does_nothing(tmp_path, monkeypatch):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    monkeypatch.setattr(export_module, "package", lambda *a, **k: pytest.fail("packaged"))

    html = export_module.export_panel(False, {"__file__": str(notebook)}, "hw", [], {}, {})

    assert str(notebook) in html.text
    assert "Export submission" in html.text


def test_unlocated_notebook_says_so():
    html = export_module.export_panel(False, {}, "hw", [], {}, {})

    assert "Cannot find this notebook" in html.text


# Packaging


def test_package_reports_disk_file_tests_and_download(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    write_tests(tmp_path)
    saved = datetime.fromtimestamp(notebook.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)
    lines = texts(result.lines)

    assert lines[0] == "⚠ Public tests: 0/3 passed"
    assert item_texts(result.lines[0]) == ["✗ Question 1: 0/2 passed", "✗ Vowels: 0/1 passed"]
    assert lines[1] == "⚠ PDF not created: typst is not installed; answers included as Markdown"
    assert not any("Using" in line or "⬇" in line for line in lines)
    assert result.filename == "hw_submission.zip"
    assert result.note == f"Exported hw.py as saved at {saved}."


# The student moves `three` into a plain cell, so the grader has to run the whole notebook.
PLAIN_CELL_EDIT = (
    "@app.function\ndef three():\n    n = ...\n    return n\n",
    "@app.cell\ndef _():\n    def three():\n        return 3\n\n    return (three,)\n",
)


def test_package_says_why_a_notebook_run_failed(tmp_path, no_pdf):
    # Issue #26: `three` is right, but an ungraded cell calling the blank `double` raises.
    source = FIXTURES / "plain_cell_answer.py"
    src = source.read_text(encoding="utf-8")
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(source).replace(*PLAIN_CELL_EDIT), encoding="utf-8")
    line = notebook.read_text(encoding="utf-8").splitlines().index("    demo = double(4) + 1") + 1

    for rel, text in public_test_files(parse_notebook(src)).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")

    result = package(notebook, name="hw", manual=[], titles={})

    assert texts(result.lines) == [
        "⚠ Public tests: 0/2 passed",
        f"⚠ `three` is unavailable: running your notebook failed: line {line} raised TypeError: "
        "unsupported operand type(s) for +: 'ellipsis' and 'int'. Tests that use it fail, "
        "on Gradescope too.",
    ]


def test_unavailable_lines_group_names_by_reason():
    lines = export_module.unavailable_lines({"b": "why", "a": "why", "c": "other"})

    assert texts(lines) == [
        "⚠ `a` and `b` are unavailable: why. Tests that use them fail, on Gradescope too.",
        "⚠ `c` is unavailable: other. Tests that use it fail, on Gradescope too.",
    ]


def test_package_uses_the_disk_file_not_the_kernel(tmp_path, no_pdf):
    # Review Focus #2: whatever the kernel holds, the saved file is what gets checked and sent.
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    write_tests(tmp_path)
    assert (
        "0/3 passed" in texts(package(notebook, name="hw", manual=["q3"], titles=TITLES).lines)[0]
    )

    notebook.write_text(solved_copy(), encoding="utf-8")
    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)

    assert texts(result.lines)[0] == "✓ Public tests: 3/3 passed"
    assert item_texts(result.lines[0]) == ["✓ Question 1: 2/2 passed", "✓ Vowels: 1/1 passed"]
    # Bytes: on Windows `write_text` writes CRLF, and the zip holds the file as saved.
    assert zip_bytes(result.data, "hw.py") == notebook.read_bytes()


def test_markdown_fallback_in_zip(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)

    assert zip_names(result.data) == ["hw.py", "hw_answers.md"]
    answers = zip_text(result.data, "hw_answers.md")
    assert "Question 3" in answers
    assert ANSWER in answers


def test_pdf_goes_in_zip(tmp_path, monkeypatch):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    seen = []

    def render(notebook, qids, titles, out, *, placeholders=None, status=None):
        seen.append(placeholders)
        out.write_bytes(b"%PDF-1.4 answers")
        return out

    monkeypatch.setattr(export_module, "render_manual_pdf", render)

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES, placeholders={"q3": "x"})

    assert seen == [{"answer_q3": "x"}]

    assert "✓ Written answers → hw.pdf" in texts(result.lines)
    assert zip_names(result.data) == ["hw.pdf", "hw.py"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["hw.py"]


def test_no_manual_questions_zip_has_only_the_notebook(tmp_path, monkeypatch):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    monkeypatch.setattr(export_module, "render_manual_pdf", lambda *a, **k: pytest.fail("pdf"))

    result = package(notebook, name="hw", manual=[], titles={})

    assert zip_names(result.data) == ["hw.py"]
    assert [line.text.split(":")[0] for line in result.lines] == ["Public tests"]


@pytest.mark.parametrize(
    ("edit", "warned"),
    [
        (("def answer_q3", "def my_answer"), True),
        (("Because the order of the terms does not change the sum.", "   "), True),
        (("", ""), True),
        (("Because the order of", "Because  the\n    order of"), True),
        (("mo.md(r", "mo.md(f"), False),
        (("Because the order", "Because, the order"), False),
    ],
)
def test_missing_empty_or_placeholder_answer_warning(tmp_path, no_pdf, edit, warned):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy().replace(*edit), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES, placeholders=PLACEHOLDERS)

    assert ("⚠ Question 3 has no answer yet" in texts(result.lines)) == warned


def test_placeholders_keyed_by_question_still_work(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES, placeholders={"q3": ANSWER})

    assert "⚠ Question 3 has no answer yet" in texts(result.lines)


PART_B = '\n@app.cell\ndef answer_q3_2():\n    mo.md("*Write part (b) here.*")\n    return\n'


@pytest.mark.parametrize(
    ("edit", "warned"),
    [
        ((ANSWER, "Addition is commutative."), ["⚠ Question 3, part 2 has no answer yet"]),
        (
            ("*Write part (b) here.*", "Also for reals."),
            ["⚠ Question 3, part 1 has no answer yet"],
        ),
        (
            (ANSWER, "Addition is commutative.", "*Write part (b) here.*", "Also for reals."),
            [],
        ),
    ],
)
def test_each_part_is_checked_against_its_own_placeholder(tmp_path, no_pdf, edit, warned):
    notebook = tmp_path / "hw.py"
    text = student_copy().replace(ANSWER_CELL, ANSWER_CELL + PART_B)

    for old, new in zip(edit[::2], edit[1::2], strict=True):
        text = text.replace(old, new)

    notebook.write_text(text, encoding="utf-8")
    placeholders = {**PLACEHOLDERS, "answer_q3_2": "*Write part (b) here.*"}

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES, placeholders=placeholders)

    assert [line for line in texts(result.lines) if "no answer" in line] == warned


def test_solved_copy_exports_without_a_warning(tmp_path, no_pdf):
    src = SOLUTION.read_text(encoding="utf-8")
    nb, cfg = parse_notebook(src), read_config(src, SOLUTION)
    text = student_notebook(src, nb, cfg, "marimo-gradescope", solutions=True)
    notebook = tmp_path / "hw.py"
    notebook.write_text(text, encoding="utf-8")

    result = package(
        notebook, name="hw", manual=["q3"], titles=TITLES, placeholders=export_placeholders(text)
    )
    # The first line is the public tests; the PDF line says the Markdown fallback was used.
    warnings = [line.text for line in result.lines[2:] if line.mark is Mark.WARN]

    assert texts(result.lines)[1].startswith("⚠ PDF not created")
    assert warnings == []


def test_answers_file_says_an_untouched_answer_has_no_answer(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES, placeholders=PLACEHOLDERS)
    answers = zip_text(result.data, "hw_answers.md")

    assert "*No answer.*" in answers
    assert ANSWER not in answers


def test_one_line_answer_cell_still_packaged(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    text = student_copy()
    assert ANSWER_CELL in text
    notebook.write_text(text.replace(ANSWER_CELL, ONE_LINER), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)

    assert zip_names(result.data) == ["hw.py", "hw_answers.md"]
    assert 'mo.md("Because.")' in zip_text(result.data, "hw_answers.md")


def test_one_line_answer_cell_with_real_pdf(tmp_path):
    # Without the fixture: the PDF shows the cell's source.
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy().replace(ANSWER_CELL, ONE_LINER), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)

    assert "✓ Written answers → hw.pdf" in texts(result.lines)
    assert zip_names(result.data) == ["hw.pdf", "hw.py"]


def test_unexpected_error_shows_failed_panel(tmp_path, monkeypatch):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    def broken(*args, **kwargs):
        raise OSError("disk <gone>")

    monkeypatch.setattr(export_module, "package", broken)

    html = export_module.export_panel(True, {"__file__": str(notebook)}, "hw", [], {}, {})

    assert "✗" in html.text
    assert "Export failed: OSError: disk &lt;gone&gt;" in html.text


def test_interrupt_in_panel_propagates(tmp_path, monkeypatch):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(export_module, "package", interrupted)

    with pytest.raises(KeyboardInterrupt):
        export_module.export_panel(True, {"__file__": str(notebook)}, "hw", [], {}, {})


def test_change_during_export_is_warned_and_snapshot_zipped(tmp_path, monkeypatch, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    original = notebook.read_bytes()
    real = export_module.run_capped
    commands = []

    def saving(command, *, cwd, timeout):
        commands.append((command, cwd))
        notebook.write_text(solved_copy(), encoding="utf-8")
        return real(command, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(export_module, "run_capped", saving)

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)
    lines = texts(result.lines)

    [(command, cwd)] = commands
    assert Path(command[3]) == notebook
    assert cwd == tmp_path
    assert "⚠ The notebook changed during the export; export again." in lines
    assert zip_bytes(result.data, "hw.py") == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["hw.py"]


def test_no_change_warning_without_a_change(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    lines = texts(package(notebook, name="hw", manual=["q3"], titles=TITLES).lines)

    assert not any("changed during the export" in line for line in lines)


def test_tests_reading_data_next_to_dunder_file_pass(tmp_path, no_pdf):
    # The grader keeps data files next to the graded copy, so this idiom passes there too.
    (tmp_path / "data.txt").write_text("42", encoding="utf-8")
    notebook = tmp_path / "hw.py"
    notebook.write_text(solved_copy(), encoding="utf-8")
    write_tests(
        tmp_path,
        "def test_q2_data():\n"
        "    from pathlib import Path\n"
        "    folder = Path(add.__globals__['__file__']).parent\n"
        "    assert (folder / 'data.txt').read_text() == '42'\n\n",
    )

    result = package(notebook, name="hw", manual=[], titles={})

    assert texts(result.lines)[0] == "✓ Public tests: 4/4 passed"


def test_snapshot_tests_read_data_next_to_the_notebook(tmp_path, no_pdf):
    (tmp_path / "data.txt").write_text("42", encoding="utf-8")
    notebook = tmp_path / "hw.py"
    notebook.write_text(solved_copy(), encoding="utf-8")
    write_tests(tmp_path, "def test_q2_data():\n    assert open('data.txt').read() == '42'\n\n")

    result = package(notebook, name="hw", manual=[], titles={})

    assert texts(result.lines)[0] == "✓ Public tests: 4/4 passed"


def test_selfcheck_timeout_is_reported(tmp_path, monkeypatch, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    write_tests(tmp_path, "def test_q2_slow():\n    import time\n    time.sleep(60)\n\n")
    monkeypatch.setattr(export_module, "SELFCHECK_TIMEOUT", 2.0)

    start = time.monotonic()
    lines = texts(package(notebook, name="hw", manual=["q3"], titles=TITLES).lines)

    assert time.monotonic() - start < 30
    assert lines[0] == "⚠ Public tests: the check timed out after 2 s"


def test_unparsable_notebook_still_packaged(tmp_path, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text("def (:\n", encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)
    lines = texts(result.lines)

    assert lines[0].startswith("⚠ Public tests could not run: ")
    assert "not valid Python" in lines[0]
    assert "⚠ Question 3 has no answer yet" in lines
    assert "hw.py" in zip_names(result.data)


def test_interrupt_stops_the_export(tmp_path, monkeypatch, no_pdf):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(export_module, "run_capped", interrupted)
    monkeypatch.setattr(export_module, "build_zip", lambda *a: pytest.fail("zipped"))

    with pytest.raises(KeyboardInterrupt):
        package(notebook, name="hw", manual=["q3"], titles=TITLES)


def test_run_capped_kills_the_child_on_interrupt(tmp_path):
    marker = tmp_path / "pid"
    script = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    timer = threading.Timer(1.0, _thread.interrupt_main)
    timer.start()

    try:
        with pytest.raises(KeyboardInterrupt):
            run_capped([sys.executable, "-c", script], cwd=tmp_path, timeout=30)
    finally:
        timer.cancel()

    pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.05)

    assert not alive(pid)


def alive(pid: int) -> bool:
    """Whether process `pid` is still running (a zombie counts as gone)."""

    if sys.platform == "win32":
        # `os.kill(pid, 0)` is not a probe on Windows; ask for the exit code instead.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False

        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False

    # A zombie still answers; check its state where /proc exists.
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except ProcessLookupError:
        # The process ended between `os.kill` and the read.
        return False
    except FileNotFoundError:
        return sys.platform != "linux"

    return state != "Z"


@pytest.mark.skipif(sys.platform == "win32", reason="checks POSIX process groups")
def test_run_capped_kills_grandchildren_on_timeout(tmp_path):
    marker = tmp_path / "pid"
    grandchild = (
        f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    script = (
        f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(60)"
    )

    assert run_capped([sys.executable, "-c", script], cwd=tmp_path, timeout=3) is None

    pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.05)

    assert not alive(pid)


def test_run_capped_timeout_returns_none(tmp_path):
    start = time.monotonic()

    command = [sys.executable, "-c", "import time; time.sleep(60)"]

    assert run_capped(command, cwd=tmp_path, timeout=1) is None
    assert time.monotonic() - start < 15


@pytest.mark.slow
def test_real_pdf_in_zip(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")

    result = package(notebook, name="hw", manual=["q3"], titles=TITLES)

    assert "✓ Written answers → hw.pdf" in texts(result.lines)
    assert zip_names(result.data) == ["hw.pdf", "hw.py"]
    assert zipfile.ZipFile(io.BytesIO(result.data)).read("hw.pdf").startswith(b"%PDF")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["hw.py"]


# Panel


def test_panel_sets_the_download_apart_below_the_checks():
    html = panel(
        (Line(Mark.OK, "Public tests: 1/1 passed", (Line(Mark.OK, "Question 1: 1/1 passed"),)),),
        Download("<a>dl</a>", "hw_submission.zip", "Exported hw.py."),
    )

    checks, download = html.split("hw_submission.zip", 1)
    assert "Question 1: 1/1 passed" in checks
    assert "<a>dl</a>" in download
    assert "border-top" in checks[checks.rindex("<div") :]


def test_panel_escapes_text_and_has_no_hard_colors():
    lines = (Line(Mark.OK, "<b>x</b> & y", (Line(Mark.FAIL, "<i>q1</i>"),)), Line(Mark.WARN, "z"))
    html = panel(lines, Download("<a>dl</a>", "hw_<1>.zip", "Exported <hw>.py."))

    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in html
    assert "&lt;i&gt;q1&lt;/i&gt;" in html
    assert "<b>x</b>" not in html and "<i>q1</i>" not in html
    assert "<a>dl</a>" in html
    assert "hw_&lt;1&gt;.zip — upload this to Gradescope" in html
    assert "Exported &lt;hw&gt;.py. Export again after further changes." in html
    assert "Export again" not in panel((Line(Mark.OK, "x"),))
    assert "color: #" not in html
    assert "color: black" not in html and "color: white" not in html


# In marimo


def outside_pytest() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def test_export_html_shows_the_located_file(tmp_path):
    # `marimo export html` runs the notebook like a script; the export panel must find its file.
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    out = tmp_path / "out" / "hw.html"

    result = subprocess.run(
        # PEP 723 headers: without `--no-sandbox` marimo may ask whether to use a sandbox.
        [sys.executable, "-m", "marimo", "export", "html", "--no-sandbox", str(notebook)]
        + ["-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    page = out.read_text(encoding="utf-8")
    assert "Cannot find this notebook" not in page
    # The page embeds the panel as JSON, which escapes the backslashes of Windows paths.
    assert json.dumps(str(notebook))[1:-1] in page
    assert "Export submission" in page


def test_export_is_a_no_op_while_grading(monkeypatch):
    monkeypatch.setenv("MGS_GRADING", "1")
    monkeypatch.setattr(export_module, "locate", lambda namespace: pytest.fail("located"))

    result = export(export_button(), name="hw", manual=[], titles={})

    assert result.text == ""


def test_run_capped_marks_the_child_as_grading(tmp_path):
    code = "import os; print(os.environ.get('MGS_GRADING'), os.environ.get('PYTEST_CURRENT_TEST'))"
    result = run_capped([sys.executable, "-c", code], cwd=tmp_path, timeout=30)

    assert result is not None
    assert result.stdout.split() == ["1", "None"]

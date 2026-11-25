import json
import os
import subprocess
import sys
from pathlib import Path

from marimo_gradescope import selfcheck
from marimo_gradescope.config import read_config
from marimo_gradescope.source import parse_notebook
from marimo_gradescope.transform import student_notebook

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"


def student_copy(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return student_notebook(src, parse_notebook(src), read_config(src, path), "marimo-gradescope")


def run(notebook: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    return subprocess.run(
        [sys.executable, "-m", "marimo_gradescope.selfcheck", str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd or notebook.parent,
        env=env,
    )


def test_source_passes_public_tests_only(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(SOLUTION.read_text(encoding="utf-8"), encoding="utf-8")

    results = selfcheck.public_results(notebook)

    assert results == {
        "q1": {"passed": 2, "total": 2, "failed": []},
        "q2": {"passed": 1, "total": 1, "failed": []},
    }


def test_student_copy_fails_its_public_tests(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(SOLUTION), encoding="utf-8")

    results = selfcheck.public_results(notebook)

    assert results == {
        "q1": {"passed": 0, "total": 2, "failed": ["test_q1_add", "test_q1_add_negative"]},
        "q2": {"passed": 0, "total": 1, "failed": ["test_q2_count"]},
    }


def test_module_prints_json_last_despite_student_prints(tmp_path):
    notebook = tmp_path / "hw.py"
    text = student_copy(SOLUTION).replace(
        '    """Sum of two numbers."""\n    ...\n',
        '    """Sum of two numbers."""\n    print("{noise}")\n    return a + b\n',
    )
    notebook.write_text(text, encoding="utf-8")

    result = run(notebook)
    last = result.stdout.strip().splitlines()[-1]

    assert result.returncode == 0, result.stderr
    assert json.loads(last)["q1"] == {"passed": 2, "total": 2, "failed": []}
    assert "{noise}" not in result.stdout


def test_reads_files_relative_to_the_notebook(tmp_path):
    (tmp_path / "data.txt").write_text("42", encoding="utf-8")
    notebook = tmp_path / "hw.py"
    text = SOLUTION.read_text(encoding="utf-8").replace(
        "    def test_q2_count():\n",
        "    def test_q2_data():\n        assert open('data.txt').read() == '42'\n\n"
        "    def test_q2_count():\n",
    )
    notebook.write_text(text, encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    for cwd in (tmp_path, elsewhere):
        result = run(notebook, cwd)

        assert json.loads(result.stdout)["q2"]["passed"] == 2, result.stderr


def test_unparsable_notebook_exits_with_message(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text("def (:\n", encoding="utf-8")

    result = run(notebook)

    assert result.returncode != 0
    assert "not valid Python" in result.stderr
    assert result.stdout == ""


def test_leaves_nothing_next_to_the_notebook(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(SOLUTION), encoding="utf-8")

    run(notebook)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["hw.py"]


def test_folder_argument_sets_the_working_directory(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "data.txt").write_text("42", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    notebook = snapshot / "hw.py"
    notebook.write_text(
        SOLUTION.read_text(encoding="utf-8").replace(
            "    def test_q2_count():\n",
            "    def test_q2_data():\n        assert open('data.txt').read() == '42'\n\n"
            "    def test_q2_count():\n",
        ),
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}

    result = subprocess.run(
        [sys.executable, "-m", "marimo_gradescope.selfcheck", str(notebook), str(data)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert json.loads(result.stdout)["q2"]["passed"] == 2, result.stderr

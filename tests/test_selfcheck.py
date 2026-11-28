import json
import subprocess
import sys
from pathlib import Path

from mogs import selfcheck
from mogs._env import clean_env
from mogs.config import read_config
from mogs.grader.bundle import GradingBundle, bundle_from_notebook
from mogs.runner import FailureReason, Outcome
from mogs.source import parse_notebook
from mogs.transform import public_test_files, student_notebook

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"


def student_copy(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return student_notebook(src, parse_notebook(src), read_config(src, path), "mogs")


def write_student(folder: Path, path: Path = SOLUTION, text: str | None = None) -> Path:
    """Write `text`, by default the student copy of `path`, as `hw.py` with its test files."""

    src = path.read_text(encoding="utf-8")
    notebook = folder / "hw.py"
    notebook.write_text(student_copy(path) if text is None else text, encoding="utf-8")

    for rel, tests in public_test_files(parse_notebook(src)).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(tests, encoding="utf-8")

    return notebook


def run(notebook: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = clean_env()
    return subprocess.run(
        [sys.executable, "-m", "mogs.selfcheck", str(notebook)],
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

    assert results["questions"] == {
        "q1": {"passed": 2, "total": 2, "failed": []},
        "q2": {"passed": 1, "total": 1, "failed": []},
    }
    assert results["unavailable"] == {}


def test_student_copy_fails_its_public_tests(tmp_path):
    notebook = write_student(tmp_path)

    results = selfcheck.public_results(notebook)

    assert results["questions"] == {
        "q1": {"passed": 0, "total": 2, "failed": ["test_q1_add", "test_q1_add_negative"]},
        "q2": {"passed": 0, "total": 1, "failed": ["test_q2_count"]},
    }


def test_solved_student_copy_passes_its_test_files(tmp_path):
    text = (
        student_copy(SOLUTION)
        .replace('Sum of two numbers."""\n    ...\n', 'Sum of two numbers."""\n    return a + b\n')
        .replace(
            'Number of vowels in `s`."""\n    ...\n',
            'Number of vowels in `s`."""\n    return sum(c in "aeiou" for c in s.lower())\n',
        )
    )
    notebook = write_student(tmp_path, text=text)

    results = selfcheck.public_results(notebook)

    assert results["questions"] == {
        "q1": {"passed": 2, "total": 2, "failed": []},
        "q2": {"passed": 1, "total": 1, "failed": []},
    }


def test_missing_test_file_fails_its_question(tmp_path):
    notebook = write_student(tmp_path)
    (tmp_path / "tests" / "q2.py").unlink()

    results = selfcheck.public_results(notebook)

    assert results["questions"]["q1"]["total"] == 2
    assert results["questions"]["q2"] == {"passed": 0, "total": 1, "failed": ["tests/q2.py"]}


def test_module_prints_json_last_despite_student_prints(tmp_path):
    text = student_copy(SOLUTION).replace(
        '    """Sum of two numbers."""\n    ...\n',
        '    """Sum of two numbers."""\n    print("{noise}")\n    return a + b\n',
    )
    notebook = write_student(tmp_path, text=text)

    result = run(notebook)
    last = result.stdout.strip().splitlines()[-1]

    assert result.returncode == 0, result.stderr
    assert json.loads(last)["questions"]["q1"] == {"passed": 2, "total": 2, "failed": []}
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

        assert json.loads(result.stdout)["questions"]["q2"]["passed"] == 2, result.stderr


def test_unparsable_notebook_exits_with_message(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text("def (:\n", encoding="utf-8")

    result = run(notebook)

    assert result.returncode != 0
    assert "not valid Python" in result.stderr
    assert result.stdout == ""


def test_leaves_nothing_next_to_the_notebook(tmp_path):
    notebook = write_student(tmp_path)

    run(notebook)

    written = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert written == ["hw.py", "tests", "tests/q1.py", "tests/q2.py"]


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
    env = clean_env()

    result = subprocess.run(
        [sys.executable, "-m", "mogs.selfcheck", str(notebook), str(data)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert json.loads(result.stdout)["questions"]["q2"]["passed"] == 2, result.stderr


def test_test_file_with_a_scope_error_fails_only_its_question(tmp_path):
    notebook = write_student(tmp_path)
    (tmp_path / "tests" / "q2.py").write_text(
        "def test_q2_bad():\n    nonlocal missing\n", encoding="utf-8"
    )

    results = selfcheck.public_results(notebook)

    assert results["questions"]["q1"]["total"] == 2
    assert results["questions"]["q2"] == {"passed": 0, "total": 1, "failed": ["tests/q2.py"]}


# The student moves `three` into a plain cell, so the grader has to run the whole notebook.
PLAIN_CELL_EDIT = (
    "@app.function\ndef three():\n    n = ...\n    return n\n",
    "@app.cell\ndef _():\n    def three():\n        return 3\n\n    return (three,)\n",
)


def test_names_a_failed_notebook_run_with_the_names_it_cost(tmp_path):
    # Issue #26: `three` is right, but an ungraded cell calling the blank `double` raises.
    source = FIXTURES / "plain_cell_answer.py"
    edited = student_copy(source).replace(*PLAIN_CELL_EDIT)
    notebook = write_student(tmp_path, source, edited)
    line = notebook.read_text(encoding="utf-8").splitlines().index("    demo = double(4) + 1") + 1

    results = selfcheck.public_results(notebook)

    assert results["questions"]["q1"] == {"passed": 0, "total": 1, "failed": ["test_q1_three"]}
    assert results["unavailable"] == {
        "three": f"running your notebook failed: line {line} raised TypeError: "
        "unsupported operand type(s) for +: 'ellipsis' and 'int'"
    }


# Every test of a solved copy, from the bundle, as `mogs build` checks it


def solved_with_bundle(folder: Path, src: str) -> tuple[Path, Path]:
    """Write the solved copy of the source `src` as `hw.py`, and its bundle as `bundle.json`."""

    nb = parse_notebook(src)
    cfg = read_config(src, folder / "source.py")
    notebook = folder / "hw.py"
    solved = student_notebook(src, nb, cfg, "mogs", solutions=True)
    notebook.write_text(solved, encoding="utf-8")
    bundle = folder / "bundle.json"
    bundle.write_text(bundle_from_notebook(nb, cfg).to_json(), encoding="utf-8")

    return notebook, bundle


def outcomes(notebook: Path, bundle: Path) -> dict[str, tuple[bool, str, FailureReason | None]]:
    loaded = GradingBundle.from_json(bundle.read_text(encoding="utf-8"))
    results = selfcheck.bundle_results(notebook, loaded)

    return {o.name: (o.passed, o.message, o.reason) for o in results}


def test_bundle_runs_public_and_hidden_tests_of_the_solved_copy(tmp_path):
    notebook, bundle = solved_with_bundle(tmp_path, SOLUTION.read_text(encoding="utf-8"))

    results = outcomes(notebook, bundle)

    assert list(results) == [
        "test_q1_add",
        "test_q1_add_negative",
        "test_q1_add_large",
        "test_q2_count",
        "test_q2_upper",
        "test_q2_empty",
    ]
    assert all(passed for passed, _, _ in results.values())


def test_bundle_names_a_failing_hidden_test(tmp_path):
    src = SOLUTION.read_text(encoding="utf-8").replace(
        "assert add(big(20), 1) == big(20) + 1", "assert add(big(20), 1) == big(20) + 2"
    )
    notebook, bundle = solved_with_bundle(tmp_path, src)

    results = outcomes(notebook, bundle)

    failed = {name: result for name, result in results.items() if not result[0]}
    assert list(failed) == ["test_q1_add_large"]
    assert failed["test_q1_add_large"][2] == FailureReason.ASSERTION


def test_bundle_runs_hidden_helpers(tmp_path):
    src = SOLUTION.read_text(encoding="utf-8").replace(
        "    # HIDDEN\n    def big(n):\n        return 10**n\n\n",
        "    # HIDDEN\n    def big(n):\n        return 10**n\n\n    return\n\n\n"
        "@app.cell\ndef _():\n    # HIDDEN\n",
    )
    notebook, bundle = solved_with_bundle(tmp_path, src)
    assert GradingBundle.from_json(bundle.read_text(encoding="utf-8")).helpers

    results = outcomes(notebook, bundle)

    assert results["test_q1_add_large"] == (True, "", None)


def test_bundle_fails_every_test_of_a_notebook_that_does_not_import(tmp_path):
    notebook, bundle = solved_with_bundle(tmp_path, SOLUTION.read_text(encoding="utf-8"))
    notebook.write_text("raise ValueError('broken')\n", encoding="utf-8")

    results = outcomes(notebook, bundle)

    assert len(results) == 6
    assert set(results.values()) == {
        (
            False,
            "your notebook failed to import (ValueError: broken)",
            FailureReason.LOAD_FAILED,
        )
    }


def test_module_with_a_bundle_prints_every_outcome_as_json(tmp_path):
    src = SOLUTION.read_text(encoding="utf-8").replace(
        'assert count_vowels("") == 0', 'assert count_vowels("") == 1'
    )
    notebook, bundle = solved_with_bundle(tmp_path, src)

    result = subprocess.run(
        [sys.executable, "-m", "mogs.selfcheck", "--bundle", str(bundle), str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=clean_env(),
    )
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    results = [Outcome.from_dict(outcome) for outcome in summary["outcomes"]]

    assert result.returncode == 0, result.stderr
    assert len(results) == 6
    assert [o.name for o in results if not o.passed] == ["test_q2_empty"]

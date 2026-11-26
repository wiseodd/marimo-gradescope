import html
import itertools
import linecache
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import marimo_gradescope
from marimo_gradescope import grade
from marimo_gradescope.check import question_title
from marimo_gradescope.config import ConfigError, read_config

FIXTURES = Path(__file__).parent / "fixtures"
TWO_CELL = FIXTURES / "two_cell_check.py"
COUNTER = itertools.count()


def text_of(panel) -> str:
    """Visible text of a panel: tags dropped, entities decoded."""

    return html.unescape(re.sub(r"<[^>]+>", "", panel.text))


def register(source: str) -> str:
    """Put `source` in `linecache` under a new name and return that name."""

    filename = f"<check-test-{next(COUNTER)}>"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    return filename


def define(source: str) -> dict[str, Any]:
    """Run source under a `linecache` name, so `inspect.getsource` finds it, as in marimo."""

    source = textwrap.dedent(source)
    filename = register(source)
    namespace: dict[str, Any] = {}
    exec(compile(source, filename, "exec"), namespace)
    return namespace


TESTS = define(
    """
    def add(a, b):
        return a - b

    def test_q1_add():
        assert add(1, 2) == 3

    def test_q1_zero():
        assert add(0, 0) == 0

    def test_q1_neg():
        assert add(-1, 1) == -2

    def test_q1_ok():
        assert True
    """
)


def pick(*names: str):
    return [TESTS[name] for name in names]


# Panel text


def test_header_counts_public_tests():
    panel = grade("q1", tests=pick("test_q1_add", "test_q1_zero", "test_q1_neg", "test_q1_ok"))

    assert "Question 1 — " not in text_of(panel)
    assert "Question 1 · 3/4 public tests passed" in text_of(panel)


def test_header_with_points():
    panel = grade("q1", tests=pick("test_q1_zero", "test_q1_add"), points=25)

    assert "Question 1 — 25 pts · 1/2 public tests passed" in text_of(panel)


def test_header_uses_the_given_title():
    panel = grade("q1", tests=pick("test_q1_zero"), points=4, title="Average <b>")

    assert "Average <b> — 4 pts · 1/1 public tests passed" in text_of(panel)
    assert "Question 1" not in text_of(panel)
    assert "Average &lt;b&gt;" in panel.text


def test_fractional_points():
    panel = grade("q1", tests=pick("test_q1_zero"), points=2.5)

    assert "Question 1 — 2.5 pts · 1/1 public tests passed" in text_of(panel)


def test_passed_and_failed_lines():
    panel = grade("q1", tests=pick("test_q1_zero", "test_q1_add"), points=25)
    text = text_of(panel)

    assert "✓ test_q1_zero" in text
    assert "✗ test_q1_add" in text
    assert text.index("✓ test_q1_zero") < text.index("✗ test_q1_add")


def test_failure_message_has_rewritten_assert():
    panel = grade("q1", tests=pick("test_q1_add"))
    text = text_of(panel)

    assert "assert add(1, 2) == 3" in text
    assert "assert -1 == 3" in text
    assert "where -1 = add(1, 2)" in text


def test_failure_message_indented_below_name():
    panel = grade("q1", tests=pick("test_q1_add"))
    failed = re.search(r"✗</span>\s*test_q1_add\s*(<pre[^>]*>)", panel.text)

    assert failed is not None
    assert "margin-left" in failed.group(1) or "padding-left" in failed.group(1)


def test_hidden_line_only_when_hidden():
    with_hidden = text_of(grade("q1", tests=pick("test_q1_ok"), hidden=4))
    without = text_of(grade("q1", tests=pick("test_q1_ok")))

    assert "+ 4 hidden tests run on Gradescope" in with_hidden
    assert "hidden" not in without


def test_no_tests_warning():
    text = text_of(grade("q1", tests=[], points=25))

    assert "No tests found for q1" in text
    assert "public tests passed" not in text


def test_other_question_ids_shown_as_is():
    text = text_of(grade("bonus", tests=pick("test_q1_ok")))

    assert "bonus · 1/1 public tests passed" in text


def test_names_and_messages_escaped():
    ns = define(
        """
        def test_q1_html():
            raise ValueError("<script>alert(1)</script>")
        """
    )

    panel = grade("q1", tests=[ns["test_q1_html"]])

    assert "<script>" not in panel.text
    assert "&lt;script&gt;" in panel.text


def test_no_hard_coded_text_colors():
    panel = grade("q1", tests=pick("test_q1_add", "test_q1_ok"), hidden=1)
    colors = re.findall(r"(?<![-\w])color:\s*([^;\"]+)", panel.text)

    assert colors
    assert all(c.strip() in {"inherit", "currentColor"} or "rgb" in c for c in colors), colors
    assert not re.search(r"#(000|fff)\b|\bblack\b|\bwhite\b(?!-)", panel.text, re.IGNORECASE)


def test_non_function_test_recorded_as_failure():
    panel = grade("q1", tests=[42])  # ty: ignore[invalid-argument-type] -- student code can pass anything

    assert "0/1 public tests passed" in text_of(panel)
    assert "TypeError" in text_of(panel)


def test_does_not_mutate_test_globals():
    ns = define(
        """
        def test_q1_a():
            assert 1 == 2
        """
    )
    original = ns["test_q1_a"]
    keys = set(ns)

    grade("q1", tests=[original])

    assert ns["test_q1_a"] is original
    assert set(ns) == keys


def test_closure_falls_back_to_direct_call():
    def make():
        expected = 3

        def test_q1_closure():
            assert 1 + 1 == expected

        return test_q1_closure

    text = text_of(grade("q1", tests=[make()]))

    assert "0/1 public tests passed" in text
    assert "AssertionError" in text
    assert "NameError" not in text


def test_lambda_runs_directly():
    text = text_of(grade("q1", tests=[lambda: None]))

    assert "1/1 public tests passed" in text


def test_lambda_line_not_run_again():
    ns = define(
        """
        calls = []
        test_q1_l = (calls.append(1), lambda: None)[1]
        """
    )

    grade("q1", tests=[ns["test_q1_l"]])

    assert ns["calls"] == [1]


def test_decorator_not_run_again():
    ns = define(
        """
        calls = []

        def mark(fn):
            calls.append(fn.__name__)
            return fn

        @mark
        def test_q1_marked():
            assert 1 == 2
        """
    )

    text = text_of(grade("q1", tests=[ns["test_q1_marked"]]))

    assert ns["calls"] == ["test_q1_marked"]
    assert "0/1 public tests passed" in text


def test_default_argument_not_run_again():
    ns = define(
        """
        calls = []

        def test_q1_default(x=calls.append(1)):
            assert 1 == 2
        """
    )

    grade("q1", tests=[ns["test_q1_default"]])

    assert ns["calls"] == [1]


def test_hostile_repr_gives_placeholder_name():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("no")

    text = text_of(grade("q1", tests=[Hostile()]))  # ty: ignore[invalid-argument-type] -- student code can pass anything

    assert "✗ <test>" in text


def test_mangled_private_names_run_original():
    # marimo compiles `_expected` in a cell as a mangled global, while the source it registers
    # (and `inspect.getsource` returns) keeps `_expected`.
    filename = register("def test_q1_private():\n    assert 1 + 2 == _expected\n")
    compiled = "def test_q1_private():\n    assert 1 + 2 == _cell_abc_expected\n"
    namespace: dict[str, Any] = {"_cell_abc_expected": 3}
    exec(compile(compiled, filename, "exec"), namespace)

    text = text_of(grade("q1", tests=[namespace["test_q1_private"]]))

    assert "1/1 public tests passed" in text
    assert "NameError" not in text


def test_matching_source_still_rewritten():
    ns = define(
        """
        _cell_abc_expected = 4

        def test_q1_private():
            assert 1 + 2 == _cell_abc_expected
        """
    )

    text = text_of(grade("q1", tests=[ns["test_q1_private"]]))

    assert "assert (1 + 2) == 4" in text


def test_hostile_repr_base_exception():
    class Hostile:
        def __repr__(self):
            raise SystemExit(1)

    text = text_of(grade("q1", tests=[Hostile()]))  # ty: ignore[invalid-argument-type] -- student code can pass anything

    assert "✗ <test>" in text


def test_long_names_capped():
    ns = define(
        """
        def test_q1_long():
            pass
        """
    )
    ns["test_q1_long"].__name__ = "x" * 5000

    text = text_of(grade("q1", tests=[ns["test_q1_long"]]))

    assert "x" * 5000 not in text
    assert "x" * 100 in text


@pytest.mark.parametrize(("points", "shown"), [(25, "25 pts"), (25.0, "25 pts"), (0.1, "0.1 pts")])
def test_points_formatting(points, shown):
    text = text_of(grade("q1", tests=[], points=points))

    assert f"Question 1 — {shown}" in text


# Discovery


def test_discovers_question_tests_from_caller_globals():
    ns = define(
        """
        from marimo_gradescope import grade

        def test_q1_a():
            assert 1 == 1

        def test_q1_b():
            assert 1 == 2

        def test_q10_c():
            assert False

        def test_q2_d():
            assert False

        test_q1_not_callable = 3

        panel = grade("q1")
        """
    )
    text = text_of(ns["panel"])

    assert "1/2 public tests passed" in text
    assert "test_q1_a" in text
    assert "test_q1_b" in text
    assert "test_q10_c" not in text
    assert "test_q2_d" not in text


def test_discovery_without_tests_warns():
    ns = define(
        """
        from marimo_gradescope import grade

        panel = grade("q1")
        """
    )

    assert "No tests found for q1" in text_of(ns["panel"])


# Package surface


def test_check_exported():
    assert marimo_gradescope.grade is grade


def test_import_stays_light():
    script = (
        "import sys, marimo_gradescope; print('_pytest' in sys.modules, 'marimo' in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False", "False"]


# In marimo


def outside_pytest() -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def export_html(notebook: Path) -> str:
    out = notebook.with_suffix(".html")
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
    return out.read_text(encoding="utf-8")


def test_exported_notebook_shows_panel(tmp_path):
    notebook = tmp_path / "two_cell_check.py"
    notebook.write_text(TWO_CELL.read_text(encoding="utf-8"), encoding="utf-8")

    page = export_html(notebook)

    assert "Question 1 \\u2014 25 pts \\u00b7 1/2 public tests passed" in page
    assert "assert -1 == 3" in page
    assert "+ 4 hidden tests run on Gradescope" in page


def test_editing_student_function_changes_panel(tmp_path):
    notebook = tmp_path / "two_cell_check.py"
    notebook.write_text(TWO_CELL.read_text(encoding="utf-8"), encoding="utf-8")
    assert "1/2 public tests passed" in export_html(notebook)

    notebook.write_text(
        TWO_CELL.read_text(encoding="utf-8").replace("return a - b", "return a + b"),
        encoding="utf-8",
    )
    page = export_html(notebook)

    assert "2/2 public tests passed" in page
    assert "assert -1 == 3" not in page


# Tests from a file


Q1_FILE = """\
def test_q1_add():
    assert add(1, 2) == 3

def test_q1_zero():
    assert add(0, 0) == 0
"""


def check_in(folder: Path, call: str, **names: object) -> Any:
    """Run `call` the way a notebook cell in `folder/hw.py` would, and return its value."""

    namespace: dict[str, Any] = {"__file__": str(folder / "hw.py"), "grade": grade, **names}
    exec(f"panel = {call}", namespace)
    return namespace["panel"]


def write_tests(folder: Path, text: str = Q1_FILE) -> None:
    (folder / "tests").mkdir(exist_ok=True)
    (folder / "tests" / "q1.py").write_text(text, encoding="utf-8")


def test_test_file_runs_against_the_notebook_names(tmp_path):
    write_tests(tmp_path)

    panel = check_in(
        tmp_path, 'grade("q1", test_file="tests/q1.py", uses=[add], points=2)', add=TESTS["add"]
    )
    text = text_of(panel)

    assert "Question 1 — 2 pts · 1/2 public tests passed" in text
    assert "✗ test_q1_add" in text
    assert "assert -1 == 3" in text
    assert "✓ test_q1_zero" in text


def test_test_file_leaves_the_kernel_globals_alone(tmp_path):
    write_tests(tmp_path)
    namespace: dict[str, Any] = {"__file__": str(tmp_path / "hw.py"), "add": TESTS["add"]}
    before = dict(namespace)

    exec(
        'import marimo_gradescope\nmarimo_gradescope.grade("q1", test_file="tests/q1.py")',
        namespace,
    )

    assert "test_q1_add" not in namespace
    assert {key: namespace[key] for key in before} == before


def test_test_file_is_found_next_to_the_notebook_not_the_working_directory(tmp_path, monkeypatch):
    write_tests(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    text = text_of(check_in(tmp_path, 'grade("q1", test_file="tests/q1.py")', add=TESTS["add"]))

    assert "1/2 public tests passed" in text


def test_missing_test_file_says_where_it_should_be(tmp_path):
    text = text_of(check_in(tmp_path, 'grade("q1", test_file="tests/q1.py")'))

    assert "Cannot find tests/q1.py next to hw.py" in text
    assert "public tests passed" not in text


def test_test_file_that_is_not_python_says_so(tmp_path):
    write_tests(tmp_path, "def (:\n")

    text = text_of(check_in(tmp_path, 'grade("q1", test_file="tests/q1.py")'))

    assert "tests/q1.py is not valid Python (line 1)" in text


def test_test_file_that_fails_to_load_fails_each_test(tmp_path):
    write_tests(tmp_path, "raise ValueError('boom')\n" + Q1_FILE)

    text = text_of(check_in(tmp_path, 'grade("q1", test_file="tests/q1.py")', add=TESTS["add"]))

    assert "0/2 public tests passed" in text
    assert text.count("the tests failed to load (ValueError: boom)") == 2


def test_test_file_that_fails_before_any_test_says_so(tmp_path):
    write_tests(tmp_path, "raise ValueError('boom')\n")

    text = text_of(check_in(tmp_path, 'grade("q1", test_file="tests/q1.py")'))

    assert "No tests found" not in text
    assert "✗ tests/q1.py" in text
    assert "the tests failed to load (ValueError: boom)" in text


def test_test_file_without_a_notebook_file_says_so():
    namespace: dict[str, Any] = {"grade": grade}
    exec('panel = grade("q1", test_file="tests/q1.py")', namespace)

    assert "Cannot find this notebook's file" in text_of(namespace["panel"])


def test_tests_and_test_file_together_raise(tmp_path):
    with pytest.raises(ValueError, match="test_file"):
        grade("q1", tests=[], test_file="tests/q1.py")


def test_check_is_a_no_op_while_grading(monkeypatch):
    monkeypatch.setenv("MGS_GRADING", "1")
    calls = []

    def test_q1_a():
        calls.append(1)

    result = grade("q1", tests=[test_q1_a], points=1)

    assert calls == []
    assert result.text == ""


@pytest.mark.parametrize("qid", ["q1", "q12", "q1a", "bonus", "Q1"])
def test_panel_title_matches_the_config_default(qid):
    src = f'import marimo_gradescope as mogs\nmogs.assignment()\nmogs.grade("{qid}", points=1)\n'
    cfg = read_config(src, Path("nb.py"))

    assert question_title(qid) == cfg.questions[qid].title


# Written questions


def test_written_panel_shows_title_and_points():
    panel = text_of(grade("q3", points=5, title="Empty lists", manual=True))

    assert "Empty lists — 5 pts" in panel
    assert "graded by hand" in panel


def test_written_panel_defaults_to_the_question_title():
    assert "Question 3 — 1 pt" in text_of(grade("q3", points=1, manual=True))


def test_written_panel_for_zero_points_says_it_is_not_graded():
    panel = text_of(grade("q3", points=0, manual=True))

    assert "pts" not in panel
    assert "not graded" in panel


def test_written_is_a_no_op_while_grading(monkeypatch):
    monkeypatch.setenv("MGS_GRADING", "1")

    assert grade("q3", points=5, manual=True).text == ""


WRITTEN_SOURCE = """\
import marimo_gradescope as mogs

mogs.assignment()


def test_q1_a():
    assert True


mogs.grade("q1", points=1)
mogs.grade("q2", points=5)
"""


@pytest.mark.parametrize(
    ("qid", "shown"), [("q2", "Written answer, graded by hand"), ("q1", "No tests found")]
)
def test_grade_in_the_source_finds_written_questions_in_the_saved_notebook(qid, shown, tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(WRITTEN_SOURCE, encoding="utf-8")
    namespace: dict[str, Any] = {"__file__": str(notebook), "grade": grade}

    exec(f'panel = grade("{qid}", points=5)', namespace)

    assert shown in text_of(namespace["panel"])


def test_grade_without_a_saved_notebook_shows_no_tests():
    namespace: dict[str, Any] = {"grade": grade}

    exec('panel = grade("q2", points=5)', namespace)

    assert "No tests found" in text_of(namespace["panel"])


def test_grade_in_the_source_names_a_config_error(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(WRITTEN_SOURCE.replace("points=5", 'points="five"'), encoding="utf-8")
    namespace: dict[str, Any] = {"__file__": str(notebook), "grade": grade}

    with pytest.raises(ConfigError, match="points"):
        exec('panel = grade("q2", points=5)', namespace)

import ast
import textwrap
from pathlib import Path

import pytest

from marimo_gradescope.config import AssignmentConfig, Question, read_config
from marimo_gradescope.source import (
    Cell,
    CellKind,
    SourceError,
    module_tests,
    parse_notebook,
    validate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "solution.py"

HEADER = """\
# /// script
# dependencies = ["marimo"]
#
# [tool.marimo-gradescope]
# questions = { q1 = 1 }
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///

import marimo

app = marimo.App()

with app.setup:
    import marimo_gradescope as mgs

"""

CONFIG = AssignmentConfig(name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1)})


def nb(cells: str, header: str = HEADER) -> str:
    return header + cells


def fixture():
    src = FIXTURE.read_text(encoding="utf-8")
    return parse_notebook(src), read_config(src, FIXTURE)


def errors(cells: str, config: AssignmentConfig = CONFIG, header: str = HEADER) -> list[str]:
    return validate(parse_notebook(nb(cells, header)), config)


def one_error(cells: str, config: AssignmentConfig = CONFIG, header: str = HEADER) -> str:
    found = errors(cells, config, header)
    assert len(found) == 1, found
    return found[0]


PUBLIC = """
@app.cell
def _():
    def test_q1_a():
        assert True
    return
"""

CHECK = """
@app.cell
def _():
    mgs.check("q1")
    return
"""


# parse_notebook


def test_parse_fixture_cells():
    notebook, _ = fixture()

    kinds = [cell.kind for cell in notebook.cells]

    assert kinds[0] is CellKind.SETUP
    assert kinds.count(CellKind.FUNCTION) == 2
    assert [cell.name for cell in notebook.cells if cell.kind is CellKind.FUNCTION] == [
        "add",
        "count_vowels",
    ]
    assert [cell.check_qid for cell in notebook.cells if cell.check_qid] == ["q1", "q2"]


def test_parse_cell_lines_include_decorators():
    src = nb(PUBLIC)
    lines = src.splitlines()

    cell = parse_notebook(src).cells[-1]

    assert lines[cell.start - 1] == "@app.cell"
    assert lines[cell.end - 1] == "    return"


def test_parse_setup_cell():
    src = nb("")
    lines = src.splitlines()

    (cell,) = parse_notebook(src).cells

    assert cell.kind is CellKind.SETUP
    assert cell.name == "setup"
    assert lines[cell.start - 1] == "with app.setup:"


def test_parse_setup_with_options():
    src = nb("", header=HEADER.replace("with app.setup:", "with app.setup(hide_code=True):"))

    (cell,) = parse_notebook(src).cells

    assert cell.kind is CellKind.SETUP


def test_parse_decorator_variants():
    src = nb(
        """
@app.cell(hide_code=True)
def _():
    return


@app.function(hide_code=True)
def f():
    return 1


@app.cell
async def _():
    return


@app.class_definition
class Point:
    x: int
"""
    )

    kinds = [cell.kind for cell in parse_notebook(src).cells[1:]]

    assert kinds == [CellKind.CELL, CellKind.FUNCTION, CellKind.CELL, CellKind.CLASS]


def test_parse_tests_and_hidden():
    notebook, _ = fixture()

    tests = [(cell.tests, cell.hidden) for cell in notebook.cells if cell.tests]

    assert tests == [
        (("test_q1_add", "test_q1_add_negative"), False),
        (("test_q1_add_large",), True),
        (("test_q2_count",), False),
        (("test_q2_upper", "test_q2_empty"), True),
    ]


def test_parse_function_cell_test():
    src = nb(
        """
@app.function
def test_q1_a():
    assert True
"""
    )

    cell = parse_notebook(src).cells[-1]

    assert cell.kind is CellKind.FUNCTION
    assert cell.tests == ("test_q1_a",)


def test_parse_hidden_needs_whole_line():
    src = nb(
        """
@app.cell
def _():
    x = 1  # HIDDEN
    return
"""
    )

    assert not parse_notebook(src).cells[-1].hidden


def test_parse_check_with_alias():
    src = nb(CHECK.replace("mgs.", "g."), header=HEADER.replace("as mgs", "as g"))

    assert parse_notebook(src).cells[-1].check_qid == "q1"


def test_parse_check_imported_function():
    src = nb(
        CHECK.replace("mgs.check", "check"),
        header=HEADER.replace(
            "import marimo_gradescope as mgs", "from marimo_gradescope import check"
        ),
    )

    assert parse_notebook(src).cells[-1].check_qid == "q1"


def test_parse_check_of_other_object_ignored():
    src = nb(CHECK.replace("mgs.", "other."))

    assert parse_notebook(src).cells[-1].check_qid is None


def test_parse_check_needs_literal_id():
    with pytest.raises(SourceError, match="string literal"):
        parse_notebook(nb(CHECK.replace('"q1"', "qid")))


def test_parse_two_checks_in_one_cell():
    cells = CHECK.replace('mgs.check("q1")', 'mgs.check("q1")\n    mgs.check("q2")')

    with pytest.raises(SourceError, match="one check"):
        parse_notebook(nb(cells))


def test_parse_syntax_error():
    with pytest.raises(SourceError, match="line"):
        parse_notebook(nb("def (:\n"))


# Notebook queries


def test_tests_by_question():
    notebook, _ = fixture()

    assert notebook.tests_by_question() == {
        "q1": (["test_q1_add", "test_q1_add_negative"], ["test_q1_add_large"]),
        "q2": (["test_q2_count"], ["test_q2_upper", "test_q2_empty"]),
    }


def test_student_names():
    notebook, _ = fixture()

    assert notebook.student_names() == {"add", "count_vowels"}


def test_student_names_from_cell_returns():
    src = nb(
        """
@app.cell
def _():
    a = 1
    b = 2
    return a, b


@app.cell
def _():
    c = 3
    return (c,)


@app.cell
def _():
    d = 4
    return d
"""
    )

    assert parse_notebook(src).student_names() == {"a", "b", "c", "d"}


def test_answer_and_prompt_cells():
    notebook, _ = fixture()

    assert [cell.name for cell in notebook.answer_cells("q3")] == ["answer_q3"]
    assert [cell.name for cell in notebook.prompt_cells("q3")] == ["prompt_q3"]
    assert notebook.answer_cells("q1") == ()


def test_answer_cells_with_suffix():
    src = nb(
        """
@app.cell
def answer_q1_a():
    return


@app.cell
def answer_q10():
    return
"""
    )

    names = [cell.name for cell in parse_notebook(src).answer_cells("q1")]

    assert names == ["answer_q1_a"]


def test_setup_source():
    notebook, _ = fixture()

    assert notebook.setup_source() == "import marimo as mo\n\nimport marimo_gradescope as mgs\n"


def test_setup_source_without_setup_cell():
    src = nb(
        PUBLIC, header=HEADER.replace("with app.setup:\n    import marimo_gradescope as mgs\n", "")
    )

    assert parse_notebook(src).setup_source() == ""


def test_cell_body_source_drops_return_and_dedents():
    notebook, _ = fixture()
    hidden = next(cell for cell in notebook.cells if cell.hidden)

    body = notebook.cell_body_source(hidden)

    assert body.startswith("# HIDDEN\ndef big(n):\n")
    assert "test_q1_add_large" in body
    assert "\nreturn\n" not in body
    assert not body.rstrip().endswith("return")
    ast.parse(body)


def test_cell_body_source_function_drops_decorator():
    notebook, _ = fixture()
    add = next(cell for cell in notebook.cells if cell.name == "add")

    body = notebook.cell_body_source(add)

    assert body.startswith("def add(a, b):\n")
    assert body.rstrip().endswith("# END SOLUTION")


def test_cell_body_source_multiline_signature():
    src = nb(
        """
@app.cell
def _(
    x,
):
    # comment
    y = x
    return (y,)
"""
    )
    notebook = parse_notebook(src)

    assert notebook.cell_body_source(notebook.cells[-1]) == "# comment\ny = x\n"


def test_cell_is_frozen():
    notebook, _ = fixture()

    with pytest.raises(AttributeError):
        notebook.cells[0].name = "x"


def test_cell_fields():
    cell = Cell(CellKind.CELL, "_", 1, 2, (), False, None)

    assert cell.check_qid is None


# validate


def test_validate_fixture_ok():
    notebook, config = fixture()

    assert validate(notebook, config) == []


def test_validate_minimal_ok():
    assert errors(PUBLIC + CHECK) == []


def test_validate_duplicate_test():
    error = one_error(PUBLIC + PUBLIC + CHECK)

    assert "test_q1_a" in error
    assert "more than once" in error


def test_validate_cell_mixing_questions():
    questions = {"q1": Question("q1", "Q1", 1), "q2": Question("q2", "Q2", 1)}
    config = AssignmentConfig(name="nb", notebook="nb.py", questions=questions)
    mixed = PUBLIC.replace("    return", "    def test_q2_b():\n        assert True\n    return")
    error = one_error(mixed + CHECK + CHECK.replace("q1", "q2"), config)

    assert "test_q1_a, test_q2_b" in error
    assert "own cell" in error


def test_validate_unknown_qid_in_test():
    error = one_error(PUBLIC + CHECK + PUBLIC.replace("q1", "q9"))

    assert "test_q9_a" in error
    assert "`q9`" in error


def test_validate_unknown_qid_in_check():
    error = one_error(PUBLIC + CHECK + CHECK.replace("q1", "q9"))

    assert "check" in error
    assert "`q9`" in error


def test_validate_auto_question_without_tests():
    assert "no tests" in one_error("")


def test_validate_hidden_only_needs_no_check():
    hidden = PUBLIC.replace("    def test", "    # HIDDEN\n    def test")

    assert errors(hidden) == []


def test_validate_public_tests_without_check():
    error = one_error(PUBLIC)

    assert "q1" in error
    assert 'mgs.check("q1")' in error


def test_validate_manual_with_tests():
    config = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )
    answer = """
@app.cell
def answer_q1():
    return
"""

    assert errors(answer, config) == []
    assert "manual" in one_error(answer + PUBLIC, config)


def test_validate_manual_without_answer():
    config = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )

    assert "answer_q1" in one_error("", config)


def test_validate_impure_public_cell():
    impure = PUBLIC.replace("    return", "    x = 1\n    return")

    error = one_error(impure + CHECK)

    assert "test_q1_a" in error
    assert "only" in error


def test_validate_public_cell_with_check():
    mixed = PUBLIC.replace("    return", '    mgs.check("q1")\n    return')

    assert "only" in one_error(mixed)


@pytest.mark.parametrize("statement", ["import math", "from os import path"])
def test_validate_public_cell_imports(statement):
    imports = PUBLIC.replace("    def test", f"    {statement}\n\n    def test")

    error = one_error(imports + CHECK)

    assert statement in error
    assert "setup cell" in error


def test_validate_hidden_cell_may_hold_helpers():
    hidden = """
@app.cell
def _():
    # HIDDEN
    k = 2

    def test_q1_h():
        assert k == 2
    return
"""

    assert errors(PUBLIC + CHECK + hidden) == []


def hidden_cell(body: str) -> str:
    lines = "\n".join(f"    {line}" if line else "" for line in body.strip().splitlines())
    return f"\n@app.cell\ndef _():\n    # HIDDEN\n{lines}\n    return\n"


ADD = """
@app.function
def add(a, b):
    return a + b
"""


def test_validate_hidden_cell_uses_name_of_a_test_cell():
    defines = hidden_cell(
        "def big(n):\n    return 10**n\n\ndef test_q1_h():\n    assert big(1) == 10"
    )
    uses = hidden_cell("def test_q1_g():\n    assert big(2) == 100")
    helper = hidden_cell("huge = big(9)")
    found = errors(PUBLIC + CHECK + defines + uses + helper)

    assert len(found) == 2, found
    assert all("`big`" in error and "test cell" in error for error in found)


def test_validate_hidden_helper_cycle():
    found = errors(PUBLIC + CHECK + hidden_cell("a = b + 1") + hidden_cell("b = a + 1"))

    assert len(found) == 1, found
    assert "cycle" in found[0]


def test_validate_hidden_cell_redefines_student_name():
    shadow = hidden_cell("def add(a, b):\n    return a + b")
    error = one_error(ADD + PUBLIC + CHECK + shadow)

    assert "hidden cell defines `add`" in error
    assert "student cell" in error


def test_validate_hidden_cell_redefines_setup_name():
    shadow = hidden_cell("mgs = None\n\ndef test_q1_h():\n    assert mgs is None")
    error = one_error(PUBLIC + CHECK + shadow)

    assert "hidden cell defines `mgs`" in error
    assert "setup cell" in error


def test_validate_hidden_cell_may_reuse_private_names():
    setup = HEADER.replace(
        "    import marimo_gradescope as mgs\n",
        "    import marimo_gradescope as mgs\n    _here = 1\n",
    )
    student = "\n@app.cell\ndef _():\n    _x = 1\n    return\n"
    hidden = hidden_cell("_here = 7\n_x = 2\n\ndef test_q1_h():\n    assert True")

    assert setup != HEADER
    assert errors(PUBLIC + CHECK + student + hidden, header=setup) == []


def test_validate_hidden_helpers_in_any_order_ok():
    later = hidden_cell("doubled = [x * 2 for x in base]") + hidden_cell("base = [add(1, 1)]")
    test = hidden_cell("def test_q1_h():\n    assert doubled == [4]")

    assert errors(ADD + PUBLIC + CHECK + later + test) == []


@pytest.mark.parametrize(
    ("body", "what"),
    [
        ("# BEGIN SOLUTION\n    x = 1\n    return", "BEGIN"),
        ("x = 1\n    # END SOLUTION\n    return", "END"),
        ("# BEGIN SOLUTION\n    # BEGIN SOLUTION\n    # END SOLUTION\n    return", "BEGIN"),
    ],
)
def test_validate_unbalanced_markers(body, what):
    cell = f"\n@app.cell\ndef _():\n    {body}\n"

    error = one_error(PUBLIC + CHECK + cell)

    assert what in error
    assert "line" in error


def test_validate_marker_region_across_cells():
    cells = """
@app.cell
def _():
    # BEGIN SOLUTION
    return


@app.cell
def _():
    # END SOLUTION
    return
"""

    assert "BEGIN" in one_error(PUBLIC + CHECK + cells)


def test_validate_balanced_markers_ok():
    cell = """
@app.function
def f():
    # BEGIN SOLUTION
    return 1
    # END SOLUTION
"""

    assert errors(PUBLIC + CHECK + cell) == []


def test_validate_missing_reactive_tests():
    header = HEADER.replace("#\n# [tool.marimo.runtime]\n# reactive_tests = false\n", "")

    error = one_error(PUBLIC + CHECK, header=header)

    assert "# [tool.marimo.runtime]\n# reactive_tests = false" in error


def test_validate_reactive_tests_true():
    header = HEADER.replace("reactive_tests = false", "reactive_tests = true")

    assert "reactive_tests" in one_error(PUBLIC + CHECK, header=header)


def test_cell_body_source_function_keeps_other_decorators():
    src = nb(
        """
@app.function
@functools.cache
def f():
    return 1
"""
    )
    notebook = parse_notebook(src)

    assert (
        notebook.cell_body_source(notebook.cells[-1])
        == "@functools.cache\ndef f():\n    return 1\n"
    )


def test_parse_misplaced_app_decorator():
    with pytest.raises(SourceError, match="app.cell"):
        parse_notebook(nb("\n@app.cell\nclass A:\n    pass\n"))


@pytest.mark.parametrize("separator", ["\x0c", "\x0b", "\x1c", "\x85", " "])
def test_parse_line_numbers_with_unicode_separators(separator):
    src = nb(f'\n@app.cell\ndef _():\n    s = "a{separator}b"\n    return\n' + PUBLIC)
    lines = src.split("\n")
    notebook = parse_notebook(src)
    special, test = notebook.cells[-2:]

    assert notebook.cell_body_source(special) == f's = "a{separator}b"\n'
    assert [lines[test.start - 1], lines[test.end - 1]] == ["@app.cell", "    return"]
    assert notebook.cell_body_source(test) == "def test_q1_a():\n    assert True\n"


def test_parse_line_numbers_with_crlf():
    src = nb(PUBLIC + CHECK).replace("\n", "\r\n")
    notebook = parse_notebook(src)

    assert notebook.cell_body_source(notebook.cells[-1]) == 'mgs.check("q1")\n'
    assert validate(notebook, CONFIG) == []


def test_validate_check_in_hidden_cell():
    hidden_check = """
@app.cell
def _():
    # HIDDEN
    def test_q1_b():
        assert True
    return

@app.cell
def _():
    # HIDDEN
    mgs.check("q1")
    return
"""

    error = one_error(PUBLIC + hidden_check)

    assert "check() for q1 is in a hidden cell" in error


@pytest.mark.parametrize(
    "comment",
    [
        "# SOLUTION: the answer",
        "# solution",
        "#SOLUTION",
        "# Solutions",
        "#  SOLUTION",
    ],
)
def test_validate_inline_marker_near_miss(comment):
    cell = f"""
@app.cell
def _():
    x = 42  {comment}
    return
"""

    error = one_error(PUBLIC + CHECK + cell)

    assert "line 32" in error
    assert "`# SOLUTION`" in error


@pytest.mark.parametrize(
    "comment", ["# begin solution", "#BEGIN SOLUTION", "# END  SOLUTION", "# end solution"]
)
def test_validate_region_marker_near_miss(comment):
    cell = f"""
@app.cell
def _():
    {comment}
    x = 42
    return
"""

    assert "looks like a solution marker" in one_error(PUBLIC + CHECK + cell)


@pytest.mark.parametrize("comment", ["# prompt: x = 1", "#PROMPT: x = 1", "# PROMPT x = 1"])
def test_validate_prompt_near_miss(comment):
    cell = f"""
@app.cell
def _():
    # BEGIN SOLUTION
    {comment}
    x = 42
    # END SOLUTION
    return
"""

    assert "`# PROMPT: <code>`" in one_error(PUBLIC + CHECK + cell)


def test_validate_prompt_like_comment_outside_regions_ok():
    cell = """
@app.cell
def _():
    # prompt template for the language model
    x = 42
    return
"""

    assert errors(PUBLIC + CHECK + cell) == []


@pytest.mark.parametrize("comment", ["# hidden tests", "#HIDDEN", "# Hidden", "#  HIDDEN"])
def test_validate_hidden_marker_near_miss(comment):
    cell = f"""
@app.cell
def _():
    {comment}
    def test_q1_secret():
        assert True
    return
"""

    error = one_error(PUBLIC + CHECK + cell)

    assert "line 32" in error
    assert "looks like a hidden marker" in error
    assert "`# HIDDEN`" in error


def test_validate_hidden_marker_after_code():
    cell = """
@app.cell
def _():
    x = 1  # HIDDEN
    return
"""

    assert "on its own line" in one_error(PUBLIC + CHECK + cell)


def test_validate_exact_markers_and_unrelated_comments_ok():
    cell = """
@app.cell
def _():
    # BEGIN SOLUTION
    # PROMPT: x = None
    x = 42
    # END SOLUTION
    y = 1  # SOLUTION
    # the resolution of the plot
    # the hiddenness of the layer
    return
"""

    assert errors(PUBLIC + CHECK + cell) == []


# Public test files


def test_module_tests_are_top_level_test_functions_in_order():
    source = (
        "def helper():\n    pass\n\ndef test_q1_b():\n    def test_inner():\n        pass\n\n"
        "async def test_q1_async():\n    pass\n\ndef test_q1_a():\n    pass\n"
    )

    assert module_tests(source) == ("test_q1_b", "test_q1_a")


def check_cell_of(code: str) -> tuple:
    nb = parse_notebook(HEADER + "import marimo\napp = marimo.App()\n" + code)
    [cell] = [cell for cell in nb.cells if cell.check_qid is not None]
    return nb, cell


def test_check_test_file_reads_the_keyword():
    nb, cell = check_cell_of(
        "with app.setup:\n    import marimo_gradescope as mgs\n\n@app.cell\ndef _():\n"
        '    mgs.check("q1", test_file="tests/q1.py", uses=[])\n    return\n'
    )

    assert nb.check_test_file(cell) == "tests/q1.py"


@pytest.mark.parametrize("arguments", ["", ", test_file=name", ", tests=[]"])
def test_check_test_file_is_none_without_a_literal(arguments):
    nb, cell = check_cell_of(
        "with app.setup:\n    import marimo_gradescope as mgs\n\n@app.cell\ndef _():\n"
        f'    mgs.check("q1"{arguments})\n    return\n'
    )

    assert nb.check_test_file(cell) is None


# Public tests go to students as written


@pytest.mark.parametrize(
    "body",
    [
        "    expected = 3  # SOLUTION\n",
        "    # BEGIN SOLUTION\n    expected = 3\n    # END SOLUTION\n",
    ],
)
def test_public_test_cell_cannot_hold_solution_markers(body):
    cell = f"""
@app.cell
def _():
    def test_q1_a():
{textwrap.indent(body, "    ")}        assert True
    return
"""

    error = one_error(cell + CHECK)

    assert "public test" in error
    assert "# HIDDEN" in error


def test_hidden_test_cell_may_hold_solution_markers():
    cell = """
@app.cell
def _():
    # HIDDEN
    def test_q1_b():
        expected = 3  # SOLUTION
        assert expected == 3
    return
"""

    assert errors(PUBLIC + cell + CHECK) == []

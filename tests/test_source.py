import ast
import textwrap
from pathlib import Path

import pytest

from mogs.config import AssignmentConfig, Question, read_config
from mogs.source import (
    Cell,
    CellKind,
    SourceError,
    cell_names,
    module_tests,
    parse_notebook,
    prompt_previews,
    source_warnings,
    validate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "solution.py"

HEADER = """\
# /// script
# dependencies = ["marimo"]
#
# [tool.uv]
# exclude-newer = "2026-01-01T00:00:00Z"
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///

import marimo

app = marimo.App()

with app.setup:
    import mogs

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
    mogs.grade("q1")
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
    assert [cell.grade_qid for cell in notebook.cells if cell.grade_qid] == ["q1", "q2", "q3"]


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
    src = nb(CHECK.replace("mogs.", "g."), header=HEADER.replace("import mogs", "import mogs as g"))

    assert parse_notebook(src).cells[-1].grade_qid == "q1"


def test_parse_check_imported_function():
    src = nb(
        CHECK.replace("mogs.grade", "grade"),
        header=HEADER.replace("import mogs", "from mogs import grade"),
    )

    assert parse_notebook(src).cells[-1].grade_qid == "q1"


def test_parse_check_of_other_object_ignored():
    src = nb(CHECK.replace("mogs.", "other."))

    assert parse_notebook(src).cells[-1].grade_qid is None


def test_parse_check_needs_literal_id():
    with pytest.raises(SourceError, match="string literal"):
        parse_notebook(nb(CHECK.replace('"q1"', "qid")))


def test_parse_two_checks_in_one_cell():
    cells = CHECK.replace('mogs.grade("q1")', 'mogs.grade("q1")\n    mogs.grade("q2")')

    with pytest.raises(SourceError, match="one grade"):
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


def test_written_cells_of_the_fixture():
    notebook, _ = fixture()

    prompts, answers = notebook.written_cells("q3")

    assert ["## Question 3" in notebook.cell_body_source(cell) for cell in prompts] == [True]
    assert ["<!-- BEGIN SOLUTION -->" in notebook.cell_body_source(cell) for cell in answers] == [
        True
    ]
    assert notebook.written_cells("q1") == ((), ())


def md(text: str) -> str:
    return f'\n@app.cell\ndef _():\n    mo.md(r"""\n    {text}\n    """)\n    return\n'


ANSWER_MD = md("<!-- BEGIN SOLUTION -->\n    Yes.\n    <!-- END SOLUTION -->")


def grade_cell(qid: str) -> str:
    return f'\n@app.cell\ndef _():\n    mogs.grade("{qid}", points=1)\n    return\n'


def bodies(notebook, cells) -> list[str]:
    return [notebook.cell_body_source(cell) for cell in cells]


def test_written_prompts_are_the_markdown_just_before_each_answer():
    notebook = parse_notebook(
        nb(
            md("Intro")
            + PUBLIC
            + grade_cell("q1")
            + md("Question 2")
            + md("More text")
            + ANSWER_MD
            + md("(b) A second part")
            + ANSWER_MD
            + grade_cell("q2")
        )
    )

    prompts, answers = notebook.written_cells("q2")

    assert bodies(notebook, prompts) == [
        'mo.md(r"""\nQuestion 2\n""")\n',
        'mo.md(r"""\nMore text\n""")\n',
        'mo.md(r"""\n(b) A second part\n""")\n',
    ]
    assert len(answers) == 2
    assert notebook.question_cells("q9") == ()


DEMO_CODE = "\n@app.cell\ndef _():\n    plot = [1, 2, 3]\n    return (plot,)\n"


def test_a_code_cell_ends_the_written_prompt():
    notebook = parse_notebook(
        nb(
            PUBLIC
            + grade_cell("q1")
            + md("A short demo")
            + DEMO_CODE
            + md("Question 2")
            + ANSWER_MD
            + grade_cell("q2")
        )
    )

    prompts, _ = notebook.written_cells("q2")

    assert bodies(notebook, prompts) == ['mo.md(r"""\nQuestion 2\n""")\n']


def test_cells_students_do_not_see_keep_the_written_prompt():
    hidden = "\n@app.cell\ndef _():\n    # HIDDEN\n    secret = 1\n    return (secret,)\n"
    instructor = "\n@app.cell\ndef instructor():\n    note = 2\n    return (note,)\n"
    tests = "\n@app.cell\ndef _():\n    def test_q2_x():\n        assert True\n    return\n"
    notebook = parse_notebook(
        nb(md("Question 2") + hidden + instructor + tests + ANSWER_MD + grade_cell("q2"))
    )

    prompts, answers = notebook.written_cells("q2")

    assert bodies(notebook, prompts) == ['mo.md(r"""\nQuestion 2\n""")\n']
    assert len(answers) == 1


def test_prompt_previews_are_the_first_line_of_each_prompt():
    long = "x" * 80
    dynamic = '\n@app.cell\ndef _():\n    mo.md(f"Question {n}")\n    return\n'
    notebook = parse_notebook(
        nb(
            md("\n    ## Question 2\n\n    Explain.")
            + ANSWER_MD
            + md(long)
            + ANSWER_MD
            + dynamic
            + ANSWER_MD
            + grade_cell("q2")
            + ANSWER_MD
            + grade_cell("q3")
        )
    )

    assert prompt_previews(notebook, "q2") == (
        "## Question 2",
        "x" * 59 + "…",
        'mo.md(f"Question {n}")',
    )
    assert prompt_previews(notebook, "q3") == ()


def test_prompt_previews_skip_empty_prompts():
    notebook = parse_notebook(nb(md("   ") + ANSWER_MD + grade_cell("q2")))

    assert notebook.written_cells("q2")[0]
    assert prompt_previews(notebook, "q2") == ()


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

    assert notebook.setup_source() == "import marimo as mo\n\nimport mogs\n"


def test_setup_source_without_setup_cell():
    src = nb(PUBLIC, header=HEADER.replace("with app.setup:\n    import mogs\n", ""))

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

    assert cell.grade_qid is None
    assert not cell.assignment
    assert not cell.instructor


def test_parse_grade_and_assignment_calls():
    notebook = parse_notebook(
        nb(
            """
@app.cell
def _():
    mogs.assignment(name="x")
    return


@app.cell
def _():
    mogs.grade("q2", points=1)
    return
"""
        )
    )
    assignment, graded = notebook.cells[1:]

    assert assignment.assignment
    assert assignment.grade_qid is None
    assert graded.grade_qid == "q2"
    assert not graded.assignment


def test_parse_grade_needs_literal_id():
    with pytest.raises(SourceError, match="string literal"):
        parse_notebook(nb("\n@app.cell\ndef _():\n    mogs.grade(qid)\n    return\n"))


@pytest.mark.parametrize(
    ("name", "instructor"),
    [("instructor", True), ("instructor_guide", True), ("instructors", False), ("_", False)],
)
def test_instructor_cells_go_by_name(name, instructor):
    notebook = parse_notebook(nb(f"\n@app.cell\ndef {name}():\n    return\n"))

    assert notebook.cells[-1].instructor is instructor


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


def test_validate_misnamed_test_next_to_good_ones_reports_only_the_name():
    misnamed = PUBLIC.replace(
        "    return", "    def test_row_count():\n        assert True\n    return"
    )
    error = one_error(misnamed + CHECK)

    assert "`test_row_count`" in error
    assert "no question `row`" in error
    assert "`test_<qid>_<name>`" in error


def test_validate_unknown_qid_in_grade():
    error = one_error(PUBLIC + CHECK + CHECK.replace("q1", "q9"))

    assert "grade" in error
    assert "`q9`" in error


def test_validate_auto_question_without_tests():
    assert "no tests" in one_error("")


def test_validate_hidden_only_needs_no_check():
    hidden = PUBLIC.replace("    def test", "    # HIDDEN\n    def test")

    assert errors(hidden) == []


def test_validate_public_tests_without_grade():
    error = one_error(PUBLIC)

    assert "q1" in error
    assert 'mogs.grade("q1", points=...)' in error


def test_validate_manual_with_tests():
    config = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )
    answer = ANSWER_MD + grade_cell("q1")

    assert errors(answer, config) == []
    assert "manual" in one_error(PUBLIC + answer, config)


def test_validate_manual_without_answer():
    config = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )

    assert "no written answer" in one_error("", config)


def test_validate_impure_public_cell():
    impure = PUBLIC.replace("    return", "    x = 1\n    return")

    error = one_error(impure + CHECK)

    assert "test_q1_a" in error
    assert "only" in error


def test_validate_public_cell_with_check():
    mixed = PUBLIC.replace("    return", '    mogs.grade("q1")\n    return')

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
    shadow = hidden_cell("mogs = None\n\ndef test_q1_h():\n    assert mogs is None")
    error = one_error(PUBLIC + CHECK + shadow)

    assert "hidden cell defines `mogs`" in error
    assert "setup cell" in error


def test_validate_hidden_cell_may_reuse_private_names():
    setup = HEADER.replace(
        "    import mogs\n",
        "    import mogs\n    _here = 1\n",
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


@pytest.mark.parametrize("runtime", ["", "#\n# [tool.marimo.runtime]\n# reactive_tests = true\n"])
def test_validate_needs_no_runtime_flag(runtime):
    header = HEADER.replace("#\n# [tool.marimo.runtime]\n# reactive_tests = false\n", runtime)

    assert errors(PUBLIC + CHECK, header=header) == []


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

    assert notebook.cell_body_source(notebook.cells[-1]) == 'mogs.grade("q1")\n'
    assert validate(notebook, CONFIG) == []


ASSIGNMENT = """
@app.cell
def _():
    mogs.assignment(name="x")
    return
"""


def test_validate_assignment_cell_ok():
    assert errors(ASSIGNMENT + PUBLIC + CHECK) == []


@pytest.mark.parametrize(
    "cell",
    [
        '\n@app.cell\ndef _():\n    mogs.assignment(name="x")\n    x = 1\n    return (x,)\n',
        '\n@app.cell\ndef _():\n    # HIDDEN\n    mogs.assignment(name="x")\n    return\n',
        '\n@app.cell\ndef _():\n    mogs.grade("q1", points=1)\n    mogs.assignment()\n'
        "    return\n",
    ],
)
def test_validate_assignment_needs_a_cell_of_its_own(cell):
    error = one_error(cell + PUBLIC + CHECK.replace("q1", "q1"))

    assert "mogs.assignment" in error
    assert "own" in error


def test_validate_written_in_hidden_cell():
    config = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )
    cells = ANSWER_MD + grade_cell("q1").replace("    mogs", "    # HIDDEN\n    mogs")

    assert "grade() for q1 is in a hidden cell" in one_error(cells, config)


INSTRUCTOR = """
@app.cell(hide_code=True)
def instructor_guide():
    mo.md("Notes")
    return
"""


def test_validate_instructor_cell_ok():
    assert errors(INSTRUCTOR + ASSIGNMENT + PUBLIC + CHECK) == []


@pytest.mark.parametrize(
    ("body", "what"),
    [
        ("    x = 1\n    return (x,)\n", "x"),
        ('    mogs.grade("q1", points=1)\n    return\n', "grade"),
        ("    def test_q1_b():\n        assert True\n    return\n", "test_q1_b"),
        ("    # HIDDEN\n    return\n", "HIDDEN"),
    ],
)
def test_validate_instructor_cell_stays_out_of_the_assignment(body, what):
    cell = f"\n@app.cell\ndef instructor_notes():\n{body}"

    found = [error for error in errors(cell + PUBLIC + CHECK) if "instructor_notes" in error]

    assert len(found) == 1, found
    assert what in found[0]


def test_validate_instructor_function_cell():
    cell = "\n@app.function\ndef instructor_helper():\n    return 1\n"

    assert "instructor_helper" in one_error(cell + PUBLIC + CHECK)


def test_validate_unknown_qid_names_the_declaration():
    error = one_error(PUBLIC + CHECK + PUBLIC.replace("q1", "q9"))

    assert 'mogs.grade("q9", points=' in error


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
    mogs.grade("q1")
    return
"""

    error = one_error(PUBLIC + hidden_check)

    assert "grade() for q1 is in a hidden cell" in error


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
    [cell] = [cell for cell in nb.cells if cell.grade_qid is not None]
    return nb, cell


def test_grade_test_file_reads_the_keyword():
    nb, cell = check_cell_of(
        "with app.setup:\n    import mogs\n\n@app.cell\ndef _():\n"
        '    mogs.grade("q1", test_file="tests/q1.py", uses=[])\n    return\n'
    )

    assert nb.grade_test_file(cell) == "tests/q1.py"


@pytest.mark.parametrize("arguments", ["", ", test_file=name", ", tests=[]"])
def test_grade_test_file_is_none_without_a_literal(arguments):
    nb, cell = check_cell_of(
        "with app.setup:\n    import mogs\n\n@app.cell\ndef _():\n"
        f'    mogs.grade("q1"{arguments})\n    return\n'
    )

    assert nb.grade_test_file(cell) is None


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


# Markers in Markdown cells


def test_markdown_markers_are_html_comments():
    assert errors(ANSWER_MD) == errors("")


@pytest.mark.parametrize(
    "line", ["# BEGIN SOLUTION", "# END SOLUTION", "# PROMPT: Your answer", "# SOLUTION"]
)
def test_hash_markers_in_markdown_are_refused(line):
    found = [error for error in errors(md(line)) if "Markdown" in error]

    assert len(found) == 1, found
    assert "<!--" in found[0]


@pytest.mark.parametrize(
    "line",
    [
        "<!-- BEGIN SOLUTON -->",
        "<!--BEGIN SOLUTION-->",
        "<!-- begin solution -->",
        "<!-- PROMT -->",
    ],
)
def test_html_marker_near_miss_in_markdown(line):
    found = [error for error in errors(md(line)) if "looks like" in error]

    assert len(found) == 1, found
    assert line in found[0]


def test_html_markers_pair_up_in_markdown():
    assert any("without a matching" in error for error in errors(md("<!-- BEGIN SOLUTION -->")))


def test_html_markers_in_inline_text_are_ordinary_text():
    code = '\n@app.cell\ndef _():\n    x = "<!-- BEGIN SOLUTION -->"\n    return\n'

    assert errors(code) == errors("")


@pytest.mark.parametrize("prefix", ["f", "rf", ""])
def test_html_marker_outside_a_plain_markdown_cell_is_refused(prefix):
    # The transform only reads markers in plain `mo.md("...")` cells; elsewhere the solution
    # would reach students.
    call = "mo.md" if prefix else "print"
    cell = (
        f'\n@app.cell\ndef _():\n    {call}({prefix}"""\n    <!-- BEGIN SOLUTION -->\n'
        '    {x}\n    <!-- END SOLUTION -->\n    """)\n    return\n'
    )

    found = [error for error in errors(cell) if "plain string" in error]

    assert len(found) == 2, found


def test_written_prompts_include_markdown_that_is_not_a_plain_string():
    dynamic = '\n@app.cell\ndef _():\n    mo.md(f"Question {n}")\n    return\n'
    notebook = parse_notebook(nb(dynamic + ANSWER_MD + grade_cell("q2")))

    prompts, answers = notebook.written_cells("q2")

    assert len(prompts) == 1
    assert len(answers) == 1


@pytest.mark.parametrize(
    "body",
    [
        '    mo.vstack([mogs.assignment(name="x"), mo.md("Read me")])\n    return\n',
        '    print(mogs.assignment(name="x"))\n    return\n',
    ],
)
def test_validate_assignment_must_be_the_cell_s_expression(body):
    error = one_error(f"\n@app.cell\ndef _():\n{body}" + PUBLIC + CHECK)

    assert "mogs.assignment" in error


def test_written_answer_is_found_by_its_html_marker():
    notebook = parse_notebook(nb(md("Question") + ANSWER_MD + grade_cell("q2")))

    prompts, answers = notebook.written_cells("q2")

    assert len(prompts) == 1
    assert len(answers) == 1


def line_of(cells: str, text: str) -> int:
    return nb(cells).splitlines().index(text) + 1


@pytest.mark.parametrize(
    "comment",
    [
        "# SOULTION",
        "# SOLUTON",
        "# SOLUTIONN",
        "# SLOUTOIN",
        "#SOULTION",
        "# soultion",
        "# Solutoin",
        "# solutions below",
    ],
)
def test_validate_inline_marker_typo(comment):
    cell = f"\n@app.cell\ndef _():\n    n = 3  {comment}\n    return (n,)\n"

    error = one_error(PUBLIC + CHECK + cell)

    assert f"line {line_of(PUBLIC + CHECK + cell, f'    n = 3  {comment}')}:" in error
    assert "looks like a solution marker" in error


@pytest.mark.parametrize(
    "comment",
    [
        "# BEGIN SOULTION",
        "# BEGN SOLUTION",
        "# BEIGN SOLUTION",
        "# BEGINSOLUTION",
        "#BEGIN SOLUTON",
        "# begin soultion",
    ],
)
def test_validate_begin_marker_typo(comment):
    cell = f"\n@app.cell\ndef _():\n    {comment}\n    x = 42\n    # END SOLUTION\n    return\n"

    error = errors(PUBLIC + CHECK + cell)[0]

    assert f"line {line_of(PUBLIC + CHECK + cell, f'    {comment}')}:" in error
    assert "looks like a solution marker" in error


@pytest.mark.parametrize(
    "comment",
    ["# END SOULTION", "# EDN SOLUTION", "# ENDSOLUTION", "# end solutoin", "#END SOLUTON"],
)
def test_validate_end_marker_typo_is_named_first(comment):
    cells = (
        PUBLIC
        + CHECK
        + f"\n@app.cell\ndef _():\n    # BEGIN SOLUTION\n    x = 42\n    {comment}\n    return\n"
        + "\n@app.cell\ndef _():\n    # BEGIN SOLUTION\n    y = 1\n    # END SOLUTION\n    return\n"
    )

    error = errors(cells)[0]

    assert f"line {line_of(cells, f'    {comment}')}:" in error
    assert "looks like a solution marker" in error


@pytest.mark.parametrize(
    "comment",
    [
        "# solve for x",
        "# resolution of the grid",
        "# Evolution of the state",
        "# POLLUTION data",
        "# SECTION 2",
        "# POSITION of the robot",
        "# end of the loop",
        "# begin training",
        "# END RESOLUTION",
        "# and then the solver",
    ],
)
def test_validate_solution_like_comment_ok(comment):
    cell = f"\n@app.cell\ndef _():\n    x = 42  {comment}\n    return\n"

    assert errors(PUBLIC + CHECK + cell) == []


@pytest.mark.parametrize(
    "line",
    [
        "<!-- BEGIN SOULTION -->",
        "<!-- BEGN SOLUTION -->",
        "<!--SOULTION-->",
        "<!-- END SLOUTION -->",
    ],
)
def test_html_marker_typo_in_markdown(line):
    found = [error for error in errors(md(line)) if "looks like" in error]

    assert len(found) == 1, found
    assert line in found[0]


def test_html_end_marker_typo_is_named_first():
    cells = md("<!-- BEGIN SOLUTION -->\n    Yes.\n    <!-- END SOULTION -->") + ANSWER_MD

    error = errors(cells)[0]

    assert f"line {line_of(cells, '    <!-- END SOULTION -->')}:" in error
    assert "looks like a solution marker" in error


@pytest.mark.parametrize("line", ["<!-- resolution notes -->", "<!-- evolution -->"])
def test_html_solution_like_comment_ok(line):
    assert errors(md(line)) == errors("")


@pytest.mark.parametrize(
    "line", ["# Solution approach", "## Solutions to exercises", "# Soltuion sketch"]
)
def test_markdown_heading_like_solution_ok(line):
    assert errors(md(line)) == errors("")


# Tests that read cell variables


DATA_SETUP = HEADER.replace(
    "    import mogs\n",
    "    import mogs\n    prices = [1.0, None, None, 4.0, None]\n",
)

VARIABLE = """
@app.cell
def _():
    n_missing = sum(p is None for p in prices)  # SOLUTION
    return (n_missing,)
"""

VARIABLE_TEST = """
@app.cell
def _():
    def test_q1_count():
        assert n_missing == 3
    return
"""

FUNCTION = """
@app.function
def count_missing(values):
    return sum(v is None for v in values)  # SOLUTION
"""

FUNCTION_TEST = """
@app.cell
def _():
    def test_q1_count():
        assert count_missing(prices) == 3
    return
"""


def line_containing(src: str, text: str) -> int:
    return next(number for number, line in enumerate(src.splitlines(), 1) if text in line)


def test_validate_test_reading_a_cell_variable():
    cells = VARIABLE + VARIABLE_TEST + CHECK
    src = nb(cells, DATA_SETUP)
    defined = line_containing(src, "n_missing = sum(")
    test = line_containing(src, "def test_q1_count")

    error = one_error(cells, header=DATA_SETUP)

    assert error == (
        f"Question `q1`: `test_q1_count` (line {test}) reads `n_missing`, which a plain "
        f"`@app.cell` defines at line {defined}. The grader imports the functions the tests "
        "use without running the notebook, so it can't get that name. Write the answer as an "
        "`@app.function` that the test calls, and put data the tests need in the setup cell "
        "(`with app.setup:`) or load it in the test."
    )


PLAIN_FUNCTION = """
@app.cell
def _():
    def count_missing(values):
        return sum(v is None for v in values)
    return (count_missing,)
"""


def test_validate_test_reading_a_function_of_a_plain_cell():
    cells = PLAIN_FUNCTION + FUNCTION_TEST + CHECK

    error = one_error(cells, header=DATA_SETUP)

    assert "`test_q1_count`" in error
    assert "reads `count_missing`" in error
    assert f"line {line_containing(nb(cells, DATA_SETUP), 'def count_missing')}" in error


def test_validate_hidden_test_reading_a_cell_variable():
    hidden = hidden_cell("def test_q1_h():\n    assert n_missing == 3")

    error = one_error(VARIABLE + PUBLIC + CHECK + hidden, header=DATA_SETUP)

    assert error.startswith("Question `q1`: `test_q1_h` (line ")
    assert "reads `n_missing`" in error


def test_validate_hidden_helper_reading_a_cell_variable():
    helper = hidden_cell("expected = n_missing + 1")
    test = hidden_cell("def test_q1_h():\n    assert expected == 4")

    error = one_error(VARIABLE + PUBLIC + CHECK + helper + test, header=DATA_SETUP)

    assert error.startswith("The `# HIDDEN` cell at line ")
    assert "reads `n_missing`" in error


def test_validate_reports_every_cell_variable_read():
    other = """
@app.cell
def _():
    total = 5
    return (total,)
"""
    tests = """
@app.cell
def _():
    def test_q1_a():
        assert n_missing == 3

    def test_q1_b():
        assert total == 5 and n_missing == 3
    return
"""

    found = errors(VARIABLE + other + tests + CHECK, header=DATA_SETUP)

    assert [error.split(" (line")[0] for error in found] == [
        "Question `q1`: `test_q1_a`",
        "Question `q1`: `test_q1_b`",
        "Question `q1`: `test_q1_b`",
    ]
    assert ["reads `n_missing`" in found[0], "reads `n_missing`" in found[1]] == [True, True]
    assert "reads `total`" in found[2]


def test_validate_test_calling_an_app_function_ok():
    assert errors(FUNCTION + FUNCTION_TEST + CHECK, header=DATA_SETUP) == []


def test_validate_test_reading_setup_data_ok():
    test = PUBLIC.replace("assert True", "assert len(prices) == 5")

    assert errors(test + CHECK, header=DATA_SETUP) == []


def test_validate_test_reading_helper_cell_names_ok():
    helper = hidden_cell("expected = 3")
    test = hidden_cell("def test_q1_h():\n    assert count_missing(prices) == expected")

    assert errors(FUNCTION + PUBLIC + CHECK + helper + test, header=DATA_SETUP) == []


def test_validate_test_reading_its_own_cell_names_ok():
    hidden = hidden_cell("n_missing = 3\n\ndef test_q1_h():\n    assert n_missing == 3")

    assert errors(PUBLIC + CHECK + hidden) == []


# Warnings


LATE_HIDDEN = """
@app.cell
def _():
    def test_q1_a():
        assert True

    # HIDDEN
    def test_q1_b():
        assert True

    return
"""


def test_hidden_after_code_warns_that_it_hides_the_whole_cell():
    [warning] = source_warnings(parse_notebook(nb(LATE_HIDDEN)))

    line = nb(LATE_HIDDEN).splitlines().index("    # HIDDEN") + 1
    assert f"line {line}" in warning
    assert "whole cell" in warning


@pytest.mark.parametrize(
    "cell",
    [
        "\n@app.cell\ndef _():\n    # HIDDEN\n    def test_q1_a():\n        assert True\n",
        "\n@app.function\n# HIDDEN\ndef test_q1_a():\n    assert True\n",
        "\n@app.function\ndef test_q1_a():\n    # HIDDEN\n    assert True\n",
    ],
)
def test_hidden_on_the_first_line_does_not_warn(cell):
    assert source_warnings(parse_notebook(nb(cell))) == []


def test_tests_next_to_other_code_warn_that_pytest_skips_them():
    cell = hidden_cell("expected = 7\n\ndef test_q1_h():\n    assert expected == 7")

    [warning] = source_warnings(parse_notebook(nb(PUBLIC + CHECK + cell)))

    assert "test_q1_h" in warning
    assert "pytest" in warning


def test_cell_of_only_tests_does_not_warn():
    cell = hidden_cell("def test_q1_h():\n    assert True\n\nclass TestMore:\n    pass")

    assert source_warnings(parse_notebook(nb(PUBLIC + CHECK + cell))) == []


@pytest.mark.parametrize(
    "statement", ["import pytest", "import pytest as pt", "from pytest import approx"]
)
def test_validate_pytest_import_needs_the_dependency(statement):
    cell = f"\n@app.cell\ndef _():\n    {statement}\n    return\n"

    error = one_error(cell + PUBLIC + CHECK)

    assert "`pytest`" in error
    assert "`dependencies`" in error


BAD_HEADER = HEADER.replace('dependencies = ["marimo"]', "dependencies = [")


def test_validate_does_not_read_a_broken_header_without_pytest_imports():
    assert errors(PUBLIC + CHECK, header=BAD_HEADER) == []


def test_validate_reports_a_broken_header_when_pytest_is_imported():
    cell = "\n@app.cell\ndef _():\n    import pytest\n    return\n"

    error = one_error(cell + PUBLIC + CHECK, header=BAD_HEADER)

    assert "header" in error


def test_validate_pytest_import_with_the_dependency_ok():
    header = HEADER.replace('dependencies = ["marimo"]', 'dependencies = ["marimo", "pytest>=8"]')
    cell = "\n@app.cell\ndef _():\n    import pytest\n    return\n"

    assert errors(cell + PUBLIC + CHECK, header=header) == []


# cell_names: `:=` in a comprehension binds in the scope that holds the comprehension (PEP 572)

WALRUS_TOP = [
    "[(w := v) for v in data]",
    "[w for v in data if (w := v)]",
    "[[(w := v) for v in r] for r in data]",
    "y = list((w := v) for v in data)",
    "y = {k: (w := k) for k in d}",
    "@deco([(w := v) for v in data])\ndef f():\n    pass",
    "def f(a=[(w := v) for v in data]):\n    pass",
    "class C(*[(w := v) for v in data]):\n    pass",
    # Annotations count as run, as up to Python 3.13 (the default grading Python).
    "def f(a: [(w := v) for v in data]):\n    pass",
    "def f(*a: [(w := v) for v in data]):\n    pass",
    "def f() -> [(w := v) for v in data]:\n    pass",
    "x: [(w := v) for v in data] = 1",
]


@pytest.mark.parametrize("source", WALRUS_TOP)
def test_cell_names_walrus_in_comprehension_binds_at_top_level(source):
    bound, read = cell_names(source)

    assert "w" in bound
    assert "w" not in read
    assert "v" not in read


WALRUS_INNER = [
    "def f():\n    return [(w := v) for v in data]",
    "fs = [lambda: (w := v) for v in data]",
    "class C:\n    def f(self):\n        return [(w := v) for v in data]",
    # With postponed annotations, they never run.
    "from __future__ import annotations\ndef f(a: [(w := v) for v in data]):\n    pass",
    "from __future__ import annotations\nx: [(w := v) for v in data] = 1",
]


@pytest.mark.parametrize("source", WALRUS_INNER)
def test_cell_names_walrus_in_an_inner_scope_stays_there(source):
    bound, read = cell_names(source)

    assert "w" not in bound
    assert "w" not in read


def test_validate_hidden_cell_redefines_student_name_with_walrus():
    shadow = hidden_cell("_ = [(add := v) for v in [1]]")
    error = one_error(ADD + PUBLIC + CHECK + shadow)

    assert "hidden cell defines `add`" in error

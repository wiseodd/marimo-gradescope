import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from mogs import __version__
from mogs.config import (
    AssignmentConfig,
    Question,
    read_config,
    read_header,
)
from mogs.source import SourceError, parse_notebook
from mogs.transform import (
    export_cells,
    pinned_requirement,
    public_test_files,
    python_literal,
    solution_leaks,
    student_notebook,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"
PURE = FIXTURES / "pure_test_cells.py"
REQUIREMENT = "mogs==9.9.9"
ANSWER = "Because the order of the terms does not change the sum."

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

CONFIG = AssignmentConfig(name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 2.5)})
# The pure fixture has no config calls, so that it runs without this package.
PURE_CONFIG = AssignmentConfig(
    name="Pure test cells",
    notebook=PURE.name,
    questions={"q1": Question("q1", "Question 1", 2), "q2": Question("q2", "Question 2", 3)},
)


def config_of(src: str, path: Path) -> AssignmentConfig:
    return PURE_CONFIG if path == PURE else read_config(src, path)


def outside_pytest() -> dict[str, str]:
    """Environment without the variable that makes marimo act as if a test were running."""

    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def student(path: Path, *, solutions: bool = False) -> str:
    src = path.read_text(encoding="utf-8")
    cfg = config_of(src, path)
    return student_notebook(src, parse_notebook(src), cfg, REQUIREMENT, solutions=solutions)


def student_of(cells: str, config: AssignmentConfig = CONFIG) -> str:
    src = HEADER + cells
    return student_notebook(src, parse_notebook(src), config, REQUIREMENT)


def body(text: str) -> str:
    """Text after the header."""

    return text.split("# ///\n", 1)[1]


# Solutions


def test_solutions_gone():
    out = student(SOLUTION)

    assert "return a + b" not in out
    assert "char in" not in out
    assert "SOLUTION" not in out


def test_solution_region_becomes_ellipsis_with_indent():
    out = student(SOLUTION)

    assert '    """Sum of two numbers."""\n    ...\n' in out


def test_prompt_lines_replace_region():
    out = student(PURE)

    assert '    """Sum of two numbers."""\n    total = None\n    return total\n\n' in out
    assert "PROMPT" not in out


def test_prompt_keeps_extra_indent_and_blank_lines():
    out = student_of("""
@app.function
def f(x):
    # BEGIN SOLUTION
    # PROMPT: if x:
    # PROMPT:     pass
    # PROMPT:
    # PROMPT: return x
    return 2 * x
    # END SOLUTION
""")

    assert "def f(x):\n    if x:\n        pass\n\n    return x\n" in out


def test_inline_solution_assignment():
    out = student(PURE)

    assert "    factor = ...\n    return factor * x\n" in out


def test_inline_solution_variants():
    out = student_of("""
@app.cell
def _():
    a: int = 1  # SOLUTION
    b = [
        1,
        2,
    ]  # SOLUTION
    print(a)  # SOLUTION
    c = 3  # not a solution
    return
""")

    assert "    a: int = ...\n    b = ...\n    c = 3  # not a solution\n    return\n" in out
    assert "print" not in out


@pytest.mark.parametrize(
    "assignment",
    [
        "    x = (\n        1\n        + 2\n    )  # SOLUTION\n",
        "    x = (1 +\n        2\n    )  # SOLUTION\n",
        "    x: int = (\n        1\n    )  # SOLUTION\n",
        '    x = (\n        "a"\n        "b"\n    )  # SOLUTION\n',
    ],
)
def test_inline_solution_parenthesized_multiline(assignment):
    out = student_of(f"""
@app.cell
def _():
{assignment}    return
""")

    assert "def _():\n    x" in out
    assert out.split("def _():\n")[1].startswith(
        ("    x = ...\n    return\n", "    x: int = ...\n    return\n")
    )


def test_inline_solution_return_and_yield_keep_the_keyword():
    out = student_of("""
@app.function
def area(r):
    return 3.14 * r * r  # SOLUTION


@app.function
def squares(n):
    yield from range(n)  # SOLUTION


@app.function
def evens(n):
    yield 2 * n  # SOLUTION
""")

    assert "def area(r):\n    return ...\n" in out
    assert "def squares(n):\n    yield from ...\n" in out
    assert "def evens(n):\n    yield ...\n" in out
    ast.parse(out)


def test_inline_solution_bare_return_and_yield_are_dropped():
    out = student_of("""
@app.function
def f(n):
    print(n)
    yield  # SOLUTION
    return  # SOLUTION
""")

    assert "def f(n):\n    print(n)\n\n" in out


def test_inline_solution_augmented_assignment_is_dropped():
    out = student_of("""
@app.cell
def _():
    x = 1
    x += 2  # SOLUTION
    return
""")

    assert "    x = 1\n    return\n" in out


def test_inline_solution_chained_and_tuple_targets():
    out = student_of("""
@app.cell
def _():
    a = b = 1  # SOLUTION
    c, d = 1, 2  # SOLUTION
    if True: e = 3  # SOLUTION
    return
""")

    assert "    a = b = ...\n    c, d = ...\n    if True: e = ...\n    return\n" in out


# Solved copy


def solved(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    cfg = config_of(src, path)
    return student_notebook(src, parse_notebook(src), cfg, REQUIREMENT, solutions=True)


def test_solved_copy_keeps_solutions_without_markers():
    out = solved(SOLUTION)

    assert '    """Sum of two numbers."""\n    return a + b\n\n' in out
    assert "return sum(char in" in out
    assert "SOLUTION" not in out


def test_solved_copy_is_the_student_copy_with_solutions():
    out = solved(PURE)
    blank = student(PURE)

    assert "PROMPT" not in out
    assert "total = None" not in out
    assert "    factor = 2\n    return factor * x\n" in out
    assert "# HIDDEN" not in out and "# HIDDEN" not in blank
    assert f'"{REQUIREMENT}"' in out
    assert "export_button = mogs.export_button()" in out
    # Only solution lines differ.
    assert body(out).count("@app.") == body(blank).count("@app.")


def test_solved_copy_inline_solutions_lose_only_the_comment():
    src = (
        HEADER
        + """
@app.cell
def _():
    a: int = 1  # SOLUTION
    b = [
        1,
        2,
    ]  # SOLUTION
    print(a)  # SOLUTION
    print(
        b,
    )  # SOLUTION
    c = 3  # not a solution
    return
"""
    )

    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT, solutions=True)

    assert (
        "    a: int = 1\n    b = [\n        1,\n        2,\n    ]\n    print(a)\n"
        "    print(\n        b,\n    )\n    c = 3  # not a solution\n    return\n"
    ) in out


def test_solved_copy_keeps_region_indent_and_drops_prompts():
    src = (
        HEADER
        + """
@app.function
def f(x):
    # BEGIN SOLUTION
    # PROMPT: return x
    if x:
        return 2 * x
    # END SOLUTION
    return 0
"""
    )

    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT, solutions=True)

    assert "def f(x):\n    if x:\n        return 2 * x\n    return 0\n" in out


REGION = "    # BEGIN SOLUTION\n    return a + b\n    # END SOLUTION\n"


@pytest.mark.parametrize(
    ("region", "kept"),
    [
        (
            "    # BEGIN SOLUTION\n    # SOLUTION\n    return a + b\n    # END SOLUTION\n",
            "    return a + b\n",
        ),
        (
            "    # BEGIN SOLUTION\n    if a: return a + b  # SOLUTION\n    return a + b\n"
            "    # END SOLUTION\n",
            "    if a: return a + b\n    return a + b\n",
        ),
        (
            "    # BEGIN SOLUTION\n    return (a +  # SOLUTION\n        b)\n    # END SOLUTION\n",
            "    return (a +\n        b)\n",
        ),
    ],
)
def test_solution_comments_inside_a_region_work_in_both_copies(region, kept):
    src = SOLUTION.read_text(encoding="utf-8")
    assert REGION in src
    src = src.replace(REGION, region)
    cfg = read_config(src, SOLUTION)

    blank = student_notebook(src, parse_notebook(src), cfg, REQUIREMENT)
    out = student_notebook(src, parse_notebook(src), cfg, REQUIREMENT, solutions=True)

    assert '    """Sum of two numbers."""\n    ...\n' in blank
    assert f'    """Sum of two numbers."""\n{kept}' in out
    assert "SOLUTION" not in out


def test_solved_copy_passes_marimo_check(tmp_path):
    notebook = tmp_path / "solution.py"
    notebook.write_text(solved(SOLUTION), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "marimo", "check", str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (result.stdout + result.stderr).strip()


def test_inline_solution_on_compound_statement_raises():
    with pytest.raises(SourceError, match="line 21"):
        student_of("""
@app.cell
def _():
    if True:  # SOLUTION
        x = 1
    return
""")


def test_inline_solution_after_other_code_raises():
    with pytest.raises(SourceError, match="own line"):
        student_of("""
@app.cell
def _():
    x = 1; print(x)  # SOLUTION
    return
""")


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        ("    # END SOLUTION\n", "does not pair up"),
        ("    # BEGIN SOLUTION\n    # BEGIN SOLUTION\n", "does not pair up"),
        ("    # BEGIN SOLUTION\n", "without a matching"),
    ],
)
def test_unpaired_markers_raise(cells, message):
    with pytest.raises(SourceError, match=message):
        student_of(f"""
@app.cell
def _():
{cells}    return
""")


def test_output_that_is_not_python_raises():
    with pytest.raises(SourceError, match="not valid Python"):
        student_of("""
@app.cell
def _():
    if True:
        print(1)  # SOLUTION
    return
""")


def test_invalid_output_names_the_source_line():
    # The dropped cell above the function shifts the copy's line numbers.
    with pytest.raises(SourceError) as info:
        student_of("""
@app.cell
def _():
    # HIDDEN
    x = 1
    return


@app.function
def f(x):
    print(x)  # SOLUTION


@app.cell
def _():
    y = 2
    return
""")

    message = str(info.value)
    assert "source line 31" in message
    assert "after function definition on line 27" in message
    assert "`@app.cell`" in message


def test_invalid_output_after_a_multiline_edit_names_the_source_line():
    # The annotation keeps its three lines in the copy.
    cells = """
@app.cell
def _():
    x: dict[
        str, int
    ] = {}  # SOLUTION
    return


@app.function
def f(x):
    print(x)  # SOLUTION


@app.cell
def _():
    y = 2
    return
"""
    with pytest.raises(SourceError) as info:
        student_of(cells)

    lines = (HEADER + cells).split("\n")
    after = lines.index("def f(x):") + 5
    assert lines[after - 1] == "@app.cell"
    message = str(info.value)
    assert f"source line {after} (`@app.cell`)" in message
    assert f"after function definition on line {after - 4}" in message


def test_invalid_prompt_names_its_own_line():
    cells = """
@app.cell
def _():
    # BEGIN SOLUTION
    # PROMPT: y = 1
    # PROMPT: x = (
    x = 2
    # END SOLUTION
    return
"""
    with pytest.raises(SourceError) as info:
        student_of(cells)

    lines = (HEADER + cells).split("\n")
    broken = lines.index("    # PROMPT: x = (") + 1
    assert f"source line {broken} (`# PROMPT: x = (`)" in str(info.value)


# Hidden cells


def test_hidden_cells_removed():
    out = student(SOLUTION)

    for name in ("test_q1_add_large", "big", "test_q2_upper", "test_q2_empty", "HIDDEN"):
        assert name not in out


def test_hidden_cell_trailing_blanks_removed():
    out = student(SOLUTION)

    assert (
        '    """Sum of two numbers."""\n    ...\n\n\n@app.cell\ndef _():\n'
        '    mogs.grade("q1"' in out
    )


# Public tests


def test_public_test_cells_removed():
    for out in (student(SOLUTION), student(SOLUTION, solutions=True)):
        assert "def test_" not in out
        assert "assert add(1, 2) == 3" not in out


def test_public_test_files_hold_each_question_s_public_tests():
    src = SOLUTION.read_text(encoding="utf-8")

    files = public_test_files(parse_notebook(src))

    assert list(files) == [PurePosixPath("tests/q1.py"), PurePosixPath("tests/q2.py")]
    assert files[PurePosixPath("tests/q1.py")] == (
        '# Public tests for q1, run by grade("q1") in the notebook.\n\n\n'
        "def test_q1_add():\n    assert add(1, 2) == 3\n\n"
        "def test_q1_add_negative():\n    assert add(-1, -2) == -3\n"
    )
    assert "test_q1_add_large" not in "".join(files.values())
    assert "test_q2_upper" not in files[PurePosixPath("tests/q2.py")]


def test_public_test_files_join_cells_and_function_cells():
    src = (
        HEADER
        + """
@app.function
def double(x):
    return 2 * x


@app.cell
def _():
    def test_q1_a():
        assert double(1) == 2

    return


@app.function
def test_q1_b():
    assert double(2) == 4


@app.cell
def _():
    mogs.grade("q1")
    return
"""
    )

    [text] = public_test_files(parse_notebook(src)).values()

    assert text.endswith(
        "def test_q1_a():\n    assert double(1) == 2\n\n\n"
        "def test_q1_b():\n    assert double(2) == 4\n"
    )
    ast.parse(text)


# Check calls


def test_check_calls_rewritten():
    out = student(SOLUTION)

    assert (
        '    mogs.grade("q1", points=4, title="Question 1", test_file="tests/q1.py", '
        "uses=[add], hidden=1)\n" in out
    )
    assert (
        '    mogs.grade("q2", points=6, title="Vowels", test_file="tests/q2.py", '
        "uses=[count_vowels], hidden=2)\n" in out
    )


def test_check_call_rewrite_keeps_surroundings():
    out = student_of("""
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    from mogs import grade as c
    panel = c(
        "q1",
    )  # comment
    panel
    return
""")

    assert (
        '    panel = c("q1", points=2.5, title="Q1", test_file="tests/q1.py", uses=[], hidden=0)'
        "  # comment\n" in out
    )


def test_check_call_points_and_title_come_from_the_config():
    out = student_of("""
@app.cell
def _():
    mogs.grade("q1", points=3, title="Old")
    return
""")

    assert (
        '    mogs.grade("q1", points=2.5, title="Q1", test_file="tests/q1.py", uses=[], hidden=0)\n'
        in out
    )


def test_check_call_after_unicode_text_on_line():
    out = student_of("""
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    x = "é"; mogs.grade("q1")
    return
""")

    assert (
        '    x = "é"; mogs.grade("q1", points=2.5, title="Q1", test_file="tests/q1.py", '
        "uses=[], hidden=0)\n" in out
    )


@pytest.mark.parametrize("title", ['It\'s "x"\n', "Bonus 🎉", "a\u2028b\\"])
def test_check_call_title_is_quoted_safely(title):
    cfg = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", title, 2.5)}
    )
    src = (
        HEADER
        + """
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    mogs.grade("q1")
    return
"""
    )
    out = student_notebook(src, parse_notebook(src), cfg, REQUIREMENT)

    tree = ast.parse(out)
    [call] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and any(k.arg == "title" for k in node.keywords)
    ]
    [keyword] = [k for k in call.keywords if k.arg == "title"]

    assert ast.literal_eval(keyword.value) == title


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        'It\'s "x"\n',
        'say "hi"',
        "it's",
        "Bonus 🎉",
        "a\u2028b\\",
        "\ud800 lone",
        "\t\x00\x7f",
        "",
    ],
)
def test_python_literal_round_trips_strings(text):
    literal = python_literal(text)

    assert ast.literal_eval(literal) == text
    assert literal[0] == ("'" if '"' in text and "'" not in text else '"')


def test_python_literal_of_lists_and_dicts():
    assert python_literal(["q1", "it's"]) == '["q1", "it\'s"]'
    assert python_literal({"q1": 'a "b"'}) == '{"q1": \'a "b"\'}'
    assert python_literal([]) == "[]"
    assert python_literal({}) == "{}"


def test_rewritten_check_call_is_ruff_formatted_when_short(tmp_path):
    out = student_of("""
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    mogs.grade("q1")
    return
""")
    [line] = [line for line in out.splitlines() if "mogs.grade(" in line]
    path = tmp_path / "call.py"
    path.write_text(line.strip() + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--diff", str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# Header


# Instructor-only cells


@pytest.mark.parametrize("solutions", [False, True])
def test_assignment_and_instructor_cells_are_dropped(solutions):
    src = (
        HEADER
        + """
@app.cell
def _():
    mogs.assignment(name="nb")
    return


@app.cell(hide_code=True)
def instructor_guide():
    mo.md("How to write this assignment")
    return


@app.cell
def _():
    x = 1
    return
"""
    )
    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT, solutions=solutions)

    assert "assignment(" not in out
    assert "instructor_guide" not in out
    assert "How to write" not in out
    assert "    x = 1\n" in out


def test_written_call_stays():
    out = student(SOLUTION)

    assert '    mogs.grade("q3", points=5, title="Question 3", manual=True)\n' in out


def test_header_pins_tool_once():
    out = student(SOLUTION)

    assert read_header(out)["dependencies"] == ["marimo", REQUIREMENT]
    assert out.count(REQUIREMENT) == 1


def test_header_gets_no_runtime_flag():
    src = HEADER.replace("#\n# [tool.marimo.runtime]\n# reactive_tests = false\n", "")
    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT)

    assert "marimo" not in read_header(out).get("tool", {})


def test_pinned_requirement():
    manual = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )

    assert pinned_requirement(CONFIG) == f"mogs=={__version__}"
    assert pinned_requirement(manual) == f"mogs[pdf]=={__version__}"


# Export cells


def test_export_cells_appended_before_main_guard():
    out = student(SOLUTION)
    tail = out.split("def answer_q3", 1)[1]

    assert tail.index("## Submit") < tail.index("export_button = mogs.export_button()")
    assert tail.index("export_button = mogs.export_button()") < tail.index("mogs.export(")
    assert tail.index("mogs.export(") < tail.index('if __name__ == "__main__":')
    assert "import mogs" not in tail
    assert "    export_button\n" in tail
    assert (
        'mogs.export(export_button, name="solution", manual=["q3"], titles={"q1": "Question 1", '
        '"q2": "Vowels", "q3": "Question 3"}, '
        'placeholders={"answer_q3": "Because the order of the terms does not change the sum."})'
        in tail
    )


def test_export_cells_parse_as_cells():
    nb = parse_notebook(student(SOLUTION))
    exporting = [cell for cell in nb.cells if "export_button" in nb.cell_body_source(cell)]

    assert len(exporting) == 2
    assert nb.cells[-1] is exporting[-1]
    assert "export_button" in nb.student_names()


def test_export_cells_without_manual_questions():
    out = student_of("""
@app.cell
def _():
    x = 1
    return
""")

    assert (
        'mogs.export(export_button, name="nb", manual=[], titles={"q1": "Q1"}, placeholders={})'
        in out
    )


def test_export_cells_use_the_setup_alias():
    src = (
        HEADER.replace("import mogs", "import mogs as grading")
        + "\n@app.cell\ndef _():\n    x = 1\n    return\n"
    )
    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT)

    assert "export_button = grading.export_button()" in out
    assert "grading.export(export_button" in out


def test_export_cells_import_the_package_when_setup_does_not():
    out = student(PURE)
    nb = parse_notebook(out)

    assert "mogs.export(export_button" in out
    assert "mogs" in nb.student_names()
    assert out.count("import mogs") == 1
    assert "    import mogs\n" in out


def test_export_cells_refuse_a_taken_mogs_name():
    src = PURE.read_text(encoding="utf-8").replace(
        "    import marimo as mo\n", "    import marimo as mo\n    mogs = 1\n"
    )

    with pytest.raises(SourceError, match="mogs"):
        student_notebook(src, parse_notebook(src), PURE_CONFIG, REQUIREMENT)


def submit_cell(out: str) -> str:
    """Body of the student copy's Submit cell."""

    nb = parse_notebook(out)
    [cell] = [cell for cell in nb.cells if "## Submit" in nb.cell_body_source(cell)]

    return nb.cell_body_source(cell)


def test_submit_note_mentions_written_answers_only_with_manual_questions():
    with_manual = submit_cell(student(SOLUTION))
    without = submit_cell(student_of("\n@app.cell\ndef _():\n    x = 1\n    return\n"))

    assert "written answers" in with_manual
    assert "written answers" not in without
    assert "zip of your notebook." in " ".join(without.split())


def test_submit_note_says_how_to_show_the_button():
    note = " ".join(submit_cell(student(SOLUTION)).split())

    assert "Run all stale cells" in note
    assert "Ctrl+Shift+R" in note


@pytest.mark.parametrize("imports_mo", [True, False])
def test_submit_note_renders_before_any_cell_runs(imports_mo):
    # With marimo's default `auto_instantiate = false`, nothing runs on open. marimo still
    # renders a cell that is just `mo.md(...)`, as long as some cell defines `mo`.
    from marimo._ast.compiler import compile_cell
    from marimo._types.ids import CellId_t

    out = student(PURE) if imports_mo else student_of("\n@app.cell\ndef _():\n    x = 1\n")
    nb = parse_notebook(out)
    defines_mo = [
        cell
        for cell in nb.cells
        if "mo" in compile_cell(nb.cell_body_source(cell), cell_id=CellId_t("c")).defs
    ]

    assert compile_cell(submit_cell(out), cell_id=CellId_t("submit")).markdown is not None
    assert len(defines_mo) == 1


MO_IN_A_CELL = "@app.cell\ndef _():\n    import marimo as mo\n\n    return (mo,)\n\n\n"


@pytest.mark.parametrize(
    ("cell", "importer"),
    [("", "return (mo, mogs)"), (MO_IN_A_CELL, "return (mogs,)")],
)
def test_export_cells_take_names_from_other_cells_as_parameters(cell, importer, tmp_path):
    pure = PURE.read_text(encoding="utf-8")
    src = pure.replace("    import marimo as mo\n", "    import math\n").replace(
        "@app.function\ndef add", cell + "@app.function\ndef add"
    )
    out = student_notebook(src, parse_notebook(src), PURE_CONFIG, REQUIREMENT)
    notebook = tmp_path / "student.py"
    notebook.write_text(out, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "marimo", "check", str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
    )

    assert 'def _(mo):\n    mo.md(r"""\n    ## Submit' in out
    assert "def _(mogs):\n    export_button = mogs.export_button()" in out
    assert "def _(export_button, mogs):\n    mogs.export(" in out
    assert importer in out
    assert out.count("import marimo as mo") == 1
    assert result.returncode == 0, result.stdout + result.stderr
    assert (result.stdout + result.stderr).strip() == ""


@pytest.mark.parametrize("solutions", [False, True])
def test_export_cells_look_for_mo_in_the_copy_not_the_source(solutions):
    # The student copy drops a solution region, imports in it included.
    src = HEADER + (
        "\n@app.cell\ndef _():\n    # BEGIN SOLUTION\n    import marimo as mo\n\n    mo = mo\n"
        "    # END SOLUTION\n    return\n"
    )

    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT, solutions=solutions)

    assert out.count("import marimo as mo") == 1
    assert ("    import marimo as mo\n\n    return (mo,)\n" in out) != solutions


def test_export_cells_ignore_mo_bound_only_in_a_solution_region():
    out = student_of(
        "\n@app.cell\ndef _():\n    # BEGIN SOLUTION\n    mo = 1\n    # END SOLUTION\n    return\n"
    )

    assert out.count("import marimo as mo") == 1


def test_export_cells_refuse_a_taken_mo_name():
    with pytest.raises(SourceError, match="`mo`"):
        student_of("\n@app.cell\ndef _():\n    mo = 1\n    return (mo,)\n")


def test_export_cells_quote_titles_safely():
    config = AssignmentConfig(
        name="nb",
        notebook="nb.py",
        questions={"q1": Question("q1", 'It\'s "x"\n', 1, manual=True)},
    )
    out = student_of("\n@app.cell\ndef answer_q1():\n    x = 1\n    return\n", config)

    assert 'titles={"q1": "It\'s \\"x\\"\\n"}' in out
    ast.parse(out)


def test_export_call_round_trips_non_bmp_text():
    config = AssignmentConfig(
        name="nb",
        notebook="hw 🎉.py",
        questions={"q1": Question("q1", "Bonus 🎉", 1, manual=True)},
    )
    cells = export_cells(config, "mogs", {"q1": "*Answer 🎉 here.*"})
    [call] = [
        node
        for node in ast.walk(ast.parse(cells))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "export"
    ]
    values = {k.arg: ast.literal_eval(k.value) for k in call.keywords}

    assert values == {
        "name": "hw 🎉",
        "manual": ["q1"],
        "titles": {"q1": "Bonus 🎉"},
        "placeholders": {"q1": "*Answer 🎉 here.*"},
    }


def test_placeholders_come_from_the_student_copy():
    config = AssignmentConfig(
        name="nb",
        notebook="nb.py",
        questions={
            "q1": Question("q1", "One", 1, manual=True),
            "q2": Question("q2", "Two", 1, manual=True),
            "q3": Question("q3", "Three", 1, manual=True),
        },
    )
    out = student_of(
        '\n@app.cell\ndef answer_q1():\n    mo.md(r"""\n    *Write   here.*\n    """)\n    return\n'
        "\n@app.cell\ndef answer_q2():\n    x = 1  # SOLUTION\n    return\n"
        '\n@app.cell\ndef answer_q3():\n    mo.md("(a)")\n    return\n'
        '\n@app.cell\ndef answer_q3_2():\n    mo.md("(b)")\n    return\n',
        config,
    )

    assert (
        'placeholders={"answer_q1": "*Write   here.*", "answer_q3": "(a)", "answer_q3_2": "(b)"}'
        in out
    )


def test_solved_copy_placeholders_come_from_the_student_copy():
    out = solved(SOLUTION)

    assert "Addition of numbers is commutative" in out
    assert (
        'placeholders={"answer_q3": "Because the order of the terms does not change the sum."}'
        in out
    )


def test_source_with_its_own_export_button_is_refused():
    with pytest.raises(SourceError, match="export_button"):
        student_of("""
@app.cell
def _():
    export_button = mogs.export_button()
    return (export_button,)
""")


# Whole notebook


@pytest.mark.parametrize("path", [SOLUTION, PURE])
def test_student_copy_parses(path):
    out = student(path)

    ast.parse(out)
    parse_notebook(out)


def test_student_copy_keeps_line_ends_and_unicode_separators():
    src = HEADER + '\n@app.cell\ndef _():\n    s = "a\u2028b"\n    x = 1  # SOLUTION\n    return\n'
    out = student_notebook(src.replace("\n", "\r\n"), parse_notebook(src), CONFIG, REQUIREMENT)

    assert '    s = "a\u2028b"\n    x = ...\n    return\n' in out
    assert "\r" not in out


def test_src_must_match_notebook():
    src = HEADER

    with pytest.raises(ValueError, match="parsed from"):
        student_notebook(src + "\n", parse_notebook(src), CONFIG, REQUIREMENT)


def test_untouched_cells_unchanged():
    src = SOLUTION.read_text(encoding="utf-8")
    out = student(SOLUTION)
    question2 = '    mo.md(r"""\n    ## Question 2\n'

    assert question2 in src
    assert (
        body(src).split(question2)[1].split("@app.function")[0]
        == (body(out).split(question2)[1].split("@app.function")[0])
    )


# Written questions


def test_written_answer_and_prompt_cells_are_named_in_the_copy():
    out = student(SOLUTION)
    nb = parse_notebook(out)

    [prompt] = nb.prompt_cells("q3")
    [answer] = nb.answer_cells("q3")

    assert "## Question 3" in nb.cell_body_source(prompt)
    assert nb.cell_body_source(answer) == f'mo.md(r"""\n{ANSWER}\n""")\n'


def test_written_answer_without_prompt_lines_gets_a_placeholder():
    src = SOLUTION.read_text(encoding="utf-8").replace(
        "    <!-- PROMPT: Because the order of the terms does not change the sum. -->\n", ""
    )
    out = student_notebook(src, parse_notebook(src), read_config(src, SOLUTION), REQUIREMENT)

    [answer] = parse_notebook(out).answer_cells("q3")

    assert "    *Write your answer here.*\n" in out
    assert "Addition of numbers" not in out
    assert answer.name == "answer_q3"


def test_solved_copy_keeps_the_written_answer():
    out = solved(SOLUTION)

    assert "    Addition of numbers is commutative: a + b = b + a.\n" in out
    assert "PROMPT" not in out
    assert "def answer_q3():" in out


def test_written_grade_call_says_manual():
    out = student(SOLUTION)

    assert '    mogs.grade("q3", points=5, title="Question 3", manual=True)\n' in out
    files = public_test_files(parse_notebook(SOLUTION.read_text()))

    assert PurePosixPath("tests/q3.py") not in files
    assert PurePosixPath("tests/q1.py") in files


@pytest.mark.parametrize("path", [SOLUTION, PURE])
def test_marimo_check_passes(path, tmp_path):
    notebook = tmp_path / "student.py"
    notebook.write_text(student(path), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "marimo", "check", str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (result.stdout + result.stderr).strip() == ""


@pytest.mark.parametrize("path", [SOLUTION, PURE])
def test_pytest_collects_no_tests_from_the_student_copy(path, tmp_path):
    notebook = tmp_path / "student.py"
    notebook.write_text(student(path), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
        + ["--rootdir", str(tmp_path), str(notebook)],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
        cwd=tmp_path,
    )

    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, result.stdout + result.stderr
    assert re.findall(r"::(test_\w+)", result.stdout) == []


def write_student(path: Path, folder: Path) -> Path:
    """Write the student copy of `path` and its public test files into `folder`."""

    src = path.read_text(encoding="utf-8")
    notebook = folder / "student.py"
    notebook.write_text(student(path), encoding="utf-8")

    for rel, text in public_test_files(parse_notebook(src)).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")

    return notebook


# marimo's reactive pytest hook (needs network for `uv run --with` on a cold cache)

MARIMO = "marimo==0.25.2"


def export_html(
    notebook: Path, tmp_path: Path, marimo: str = MARIMO
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # The test's interpreter, not whichever Python uv would pick, and no sandbox prompt
        # for the PEP 723 header.
        ["uv", "run", "--python", sys.executable, "--with", marimo, "marimo", "export", "html"]
        + ["--no-sandbox", str(notebook), "-o", str(tmp_path / f"{notebook.stem}.html")],
        capture_output=True,
        text=True,
        check=False,
        env=outside_pytest(),
        cwd=Path(__file__).parent.parent,
    )


@pytest.mark.slow
def test_export_html_source_and_student_copy(tmp_path):
    # Copies in tmp_path, since marimo writes a `__marimo__` cache next to the notebook.
    source = tmp_path / "source.py"
    source.write_text(PURE.read_text(encoding="utf-8"), encoding="utf-8")
    notebook = write_student(PURE, tmp_path)

    for path in (source, notebook):
        result = export_html(path, tmp_path)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert "RuntimeError" not in output, output


@pytest.mark.slow
def test_export_html_with_reactive_tests_on(tmp_path):
    # marimo runs pytest after each test cell. Without the package's import-time warm-up of
    # `sysconfig`, marimo 0.25 and 0.26 crash with "dictionary changed size during iteration".
    src = SOLUTION.read_text(encoding="utf-8")
    assert "reactive_tests" not in src
    notebook = tmp_path / "source.py"
    notebook.write_text(src, encoding="utf-8")

    result = export_html(notebook, tmp_path)
    output = result.stdout + result.stderr
    page = (tmp_path / "source.html").read_text(encoding="utf-8")

    assert result.returncode == 0, output
    assert "RuntimeError" not in output, output
    assert "Passed Tests" in page


@pytest.mark.slow
@pytest.mark.parametrize("marimo", [MARIMO, "marimo==0.24.2"])
def test_export_html_student_copy_with_check_panels(marimo, tmp_path):
    notebook = write_student(SOLUTION, tmp_path)

    result = export_html(notebook, tmp_path, marimo)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    page = (tmp_path / "student.html").read_text(encoding="utf-8")
    assert "RuntimeError" not in output, output
    assert "Question 1 \\u2014 4 pts \\u00b7 0/2 public tests passed" in page
    assert "+ 1 hidden test run on Gradescope" in page
    assert "Vowels \\u2014 6 pts \\u00b7 0/1 public tests passed" in page
    assert "+ 2 hidden tests run on Gradescope" in page
    assert "Export submission" in page
    # The page embeds the panel as JSON, which escapes the backslashes of Windows paths.
    assert json.dumps(str(notebook))[1:-1] in page
    assert "Cannot find this notebook" not in page


# Leak check


LEAKY = """
@app.cell
def _():
    import math
    # BEGIN SOLUTION
    # PROMPT: y = ...
    import math
    y = math.sqrt(
        2
    )
    pass
    ...
    # END SOLUTION
    return (y,)


@app.cell
def _():
    n = 3  # SOLUTION
    return (n,)
"""


def leaks_in(student_text: str) -> list[str]:
    src = HEADER + LEAKY
    return solution_leaks(parse_notebook(src), student_text)


def line_in_leaky(text: str) -> int:
    lines = (HEADER + LEAKY).splitlines()
    return next(number for number, line in enumerate(lines, start=1) if line == text)


def test_solution_leaks_names_region_line():
    found = leaks_in("x = 1\n    y = math.sqrt(\n")

    assert len(found) == 1, found
    assert f"line {line_in_leaky('    y = math.sqrt(')}:" in found[0]
    assert "`y = math.sqrt(`" in found[0]


def test_solution_leaks_names_inline_solution_line():
    found = leaks_in("    n = 3\n")

    assert len(found) == 1, found
    assert f"line {line_in_leaky('    n = 3  # SOLUTION')}:" in found[0]


def test_solution_leaks_skips_trivial_public_and_prompt_lines():
    assert leaks_in("    import math\n    pass\n    ...\n    )\n        2\n    y = ...\n") == []


def test_solution_leaks_none_in_student_copy():
    assert leaks_in(student_of(LEAKY)) == []
    assert solution_leaks(parse_notebook(SOLUTION.read_text()), student(SOLUTION)) == []


def test_student_notebook_refuses_leaked_solution(monkeypatch):
    # A transform bug that keeps the regions as written must not reach students.
    monkeypatch.setattr("mogs.transform._solution_regions", lambda *args: set())

    with pytest.raises(SourceError, match=r"y = math\.sqrt\("):
        student_of(LEAKY)


def test_solved_copy_skips_leak_check():
    assert "y = math.sqrt(" in student_notebook(
        HEADER + LEAKY, parse_notebook(HEADER + LEAKY), CONFIG, REQUIREMENT, solutions=True
    )

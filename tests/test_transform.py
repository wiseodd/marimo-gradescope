import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from marimo_gradescope import __version__
from marimo_gradescope.config import (
    AssignmentConfig,
    Question,
    read_config,
    read_header,
    split_lines,
)
from marimo_gradescope.source import SourceError, parse_notebook
from marimo_gradescope.transform import (
    export_cells,
    pinned_requirement,
    python_literal,
    student_notebook,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"
PURE = FIXTURES / "pure_test_cells.py"
REQUIREMENT = "marimo-gradescope==9.9.9"
ANSWER = "Because the order of the terms does not change the sum."

HEADER = """\
# /// script
# dependencies = ["marimo"]
#
# [tool.marimo-gradescope]
# questions = { q1 = 2.5 }
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///

import marimo

app = marimo.App()

with app.setup:
    import marimo_gradescope as mgs

"""

CONFIG = AssignmentConfig(name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 2.5)})


def outside_pytest() -> dict[str, str]:
    """Environment without the variable that makes marimo act as if a test were running."""

    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def student(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return student_notebook(src, parse_notebook(src), read_config(src, path), REQUIREMENT)


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
    cfg = read_config(src, path)
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
    assert "[tool.marimo-gradescope]" not in out
    assert f'"{REQUIREMENT}"' in out
    assert "export_button = mgs.export_button()" in out
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


# Hidden cells


def test_hidden_cells_removed():
    out = student(SOLUTION)

    for name in ("test_q1_add_large", "big", "test_q2_upper", "test_q2_empty", "HIDDEN"):
        assert name not in out


def test_hidden_cell_trailing_blanks_removed():
    out = student(SOLUTION)

    assert (
        "        assert add(-1, -2) == -3\n\n    return\n\n\n@app.cell\ndef _():\n    mgs.check"
        in out
    )


# Check calls


def test_check_calls_rewritten():
    out = student(SOLUTION)

    assert (
        '    mgs.check("q1", tests=[test_q1_add, test_q1_add_negative], points=4, hidden=1, '
        'title="Question 1")\n' in out
    )
    assert '    mgs.check("q2", tests=[test_q2_count], points=6, hidden=2, title="Vowels")\n' in out


def test_check_call_rewrite_keeps_surroundings():
    out = student_of("""
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    from marimo_gradescope import check as c
    panel = c(
        "q1",
    )  # comment
    panel
    return
""")

    assert (
        '    panel = c("q1", tests=[test_q1_a], points=2.5, hidden=0, title="Q1")  # comment\n'
        in out
    )


def test_check_call_with_other_arguments_raises():
    with pytest.raises(SourceError, match="only the question id"):
        student_of("""
@app.cell
def _():
    mgs.check("q1", points=3)
    return
""")


def test_check_call_after_unicode_text_on_line():
    out = student_of("""
@app.cell
def _():
    def test_q1_a():
        assert True
    return

@app.cell
def _():
    x = "é"; mgs.check("q1")
    return
""")

    assert (
        '    x = "é"; mgs.check("q1", tests=[test_q1_a], points=2.5, hidden=0, title="Q1")\n' in out
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
    mgs.check("q1")
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
    mgs.check("q1")
    return
""")
    [line] = [line for line in out.splitlines() if "mgs.check(" in line]
    path = tmp_path / "call.py"
    path.write_text(line.strip() + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--diff", str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# Header


def test_header_tool_table_stripped():
    header = read_header(student(SOLUTION))

    assert "marimo-gradescope" not in header.get("tool", {})


def test_header_pins_tool_and_sets_runtime_flag_once():
    out = student(SOLUTION)

    assert read_header(out)["dependencies"] == ["marimo", REQUIREMENT]
    assert out.count("reactive_tests = false") == 1
    assert out.count(REQUIREMENT) == 1


def test_header_runtime_flag_added_when_missing():
    src = HEADER.replace("#\n# [tool.marimo.runtime]\n# reactive_tests = false\n", "")
    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT)

    assert read_header(out)["tool"] == {"marimo": {"runtime": {"reactive_tests": False}}}


def test_pinned_requirement():
    manual = AssignmentConfig(
        name="nb", notebook="nb.py", questions={"q1": Question("q1", "Q1", 1, manual=True)}
    )

    assert pinned_requirement(CONFIG) == f"marimo-gradescope=={__version__}"
    assert pinned_requirement(manual) == f"marimo-gradescope[pdf]=={__version__}"


# Export cells


def test_export_cells_appended_before_main_guard():
    out = student(SOLUTION)
    tail = out.split("def answer_q3", 1)[1]

    assert tail.index("## Submit") < tail.index("export_button = mgs.export_button()")
    assert tail.index("export_button = mgs.export_button()") < tail.index("mgs.export(")
    assert tail.index("mgs.export(") < tail.index('if __name__ == "__main__":')
    assert "import marimo_gradescope" not in tail
    assert "    export_button\n" in tail
    assert (
        'mgs.export(export_button, name="solution", manual=["q3"], titles={"q3": "Question 3"}, '
        'placeholders={"q3": "Because the order of the terms does not change the sum."})' in tail
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

    assert 'mgs.export(export_button, name="nb", manual=[], titles={}, placeholders={})' in out


def test_export_cells_use_the_setup_alias():
    src = HEADER.replace("as mgs", "as grading") + "\n@app.cell\ndef _():\n    x = 1\n    return\n"
    out = student_notebook(src, parse_notebook(src), CONFIG, REQUIREMENT)

    assert "export_button = grading.export_button()" in out
    assert "grading.export(export_button" in out


def test_export_cells_import_the_package_when_setup_does_not():
    out = student(PURE)
    nb = parse_notebook(out)

    assert "mgs.export(export_button" in out
    assert "mgs" in nb.student_names()
    assert out.count("import marimo_gradescope as mgs") == 1


def test_export_cells_refuse_a_taken_mgs_name():
    src = PURE.read_text(encoding="utf-8").replace(
        "    import marimo as mo\n", "    import marimo as mo\n    mgs = 1\n"
    )

    with pytest.raises(SourceError, match="marimo_gradescope"):
        student_notebook(src, parse_notebook(src), read_config(src, PURE), REQUIREMENT)


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
    cells = export_cells(config, "mgs", {"q1": "*Answer 🎉 here.*"})
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
        "\n@app.cell\ndef answer_q2():\n    x = 1  # SOLUTION\n    return\n",
        config,
    )

    assert 'placeholders={"q1": "*Write   here.*"}' in out


def test_source_with_its_own_export_button_is_refused():
    with pytest.raises(SourceError, match="export_button"):
        student_of("""
@app.cell
def _():
    export_button = mgs.export_button()
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
    out = student(SOLUTION).replace(
        export_cells(read_config(src, SOLUTION), "mgs", {"q3": ANSWER}) + "\n\n", ""
    )

    assert (
        body(src).split("@app.cell(hide_code=True)\ndef prompt_q3")[1]
        == (body(out).split("@app.cell(hide_code=True)\ndef prompt_q3")[1])
    )


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


@pytest.mark.parametrize(
    ("path", "public"),
    [
        (SOLUTION, ["test_q1_add", "test_q1_add_negative", "test_q2_count"]),
        (PURE, ["test_q1_add", "test_q1_add_negative", "test_q2_double", "test_q2_double_float"]),
    ],
)
def test_pytest_collects_exactly_public_tests(path, public, tmp_path):
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
    collected = re.findall(r"::(test_\w+)", result.stdout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(collected) == public


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
    notebook = tmp_path / "student.py"
    notebook.write_text(student(PURE), encoding="utf-8")

    for path in (source, notebook):
        result = export_html(path, tmp_path)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert "RuntimeError" not in output, output


@pytest.mark.slow
def test_export_html_without_runtime_flag_crashes(tmp_path):
    lines = split_lines(PURE.read_text(encoding="utf-8"))
    flag = lines.index("# [tool.marimo.runtime]")
    assert lines[flag + 1] == "# reactive_tests = false"
    notebook = tmp_path / "reactive.py"
    notebook.write_text("\n".join(lines[: flag - 1] + lines[flag + 2 :]), encoding="utf-8")

    result = export_html(notebook, tmp_path)

    assert "dictionary changed size during iteration" in result.stdout + result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("marimo", [MARIMO, "marimo==0.24.2"])
def test_export_html_student_copy_with_check_panels(marimo, tmp_path):
    notebook = tmp_path / "student.py"
    notebook.write_text(student(SOLUTION), encoding="utf-8")

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

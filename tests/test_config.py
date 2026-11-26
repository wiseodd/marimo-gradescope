from pathlib import Path

import pytest

from marimo_gradescope.config import (
    AssignmentConfig,
    ConfigError,
    Question,
    read_config,
    read_header,
    remove_dependency,
    set_dependency,
)

NB = Path("/course/A_01/hw1.py")

CODE = """
import marimo

app = marimo.App()


@app.cell
def _():
    x = [1, 2]  # [not a table]
    return
"""


def notebook(header: str) -> str:
    lines = [f"# {line}" if line else "#" for line in header.strip("\n").splitlines()]
    return "# /// script\n" + "\n".join(lines) + "\n# ///\n" + CODE


BASIC = notebook(
    """
requires-python = ">=3.13"
dependencies = [
    "marimo-gradescope",
    "numpy",
]
"""
)


def count(src: str, line: str) -> int:
    return sum(1 for s in src.splitlines() if s.strip() == line)


# read_header


def test_read_header_parses_toml():
    header = read_header(BASIC)

    assert header["requires-python"] == ">=3.13"
    assert header["dependencies"] == ["marimo-gradescope", "numpy"]


def test_read_header_missing():
    with pytest.raises(ConfigError, match="# /// script"):
        read_header(CODE)


def test_read_header_invalid_toml():
    with pytest.raises(ConfigError, match="TOML"):
        read_header(notebook("dependencies = ["))


# read_config


def cells(*bodies: str, alias: str = "import marimo_gradescope as mogs") -> str:
    """A notebook with a header, a setup cell importing this package, and plain cells."""

    out = HEADER + f"\nimport marimo\n\napp = marimo.App()\n\nwith app.setup:\n    {alias}\n"

    for body in bodies:
        indented = "".join(f"    {line}\n" if line else "\n" for line in body.splitlines())
        out += f"\n\n@app.cell\ndef _():\n{indented}    return\n"

    return out


HEADER = (
    '# /// script\n# requires-python = ">=3.13"\n# dependencies = ["marimo-gradescope"]\n# ///\n'
)
ASSIGNMENT = 'mogs.assignment(name="HW1")'


def test_questions_from_grade_calls_in_file_order():
    src = cells(
        ASSIGNMENT,
        "def test_q2_a():\n    assert True",
        'mogs.grade("q2", points=10, title="Gradients")',
        'mogs.grade("essay", points=5)',
        "# HIDDEN\ndef test_q1_a():\n    assert True",
        'mogs.grade("q1", points=2.5)',
    )

    cfg = read_config(src, NB)

    assert list(cfg.questions) == ["q2", "essay", "q1"]
    assert cfg.questions == {
        "q2": Question("q2", "Gradients", 10),
        "essay": Question("essay", "essay", 5, manual=True),
        "q1": Question("q1", "Question 1", 2.5),
    }


def test_a_question_without_tests_is_graded_by_hand():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points=1)', "def test_q12_a():\n    assert True")

    assert read_config(src, NB).questions["q1"].manual


def test_defaults():
    cfg = read_config(cells("mogs.assignment()", 'mogs.grade("q1", points=1)'), NB)

    assert cfg == AssignmentConfig(name="hw1", notebook="hw1.py", questions=cfg.questions)
    assert cfg.files == ()
    assert cfg.timeout == 60
    assert cfg.budget == 540
    assert cfg.memory_mb == 4096
    assert cfg.python == "3.13"
    assert cfg.requirements is None


def test_all_assignment_arguments():
    src = cells(
        "mogs.assignment(\n"
        '    name="Assignment 1",\n'
        '    files=["data.csv", "img/cat.png"],\n'
        "    timeout=30,\n"
        "    budget=300.5,\n"
        "    memory=2048,\n"
        '    python="3.14",\n'
        '    requirements="../requirements.txt",\n'
        ")",
        'mogs.grade("q1", points=1)',
    )

    cfg = read_config(src, NB)

    assert cfg.name == "Assignment 1"
    assert cfg.notebook == "hw1.py"
    assert cfg.files == ("data.csv", "img/cat.png")
    assert cfg.timeout == 30
    assert cfg.budget == 300.5
    assert cfg.memory_mb == 2048
    assert cfg.python == "3.14"
    assert cfg.requirements == Path("/course/A_01/../requirements.txt")


@pytest.mark.parametrize(
    "alias",
    [
        "import marimo_gradescope as grader",
        "from marimo_gradescope import assignment, grade as run_grade",
    ],
)
def test_calls_follow_the_import_names(alias):
    prefix = "grader." if alias.startswith("import") else ""
    grade = "grader.grade" if prefix else "run_grade"
    src = cells(
        f'{prefix}assignment(name="x")',
        f'{grade}("q1", points=1)',
        f'{grade}("q2", points=2)',
        alias=alias,
    )

    cfg = read_config(src, NB)

    assert cfg.name == "x"
    assert list(cfg.questions) == ["q1", "q2"]


def test_calls_of_other_modules_do_not_count():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points=1)', 'other.grade("q2", points=1)')

    assert list(read_config(src, NB).questions) == ["q1"]


def test_missing_assignment_call():
    with pytest.raises(ConfigError, match=r"mogs\.assignment"):
        read_config(cells('mogs.grade("q1", points=1)'), NB)


def test_two_assignment_calls():
    src = cells(ASSIGNMENT, ASSIGNMENT, 'mogs.grade("q1", points=1)')

    with pytest.raises(ConfigError, match="more than once"):
        read_config(src, NB)


def test_no_questions():
    with pytest.raises(ConfigError, match=r"no questions.*mogs\.grade"):
        read_config(cells(ASSIGNMENT), NB)


def test_question_declared_twice():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points=1)', 'mogs.grade("q1", points=1)')

    with pytest.raises(ConfigError, match=r"`q1`.*more than once"):
        read_config(src, NB)


def test_header_table_is_refused():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points=1)').replace(
        "# ///\n", "#\n# [tool.marimo-gradescope]\n# name = 'x'\n# ///\n", 1
    )

    with pytest.raises(ConfigError, match=r"\[tool\.marimo-gradescope\].*mogs\.assignment"):
        read_config(src, NB)


def test_works_without_a_header():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points=1)').removeprefix(HEADER)

    assert read_config(src, NB).name == "HW1"


def test_invalid_python():
    with pytest.raises(ConfigError, match="not valid Python"):
        read_config(cells(ASSIGNMENT) + "\ndef (:\n", NB)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        ('mogs.grade("q1")', "needs `points`"),
        ('mogs.grade("q1", points="ten")', "points"),
        ('mogs.grade("q1", points=True)', "points"),
        ('mogs.grade("q1", points=-1)', "points"),
        ('mogs.grade("q1", points=n)', "`points`.*literal"),
        ('mogs.grade("q1", points=1, title=3)', "title"),
        ('mogs.grade("q1", points=1, bonus=2)', "bonus"),
        ('mogs.grade("q1", points=1, hidden=2)', "hidden"),
        ('mogs.grade("q1", 1)', "keyword"),
        ("mogs.grade(points=1)", "question id"),
        ("mogs.grade(qid, points=1)", "question id"),
        ('mogs.grade("q1", **extra)', r"\*\*"),
        ('mogs.grade("q_1", points=1)', "q_1"),
        ('mogs.grade("q1", points=1, manual=True)', "manual"),
    ],
)
def test_bad_question_arguments(call: str, match: str):
    src = cells(ASSIGNMENT, call)

    with pytest.raises(ConfigError, match=match):
        read_config(src, NB)


def test_bad_question_names_its_call_and_line():
    src = cells(ASSIGNMENT, 'mogs.grade("q1", points="ten")')
    line = src.splitlines().index('    mogs.grade("q1", points="ten")') + 1

    with pytest.raises(ConfigError, match=rf"mogs\.grade\(\) \(line {line}\)"):
        read_config(src, NB)


@pytest.mark.parametrize(
    ("arguments", "key"),
    [
        ("name=1", "name"),
        ("memory=1000", "memory"),
        ("memory=4096.5", "memory"),
        ("timeout=0", "timeout"),
        ('budget="long"', "budget"),
        ('files="data.csv"', "files"),
        ("python=3.13", "python"),
        ("requirements=1", "requirements"),
        ("timeuot=10", "timeuot"),
        ("name=NAME", "name"),
        ('"HW1"', "keyword"),
    ],
)
def test_bad_assignment_arguments_name_the_key(arguments: str, key: str):
    src = cells(f"mogs.assignment({arguments})", 'mogs.grade("q1", points=1)')

    with pytest.raises(ConfigError, match=key):
        read_config(src, NB)


def test_files_may_be_a_tuple():
    src = cells('mogs.assignment(files=("a.csv",))', 'mogs.grade("q1", points=1)')

    assert read_config(src, NB).files == ("a.csv",)


def named(name: str) -> str:
    return cells(f"mogs.assignment(name={name!r})", 'mogs.grade("q1", points=1)')


@pytest.mark.parametrize(
    "name", ["../../escaped", "HW 1/2", "a\\b", ".hidden", "", "tab\there", "nul\u0000"]
)
def test_name_unfit_for_a_file_name(name: str):
    with pytest.raises(ConfigError, match="`name`"):
        read_config(named(name), NB)


@pytest.mark.parametrize("name", ["CON", "aux", "com1", "Nul.tar", "a:b", "why?", "dot.", "space "])
def test_name_must_work_on_windows(name: str):
    # The student zip and the folder in it are named after `name`.
    with pytest.raises(ConfigError, match="`name`.*Windows"):
        read_config(named(name), NB)


def test_default_name_from_the_notebook_must_work_on_windows():
    src = cells("mogs.assignment()", 'mogs.grade("q1", points=1)')

    with pytest.raises(ConfigError, match="`name`.*Windows"):
        read_config(src, Path("con.py"))


@pytest.mark.parametrize("name", ["HW1", "hw 1", "CS9840B_A01", "console", "con-1"])
def test_name_fit_for_windows(name: str):
    assert read_config(named(name), NB).name == name


# set_dependency


@pytest.mark.parametrize(
    "existing",
    ["marimo-gradescope", "marimo-gradescope>=0.1", "marimo_gradescope[pdf] >= 0.1"],
)
def test_set_dependency_replaces(existing: str):
    src = notebook(f'dependencies = [\n    "numpy",\n    "{existing}",\n    "scipy",\n]')

    out = set_dependency(src, "marimo-gradescope==0.1.0")

    assert read_header(out)["dependencies"] == ["numpy", "marimo-gradescope==0.1.0", "scipy"]
    assert out.endswith(CODE)


def test_set_dependency_adds():
    src = notebook('dependencies = ["nbconvert[webpdf]", "numpy"]\n\n[tool.uv]\nfoo = "]"')

    out = set_dependency(src, "marimo-gradescope==0.1.0")
    header = read_header(out)

    assert header["dependencies"] == ["nbconvert[webpdf]", "numpy", "marimo-gradescope==0.1.0"]
    assert header["tool"]["uv"] == {"foo": "]"}


def test_set_dependency_without_dependencies_key():
    src = notebook('requires-python = ">=3.13"\n\n[tool.uv]\nfoo = 1')

    out = set_dependency(src, "marimo-gradescope==0.1.0")
    header = read_header(out)

    assert header["dependencies"] == ["marimo-gradescope==0.1.0"]
    assert header["requires-python"] == ">=3.13"
    assert header["tool"]["uv"] == {"foo": 1}


def test_set_dependency_keeps_other_header_text():
    out = set_dependency(BASIC, "marimo-gradescope==0.1.0")

    assert read_header(out)["dependencies"] == ["marimo-gradescope==0.1.0", "numpy"]
    assert out.endswith(CODE)


# remove_dependency


def test_remove_dependency_drops_every_match():
    src = notebook(
        'dependencies = [\n    "numpy",\n    "marimo-gradescope==0.1.0",\n    "scipy",\n'
        '    "marimo_gradescope[pdf]",\n]\n\n[tool.uv]\nfoo = "]"'
    )

    out = remove_dependency(src, "marimo-gradescope")
    header = read_header(out)

    assert header["dependencies"] == ["numpy", "scipy"]
    assert header["tool"]["uv"] == {"foo": "]"}
    assert out.endswith(CODE)


def test_remove_dependency_absent_is_unchanged():
    src = notebook('requires-python = ">=3.13"\n\n[tool.uv]\nfoo = 1')

    assert remove_dependency(src, "marimo-gradescope") == src
    assert remove_dependency(BASIC, "scipy") == BASIC


def test_remove_dependency_rejects_bad_name():
    with pytest.raises(ConfigError, match="distribution name"):
        remove_dependency(BASIC, "==1.0")


# Question ids become file names


@pytest.mark.parametrize(
    ("qids", "message"),
    [
        (["q1", "Q1"], "`Q1`.*`q1`"),
        (["CON"], "`CON`"),
        (["nul"], "`nul`"),
        (["com1"], "`com1`"),
    ],
)
def test_question_ids_must_make_portable_file_names(qids, message):
    src = cells(ASSIGNMENT, *(f'mogs.grade("{qid}", points=1)' for qid in qids))

    with pytest.raises(ConfigError, match=message):
        read_config(src, Path("hw.py"))

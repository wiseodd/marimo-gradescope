from pathlib import Path

import pytest

from marimo_gradescope.config import (
    AssignmentConfig,
    ConfigError,
    Question,
    ensure_runtime_flag,
    read_config,
    read_header,
    remove_dependency,
    set_dependency,
    strip_tool_table,
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

[tool.marimo-gradescope]
questions = { q1 = 25, q2 = 10.5 }
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


def test_questions_short_form():
    cfg = read_config(BASIC, NB)

    assert cfg.questions == {
        "q1": Question("q1", "Question 1", 25),
        "q2": Question("q2", "Question 2", 10.5),
    }


def test_questions_table_form():
    src = notebook(
        """
[tool.marimo-gradescope.questions]
q1 = 25
q2 = { points = 10, title = "Gradients" }

[tool.marimo-gradescope.questions.essay]
points = 5
manual = true
"""
    )

    cfg = read_config(src, NB)

    assert cfg.questions == {
        "q1": Question("q1", "Question 1", 25),
        "q2": Question("q2", "Gradients", 10),
        "essay": Question("essay", "essay", 5, manual=True),
    }


def test_defaults():
    cfg = read_config(BASIC, NB)

    assert cfg == AssignmentConfig(
        name="hw1",
        notebook="hw1.py",
        questions=cfg.questions,
    )
    assert cfg.files == ()
    assert cfg.timeout == 60
    assert cfg.budget == 540
    assert cfg.memory_mb == 4096
    assert cfg.python == "3.13"
    assert cfg.requirements is None


def test_all_keys():
    src = notebook(
        """
[tool.marimo-gradescope]
name = "Assignment 1"
questions = { q1 = 1 }
files = ["data.csv", "img/cat.png"]
timeout = 30
budget = 300.5
memory = 2048
python = "3.14"
requirements = "../requirements.txt"
"""
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


def test_missing_header():
    with pytest.raises(ConfigError, match="# /// script"):
        read_config(CODE, NB)


def test_missing_tool_table():
    with pytest.raises(ConfigError, match=r"\[tool\.marimo-gradescope\]"):
        read_config(notebook('dependencies = ["numpy"]'), NB)


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("questions = { q1 = 'ten' }", "q1"),
        ("questions = { q1 = true }", "q1"),
        ("questions = { q1 = -1 }", "q1"),
        ("questions = { q1 = { title = 'x' } }", "q1"),
        ("questions = { q1 = { points = 1, bonus = 2 } }", "bonus"),
        ("questions = { q1 = { points = 1, manual = 'yes' } }", "manual"),
        ("questions = { q_1 = 1 }", "q_1"),
        ("questions = {}", "questions"),
        ("name = 1\nquestions = { q1 = 1 }", "name"),
        ("questions = { q1 = 1 }\nmemory = 1000", "memory"),
        ("questions = { q1 = 1 }\nmemory = 4096.5", "memory"),
        ("questions = { q1 = 1 }\ntimeout = 0", "timeout"),
        ("questions = { q1 = 1 }\nbudget = 'long'", "budget"),
        ("questions = { q1 = 1 }\nfiles = 'data.csv'", "files"),
        ("questions = { q1 = 1 }\npython = 3.13", "python"),
        ("questions = { q1 = 1 }\nrequirements = 1", "requirements"),
        ("questions = { q1 = 1 }\ntimeuot = 10", "timeuot"),
    ],
)
def test_bad_values_name_the_key(table: str, key: str):
    src = notebook("[tool.marimo-gradescope]\n" + table)

    with pytest.raises(ConfigError, match=key):
        read_config(src, NB)


@pytest.mark.parametrize(
    "name", ["../../escaped", "HW 1/2", "a\\\\b", ".hidden", "", "tab\\there", "nul\\u0000"]
)
def test_name_unfit_for_a_file_name(name: str):
    src = notebook(f'[tool.marimo-gradescope]\nname = "{name}"\nquestions = {{ q1 = 1 }}')

    with pytest.raises(ConfigError, match="`name`"):
        read_config(src, NB)


def test_missing_questions():
    src = notebook("[tool.marimo-gradescope]\nname = 'x'")

    with pytest.raises(ConfigError, match="questions"):
        read_config(src, NB)


# strip_tool_table


def test_strip_tool_table_keeps_deps_and_code():
    src = notebook(
        """
requires-python = ">=3.13"
dependencies = [
    "marimo-gradescope",
    "numpy",
]

[tool.marimo-gradescope]
questions = { q1 = 25 }
files = [
    "data.csv",
]

[tool.marimo-gradescope.questions.q2]
points = 5

[tool.uv]
exclude-newer = "2026-11-01T00:00:00Z"
"""
    )

    out = strip_tool_table(src)
    header = read_header(out)

    assert "marimo-gradescope" not in header.get("tool", {})
    assert header["dependencies"] == ["marimo-gradescope", "numpy"]
    assert header["tool"]["uv"] == {"exclude-newer": "2026-11-01T00:00:00Z"}
    assert out.endswith(CODE)


def test_strip_tool_table_at_end_leaves_no_trailing_blank():
    out = strip_tool_table(BASIC)

    assert "marimo-gradescope]" not in out
    assert "#\n# ///" not in out
    assert read_header(out)["dependencies"] == ["marimo-gradescope", "numpy"]
    assert out.endswith(CODE)


def test_strip_tool_table_without_table_is_identity():
    src = notebook('dependencies = ["numpy"]')

    assert strip_tool_table(src) == src


# ensure_runtime_flag


def runtime(src: str) -> dict:
    return read_header(src)["tool"]["marimo"]["runtime"]


def test_ensure_runtime_flag_adds_table():
    out = ensure_runtime_flag(BASIC)

    assert runtime(out) == {"reactive_tests": False}
    assert count(out, "# [tool.marimo.runtime]") == 1
    assert read_config(out, NB) == read_config(BASIC, NB)
    assert out.endswith(CODE)


def test_ensure_runtime_flag_merges_into_table_without_key():
    src = notebook(
        """
dependencies = []

[tool.marimo.runtime]
auto_instantiate = false

[tool.marimo-gradescope]
questions = { q1 = 1 }
"""
    )

    out = ensure_runtime_flag(src)

    assert runtime(out) == {"auto_instantiate": False, "reactive_tests": False}
    assert count(out, "# [tool.marimo.runtime]") == 1


@pytest.mark.parametrize("value", ["true", "false"])
def test_ensure_runtime_flag_with_key_already_set(value: str):
    src = notebook(f"dependencies = []\n\n[tool.marimo.runtime]\nreactive_tests = {value}")

    out = ensure_runtime_flag(src)

    assert runtime(out) == {"reactive_tests": False}
    assert count(out, "# [tool.marimo.runtime]") == 1
    assert ensure_runtime_flag(out) == out


def test_ensure_runtime_flag_is_idempotent():
    once = ensure_runtime_flag(BASIC)

    assert ensure_runtime_flag(once) == once


def test_ensure_runtime_flag_dotted_key_conflict_raises():
    src = notebook("[tool.marimo]\nruntime = { reactive_tests = true }")

    with pytest.raises(ConfigError, match="reactive_tests"):
        ensure_runtime_flag(src)


def test_ensure_runtime_flag_missing_header():
    with pytest.raises(ConfigError, match="# /// script"):
        ensure_runtime_flag(CODE)


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
    assert read_config(out, NB) == read_config(BASIC, NB)


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


def test_read_config_keeps_unicode_line_separator_in_value():
    src = BASIC.replace("[tool.marimo-gradescope]\n", '[tool.marimo-gradescope]\n# name = "a b"\n')

    assert read_config(src, NB).name == "a b"


def test_strip_tool_table_crlf():
    out = strip_tool_table(BASIC.replace("\n", "\r\n"))

    assert "tool" not in read_header(out)

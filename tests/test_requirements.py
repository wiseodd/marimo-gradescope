import dataclasses
from pathlib import Path

import pytest

from mogs.config import ConfigError, read_config, read_header
from mogs.requirements import (
    Index,
    Requirements,
    gradescope_requirements,
    read_requirements,
    script_header,
    with_header,
    write_header,
)

TOOL = "mogs==0.1.0"

CODE = """
import marimo

import mogs

app = marimo.App()

mogs.assignment()
mogs.grade("q1", points=25)
"""

HEADER = """\
# /// script
# requires-python = ">=3.13"
# dependencies = ["marimo", "mogs", "scipy"]
#
# [tool.uv]
# exclude-newer = "2026-01-01T00:00:00Z"
#
# [[tool.uv.index]]
# name = "old"
# url = "https://example.com/old"
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///
"""

DS3000 = """\
numpy==2.4.2
pandas==3.0.0
torch==2.12.0+cpu
lightgbm==4.7.0

--extra-index-url https://download.pytorch.org/whl/cpu
"""


def assignment(tmp_path: Path, requirements: str | None, settings: str = "") -> Path:
    """A notebook in `tmp_path`, with `requirements.txt` next to it unless `None`."""

    notebook = tmp_path / "hw.py"
    code = CODE.replace("assignment()", f"assignment({settings})")
    notebook.write_text(HEADER + code, encoding="utf-8")

    if requirements is not None:
        (tmp_path / "requirements.txt").write_text(requirements, encoding="utf-8")

    return notebook


def requirements_of(tmp_path: Path, text: str) -> Requirements:
    notebook = assignment(tmp_path, text)

    return read_requirements(read_config(notebook.read_text(), notebook))


# Reading the file


def test_file_defaults_to_requirements_txt_next_to_the_notebook(tmp_path):
    requirements = requirements_of(tmp_path, "numpy==2.0\n")

    assert requirements.path == tmp_path / "requirements.txt"
    assert requirements.packages == ("numpy==2.0",)


def test_file_relative_to_the_notebook(tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "reqs.txt").write_text("numpy\n", encoding="utf-8")
    notebook = assignment(tmp_path, None, 'requirements="env/reqs.txt"')

    requirements = read_requirements(read_config(notebook.read_text(), notebook))

    assert requirements.path == tmp_path / "env" / "reqs.txt"
    assert requirements.packages == ("numpy",)


def test_missing_file_names_it(tmp_path):
    notebook = assignment(tmp_path, None)

    with pytest.raises(ConfigError, match="requirements.txt") as error:
        read_requirements(read_config(notebook.read_text(), notebook))

    assert str(tmp_path / "requirements.txt") in str(error.value)


def test_config_without_a_file_is_a_bug():
    cfg = dataclasses.replace(read_config(HEADER + CODE, Path("/x/hw.py")), requirements=None)

    with pytest.raises(ValueError, match="no requirements file"):
        read_requirements(cfg)


def test_lines_as_written(tmp_path):
    text = (
        "# pinned\n"
        "\n"
        "numpy==2.0  # inline\n"
        "torch==2.0+cpu ; sys_platform != 'darwin'\n"
        "pkg @ https://example.com/pkg-1.0-py3-none-any.whl\n"
        "--index-url https://pypi.org/simple\n"
        "--extra-index-url=https://download.pytorch.org/whl/cpu\n"
        "-i https://example.com/simple\n"
    )

    requirements = requirements_of(tmp_path, text)

    assert requirements.packages == (
        "numpy==2.0",
        "torch==2.0+cpu ; sys_platform != 'darwin'",
        "pkg @ https://example.com/pkg-1.0-py3-none-any.whl",
    )
    assert requirements.indexes == (
        Index("https://pypi.org/simple", default=True),
        Index("https://download.pytorch.org/whl/cpu"),
        Index("https://example.com/simple", default=True),
    )
    assert requirements.text == text


def test_mogs_lines_are_left_out(tmp_path):
    requirements = requirements_of(tmp_path, "mogs>=0.0.1\nnumpy\nMogs[pdf]\n")

    assert requirements.packages == ("numpy",)


@pytest.mark.parametrize(
    "line",
    [
        "-r other.txt",
        "-r https://example.com/requirements.txt",
        "--requirement=other.txt",
        "-c constraints.txt",
        "-e .",
        "-f https://example.com/links",
        "--find-links ./wheels",
        "--pre",
        "--hash=sha256:abc",
        "git+https://github.com/me/repo@v1",
        "numpy >> 1",
    ],
)
def test_other_option_lines_are_refused(line, tmp_path):
    with pytest.raises(ConfigError, match="--extra-index-url") as error:
        requirements_of(tmp_path, f"numpy\n{line}\n")

    assert line in str(error.value)


@pytest.mark.parametrize(
    "line",
    [
        "./pkg",
        "../wheels/pkg-1.0-py3-none-any.whl",
        "/home/me/pkg",
        "pkg-1.0.tar.gz",
        "pkg @ file:///home/me/pkg-1.0.whl",
        "--extra-index-url file:///home/me/simple",
        "--index-url=file:///home/me/simple",
        "-i /home/me/simple",
    ],
)
def test_local_files_are_refused(line, tmp_path):
    with pytest.raises(ConfigError, match="local file") as error:
        requirements_of(tmp_path, f"numpy\n{line}\n")

    assert line in str(error.value)


# The header


def test_header_of_pins_and_an_extra_index(tmp_path):
    requirements = requirements_of(tmp_path, DS3000)

    assert script_header(requirements, "3.13", "mogs") == [
        'requires-python = "==3.13.*"',
        "dependencies = [",
        '    "numpy==2.4.2",',
        '    "pandas==3.0.0",',
        '    "torch==2.12.0+cpu",',
        '    "lightgbm==4.7.0",',
        '    "mogs",',
        "]",
        "",
        "[tool.uv]",
        'index-strategy = "unsafe-best-match"',
        "",
        "[[tool.uv.index]]",
        'name = "index-1"',
        'url = "https://download.pytorch.org/whl/cpu"',
    ]


def test_header_index_url_is_the_default():
    requirements = Requirements(("numpy",), (Index("https://example.com/simple", default=True),))

    assert script_header(requirements, "3.12", TOOL) == [
        'requires-python = "==3.12.*"',
        "dependencies = [",
        '    "numpy",',
        '    "mogs==0.1.0",',
        "]",
        "",
        "[[tool.uv.index]]",
        'name = "index-1"',
        'url = "https://example.com/simple"',
        "default = true",
    ]


def test_header_of_an_empty_file():
    assert script_header(Requirements(), "3.13.5", "mogs") == [
        'requires-python = "==3.13.5.*"',
        "dependencies = [",
        '    "mogs",',
        "]",
    ]


@pytest.mark.parametrize("python", ["latest", "3", "3.13; rm -rf /", ""])
def test_header_needs_a_python_version(python):
    with pytest.raises(ConfigError, match="`python`"):
        script_header(Requirements(), python, "mogs")


def test_with_header_keeps_tool_tables_but_uv_ones():
    out = with_header(HEADER + CODE, Requirements(("numpy",)), "3.13", "mogs")

    assert out == (
        "# /// script\n"
        '# requires-python = "==3.13.*"\n'
        "# dependencies = [\n"
        '#     "numpy",\n'
        '#     "mogs",\n'
        "# ]\n"
        "#\n"
        "# [tool.marimo.runtime]\n"
        "# reactive_tests = false\n"
        "# ///\n" + CODE
    )


def test_with_header_adds_a_missing_header():
    out = with_header(CODE.lstrip(), Requirements(), "3.13", "mogs")

    assert out == (
        "# /// script\n"
        '# requires-python = "==3.13.*"\n'
        "# dependencies = [\n"
        '#     "mogs",\n'
        "# ]\n"
        "# ///\n"
        "\n" + CODE.lstrip()
    )


def test_with_header_refuses_an_invalid_old_header():
    src = HEADER.replace('"scipy"]', '"scipy"') + CODE

    with pytest.raises(ConfigError, match="TOML"):
        with_header(src, Requirements(), "3.13", "mogs")


# Writing the source notebook's header


def test_write_header_warns_about_packages_only_the_header_lists(tmp_path):
    notebook = assignment(tmp_path, "numpy\n", 'python="3.12"')

    warnings = write_header(notebook)

    header = read_header(notebook.read_text(encoding="utf-8"))
    assert header["requires-python"] == "==3.12.*"
    assert header["dependencies"] == ["numpy", "mogs"]
    assert header["tool"] == {"marimo": {"runtime": {"reactive_tests": False}}}
    assert warnings == [
        f"The header of {notebook} listed 'scipy', which {tmp_path / 'requirements.txt'} does "
        f"not. Add them to {tmp_path / 'requirements.txt'}; mogs rewrites the header from it."
    ]


def test_write_header_is_quiet_when_the_file_lists_everything(tmp_path):
    notebook = assignment(tmp_path, "SciPy>=1\n")

    assert write_header(notebook) == []
    assert write_header(notebook) == []


def test_write_header_needs_no_questions(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text("import mogs\n\nmogs.assignment()\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")

    assert write_header(notebook) == []
    assert notebook.read_text(encoding="utf-8").startswith("# /// script\n")


def test_write_header_names_a_missing_file(tmp_path):
    notebook = assignment(tmp_path, None)
    before = notebook.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="requirements.txt"):
        write_header(notebook)

    assert notebook.read_text(encoding="utf-8") == before


# Gradescope's file


def test_gradescope_gets_the_file_as_written_and_mogs(tmp_path):
    requirements = requirements_of(tmp_path, DS3000)

    text, warnings = gradescope_requirements(requirements, TOOL)

    assert text == DS3000 + TOOL + "\n"
    assert warnings == []


def test_gradescope_file_without_trailing_newline():
    text, _ = gradescope_requirements(Requirements(text="numpy==2.0"), TOOL)

    assert text == f"numpy==2.0\n{TOOL}\n"


def test_gradescope_file_drops_its_own_mogs_line():
    requirements = Requirements(text="mogs>=0.0.1\nnumpy\nMogs[pdf]\n")

    text, warnings = gradescope_requirements(requirements, TOOL)

    assert text == f"numpy\n{TOOL}\n"
    assert len(warnings) == 1
    assert "mogs>=0.0.1" in warnings[0]


@pytest.mark.parametrize("version", ["0.1.0.dev3", "0.1.dev0+g1234", "0.1.0+local"])
def test_unreleased_version_warns(version):
    tool = f"mogs=={version}"

    text, warnings = gradescope_requirements(Requirements(text="numpy\n"), tool)

    assert text == f"numpy\n{tool}\n"
    assert len(warnings) == 1
    assert version in warnings[0]
    assert "not on PyPI" in warnings[0]


def test_tool_requirement_must_name_the_tool():
    with pytest.raises(ValueError, match="mogs"):
        gradescope_requirements(Requirements(text="numpy\n"), "numpy==1.0")

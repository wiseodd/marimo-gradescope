import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mogs.config import ConfigError, read_config
from mogs.requirements import RequirementsError, gradescope_requirements

REPO = Path(__file__).parent.parent
TOOL = "mogs==0.1.0"

CODE = """
import marimo

import mogs

app = marimo.App()

mogs.assignment()
mogs.grade("q1", points=25)
"""


def notebook(header: str, settings: str = "") -> str:
    lines = [f"# {line}" if line else "#" for line in header.strip("\n").splitlines()]
    code = CODE.replace("mogs.assignment()", f"mogs.assignment({settings})")
    return "# /// script\n" + "\n".join(lines) + "\n# ///\n" + code


def with_python(src: str, python: str) -> str:
    return src.replace("mogs.assignment(", f'mogs.assignment(python="{python}", ', 1)


TORCH = notebook(
    """
requires-python = ">=3.13"
dependencies = ["marimo", "mogs==0.0.0", "torch"]

[tool.uv.sources]
torch = [{ index = "pytorch-cpu", marker = "sys_platform != 'darwin'" }]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true
"""
)


def tool_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("mogs")]


def requirement_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith(("#", " ", "-"))]


def config(src: str, tmp_path: Path):
    path = tmp_path / "hw.py"
    path.write_text(src, encoding="utf-8")
    return read_config(src, path)


# From the header, through `uv export`


@pytest.mark.network
def test_header_with_explicit_index(tmp_path):
    text, warnings = gradescope_requirements(TORCH, config(TORCH, tmp_path), TOOL)
    lines = text.splitlines()

    assert lines[0] == "--extra-index-url https://download.pytorch.org/whl/cpu"
    assert any(line.startswith("torch==") and "+cpu" in line for line in lines)
    assert any(line.startswith("marimo==") for line in lines)
    assert tool_lines(text) == [TOOL]
    assert lines[-1] == TOOL
    assert warnings == []


@pytest.mark.network
def test_header_requirements_install(tmp_path):
    # The real tool line pins a release; a local path lets the dry run resolve it.
    tool = f"mogs @ {REPO.as_uri()}"
    text, _ = gradescope_requirements(TORCH, config(TORCH, tmp_path), tool)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(text, encoding="utf-8")
    uv = shutil.which("uv")
    assert uv is not None

    subprocess.run(
        [uv, "venv", "-q", "--python", "3.13", str(tmp_path / "venv")], check=True, cwd=tmp_path
    )
    result = subprocess.run(
        [uv, "pip", "install", "--dry-run", "--python", str(tmp_path / "venv" / "bin" / "python")]
        # Resolved for Gradescope's Linux, wherever the test runs: the CPU index is only
        # used off macOS.
        + ["--python-platform", "x86_64-manylinux_2_28"]
        + ["--index-strategy", "unsafe-best-match", "-r", str(requirements)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "+cpu" in result.stderr


@pytest.mark.network
def test_header_without_indexes(tmp_path):
    src = notebook('dependencies = ["mogs", "pytest"]\n\n')

    text, _ = gradescope_requirements(src, config(src, tmp_path), TOOL)

    assert not any(line.startswith("-") for line in text.splitlines())
    assert any(line.startswith("pytest==") for line in text.splitlines())
    assert tool_lines(text) == [TOOL]
    assert "# via" not in text


@pytest.mark.network
def test_default_index_becomes_index_url(tmp_path):
    src = notebook(
        'dependencies = ["pytest"]\n\n[[tool.uv.index]]\nname = "main"\n'
        'url = "https://pypi.org/simple"\ndefault = true\n'
    )

    text, _ = gradescope_requirements(src, config(src, tmp_path), TOOL)

    assert text.splitlines()[0] == "--index-url https://pypi.org/simple"
    assert "--extra-index-url" not in text


@pytest.mark.parametrize(
    "uv",
    [
        '[[tool.uv.index]]\nname = "w"\nurl = "https://example.com/wheels"\nformat = "flat"',
        '[tool.uv]\nindex-url = "https://example.com/simple"',
        '[tool.uv]\nextra-index-url = ["https://example.com/simple"]',
        '[tool.uv]\nfind-links = ["https://example.com/links"]',
        '[[tool.uv.index]]\nname = "x"',
    ],
)
def test_unsupported_index_config(uv, tmp_path):
    src = notebook(f'dependencies = ["pytest"]\n\n{uv}\n')

    with pytest.raises(ConfigError, match="tool.uv"):
        gradescope_requirements(src, config(src, tmp_path), TOOL)


def test_uv_export_error_shows_stderr(tmp_path):
    src = notebook('dependencies = ["numpy >> 1"]\n')

    with pytest.raises(RequirementsError, match="uv export") as error:
        gradescope_requirements(src, config(src, tmp_path), TOOL)

    assert "numpy >> 1" in str(error.value)


# From a requirements file


OVERRIDE = notebook(
    """
dependencies = ["mogs", "torch"]
""",
    'requirements="env/requirements.txt"',
)


def test_header_index_with_file_url_rejected(tmp_path):
    src = TORCH.replace("https://download.pytorch.org/whl/cpu", "file:///home/me/simple")

    with pytest.raises(ConfigError, match="file:///home/me/simple"):
        gradescope_requirements(src, config(src, tmp_path), TOOL)


def test_override_file_relative_to_notebook(tmp_path):
    folder = tmp_path / "A_01"
    (folder / "env").mkdir(parents=True)
    (folder / "env" / "requirements.txt").write_text(
        "numpy==2.0\n# pinned\n--extra-index-url https://example.com/simple\n", encoding="utf-8"
    )
    path = folder / "hw.py"
    path.write_text(OVERRIDE, encoding="utf-8")

    text, warnings = gradescope_requirements(OVERRIDE, read_config(OVERRIDE, path), TOOL)

    assert text == f"numpy==2.0\n# pinned\n--extra-index-url https://example.com/simple\n{TOOL}\n"
    assert warnings == []


def test_override_file_without_trailing_newline(tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text("numpy==2.0", encoding="utf-8")

    text, _ = gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), TOOL)

    assert text == f"numpy==2.0\n{TOOL}\n"


def test_override_file_drops_its_own_tool_line(tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text(
        "mogs>=0.0.1\nnumpy\nMogs[pdf]\n", encoding="utf-8"
    )

    text, warnings = gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), TOOL)

    assert text == f"numpy\n{TOOL}\n"
    assert tool_lines(text) == [TOOL]
    assert len(warnings) == 1
    assert "mogs>=0.0.1" in warnings[0]


@pytest.mark.parametrize(
    "line",
    [
        "-r other.txt",
        "--requirement=other.txt",
        "-c constraints.txt",
        "--constraint constraints.txt",
        "-e .",
        "--editable ./pkg",
        "./pkg",
        "../wheels/pkg-1.0-py3-none-any.whl",
        "/home/me/pkg",
        "dist/pkg-1.0-py3-none-any.whl",
        "pkg-1.0.tar.gz",
        "--find-links ./wheels",
        "-f wheels",
        "pkg @ file:///home/me/pkg-1.0.whl",
        "file:///home/me/pkg-1.0-py3-none-any.whl",
        "git+file:///home/me/repo",
        "-r file:///home/me/other.txt",
        "-e file:///home/me/pkg",
        "--extra-index-url file:///home/me/simple",
        "--index-url=file:///home/me/simple",
        "-i /home/me/simple",
    ],
)
def test_override_file_rejects_local_paths(line, tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text(f"numpy\n{line}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="requirements") as error:
        gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), TOOL)

    assert line in str(error.value)


def test_override_file_keeps_urls(tmp_path):
    lines = (
        "https://example.com/pkg-1.0-py3-none-any.whl\n"
        "https://example.com/pkg-1.0.tar.gz ; python_version >= '3.13'\n"
        "git+https://github.com/me/repo@v1\n"
        "-r https://example.com/requirements.txt\n"
        "--requirement=https://example.com/requirements.txt\n"
        "-c https://example.com/constraints.txt\n"
        "--extra-index-url https://download.pytorch.org/whl/cpu\n"
        "--index-url=https://pypi.org/simple\n"
        "-i https://pypi.org/simple\n"
        "--find-links https://example.com/links\n"
        "torch==2.0+cpu ; sys_platform != 'darwin'  # cpu\n"
        "pkg @ https://example.com/pkg-1.0-py3-none-any.whl\n"
        "scipy==1.0 \\\n"
        "    --hash=sha256:abc\n"
        "\n"
        "# ./not/a/path\n"
    )
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text(lines, encoding="utf-8")

    text, _ = gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), TOOL)

    assert text == lines + TOOL + "\n"


@pytest.mark.parametrize(
    ("requires", "python", "warns"),
    [(">=3.13", "3.12", True), (">=3.13", "3.13", False), (">=3.11,<3.13", "3.13.1", True)],
)
def test_python_outside_requires_python_warns(requires, python, warns, tmp_path):
    src = OVERRIDE.replace("# dependencies", f'# requires-python = "{requires}"\n# dependencies')
    src = with_python(src, python)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text("numpy\n", encoding="utf-8")

    _, warnings = gradescope_requirements(src, config(src, tmp_path), TOOL)

    if warns:
        assert len(warnings) == 1
        assert python in warnings[0] and requires in warnings[0]
    else:
        assert warnings == []


def test_invalid_requires_python(tmp_path):
    src = OVERRIDE.replace("# dependencies", '# requires-python = "three"\n# dependencies')
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text("numpy\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="requires-python"):
        gradescope_requirements(src, config(src, tmp_path), TOOL)


def test_override_file_missing(tmp_path):
    with pytest.raises(ConfigError, match="requirements.txt"):
        gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), TOOL)


# Tool line


@pytest.mark.parametrize("version", ["0.1.0.dev3", "0.1.dev0+g1234", "0.1.0+local"])
def test_unreleased_version_warns(version, tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    tool = f"mogs=={version}"

    text, warnings = gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), tool)

    assert tool_lines(text) == [tool]
    assert len(warnings) == 1
    assert version in warnings[0]
    assert "not on PyPI" in warnings[0]


def test_tool_requirement_must_name_the_tool(tmp_path):
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "requirements.txt").write_text("numpy\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mogs"):
        gradescope_requirements(OVERRIDE, config(OVERRIDE, tmp_path), "numpy==1.0")


@pytest.mark.slow
def test_wheel_ships_typst_licenses(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None

    subprocess.run([uv, "build", "--wheel", "-q", "-o", str(tmp_path)], check=True, cwd=REPO)
    wheel = next(tmp_path.glob("*.whl"))
    names = subprocess.run(
        [sys.executable, "-m", "zipfile", "-l", str(wheel)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # The templates of `setup.sh` and `run_autograder` are in the extension module.
    assert "templates/" not in names

    # The Typst packages the PDF of written answers needs are in the extension module; their
    # licenses ship with the wheel's metadata.
    assert "mogs/_typst/" not in names
    for package in ("cmarker/0.1.10", "mitex/0.2.7"):
        assert f"crates/mogs-pdf/typst/preview/{package}/LICENSE" in names

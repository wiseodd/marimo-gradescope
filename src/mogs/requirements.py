"""The assignment's requirements file, and what mogs makes from it.

The file lists the packages in pip's format. mogs writes the notebook's PEP 723 header from
it, and Gradescope installs it as written, with one line for mogs added.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from mogs.config import (
    ASSIGNMENT,
    HEADER_RE,
    TOOL,
    AssignmentConfig,
    ConfigError,
    UsageError,
    normalize_newlines,
    normalized_name,
    read_assignment_config,
    read_header,
    replace_header,
)

PIN_RE = re.compile(r"==\s*([^\s;,]+)")
OPTION_RE = re.compile(r"(--?[A-Za-z][A-Za-z-]*)(?:\s*=\s*|\s+|$)(.*)")
ARCHIVE_RE = re.compile(r"\.(whl|tar\.gz|zip)$")
# A remote URL, such as `https://...` or `git+https://...`; `file:` URLs are local.
REMOTE_URL_RE = re.compile(r"^(?![a-z+]*file:)[a-z][a-z0-9+.-]*://", re.IGNORECASE)
INDEX_OPTIONS = ("-i", "--index-url")
EXTRA_INDEX_OPTIONS = ("--extra-index-url",)
# A Python version such as `3.13` or `3.13.5`, as `uv venv --python` takes it on Gradescope.
PYTHON_RE = re.compile(r"3\.\d+(\.\d+)?")
# mogs depends on marimo, so a header that lists marimo needs no line of its own for it.
MARIMO = "marimo"


class RequirementsError(UsageError):
    """The notebook's environment cannot be set up."""


@dataclass(frozen=True, slots=True)
class Index:
    """A package index of the requirements file.

    Attributes:
        url: Its URL.
        default: Given with `--index-url`, so it replaces PyPI.
    """

    url: str
    default: bool = False


@dataclass(frozen=True, slots=True)
class Requirements:
    """A requirements file, read and checked.

    Attributes:
        packages: Its requirements, without comments or mogs lines, in file order.
        indexes: Its index options, in file order.
        text: The file as written.
        path: The file.
    """

    packages: tuple[str, ...] = ()
    indexes: tuple[Index, ...] = ()
    text: str = ""
    path: Path = Path("requirements.txt")


def read_requirements(cfg: AssignmentConfig) -> Requirements:
    """Read and check the requirements file of `cfg`.

    Raises:
        ValueError: If `cfg` was not read from a notebook, so it names no file.
        ConfigError: If the file is missing, or has a line other than a requirement, an
            `--index-url` or `--extra-index-url` option, a comment or a blank line. The
            message names the file and the line.
    """

    if cfg.requirements is None:
        raise ValueError("The config names no requirements file.")

    path = cfg.requirements

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{ASSIGNMENT} `requirements`: cannot read {path}: {e}") from e

    packages: list[str] = []
    indexes: list[Index] = []

    for line in text.splitlines():
        match _parse_line(line, path):
            case None:
                pass
            case Index() as index:
                indexes.append(index)
            case str() as package if normalized_name(package) != TOOL:
                packages.append(package)
            case str():
                pass

    return Requirements(tuple(packages), tuple(indexes), text, path)


def _parse_line(line: str, path: Path) -> str | Index | None:
    """The requirement or index of one line of the file at `path`; `None` for no code."""

    code = re.split(r"(^|\s)#", line, maxsplit=1)[0].strip()
    if not code:
        return None

    local = ConfigError(
        f"{ASSIGNMENT} `requirements`: {path} has {line.strip()!r}, which points at a local "
        "file that Gradescope does not have. Use package names or URLs."
    )
    unsupported = ConfigError(
        f"{ASSIGNMENT} `requirements`: {path} has {line.strip()!r}. Only requirements, "
        "`--index-url` and `--extra-index-url` lines are supported."
    )

    option = OPTION_RE.fullmatch(code)
    if option is not None:
        name, value = option.groups()
        value = value.strip()

        if name not in INDEX_OPTIONS + EXTRA_INDEX_OPTIONS:
            raise unsupported
        if not REMOTE_URL_RE.match(value):
            raise local

        return Index(value, default=name in INDEX_OPTIONS)

    target = code.split(";", maxsplit=1)[0].strip()
    if "@" in target:
        if target.split("@", maxsplit=1)[1].strip().startswith("file:"):
            raise local
    elif "/" in target or "\\" in target or target.startswith(("~", ".")):
        raise local
    elif ARCHIVE_RE.search(target):
        raise local

    try:
        Requirement(code)
    except InvalidRequirement as e:
        raise unsupported from e

    return code


def script_header(requirements: Requirements, python: str, tool_requirement: str) -> list[str]:
    """The TOML lines of the notebook's PEP 723 header.

    It requires `python`, and depends on the requirements, then on `tool_requirement`. Each
    index becomes a `[[tool.uv.index]]`, and with an extra index uv resolves across all of
    them, as pip does.

    Raises:
        ConfigError: If `python` is not a version such as "3.13".
    """

    if not PYTHON_RE.fullmatch(python):
        raise ConfigError(
            f'{ASSIGNMENT} `python` must be a version such as "3.13", got {python!r}.'
        )

    dependencies = [*requirements.packages, tool_requirement]
    lines = [f"requires-python = {json.dumps(f'=={python}.*')}", "dependencies = ["]
    lines += [f"    {json.dumps(dependency)}," for dependency in dependencies]
    lines += ["]"]

    if any(not index.default for index in requirements.indexes):
        lines += ["", "[tool.uv]", 'index-strategy = "unsafe-best-match"']

    for number, index in enumerate(requirements.indexes, start=1):
        lines += ["", "[[tool.uv.index]]", f'name = "index-{number}"']
        lines += [f"url = {json.dumps(index.url)}"]

        if index.default:
            lines += ["default = true"]

    return lines


def with_header(src: str, requirements: Requirements, python: str, tool_requirement: str) -> str:
    """`src` with the header that `script_header` gives, in place of its own.

    Raises:
        ConfigError: If `python` is invalid or the old header is not valid TOML.
    """

    return replace_header(src, script_header(requirements, python, tool_requirement))


def write_header(notebook: Path) -> list[str]:
    """Write the header of the source notebook from its requirements file.

    The header depends on plain mogs, which brings marimo.

    Returns:
        A warning when the old header lists packages that the file does not, such as those
        that marimo's package panel adds there.

    Raises:
        ConfigError: If the config, the requirements file or the old header is invalid.
        OSError: If the notebook cannot be read or written.
    """

    src = notebook.read_text(encoding="utf-8")
    cfg = read_assignment_config(src, notebook)
    requirements = read_requirements(cfg)
    has_header = HEADER_RE.search(normalize_newlines(src)) is not None
    old = read_header(src).get("dependencies", []) if has_header else []

    listed = {normalized_name(package) for package in requirements.packages}
    listed |= {TOOL, MARIMO}
    missing = [dep for dep in old if normalized_name(dep) not in listed]

    text = with_header(src, requirements, cfg.python, TOOL)
    if text != src:
        notebook.write_text(text, encoding="utf-8")

    if not missing:
        return []

    return [
        f"The header of {notebook} listed {', '.join(map(repr, missing))}, which "
        f"{requirements.path} does not. Add them to {requirements.path}; mogs rewrites the "
        "header from it."
    ]


def gradescope_requirements(
    requirements: Requirements, tool_requirement: str
) -> tuple[str, list[str]]:
    """Build the text of Gradescope's `requirements.txt`.

    It is the file as written, minus any mogs line, then `tool_requirement`.

    Args:
        requirements: The assignment's requirements file.
        tool_requirement: The mogs line to append, such as `mogs==0.1.0`.

    Returns:
        The file text, and warnings for the instructor.

    Raises:
        ValueError: If `tool_requirement` is not a mogs requirement.
    """

    if normalized_name(tool_requirement) != TOOL:
        raise ValueError(f"Expected a {TOOL} requirement, got {tool_requirement!r}.")

    warnings = _version_warnings(tool_requirement)
    text = requirements.text
    lines = [line for line in text.splitlines() if normalized_name(line) != TOOL]
    ignored = [line.strip() for line in text.splitlines() if normalized_name(line) == TOOL]

    if ignored:
        warnings.append(
            f"Ignored {', '.join(map(repr, ignored))} in {requirements.path}: the build "
            f"adds {tool_requirement!r}."
        )

    return "".join(f"{line}\n" for line in [*lines, tool_requirement]), warnings


def _version_warnings(tool_requirement: str) -> list[str]:
    """Warn when the pinned mogs is a dev or local version."""

    match = PIN_RE.search(tool_requirement)
    if match is None:
        return []

    version = match.group(1)
    if ".dev" in version or "+" in version:
        return [
            f"{TOOL} {version} is a development version, not on PyPI; Gradescope can't install it."
        ]

    return []

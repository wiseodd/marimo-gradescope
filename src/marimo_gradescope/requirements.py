"""The `requirements.txt` that Gradescope installs.

It comes from the notebook header's dependencies, pinned by `uv export`, or from the file
that `mogs.assignment(requirements=...)` names. Either way it ends with one line for
marimo-gradescope itself.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from marimo_gradescope.config import (
    ASSIGNMENT,
    TOOL,
    AssignmentConfig,
    ConfigError,
    normalized_name,
    read_header,
    remove_dependency,
)

PIN_RE = re.compile(r"==\s*([^\s;,]+)")
OPTION_RE = re.compile(r"(--?[A-Za-z][A-Za-z-]*)(?:\s*=\s*|\s+|$)(.*)")
ARCHIVE_RE = re.compile(r"\.(whl|tar\.gz|zip)$")
# A remote URL, such as `https://...` or `git+https://...`; `file:` URLs are local.
REMOTE_URL_RE = re.compile(r"^(?![a-z+]*file:)[a-z][a-z0-9+.-]*://", re.IGNORECASE)
# Options that point at files next to the requirements file.
LOCAL_OPTIONS = ("-r", "--requirement", "-c", "--constraint", "-e", "--editable")
LINK_OPTIONS = ("-f", "--find-links")
INDEX_OPTIONS = ("-i", "--index-url", "--extra-index-url")
# uv's older index settings, which `uv export` does not carry over either.
LEGACY_INDEX_KEYS = ("index-url", "extra-index-url", "find-links")


class RequirementsError(Exception):
    """`uv export` could not pin the header's dependencies."""


def gradescope_requirements(
    src: str, cfg: AssignmentConfig, tool_requirement: str
) -> tuple[str, list[str]]:
    """Build the text of Gradescope's `requirements.txt`.

    With `cfg.requirements`, the file is used as it is, minus any marimo-gradescope line.
    Otherwise `uv export` pins the header's dependencies, without marimo-gradescope, and each
    `[[tool.uv.index]]` becomes an `--extra-index-url` line (`--index-url` for the default
    index), since the export drops them.

    Args:
        src: Notebook source.
        cfg: Its config.
        tool_requirement: The marimo-gradescope line to append, such as
            `marimo-gradescope==0.1.0`.

    Returns:
        The file text, and warnings for the instructor.

    Raises:
        ValueError: If `tool_requirement` is not a marimo-gradescope requirement.
        ConfigError: If the requirements file is missing or names local files, or the header
            is invalid or uses index settings that cannot be carried over.
        RequirementsError: If `uv export` fails.
    """

    if normalized_name(tool_requirement) != TOOL:
        raise ValueError(f"Expected a {TOOL} requirement, got {tool_requirement!r}.")

    warnings = _version_warnings(tool_requirement) + _python_warnings(src, cfg)

    if cfg.requirements is None:
        lines = _index_lines(src)
        lines += _uv_export(remove_dependency(src, TOOL)).splitlines()
    else:
        text = _read_requirements(cfg.requirements)
        for line in text.splitlines():
            _check_portable(line, cfg.requirements)

        lines = [line for line in text.splitlines() if normalized_name(line) != TOOL]
        ignored = [line.strip() for line in text.splitlines() if normalized_name(line) == TOOL]

        if ignored:
            warnings.append(
                f"Ignored {', '.join(map(repr, ignored))} in {cfg.requirements}: the build "
                f"adds {tool_requirement!r}."
            )

    return "".join(f"{line}\n" for line in [*lines, tool_requirement]), warnings


def _read_requirements(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{ASSIGNMENT} `requirements`: cannot read {path}: {e}") from e


def _check_portable(line: str, path: Path) -> None:
    """Reject a requirements line that only works next to the original file."""

    code = re.split(r"(^|\s)#", line, maxsplit=1)[0].strip().removesuffix("\\").strip()
    if not code:
        return

    error = ConfigError(
        f"{ASSIGNMENT} `requirements`: {path} has {line.strip()!r}, which points at a local "
        "file that Gradescope does not have. Use package names or URLs."
    )

    option = OPTION_RE.fullmatch(code)
    if option is not None:
        name, value = option.groups()
        remote = REMOTE_URL_RE.match(value.strip())
        if name in LOCAL_OPTIONS + LINK_OPTIONS + INDEX_OPTIONS and not remote:
            raise error
        return

    target = code.split(";", maxsplit=1)[0].strip()
    if REMOTE_URL_RE.match(target):
        return
    if "@" in target:
        if target.split("@", maxsplit=1)[1].strip().startswith("file:"):
            raise error
    elif "/" in target or "\\" in target or target.startswith(("~", ".")):
        raise error
    elif ARCHIVE_RE.search(target):
        raise error


def _index_lines(src: str) -> list[str]:
    """Requirements-file index options for the header's `[[tool.uv.index]]` entries."""

    uv = read_header(src).get("tool", {}).get("uv", {})
    for key in LEGACY_INDEX_KEYS:
        if key in uv:
            raise ConfigError(
                f"[tool.uv] `{key}` is not supported; use [[tool.uv.index]] entries instead."
            )

    lines = []

    match uv.get("index", []):
        case list() as indexes:
            for index in indexes:
                match index:
                    case {"format": "flat"}:
                        raise ConfigError(
                            f'[[tool.uv.index]] with format = "flat" is not supported: {index!r}.'
                        )
                    case {"url": str() as url} if not REMOTE_URL_RE.match(url):
                        raise ConfigError(
                            f"[[tool.uv.index]] url {url!r} is local; Gradescope does not have "
                            "it. Use a remote index URL."
                        )
                    case {"url": str() as url, "default": True}:
                        lines.insert(0, f"--index-url {url}")
                    case {"url": str() as url}:
                        lines.append(f"--extra-index-url {url}")
                    case _:
                        raise ConfigError(f"[[tool.uv.index]] entry without a `url`: {index!r}.")
        case other:
            raise ConfigError(f"[[tool.uv.index]] must be an array of tables, got {other!r}.")

    return lines


def _uv_export(src: str) -> str:
    """Pin the dependencies in the header of `src` with `uv export --script`."""

    uv = shutil.which("uv")
    if uv is None:
        raise RequirementsError("`uv` is not on PATH; it is needed to pin the dependencies.")

    # A temporary folder, so uv picks up no project around the current directory.
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "notebook.py"
        script.write_text(src, encoding="utf-8")

        command = [uv, "export", "--script", str(script), "--format", "requirements-txt"]
        command += ["--no-hashes", "--no-header", "--no-annotate", "--quiet"]
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=folder)

    if result.returncode != 0:
        raise RequirementsError(
            f"`uv export` could not pin the header's dependencies:\n{result.stderr.strip()}"
        )

    return result.stdout


def _version_warnings(tool_requirement: str) -> list[str]:
    """Warn when the pinned marimo-gradescope is a dev or local version."""

    match = PIN_RE.search(tool_requirement)
    if match is None:
        return []

    version = match.group(1)
    if ".dev" in version or "+" in version:
        return [
            f"{TOOL} {version} is a development version, not on PyPI; Gradescope can't install it."
        ]

    return []


def _python_warnings(src: str, cfg: AssignmentConfig) -> list[str]:
    """Warn when `cfg.python` is outside the header's `requires-python`."""

    match read_header(src).get("requires-python"):
        case None:
            return []
        case str() as spec:
            pass
        case other:
            raise ConfigError(f"The header's `requires-python` must be a string, got {other!r}.")

    try:
        specifiers = SpecifierSet(spec)
    except InvalidSpecifier as e:
        raise ConfigError(f"The header's `requires-python` is not valid: {spec!r}.") from e

    if specifiers.contains(cfg.python):
        return []

    return [
        f"{ASSIGNMENT} python={cfg.python!r} does not satisfy the header's "
        f"requires-python = {spec!r}."
    ]

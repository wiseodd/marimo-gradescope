"""What `mogs build` asks of the source notebook, as plain data.

`mogs._native` runs the command in Rust. This module finds, reads and checks the source
notebook, and computes from it what follows Python's rules: the student notebook and the
solved copy, the public tests, the grading bundle, Gradescope's `requirements.txt`, the data
files and the warnings.
"""

import ast
import importlib.metadata
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlparse
from urllib.request import url2pathname

from mogs import __version__
from mogs._native import data_files as shared_data_files
from mogs.config import (
    ASSIGNMENT,
    TOOL,
    Api,
    AssignmentConfig,
    ConfigError,
    UsageError,
    api_calls,
    located,
    read_config,
)
from mogs.grader.bundle import bundle_from_notebook
from mogs.requirements import RequirementsError, gradescope_requirements, read_requirements
from mogs.source import (
    NO_TEXT,
    Notebook,
    parse_notebook,
    prompt_previews,
    source_warnings,
    validate,
)
from mogs.transform import TESTS_FOLDER, pinned_requirement, public_test_files, student_notebook

# What an `assignment(...)` call looks like in a file that is not valid Python.
ASSIGNMENT_RE = re.compile(r"\bassignment\s*\(")
# The files of the autograder zip, which data files must not replace.
ZIP_FILES = ("setup.sh", "run_autograder", "bundle.json", "requirements.txt")
PORTING_URL = "https://mogs.kristia.de/porting"
# Otter Assign's own markers. Its solution and `# HIDDEN` markers are left out: mogs uses them
# too.
OTTER_RE = re.compile(
    r"(?<![\w#])# (?:ASSIGNMENT CONFIG|(?:BEGIN|END) (?:QUESTION|TESTS|PROMPT|TEST CONFIG))\b"
)


class BuildError(UsageError):
    """The assignment cannot be built: no single notebook, invalid source, or a failed check."""


@dataclass(frozen=True, slots=True)
class Assignment:
    """A source notebook, read and validated.

    Attributes:
        path: The source notebook.
        src: Its text.
        cfg: Its config.
        nb: Its parsed cells.
    """

    path: Path
    src: str
    cfg: AssignmentConfig
    nb: Notebook


class BuildInputs(TypedDict):
    """What a build writes and checks.

    The bundle is JSON. `data` holds (path relative to the notebook, source) pairs, and
    `tests` holds (path relative to the notebook, text) pairs.
    `prompts` has, for each manual question, the first line of each of its prompt cells, as
    `prompt_previews` gives it, or `NO_TEXT` when none has text.
    """

    bundle_json: str
    data: list[tuple[str, Path]]
    student: str
    solved: str
    tests: list[tuple[str, str]]
    requirements: str
    warnings: list[str]
    prompts: dict[str, list[str]]


def read_assignment(assignment_dir: Path) -> Assignment:
    """Find, read and validate the source notebook in `assignment_dir`.

    Raises:
        BuildError: If there is no single source notebook, or it is invalid.
        ConfigError: If its config is invalid.
        SourceError: If it cannot be parsed.
    """

    path = find_notebook(assignment_dir)
    src = path.read_text(encoding="utf-8")

    # All three read only the source's lines, so their spans point into `path`.
    with located(path):
        cfg = read_config(src, path)
        nb = parse_notebook(src)
        errors = validate(nb, cfg)

    if errors:
        listed = "\n".join("- " + error.replace("\n", "\n  ") for error in errors)
        raise BuildError(f"{path} has {len(errors)} problem(s):\n{listed}")

    return Assignment(path, src, cfg, nb)


def build_inputs(assignment: Assignment) -> BuildInputs:
    """What a build writes and checks, from the notebook.

    The mogs line of `requirements` and student notebooks pin this version.

    Raises:
        ConfigError: If a data file, the requirements file, the header or `python` is invalid.
        SourceError: If the notebook cannot be transformed.
    """

    src, cfg, nb = assignment.src, assignment.cfg, assignment.nb

    data = data_files(assignment.path.parent, cfg)

    path = assignment.path
    reqs = read_requirements(cfg)
    student = student_notebook(src, nb, cfg, reqs, pinned_requirement(), path=path)
    solved = student_notebook(src, nb, cfg, reqs, pinned_requirement(), solutions=True, path=path)
    bundle = bundle_from_notebook(nb, cfg)
    tests = public_test_files(nb)
    requirements, warnings = gradescope_requirements(reqs, f"{TOOL}=={__version__}")
    prompts = {
        q.id: list(prompt_previews(nb, q.id) or (NO_TEXT,))
        for q in cfg.questions.values()
        if q.manual
    }

    return {
        "bundle_json": bundle.to_json(),
        "student": student,
        "solved": solved,
        "tests": [(str(rel), text) for rel, text in tests.items()],
        "data": [(str(rel), source) for rel, source in data.items()],
        "requirements": requirements,
        "warnings": source_warnings(nb) + warnings,
        "prompts": prompts,
    }


def find_notebook(assignment_dir: Path) -> Path:
    """The one `*.py` file in `assignment_dir` that calls `assignment(...)`.

    Raises:
        BuildError: If `assignment_dir` is missing or not a folder, or it holds no such file
            or several.
    """

    if not assignment_dir.exists():
        raise BuildError(f"{assignment_dir} does not exist.")

    if not assignment_dir.is_dir():
        raise BuildError(f"{assignment_dir} is not a folder.")

    candidates = [
        path
        for path in sorted(assignment_dir.glob("*.py"))
        if path.is_file() and _is_notebook(path.read_text(encoding="utf-8", errors="replace"))
    ]

    match candidates:
        case [path]:
            return path
        case []:
            otter = _otter_hint(
                [*sorted(assignment_dir.glob("*.py")), *sorted(assignment_dir.glob("*.ipynb"))]
            )
            raise BuildError(
                f"No notebook in {assignment_dir} calls `mogs.assignment(...)`. "
                + (
                    otter
                    or "Add a cell with that call, or start a new assignment with `mogs init`."
                )
            )
        case _:
            names = "\n".join(f"  {path.name}" for path in candidates)
            raise BuildError(
                f"Several notebooks in {assignment_dir} call `mogs.assignment(...)`; keep "
                f"exactly one:\n{names}"
            )


def source_notebook(path: Path) -> Path:
    """The source notebook at `path`, a notebook file or the assignment folder holding it.

    Raises:
        BuildError: If `path` is missing, is a file that does not call `assignment(...)`, or is
            a folder that `find_notebook` rejects.
    """

    if not path.is_file():
        return find_notebook(path)

    if not _is_notebook(path.read_text(encoding="utf-8", errors="replace")):
        raise BuildError(
            f"{path} does not call `mogs.assignment(...)`. "
            + (_otter_hint([path]) or "Pass the source notebook or the assignment folder.")
        )

    return path


def _otter_hint(paths: Iterable[Path]) -> str | None:
    """Where the first of `paths` with an Otter Assign marker has it, and where to read on.

    Returns:
        The hint, or `None` when no file has such a marker.
    """

    for path in paths:
        if not path.is_file():
            continue

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, start=1):
            found = OTTER_RE.search(line)
            if found:
                return (
                    f"{path}:{number} has `{found.group()}`, which is otter-assign syntax. "
                    f"Port the notebook to mogs by hand: {PORTING_URL}"
                )

    return None


def _is_notebook(text: str) -> bool:
    """A source notebook: it calls `assignment(...)`, under any name its imports give it.

    A file that is not valid Python counts when it looks like it calls `assignment`, so that
    reading it reports the syntax error.
    """

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return bool(ASSIGNMENT_RE.search(text))

    return bool(api_calls(tree, tree, Api.ASSIGNMENT))


def data_files(assignment_dir: Path, cfg: AssignmentConfig) -> dict[PurePosixPath, Path]:
    """The data files to ship, by path relative to the notebook, with folders expanded.

    Uses the sandbox's own checks and junk filter (`mogs._native.data_files`).

    Raises:
        ConfigError: If an entry of `files` is missing, is not inside the notebook's folder, or
            would replace the notebook, its public tests folder, a file of the autograder zip
            or the grader's copy of the submission.
    """

    # Case-insensitive, as the file systems of macOS and Windows are.
    reserved = {name.casefold() for name in (*ZIP_FILES, cfg.notebook, TESTS_FOLDER)}

    for name in cfg.files:
        first = PurePosixPath(name).parts[:1]
        if first and first[0].casefold() in reserved:
            raise ConfigError(
                f"{ASSIGNMENT} `files` entry {name!r} would replace `{first[0]}` in the build "
                "output."
            )

    return shared_data_files(assignment_dir, cfg.files)


def decoded(*streams: bytes) -> str:
    """A command's output as `subprocess.run(..., text=True)` reads it, joined and stripped.

    Raises:
        UnicodeDecodeError: If the output is not in the locale's encoding.
    """

    return "".join(io.TextIOWrapper(io.BytesIO(stream)).read() for stream in streams).strip()


# The installed mogs


def installed_tool_args() -> list[str]:
    """`uv pip install` arguments that install the running mogs.

    Raises:
        RequirementsError: If its `direct_url.json` names no source `tool_install_args` knows.
    """

    return tool_install_args(importlib.metadata.distribution(TOOL).read_text("direct_url.json"))


def tool_install_args(direct_url: str | None) -> list[str]:
    """`uv pip install` arguments that install the running mogs.

    Args:
        direct_url: Its `direct_url.json` (PEP 610), present when it was installed from a
            folder, an archive or a repository; `None` when it came from an index.

    Raises:
        RequirementsError: If `direct_url` describes none of these.
    """

    match None if direct_url is None else json.loads(direct_url):
        case None:
            return [f"{TOOL}=={__version__}"]
        case {"url": str(url), "dir_info": {"editable": True}} as info:
            return ["--editable", _local_path(url, info)]
        case {"url": str(url), "dir_info": dict()} as info:
            return [_local_path(url, info)]
        case {"url": str(url), "archive_info": dict()} as info:
            return [f"{TOOL} @ {url}{_fragment(info)}"]
        case {"url": str(url), "vcs_info": {"vcs": str(vcs), "commit_id": str(commit)}} as info:
            return [f"{TOOL} @ {vcs}+{url}@{commit}{_fragment(info)}"]
        case other:
            raise RequirementsError(
                f"Cannot tell where {TOOL} was installed from; its direct_url.json is {other!r}."
            )


def _local_path(url: str, info: dict[str, object]) -> str:
    """The folder a `file://` URL names, with PEP 610's `subdirectory` appended."""

    path = Path(url2pathname(urlparse(url).path))

    match info.get("subdirectory"):
        case str(subdirectory):
            path = path / subdirectory

    return str(path)


def _fragment(info: dict[str, object]) -> str:
    """The `#subdirectory=...` URL fragment for PEP 610's `subdirectory`, if any."""

    match info.get("subdirectory"):
        case str(subdirectory):
            return f"#subdirectory={subdirectory}"
        case _:
            return ""

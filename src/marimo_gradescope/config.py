"""Assignment config from the notebook's PEP 723 header, and text edits to that header.

The config lives in the `[tool.marimo-gradescope]` table. The header edits work on the text,
so comments and layout outside the edited part survive.
"""

import copy
import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TOOL = "marimo-gradescope"
MIN_MEMORY_MB = 2048
RUNTIME_SNIPPET = "# [tool.marimo.runtime]\n# reactive_tests = false"

# The reference regex from PEP 723, for the `script` block.
HEADER_RE = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
TABLE_RE = re.compile(r"^\[\[?\s*([A-Za-z0-9_-]+(?:\s*\.\s*[A-Za-z0-9_-]+)*)\s*\]\]?\s*(#.*)?$")
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
QID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# Device names Windows reserves in any case, with or without an extension.
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10))}
)


class ConfigError(Exception):
    """The notebook header or its `[tool.marimo-gradescope]` table is invalid."""


@dataclass(frozen=True, slots=True)
class Question:
    """One graded question.

    Attributes:
        id: Question id, the `<qid>` in test names `test_<qid>_*`.
        title: Name shown on Gradescope.
        points: Maximum score.
        manual: Graded by hand from the submitted PDF, not by tests.
    """

    id: str
    title: str
    points: float
    manual: bool = False


@dataclass(frozen=True, slots=True)
class AssignmentConfig:
    """Assignment settings read from the notebook header.

    Attributes:
        name: Assignment name. Defaults to the notebook's file stem.
        notebook: File name of the source notebook.
        questions: Questions by id, in header order.
        files: Extra files (data) shipped with the notebook, relative to its directory.
        timeout: Seconds allowed per test.
        budget: Seconds allowed for the whole grading run.
        memory_mb: Memory limit for student code, in MB (TOML key `memory`).
        python: Python version on Gradescope.
        requirements: Requirements file that overrides the header dependencies.
    """

    name: str
    notebook: str
    questions: dict[str, Question]
    files: tuple[str, ...] = ()
    timeout: float = 60
    budget: float = 540
    memory_mb: int = 4096
    python: str = "3.13"
    requirements: Path | None = None


# Text


def normalize_newlines(text: str) -> str:
    r"""Turn `\r\n` and `\r` line ends into `\n`."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    r"""Split `text` into lines the way Python numbers them.

    Unlike `str.splitlines`, this splits only at line ends, not at characters such as
    `\x0c` or `\u2028`, so index `i` holds AST line `i + 1`.
    """

    return normalize_newlines(text).split("\n")


# Reading


def read_header(src: str) -> dict:
    """Parse the PEP 723 `script` header of a notebook.

    Raises:
        ConfigError: If the header is missing or is not valid TOML.
    """

    _, body, _ = _split(src)

    try:
        return tomllib.loads("\n".join(body))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"The `# /// script` header is not valid TOML: {e}") from e


def read_config(src: str, path: Path) -> AssignmentConfig:
    """Read the `[tool.marimo-gradescope]` table of the notebook at `path`.

    Args:
        src: Notebook source.
        path: Notebook path. Names the notebook and anchors `requirements`.

    Raises:
        ConfigError: If the table is missing or a value is invalid. The message names the key.
    """

    match read_header(src).get("tool", {}).get(TOOL):
        case dict() as table:
            pass
        case None:
            raise ConfigError(f"The `# /// script` header has no [tool.{TOOL}] table.")
        case _:
            raise ConfigError(f"[tool.{TOOL}] must be a table.")

    match table.get("questions"):
        case dict() as raw if raw:
            questions = {qid: _question(qid, value) for qid, value in raw.items()}
            _check_file_names(list(questions))
        case None:
            raise ConfigError(f"[tool.{TOOL}] needs `questions`, e.g. questions = {{ q1 = 25 }}.")
        case _:
            raise ConfigError(f"[tool.{TOOL}] `questions` must be a non-empty table.")

    config = AssignmentConfig(name=path.stem, notebook=path.name, questions=questions)

    for key, value in table.items():
        match key:
            case "questions":
                pass
            case "name":
                config = copy.replace(config, name=_name(_string(key, value)))
            case "python":
                config = copy.replace(config, python=_string(key, value))
            case "files":
                config = copy.replace(config, files=_files(value))
            case "timeout":
                config = copy.replace(config, timeout=_seconds(key, value))
            case "budget":
                config = copy.replace(config, budget=_seconds(key, value))
            case "memory":
                config = copy.replace(config, memory_mb=_memory(value))
            case "requirements":
                config = copy.replace(config, requirements=path.parent / _string(key, value))
            case _:
                raise ConfigError(f"[tool.{TOOL}]: unknown key `{key}`.")

    return config


def _check_file_names(qids: list[str]) -> None:
    """Question ids name the public test files, `tests/<id>.py`.

    Raises:
        ConfigError: If an id is a name Windows reserves, or two ids differ only in case. Such
            files would collide on macOS and Windows.
    """

    seen: dict[str, str] = {}

    for qid in qids:
        folded = qid.casefold()

        if folded in WINDOWS_RESERVED:
            raise ConfigError(
                f"[tool.{TOOL}] question id `{qid}` is a file name Windows reserves. Its public "
                f"tests go to `tests/{qid}.py`; pick another id."
            )

        if folded in seen:
            raise ConfigError(
                f"[tool.{TOOL}] question ids `{qid}` and `{seen[folded]}` differ only in case. "
                "Their public test files would be the same file on macOS and Windows."
            )

        seen[folded] = qid


def _question(qid: str, value: object) -> Question:
    """Parse one entry of `questions`: a number of points, or a table."""

    if not QID_RE.match(qid):
        raise ConfigError(
            f"[tool.{TOOL}] question id `{qid}` must be letters and digits, starting with a "
            "letter (no `_`, since tests are named `test_<id>_<name>`)."
        )

    where = f"[tool.{TOOL}] question `{qid}`"
    match value:
        case int() | float():
            return Question(qid, default_title(qid), _points(where, value))
        case {"points": points, **rest}:
            question = Question(qid, default_title(qid), _points(where, points))
            for key, item in rest.items():
                match key, item:
                    case "title", str():
                        question = copy.replace(question, title=item)
                    case "manual", bool():
                        question = copy.replace(question, manual=item)
                    case "title" | "manual", _:
                        raise ConfigError(f"{where}: bad `{key}` value {item!r}.")
                    case _:
                        raise ConfigError(f"{where}: unknown key `{key}`.")
            return question
        case dict():
            raise ConfigError(f"{where}: missing `points`.")
        case _:
            raise ConfigError(f"{where}: expected points or a table, got {value!r}.")


def _points(where: str, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value >= 0:
            return float(value)
    raise ConfigError(f"{where}: points must be a number >= 0, got {value!r}.")


def default_title(qid: str) -> str:
    """`q1` → `Question 1`, `q2b` → `Question 2b`; other ids stay as they are."""

    match = re.fullmatch(r"q(\d\w*)", qid)
    return f"Question {match.group(1)}" if match else qid


def _string(key: str, value: object) -> str:
    match value:
        case str():
            return value
    raise ConfigError(f"[tool.{TOOL}] `{key}` must be a string, got {value!r}.")


def _name(value: str) -> str:
    """Check that the assignment name works as part of a file name."""

    if (
        not value
        or value.startswith(".")
        or any(char in "/\\" or unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ConfigError(
            f"[tool.{TOOL}] `name` names the autograder zip, so it must not be empty, start "
            f"with `.`, or hold `/`, `\\` or control characters; got {value!r}."
        )

    return value


def _files(value: object) -> tuple[str, ...]:
    match value:
        case list() if all(isinstance(item, str) for item in value):
            return tuple(value)
    raise ConfigError(f"[tool.{TOOL}] `files` must be a list of paths, got {value!r}.")


def _seconds(key: str, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value > 0:
            return float(value)
    raise ConfigError(f"[tool.{TOOL}] `{key}` must be a number of seconds > 0, got {value!r}.")


def _memory(value: object) -> int:
    match value:
        case bool():
            pass
        case int() if value >= MIN_MEMORY_MB:
            return value
    raise ConfigError(
        f"[tool.{TOOL}] `memory` must be a whole number of MB >= {MIN_MEMORY_MB}, got {value!r}."
    )


# Header text edits


def strip_tool_table(src: str) -> str:
    """Remove the `[tool.marimo-gradescope]` table and its subtables from the header.

    Returns `src` unchanged when there is no header or no such table.

    Raises:
        ConfigError: If the config is written in a form this edit does not find, such as a
            dotted key under `[tool]`.
    """

    if HEADER_RE.search(normalize_newlines(src)) is None:
        return src

    before, body, after = _split(src)
    kept = []
    dropping = False

    for line in body:
        table = _table_name(line)
        if table is not None:
            dropping = table == f"tool.{TOOL}" or table.startswith(f"tool.{TOOL}.")
        if not dropping:
            kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    out = _join(before, kept, after)

    if TOOL in read_header(out).get("tool", {}):
        raise ConfigError(f"Write the config as a [tool.{TOOL}] table, not as dotted keys.")

    return out


def ensure_runtime_flag(src: str) -> str:
    """Set `reactive_tests = false` under `[tool.marimo.runtime]` in the header.

    Adds the key to an existing `[tool.marimo.runtime]` table, or appends the table.

    Raises:
        ConfigError: If the header is missing, or the result does not hold the flag (the
            runtime config is written as an inline table or dotted keys).
    """

    before, body, after = _split(src)
    start = next(
        (i for i, line in enumerate(body) if _table_name(line) == "tool.marimo.runtime"), None
    )

    if start is None:
        if body and body[-1].strip():
            body.append("")
        body += ["[tool.marimo.runtime]", "reactive_tests = false"]
    else:
        end = next(
            (i for i in range(start + 1, len(body)) if _table_name(body[i]) is not None),
            len(body),
        )
        key = next(
            (i for i in range(start + 1, end) if re.match(r"\s*reactive_tests\s*=", body[i])),
            None,
        )
        if key is None:
            body.insert(start + 1, "reactive_tests = false")
        else:
            body[key] = "reactive_tests = false"

    out = _join(before, body, after)
    message = (
        f"Could not set reactive_tests = false. Write the marimo runtime config as:\n"
        f"{RUNTIME_SNIPPET}"
    )

    try:
        header = read_header(out)
    except ConfigError as e:
        raise ConfigError(message) from e

    match header:
        case {"tool": {"marimo": {"runtime": {"reactive_tests": False}}}}:
            return out
        case _:
            raise ConfigError(message)


def set_dependency(src: str, requirement: str) -> str:
    """Replace the header dependency with the same distribution name, or add it.

    The first matching entry is replaced in place; further matches are removed. The
    `dependencies` array is rewritten one entry per line.

    Raises:
        ConfigError: If the header is missing or `requirement` has no distribution name.
    """

    name = normalized_name(requirement)
    if name is None:
        raise ConfigError(f"Not a requirement: {requirement!r}.")

    deps = read_header(src).get("dependencies", [])
    new: list[str] = []
    for dep in deps:
        if normalized_name(dep) != name:
            new.append(dep)
        elif requirement not in new:
            new.append(requirement)
    if requirement not in new:
        new.append(requirement)

    return _write_dependencies(src, new)


def remove_dependency(src: str, dist_name: str) -> str:
    """Remove every header dependency on the distribution `dist_name`.

    Returns `src` unchanged when no dependency matches. Otherwise the `dependencies` array
    is rewritten one entry per line.

    Raises:
        ConfigError: If the header is missing or `dist_name` is not a distribution name.
    """

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dist_name):
        raise ConfigError(f"Not a distribution name: {dist_name!r}.")

    name = normalized_name(dist_name)
    deps = read_header(src).get("dependencies", [])
    kept = [dep for dep in deps if normalized_name(dep) != name]
    if len(kept) == len(deps):
        return src

    out = _write_dependencies(src, kept)
    if read_header(out).get("dependencies") != kept:
        raise ConfigError(f"Could not remove `{dist_name}` from the header's `dependencies`.")

    return out


def _write_dependencies(src: str, deps: list[str]) -> str:
    """Write `deps` as the header's `dependencies` array, adding the array if missing."""

    before, body, after = _split(src)
    text = "\n".join(body)
    array = ["dependencies = ["] + [f"    {json.dumps(dep)}," for dep in deps] + ["]"]

    span = _dependencies_span(text)
    if span is None:
        text = "\n".join(array + body)
    else:
        text = text[: span[0]] + "\n".join(array) + text[span[1] :]

    return _join(before, text.split("\n"), after)


def normalized_name(requirement: str) -> str | None:
    """Normalized distribution name (PEP 503) of a requirement string."""

    match = NAME_RE.match(requirement)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else None


def _dependencies_span(text: str) -> tuple[int, int] | None:
    """Character span of the top-level `dependencies = [...]` in the header TOML."""

    first_table = next(
        (m.start() for m in re.finditer(r"(?m)^.*$", text) if _table_name(m.group())), len(text)
    )
    match = re.search(r"(?m)^dependencies\s*=\s*\[", text[:first_table])
    if match is None:
        return None

    depth = 0
    quote = None
    i = match.end() - 1

    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"':
                i += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return match.start(), i + 1
        i += 1

    raise ConfigError("The header's `dependencies` array is not closed.")


# Header text helpers


def _split(src: str) -> tuple[str, list[str], str]:
    """Split `src` into text before the header body, the body as TOML lines, and the rest."""

    src = normalize_newlines(src)
    match = HEADER_RE.search(src)
    if match is None:
        raise ConfigError("No PEP 723 `# /// script` header found.")

    content = match.group("content").removesuffix("\n")
    body = [line[2:] for line in split_lines(content)]
    return src[: match.start("content")], body, src[match.end("content") :]


def _join(before: str, body: list[str], after: str) -> str:
    """Inverse of `_split`."""

    lines = [f"# {line}" if line else "#" for line in body]
    return before + "".join(line + "\n" for line in lines) + after


def _table_name(line: str) -> str | None:
    """Dotted name of a TOML table header line, with spaces around dots removed."""

    match = TABLE_RE.match(line.strip())
    return re.sub(r"\s+", "", match.group(1)) if match else None

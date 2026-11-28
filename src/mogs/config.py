"""Assignment config from the notebook's calls, and text edits to its PEP 723 header.

The config lives in cells of the source notebook: `mogs.assignment(...)` for the assignment,
and `mogs.grade(...)` for each question. They are read from the AST, so
their arguments must be literals. The header edits work on the text, so comments and layout
outside the edited part survive.
"""

import ast
import copy
import json
import re
import tomllib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

TOOL = "mogs"
PACKAGE = "mogs"
ASSIGNMENT = "mogs.assignment()"
# Folder next to the student notebook with its public tests, `tests/<qid>.py`.
TESTS_FOLDER = "tests"
MIN_MEMORY_MB = 2048

# The reference regex from PEP 723, for the `script` block.
HEADER_RE = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
TABLE_RE = re.compile(r"^\[\[?\s*([A-Za-z0-9_-]+(?:\s*\.\s*[A-Za-z0-9_-]+)*)\s*\]\]?\s*(#.*)?$")
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
QID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# Characters Windows forbids in file names.
WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
# Device names Windows reserves in any case, with or without an extension.
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10))}
)


class UsageError(Exception):
    """An error in what the user gave, which fails a `mogs` command with its message."""


class ConfigError(UsageError):
    """The notebook header or its config calls are invalid."""


class Api(StrEnum):
    """The functions of this package whose calls hold the config."""

    ASSIGNMENT = "assignment"
    GRADE = "grade"


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
    """Assignment settings read from the notebook's config calls.

    Attributes:
        name: Assignment name. Defaults to the notebook's file stem.
        notebook: File name of the source notebook.
        questions: Questions by id, in notebook order.
        files: Extra files (data) shipped with the notebook, relative to its directory.
        timeout: Seconds allowed per test.
        budget: Seconds allowed for the whole grading run.
        memory_mb: Memory limit for student code, in MB (argument `memory`).
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
    """Read the config calls of the notebook at `path`.

    The assignment comes from its one `assignment(...)` call. Each `grade(...)` call declares
    a question, in file order. A question with `test_<qid>_*` functions is auto-graded; one
    without is graded by hand.

    Args:
        src: Notebook source.
        path: Notebook path. Names the notebook and anchors `requirements`.

    Raises:
        ConfigError: If a call is missing, repeated or has an invalid argument. The message
            names the call.
    """

    src = normalize_newlines(src)

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise ConfigError(f"The notebook is not valid Python (line {e.lineno}): {e.msg}") from e

    match api_calls(tree, tree, Api.ASSIGNMENT):
        case [call]:
            config = _assignment(call, path)
        case []:
            raise ConfigError(
                f"The notebook has no `{ASSIGNMENT}` call. Add a cell with "
                f'`mogs.assignment(name="{path.stem}")`.'
            )
        case [_, second, *_]:
            raise ConfigError(
                f"{ASSIGNMENT} (line {second.lineno}): the notebook calls it more than once."
            )

    tested = {
        question_id(node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }
    questions: dict[str, Question] = {}
    lines: dict[str, int] = {}

    for call in api_calls(tree, tree, Api.GRADE):
        question = _question(call)
        question = copy.replace(question, manual=question.id not in tested)

        if question.id in questions:
            raise ConfigError(
                f"{_where(call)}: question `{question.id}` is declared more than once (first at "
                f"line {lines[question.id]})."
            )

        questions[question.id] = question
        lines[question.id] = call.lineno

    if not questions:
        raise ConfigError(
            'The notebook has no questions. End each one with a `mogs.grade("q1", points=...)` '
            "cell."
        )

    _check_file_names(list(questions))

    return copy.replace(config, questions=questions)


def api_aliases(tree: ast.Module, api: Api) -> tuple[set[str], set[str]]:
    """Names the notebook's imports bind to this package, and to its function `api`."""

    modules: set[str] = set()
    functions: set[str] = set()

    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                modules |= {alias.asname or alias.name for alias in names if alias.name == PACKAGE}
            case ast.ImportFrom(module=module, names=names) if module == PACKAGE:
                functions |= {alias.asname or alias.name for alias in names if alias.name == api}

    return modules, functions


def api_calls(tree: ast.Module, node: ast.AST, api: Api) -> list[ast.Call]:
    """Calls to this package's function `api` inside `node`, in source order.

    Args:
        tree: The whole notebook, for its imports.
        node: The part to search.
        api: The function.
    """

    modules, functions = api_aliases(tree, api)
    calls: list[ast.Call] = []

    for call in ast.walk(node):
        match call:
            case ast.Call(func=ast.Attribute(value=ast.Name(id=module), attr=attr)) if (
                module in modules and attr == api
            ):
                calls.append(call)
            case ast.Call(func=ast.Name(id=function)) if function in functions:
                calls.append(call)

    return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def _where(call: ast.Call) -> str:
    """`mogs.grade() (line 12)`: the call as written, and its line."""

    return f"{ast.unparse(call.func)}() (line {call.lineno})"


def _arguments(call: ast.Call, allowed: Iterable[str]) -> dict[str, object]:
    """The keyword arguments of `call`, as literal values.

    Raises:
        ConfigError: If a keyword is not in `allowed`, is `**`, or is not a literal.
    """

    where = _where(call)
    allowed = tuple(allowed)
    values: dict[str, object] = {}

    for keyword in call.keywords:
        match keyword.arg:
            case None:
                raise ConfigError(f"{where}: write each argument out; `**` is not read.")
            case str() as key if key not in allowed:
                raise ConfigError(
                    f"{where}: unknown argument `{key}`. It takes {', '.join(allowed)}."
                )
            case str() as key:
                try:
                    values[key] = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as e:
                    raise ConfigError(
                        f"{where}: `{key}` must be a literal value, such as a number or a "
                        f"string, got `{ast.unparse(keyword.value)}`. The build reads it "
                        "without running the notebook."
                    ) from e

    return values


def _assignment(call: ast.Call, path: Path) -> AssignmentConfig:
    """The assignment settings of an `assignment(...)` call."""

    where = _where(call)

    if call.args:
        raise ConfigError(f'{where}: pass each setting as a keyword, e.g. name="HW1".')

    keys = ("name", "files", "timeout", "budget", "memory", "python", "requirements")
    config = AssignmentConfig(name=path.stem, notebook=path.name, questions={})

    for key, value in _arguments(call, keys).items():
        match key:
            case "name":
                config = copy.replace(config, name=_string(where, key, value))
            case "python":
                config = copy.replace(config, python=_string(where, key, value))
            case "files":
                config = copy.replace(config, files=_files(where, value))
            case "timeout":
                config = copy.replace(config, timeout=_seconds(where, key, value))
            case "budget":
                config = copy.replace(config, budget=_seconds(where, key, value))
            case "memory":
                config = copy.replace(config, memory_mb=_memory(where, value))
            case "requirements":
                requirements = path.parent / _string(where, key, value)
                config = copy.replace(config, requirements=requirements)
            case _:
                raise AssertionError(f"unhandled argument {key!r}")

    return copy.replace(config, name=_name(where, config.name))


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
                f"Question id `{qid}` is a file name Windows reserves. Its public tests go to "
                f"`tests/{qid}.py`; pick another id."
            )

        if folded in seen:
            raise ConfigError(
                f"Question ids `{qid}` and `{seen[folded]}` differ only in case. Their public "
                "test files would be the same file on macOS and Windows."
            )

        seen[folded] = qid


def question_id(name: str) -> str:
    """Question id in a test name: `test_q1_norm` → `q1`."""

    return name.removeprefix("test_").split("_", 1)[0]


def _question(call: ast.Call) -> Question:
    """The question a `grade(...)` call declares, as auto-graded."""

    where = _where(call)
    example = f'{ast.unparse(call.func)}("q1", points=10)'

    match call.args:
        case [ast.Constant(value=str() as qid)]:
            pass
        case [ast.Constant(value=str()), *_]:
            raise ConfigError(f"{where}: pass `points` and `title` as keywords, e.g. {example}.")
        case _:
            raise ConfigError(
                f"{where}: needs the question id as a string literal first argument, "
                f"e.g. {example}."
            )

    if not QID_RE.match(qid):
        raise ConfigError(
            f"{where}: question id `{qid}` must be letters and digits, starting with a letter "
            "(no `_`, since tests are named `test_<id>_<name>`)."
        )

    values = _arguments(call, ("points", "title"))

    if "points" not in values:
        raise ConfigError(f"{where}: needs `points`, e.g. {example}.")

    title = values.get("title", default_title(qid))
    match title:
        case str():
            pass
        case _:
            raise ConfigError(f"{where}: `title` must be a string, got {title!r}.")

    return Question(qid, title, _points(where, values["points"]))


def _points(where: str, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value >= 0:
            return float(value)
    raise ConfigError(f"{where}: `points` must be a number >= 0, got {value!r}.")


def default_title(qid: str) -> str:
    """`q1` → `Question 1`, `question_2b` → `Question 2b`, any case; other ids stay as they are."""

    match = re.fullmatch(r"(?:q|question)_?(\d\w*)", qid, re.IGNORECASE)
    return f"Question {match.group(1)}" if match else qid


def _string(where: str, key: str, value: object) -> str:
    match value:
        case str():
            return value
    raise ConfigError(f"{where}: `{key}` must be a string, got {value!r}.")


def _name(where: str, value: str) -> str:
    """Check that the assignment name works as a file name, on Windows too.

    The name names the autograder zip, the student zip and the folder in the student zip.
    """

    if (
        not value
        or value.startswith(".")
        or any(char in "/\\" or unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ConfigError(
            f"{where}: `name` names the autograder zip, so it must not be empty, start with "
            f"`.`, or hold `/`, `\\` or control characters; got {value!r}."
        )

    if (
        value.split(".", 1)[0].casefold() in WINDOWS_RESERVED
        or any(char in WINDOWS_FORBIDDEN for char in value)
        or value.endswith((".", " "))
    ):
        raise ConfigError(
            f"{where}: `name` {value!r} is not a valid file name on Windows. It names the "
            "student zip and its folder, which students on Windows must be able to unzip. Pick "
            "another `name`, or set one if it comes from the notebook's file name."
        )

    return value


def check_name(value: str) -> str:
    """Check that `value` works as an assignment name; see `_name`.

    Raises:
        ConfigError: If it does not.
    """

    return _name("Assignment name", value)


def _files(where: str, value: object) -> tuple[str, ...]:
    match value:
        case list() | tuple() if all(isinstance(item, str) for item in value):
            return tuple(value)
    raise ConfigError(f"{where}: `files` must be a list of paths, got {value!r}.")


def _seconds(where: str, key: str, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value > 0:
            return float(value)
    raise ConfigError(f"{where}: `{key}` must be a number of seconds > 0, got {value!r}.")


def _memory(where: str, value: object) -> int:
    match value:
        case bool():
            pass
        case int() if value >= MIN_MEMORY_MB:
            return value
    raise ConfigError(
        f"{where}: `memory` must be a whole number of MB >= {MIN_MEMORY_MB}, got {value!r}."
    )


# Header text edits


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

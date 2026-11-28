"""Assignment config from the notebook's calls, and text edits to its PEP 723 header.

The config lives in cells of the source notebook: `mogs.assignment(...)` for the assignment,
and `mogs.grade(...)` for each question. They are read from the AST, so
their arguments must be literals. The header edit works on the text, so the rest of the
notebook keeps its exact layout.
"""

import ast
import contextlib
import dataclasses
import difflib
import re
import sys
import tomllib
import unicodedata
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

TOOL = "mogs"
PACKAGE = "mogs"
ASSIGNMENT = "mogs.assignment()"
# Folder next to the student notebook with its public tests, `tests/<qid>.py`.
TESTS_FOLDER = "tests"
MIN_MEMORY_MB = 2048
# The Python mogs runs on, such as `3.11`. A notebook must parse with it.
RUNNING_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"

# The reference regex from PEP 723, for the `script` block.
HEADER_RE = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
QID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# Characters Windows forbids in file names.
WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
# Device names Windows reserves in any case, with or without an extension.
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10))}
)


@dataclass(frozen=True, slots=True)
class Span:
    """Where in a notebook an error is, as `ast` gives positions.

    Attributes:
        path: The notebook, once the error leaves the code that read it.
        line: First line, 1-based.
        col: Start in that line, a 0-based offset in UTF-8 bytes.
        end_line: Last line, 1-based.
        end_col: End in that line, a 0-based offset in UTF-8 bytes, exclusive.
    """

    path: Path | None
    line: int
    col: int
    end_line: int
    end_col: int

    @staticmethod
    def of(node: ast.expr | ast.stmt | ast.keyword) -> "Span":
        """The span of an AST node, or of its first character when it has no end."""

        match node.end_lineno, node.end_col_offset:
            case int() as end_line, int() as end_col:
                return Span(None, node.lineno, node.col_offset, end_line, end_col)
            case _:
                return Span(None, node.lineno, node.col_offset, node.lineno, node.col_offset + 1)

    @staticmethod
    def of_line(lines: list[str], number: int) -> "Span":
        """The span of line `number` of `lines`, without its indent and trailing blanks."""

        text = lines[number - 1]
        indent = len(text) - len(text.lstrip())
        start = len(text[:indent].encode())
        end = len(text.rstrip().encode())

        return Span(None, number, start, number, max(end, start))


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """What the command line shows of an error: the notebook line it points at.

    Attributes:
        message: The text without the call or line it names, which `span` shows instead.
        span: Where the error is, if known.
        label: A short note at the span.
        help: A fix for a common mistake.
    """

    message: str
    span: Span | None = None
    label: str | None = None
    help: str | None = None


class UsageError(Exception):
    """An error in what the user gave, which fails a `mogs` command with its message.

    `str()` of the error is its full text, which panels and the self-check show.
    `diagnostic` lets the command line point at the notebook line instead.
    """

    def __init__(
        self,
        text: str,
        *,
        message: str | None = None,
        span: Span | None = None,
        label: str | None = None,
        help: str | None = None,
    ) -> None:
        super().__init__(text)

        self.diagnostic = Diagnostic(text if message is None else message, span, label, help)

    def at(self, path: Path) -> Self:
        """Place the span of this error in the notebook at `path`, and return the error.

        An error without a span, or whose span names a file already, stays as it is.
        """

        match self.diagnostic.span:
            case Span(path=None) as span:
                span = dataclasses.replace(span, path=path)
                self.diagnostic = dataclasses.replace(self.diagnostic, span=span)

        return self


@contextlib.contextmanager
def located(path: Path | None) -> Generator[None]:
    """Place the spans of the `UsageError`s raised inside in the notebook at `path`.

    Wrap only code whose spans count lines of that notebook. With `path` None, nothing
    changes.
    """

    try:
        yield
    except UsageError as error:
        if path is not None:
            error.at(path)
        raise


def syntax_span(src: str, error: SyntaxError) -> Span | None:
    """Where `ast.parse(src)` failed, from the error's 1-based character offsets.

    None when the error names no line of `src`, as at the end of the file.
    """

    lines = split_lines(src)

    match error.lineno, error.offset:
        case int() as line, int() as offset if 1 <= line <= len(lines) and offset >= 1:
            pass
        case _:
            return None

    text = lines[line - 1]
    start = min(offset - 1, len(text))
    end = start + 1

    match error.end_lineno, error.end_offset:
        case int() as end_line, int() as end_offset if end_line == line and end_offset > offset:
            end = min(end_offset - 1, len(text))

    col = len(text[:start].encode())

    return Span(None, line, col, line, max(len(text[:end].encode()), col))


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
        requirements: The requirements file, `requirements.txt` next to the notebook unless
            set. `None` in a config not read from a notebook, such as the grading bundle's.
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
    tree = _parse(src)
    config = _assignment_config(tree, path)

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
        question = dataclasses.replace(question, manual=question.id not in tested)

        if question.id in questions:
            first = lines[question.id]
            where = _where(call).at(call.args[0])

            raise where.error(
                f"question `{question.id}` is declared more than once (first at line {first}).",
                help="give each question its own id",
            )

        questions[question.id] = question
        lines[question.id] = call.lineno

    if not questions:
        raise ConfigError(
            'The notebook has no questions. End each one with a `mogs.grade("q1", points=...)` '
            "cell."
        )

    _check_file_names(list(questions))

    return dataclasses.replace(config, questions=questions)


def read_assignment_config(src: str, path: Path) -> AssignmentConfig:
    """Read only the `assignment(...)` call of the notebook at `path`, without questions.

    Raises:
        ConfigError: If the call is missing, repeated or has an invalid argument.
    """

    return _assignment_config(_parse(normalize_newlines(src)), path)


def _parse(src: str) -> ast.Module:
    """Parse the notebook `src`."""

    try:
        return ast.parse(src)
    except SyntaxError as e:
        raise ConfigError(
            f"The notebook is not valid Python {RUNNING_PYTHON} (line {e.lineno}): {e.msg}",
            message=f"The notebook is not valid Python {RUNNING_PYTHON}: {e.msg}",
            span=syntax_span(src, e),
        ) from e


def _assignment_config(tree: ast.Module, path: Path) -> AssignmentConfig:
    """The settings of the one `assignment(...)` call in `tree`."""

    match api_calls(tree, tree, Api.ASSIGNMENT):
        case []:
            raise ConfigError(
                f"The notebook has no `{ASSIGNMENT}` call. Add a cell with "
                f'`mogs.assignment(name="{path.stem}")`.'
            )
        case [_, second, *_]:
            raise ConfigError(
                f"{ASSIGNMENT} (line {second.lineno}): the notebook calls it more than once.",
                message="the notebook calls it more than once.",
                span=Span.of(second.func),
                help=f"keep one `{ASSIGNMENT[:-2]}(...)` call, with all the settings",
            )
        case [call]:
            pass

    return _assignment(call, path)


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


@dataclass(frozen=True, slots=True)
class Where:
    """What a config error names, such as `mogs.grade() (line 12)`, and the node it is about.

    Attributes:
        name: The call as written and its line, or another name.
        node: The node the error points at, if any.
    """

    name: str
    node: ast.expr | ast.keyword | None = None

    def __str__(self) -> str:
        return self.name

    def at(self, node: ast.expr | ast.keyword) -> "Where":
        """The same name, pointing at `node`."""

        return dataclasses.replace(self, node=node)

    def error(self, text: str, *, label: str | None = None, help: str | None = None) -> ConfigError:
        """`ConfigError("<name>: <text>")`, located at the node."""

        return ConfigError(
            f"{self.name}: {text}",
            message=text,
            span=None if self.node is None else Span.of(self.node),
            label=label,
            help=help,
        )


def _where(call: ast.Call) -> Where:
    """`mogs.grade() (line 12)`: the call as written, and its line, pointing at the call."""

    return Where(f"{ast.unparse(call.func)}() (line {call.lineno})", call.func)


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
                raise where.at(keyword).error("write each argument out; `**` is not read.")
            case str() as key if key not in allowed:
                close = difflib.get_close_matches(key, allowed, n=1)
                raise where.at(keyword).error(
                    f"unknown argument `{key}`. It takes {', '.join(allowed)}.",
                    help=f"did you mean `{close[0]}`?" if close else None,
                )
            case str() as key:
                try:
                    values[key] = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as e:
                    raise where.at(keyword.value).error(
                        f"`{key}` must be a literal value, such as a number or a "
                        f"string, got `{ast.unparse(keyword.value)}`. The build reads it "
                        "without running the notebook."
                    ) from e

    return values


def _assignment(call: ast.Call, path: Path) -> AssignmentConfig:
    """The assignment settings of an `assignment(...)` call."""

    where = _where(call)

    if call.args:
        raise where.at(call.args[0]).error('pass each setting as a keyword, e.g. name="HW1".')

    keys = ("name", "files", "timeout", "budget", "memory", "python", "requirements")
    config = AssignmentConfig(
        name=path.stem,
        notebook=path.name,
        questions={},
        requirements=path.parent / "requirements.txt",
    )
    nodes = _values(call)

    for key, value in _arguments(call, keys).items():
        at = where.at(nodes[key])

        match key:
            case "name":
                config = dataclasses.replace(config, name=_string(at, key, value))
            case "python":
                config = dataclasses.replace(config, python=_string(at, key, value))
            case "files":
                config = dataclasses.replace(config, files=_files(at, value))
            case "timeout":
                config = dataclasses.replace(config, timeout=_seconds(at, key, value))
            case "budget":
                config = dataclasses.replace(config, budget=_seconds(at, key, value))
            case "memory":
                config = dataclasses.replace(config, memory_mb=_memory(at, value))
            case "requirements":
                requirements = path.parent / _string(at, key, value)
                config = dataclasses.replace(config, requirements=requirements)
            case _:
                raise AssertionError(f"unhandled argument {key!r}")

    named = where.at(nodes["name"]) if "name" in nodes else where

    return dataclasses.replace(config, name=_name(named, config.name))


def _values(call: ast.Call) -> dict[str, ast.expr]:
    """The value nodes of the named keyword arguments of `call`."""

    return {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}


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

    needs_id = f"needs the question id as a string literal first argument, e.g. {example}."

    match call.args:
        case [ast.Constant(value=str() as qid)]:
            pass
        case [ast.Constant(value=str()), ast.Constant(value=bool())]:
            raise where.at(call.args[1]).error(
                f"pass `points` and `title` as keywords, e.g. {example}."
            )
        case [ast.Constant(value=str()), ast.Constant(value=int() | float()) as second]:
            raise where.at(second).error(
                f"pass `points` and `title` as keywords, e.g. {example}.",
                help=f"write it as a keyword: points={ast.unparse(second)}",
            )
        case [ast.Constant(value=str()), ast.Constant(value=str()) as second]:
            raise where.at(second).error(
                f"pass `points` and `title` as keywords, e.g. {example}.",
                help=f"write it as a keyword: title={ast.unparse(second)}",
            )
        case [ast.Constant(value=str()), second, *_]:
            raise where.at(second).error(f"pass `points` and `title` as keywords, e.g. {example}.")
        case [ast.Name(id=name) as first, *_]:
            raise where.at(first).error(needs_id, help=f'quote the id: "{name}"')
        case [first, *_]:
            raise where.at(first).error(needs_id)
        case _:
            raise where.error(needs_id)

    if not QID_RE.match(qid):
        fixed = re.sub(r"[^A-Za-z0-9]", "", qid)
        raise where.at(call.args[0]).error(
            f"question id `{qid}` must be letters and digits, starting with a letter "
            "(no `_`, since tests are named `test_<id>_<name>`).",
            help=f"try `{fixed}`" if QID_RE.match(fixed) else None,
        )

    values = _arguments(call, ("points", "title"))
    nodes = _values(call)

    if "points" not in values:
        raise where.error(f"needs `points`, e.g. {example}.")

    title = values.get("title", default_title(qid))
    match title:
        case str():
            pass
        case _:
            raise where.at(nodes["title"]).error(f"`title` must be a string, got {title!r}.")

    return Question(qid, title, _points(where.at(nodes["points"]), values["points"]))


def _points(where: Where, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value >= 0:
            return float(value)
    text = f"`points` must be a number >= 0, got {value!r}."
    raise _number_error(where, "points", text, value, lambda number: number >= 0)


def _number_error(
    where: Where, key: str, text: str, value: object, valid: Callable[[int | float], bool]
) -> ConfigError:
    """The error for a bad number, with a help line when `value` is a valid number in quotes.

    The quotes come off only when what is inside is a Python number literal, so `"010"` gets
    no help.
    """

    match value:
        case str():
            try:
                number = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                number = None
        case _:
            number = None

    match number:
        case bool():
            return where.error(text)
        case int() | float() if valid(number):
            return where.error(text, label="a string", help=f"drop the quotes: {key}={value}")
        case _:
            return where.error(text)


def default_title(qid: str) -> str:
    """`q1` → `Question 1`, `question_2b` → `Question 2b`, any case; other ids stay as they are."""

    match = re.fullmatch(r"(?:q|question)_?(\d\w*)", qid, re.IGNORECASE)
    return f"Question {match.group(1)}" if match else qid


def _string(where: Where, key: str, value: object) -> str:
    match value:
        case str():
            return value
    raise where.error(f"`{key}` must be a string, got {value!r}.")


def _name(where: Where, value: str) -> str:
    """Check that the assignment name works as a file name, on Windows too.

    The name names the autograder zip, the student zip and the folder in the student zip.
    """

    if (
        not value
        or value.startswith(".")
        or any(char in "/\\" or unicodedata.category(char) == "Cc" for char in value)
    ):
        raise where.error(
            "`name` names the autograder zip, so it must not be empty, start with "
            f"`.`, or hold `/`, `\\` or control characters; got {value!r}."
        )

    if (
        value.split(".", 1)[0].casefold() in WINDOWS_RESERVED
        or any(char in WINDOWS_FORBIDDEN for char in value)
        or value.endswith((".", " "))
    ):
        raise where.error(
            f"`name` {value!r} is not a valid file name on Windows. It names the "
            "student zip and its folder, which students on Windows must be able to unzip. Pick "
            "another `name`, or set one if it comes from the notebook's file name."
        )

    return value


def check_name(value: str) -> str:
    """Check that `value` works as an assignment name; see `_name`.

    Raises:
        ConfigError: If it does not.
    """

    return _name(Where("Assignment name"), value)


def _files(where: Where, value: object) -> tuple[str, ...]:
    match value:
        case list() | tuple() if all(isinstance(item, str) for item in value):
            return tuple(value)
    raise where.error(f"`files` must be a list of paths, got {value!r}.")


def _seconds(where: Where, key: str, value: object) -> float:
    match value:
        case bool():
            pass
        case int() | float() if value > 0:
            return float(value)
    text = f"`{key}` must be a number of seconds > 0, got {value!r}."
    raise _number_error(where, key, text, value, lambda number: number > 0)


def _memory(where: Where, value: object) -> int:
    match value:
        case bool():
            pass
        case int() if value >= MIN_MEMORY_MB:
            return value
    text = f"`memory` must be a whole number of MB >= {MIN_MEMORY_MB}, got {value!r}."
    raise _number_error(
        where,
        "memory",
        text,
        value,
        lambda number: isinstance(number, int) and number >= MIN_MEMORY_MB,
    )


# Header text edits


def replace_header(src: str, body: list[str]) -> str:
    """Put the TOML lines `body` in the PEP 723 header of `src`, in place of the old header.

    mogs owns the header, so nothing of the old one stays. Without a header, the new one goes
    at the top, followed by a blank line.

    Raises:
        ConfigError: If the old header is not valid TOML.
    """

    src = normalize_newlines(src)

    if HEADER_RE.search(src) is None:
        return _join("# /// script\n", body, "# ///\n\n" + src)

    read_header(src)
    before, _, after = _split(src)

    return _join(before, body, after)


def normalized_name(requirement: str) -> str | None:
    """Normalized distribution name (PEP 503) of a requirement string."""

    match = NAME_RE.match(requirement)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else None


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

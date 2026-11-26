"""The `check()` panel a student sees under each auto-graded question."""

import ast
import html
import inspect
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import CodeType, FunctionType
from typing import TYPE_CHECKING

from marimo_gradescope._env import grading
from marimo_gradescope.config import default_title
from marimo_gradescope.runner import Outcome, run_test, run_test_source, shorten
from marimo_gradescope.source import module_tests

if TYPE_CHECKING:
    import marimo as mo

# Translucent accents and inherited text colors read in both light and dark themes.
PASS = "rgba(34, 160, 80, 0.9)"
FAIL = "rgba(220, 60, 60, 0.9)"
WARN = "rgba(210, 140, 20, 0.9)"
SHADE = "rgba(127, 127, 127, 0.12)"

PANEL_STYLE = (
    "border: 1px solid rgba(127, 127, 127, 0.35); border-left: 4px solid {accent}; "
    "border-radius: 6px; padding: 0.9em 1.2em; line-height: 1.55; color: inherit; "
    "background: {shade};"
)
TITLE_STYLE = "font-weight: 600; margin-bottom: 0.4em;"
LIST_STYLE = "list-style: none; margin: 0; padding: 0;"
ITEM_STYLE = "margin: 0.15em 0;"
MARK_STYLE = "color: {accent}; font-weight: 700;"
MESSAGE_STYLE = (
    "margin: 0.3em 0 0.5em; margin-left: 1.5em; padding: 0.5em 0.7em; border-radius: 4px; "
    f"background: {SHADE}; color: inherit; white-space: pre-wrap; font-size: 0.85em;"
)
NOTE_STYLE = "margin-top: 0.4em; opacity: 0.75;"
NO_NOTEBOOK = (
    "Cannot find this notebook's file, so its public tests cannot run. Save the notebook, then "
    "open it with `marimo edit <your notebook>.py`."
)


def check(
    qid: str,
    *,
    tests: Sequence[Callable[[], object]] | None = None,
    test_file: str | None = None,
    uses: Sequence[object] = (),
    points: float | None = None,
    hidden: int = 0,
    title: str | None = None,
) -> "mo.Html":
    """Run a question's public tests and show the results as a panel.

    In the source notebook, `check("q1")` finds every `test_q1_*` callable among the caller's
    globals, hidden tests included. The student copy keeps its public tests in a file next to
    the notebook and passes its path as `test_file`. That file runs as a module in a copy of the
    caller's globals, with assertion rewriting. The student copy also passes the names the
    tests use as `uses`, so marimo runs the check cell again when one of them changes.

    Each test is compiled again from its source with pytest's assertion rewriting, so failed
    asserts show their values. The recompiled test runs against its own snapshot of its
    globals, copied when `check` runs, so the kernel's globals stay untouched. A test without
    source, one whose source does not match its code (as with marimo's mangled `_private`
    names), or one that does not compile again runs as it is.

    While grading (`MGS_GRADING=1`), it runs nothing and returns an empty panel, so a whole
    notebook run on the grader does not run the public tests a second time.

    Args:
        qid: Question id, e.g. `"q1"`.
        tests: The tests to run; `None` finds them in the caller's globals, unless `test_file`
            is given.
        test_file: Path of a file with the tests, relative to the notebook's folder.
        uses: The names the tests use. Only marimo reads them, to know when to run the cell.
        points: Points for the question, shown in the title.
        hidden: Number of hidden tests, shown as a note.
        title: Question title for the panel; `None` gives `Question N` for `qN`, else `qid`.

    Returns:
        The panel, as marimo HTML.

    Raises:
        ValueError: If both `tests` and `test_file` are given.
    """

    import marimo as mo

    if tests is not None and test_file is not None:
        raise ValueError("check() takes `tests` or `test_file`, not both.")

    if grading():
        return mo.Html("")

    namespace = sys._getframe(1).f_globals
    problem = None

    if test_file is not None:
        outcomes, problem = file_outcomes(test_file, namespace)
    else:
        found = discover(qid, namespace) if tests is None else tests
        outcomes = [run_test(name_of(test), with_rewritten_asserts(test)) for test in found]

    return mo.Html(panel(qid, outcomes, points, hidden, title, problem))


def file_outcomes(
    test_file: str, namespace: Mapping[str, object]
) -> tuple[list[Outcome], str | None]:
    """Run the tests in `test_file`, next to the notebook, in a copy of `namespace`.

    Returns:
        The outcomes, and why there are none when the file cannot be found or parsed.
    """

    notebook = locate(namespace)
    if notebook is None:
        return [], NO_NOTEBOOK

    path = notebook.parent / test_file

    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], (
            f"Cannot find {test_file} next to {notebook.name}. Keep the `tests` folder of the "
            "assignment next to the notebook."
        )
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"Cannot read {test_file}: {exc}"

    try:
        names = module_tests(source)
    except SyntaxError as exc:
        return [], f"{test_file} is not valid Python (line {exc.lineno})."

    return run_test_source(source, test_file, names, dict(namespace)), None


def locate(namespace: Mapping[str, object]) -> Path | None:
    """The notebook file: `__file__` in `namespace`, else marimo's running notebook, else `None`.

    The second lookup uses marimo's private runtime context and gives `None` when that fails.
    """

    match namespace.get("__file__"):
        case str() as file if file:
            return Path(file).resolve()

    try:
        from marimo._runtime.context import get_context

        filename = get_context().filename
    except Exception:
        return None

    return Path(filename).resolve() if filename else None


def discover(qid: str, namespace: dict[str, object]) -> list[Callable[[], object]]:
    """Callables named `test_<qid>_*` in `namespace`, in definition order."""

    prefix = f"test_{qid}_"

    return [
        value
        for name, value in list(namespace.items())
        if name.startswith(prefix) and callable(value)
    ]


def name_of(test: object) -> str:
    """A function's name, or the `repr` of anything else, cut to `MAX_SOURCE_WIDTH` characters."""

    try:
        match test:
            case FunctionType():
                name = str.__str__(test.__name__)
            case _:
                name = str.__str__(repr(test))
    except BaseException:
        return "<test>"

    return shorten(name)


def with_rewritten_asserts(test: Callable[[], object]) -> Callable[[], object]:
    """The test compiled again with assertion rewriting, or the test itself when that fails."""

    # Closures would lose their cells when compiled again at module level.
    match test:
        case FunctionType() if not test.__code__.co_freevars:
            pass
        case _:
            return test

    from marimo_gradescope._assertrewrite import compile_tests

    try:
        source = textwrap.dedent(inspect.getsource(test))
        tree = ast.parse(source)
    except Exception:
        return test

    # Run nothing but a plain definition: a lambda's source is its whole line, and
    # decorators, defaults and annotations are arbitrary code that already ran once.
    match tree.body:
        case [
            ast.FunctionDef(
                name=name,
                args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kwarg=None),
                decorator_list=[],
                returns=None,
            )
        ] if name == test.__name__:
            pass
        case _:
            return test

    # marimo compiles a cell's `_private` names mangled, but its registered source keeps them
    # plain, so the recompiled test would look up names the kernel does not have. Recompile only
    # when the source asks for no global or attribute the running code lacks.
    try:
        plain = compile(source, "<check>", "exec", dont_inherit=True)
    except Exception:
        return test

    match [const for const in plain.co_consts if isinstance(const, CodeType)]:
        case [CodeType() as recompiled] if names_used(recompiled) <= names_used(test.__code__):
            pass
        case _:
            return test

    try:
        code = compile_tests(source, test.__code__.co_filename)

        # A copy, so the kernel's own globals keep the original function.
        namespace = dict(test.__globals__)
        exec(code, namespace)
    except Exception:
        return test

    match namespace.get(test.__name__):
        case FunctionType() as rewritten:
            return rewritten
        case _:
            return test


def names_used(code: CodeType) -> set[str]:
    """Global and attribute names in `code` and the code nested in it."""

    names = set(code.co_names)

    for const in code.co_consts:
        match const:
            case CodeType():
                names |= names_used(const)

    return names


def panel(
    qid: str,
    outcomes: list[Outcome],
    points: float | None,
    hidden: int,
    title: str | None = None,
    problem: str | None = None,
) -> str:
    """HTML for the panel, headed by `title`, or by `question_title(qid)` when it is `None`.

    `problem` says why there are no outcomes; it defaults to "No tests found".
    """

    title = question_title(qid) if title is None else title

    if points is not None:
        title += f" — {format_points(points)}"

    if not outcomes:
        accent = WARN
        warning = html.escape(problem or f"No tests found for {qid}")
        body = f'<div style="{MARK_STYLE.format(accent=accent)}">{warning}</div>'
    else:
        passed = sum(outcome.passed for outcome in outcomes)
        accent = PASS if passed == len(outcomes) else FAIL
        title += f" · {passed}/{len(outcomes)} public tests passed"
        items = "".join(outcome_item(outcome) for outcome in outcomes)
        body = f'<ul style="{LIST_STYLE}">{items}</ul>'

    if hidden > 0:
        noun = "test" if hidden == 1 else "tests"
        body += f'<div style="{NOTE_STYLE}">+ {hidden} hidden {noun} run on Gradescope</div>'

    return (
        f'<div style="{PANEL_STYLE.format(accent=accent, shade=SHADE)}">'
        f'<div style="{TITLE_STYLE}">{html.escape(title)}</div>'
        f"{body}</div>"
    )


def outcome_item(outcome: Outcome) -> str:
    """One list item: a mark, the test name and, for a failure, its message."""

    name = html.escape(outcome.name)

    if outcome.passed:
        mark = f'<span style="{MARK_STYLE.format(accent=PASS)}">✓</span>'
        return f'<li style="{ITEM_STYLE}">{mark} {name}</li>'

    mark = f'<span style="{MARK_STYLE.format(accent=FAIL)}">✗</span>'
    message = f'<pre style="{MESSAGE_STYLE}">{html.escape(outcome.message)}</pre>'

    return f'<li style="{ITEM_STYLE}">{mark} {name}{message}</li>'


def question_title(qid: str) -> str:
    """The title a question gets when the config gives none; see `config.default_title`."""

    return default_title(qid)


def format_points(points: float) -> str:
    """`25 pts`, `2.5 pts` or `1 pt`."""

    shown = f"{points:g}"

    return f"{shown} pt" if points == 1 else f"{shown} pts"

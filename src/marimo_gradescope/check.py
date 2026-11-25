"""The `check()` panel a student sees under each auto-graded question."""

import ast
import html
import inspect
import sys
import textwrap
from collections.abc import Callable, Sequence
from types import CodeType, FunctionType
from typing import TYPE_CHECKING

from marimo_gradescope._env import grading
from marimo_gradescope.config import default_title
from marimo_gradescope.runner import Outcome, run_test, shorten

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


def check(
    qid: str,
    *,
    tests: Sequence[Callable[[], object]] | None = None,
    points: float | None = None,
    hidden: int = 0,
    title: str | None = None,
) -> "mo.Html":
    """Run a question's public tests and show the results as a panel.

    In the source notebook, `check("q1")` finds every `test_q1_*` callable among the caller's
    globals, hidden tests included. The student copy passes the public tests explicitly, so the
    check cell re-runs whenever a test or a function it uses changes.

    Each test is compiled again from its source with pytest's assertion rewriting, so failed
    asserts show their values. The recompiled test runs against its own snapshot of its
    globals, copied when `check` runs, so the kernel's globals stay untouched. A test without
    source, one whose source does not match its code (as with marimo's mangled `_private`
    names), or one that does not compile again runs as it is.

    While grading (`MGS_GRADING=1`), it runs nothing and returns an empty panel, so a whole
    notebook run on the grader does not run the public tests a second time.

    Args:
        qid: Question id, e.g. `"q1"`.
        tests: The tests to run; `None` finds them in the caller's globals.
        points: Points for the question, shown in the title.
        hidden: Number of hidden tests, shown as a note.
        title: Question title for the panel; `None` gives `Question N` for `qN`, else `qid`.

    Returns:
        The panel, as marimo HTML.
    """

    import marimo as mo

    if grading():
        return mo.Html("")

    if tests is None:
        tests = discover(qid, sys._getframe(1).f_globals)

    outcomes = [run_test(name_of(test), with_rewritten_asserts(test)) for test in tests]

    return mo.Html(panel(qid, outcomes, points, hidden, title))


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
) -> str:
    """HTML for the panel, headed by `title`, or by `question_title(qid)` when it is `None`."""

    title = question_title(qid) if title is None else title

    if points is not None:
        title += f" — {format_points(points)}"

    if not outcomes:
        accent = WARN
        warning = html.escape(f"No tests found for {qid}")
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

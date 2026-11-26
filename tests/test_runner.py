import linecache
import subprocess
import sys
import textwrap
import traceback
from collections.abc import Callable

import pytest

from marimogs._assertrewrite import compile_tests
from marimogs.runner import (
    MAX_BYTES,
    MAX_LINES,
    FailureReason,
    Outcome,
    cap,
    run_test,
    run_test_source,
)

TESTS = """\
def student_divide(a, b):
    return a / b


def test_sum():
    a = [1, 2, 3]
    assert sum(a) == 7


def test_divide():
    assert student_divide(1, 0) == 0


def test_ok():
    assert sum([1, 2, 3]) == 6
"""


def load(source: str, filename: str = "<tests>") -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(compile_tests(source, filename), namespace)
    return namespace


def load_fn(source: str, name: str):
    fn = load(textwrap.dedent(source))[name]
    assert callable(fn)
    return fn


def raiser(exc: BaseException):
    def fn() -> None:
        raise exc

    return fn


def within_caps(message: str) -> bool:
    return len(message.splitlines()) <= MAX_LINES and len(message.encode()) <= MAX_BYTES


# compile_tests


def test_rewritten_assert_shows_values():
    fn = load(TESTS)["test_sum"]
    assert callable(fn)

    outcome = run_test("test_sum", fn)

    assert not outcome.passed
    assert outcome.reason == FailureReason.ASSERTION
    assert "6 == 7" in outcome.message
    assert "sum(" in outcome.message
    assert "assert sum(a) == 7" in outcome.message


def test_rewritten_assert_outside_pytest():
    script = textwrap.dedent(
        """
        from marimogs._assertrewrite import compile_tests
        from marimogs.runner import run_test

        ns = {}
        exec(compile_tests("def test_sum():\\n    assert sum([1, 2, 3]) == 7\\n", "<t>"), ns)
        print(run_test("test_sum", ns["test_sum"]).message)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert "assert 6 == 7" in result.stdout
    assert "where 6 = sum([1, 2, 3])" in result.stdout


def test_compile_tests_registers_source_for_tracebacks():
    fn = load(TESTS, "<registered>")["test_divide"]
    assert callable(fn)

    with pytest.raises(ZeroDivisionError) as info:
        fn()

    text = "".join(traceback.format_exception(info.value))
    assert "assert student_divide(1, 0) == 0" in text
    assert "return a / b" in text


def test_compile_tests_keeps_multiple_functions():
    namespace = load(TESTS)

    assert {"student_divide", "test_sum", "test_divide", "test_ok"} <= namespace.keys()


def test_compile_tests_syntax_error_propagates():
    with pytest.raises(SyntaxError):
        compile_tests("def test_x(:\n    pass\n", "<bad>")


def test_compile_tests_same_source_reuses_entry():
    source = "def test_x():\n    assert 1 == 2\n"
    before = len(linecache.cache)

    first = compile_tests(source, "<reuse>")
    second = compile_tests(source, "<reuse>")

    assert first.co_filename == second.co_filename
    assert first.co_filename.startswith("<mgs-tests-")
    assert len(linecache.cache) == before + 1


def test_compile_tests_different_sources_get_different_entries():
    first = compile_tests("x = 1\n", "<cell>")
    second = compile_tests("x = 2\n", "<cell>")

    assert first.co_filename != second.co_filename
    assert linecache.getline(first.co_filename, 1) == "x = 1\n"
    assert linecache.getline(second.co_filename, 1) == "x = 2\n"


# run_test


def test_passing_test():
    fn = load(TESTS)["test_ok"]
    assert callable(fn)

    outcome = run_test("test_ok", fn)

    assert outcome == Outcome("test_ok", True, seconds=outcome.seconds)
    assert outcome.reason is None
    assert outcome.seconds >= 0


def test_error_in_student_code_shows_test_line():
    fn = load(TESTS)["test_divide"]
    assert callable(fn)

    outcome = run_test("test_divide", fn)

    assert not outcome.passed
    assert outcome.reason == FailureReason.ERROR
    assert "assert student_divide(1, 0) == 0" in outcome.message
    assert "ZeroDivisionError: division by zero" in outcome.message
    assert "return a / b" in outcome.message
    assert outcome.message.index("student_divide(1, 0)") < outcome.message.index("a / b")


def test_multiline_assert_shows_whole_statement():
    fn = load_fn(
        """
        def test_long():
            assert (
                1 + 1
                == 3
            )
        """,
        "test_long",
    )

    outcome = run_test("test_long", fn)

    assert "1 + 1" in outcome.message
    assert "== 3" in outcome.message


def test_plain_callable_without_registered_source():
    outcome = run_test("lambda", lambda: 1 / 0)

    assert outcome.reason == FailureReason.ERROR
    assert "ZeroDivisionError" in outcome.message


def test_assertion_message_kept():
    outcome = run_test("t", raiser(AssertionError("custom text")))

    assert outcome.reason == FailureReason.ASSERTION
    assert "AssertionError: custom text" in outcome.message


def test_empty_assertion_error():
    outcome = run_test("t", raiser(AssertionError()))

    assert outcome.reason == FailureReason.ASSERTION
    assert "AssertionError" in outcome.message


class BadStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no str for you")


class BadStrExit(Exception):
    def __str__(self) -> str:
        raise SystemExit(1)


class CustomBase(BaseException):
    pass


class BadClass(Exception):
    @property
    def __class__(self):
        raise ZeroDivisionError


class BadNameMeta(type):
    @property
    def __name__(cls):  # ty: ignore[incompatible-property-override] -- hostile on purpose
        raise ZeroDivisionError


class BadName(Exception, metaclass=BadNameMeta):
    pass


class HostileStr(str):
    def __bool__(self) -> bool:
        raise ZeroDivisionError

    def __format__(self, spec: str) -> str:
        raise ZeroDivisionError


class BadStrSubclass(Exception):
    def __str__(self) -> str:
        return HostileStr("hostile text")


@pytest.mark.parametrize(
    ("make", "text"),
    [
        (BadStr, "BadStr"),
        (BadStrExit, "BadStrExit"),
        (lambda: SystemExit(3), "SystemExit"),
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (GeneratorExit, "GeneratorExit"),
        (lambda: CustomBase("boom"), "CustomBase"),
        (BadClass, ""),
        (BadName, ""),
        (BadStrSubclass, "hostile text"),
    ],
    ids=[
        "str-raises",
        "str-raises-exit",
        "system-exit",
        "keyboard-interrupt",
        "generator-exit",
        "base-exception",
        "class-property-raises",
        "metaclass-name-raises",
        "str-subclass-hostile",
    ],
)
def test_hostile_exceptions_recorded(make: Callable[[], BaseException], text: str):
    outcome = run_test("t", raiser(make()))

    assert not outcome.passed
    assert outcome.reason == FailureReason.ERROR
    assert text in outcome.message


def test_memory_error_is_out_of_memory():
    outcome = run_test("t", raiser(MemoryError()))

    assert outcome.reason == FailureReason.OUT_OF_MEMORY


def test_long_message_capped_by_lines():
    outcome = run_test("t", raiser(ValueError("\n".join(f"line {i}" for i in range(1000)))))

    assert within_caps(outcome.message)
    assert "ValueError" in outcome.message


def test_long_message_capped_by_bytes():
    outcome = run_test("t", raiser(ValueError("é" * 100_000)))

    assert within_caps(outcome.message)
    assert "ValueError" in outcome.message


def test_rewritten_assert_with_huge_values_capped():
    fn = load_fn(
        """
        def test_big():
            assert list(range(10_000)) == []
        """,
        "test_big",
    )

    outcome = run_test("test_big", fn)

    assert within_caps(outcome.message)
    assert "assert list(range(10_000)) == []" in outcome.message


def test_recursion_error_message():
    fn = load_fn(
        """
        def f(n):
            return f(n + 1)

        def test_deep():
            f(0)
        """,
        "test_deep",
    )

    outcome = run_test("test_deep", fn)

    assert outcome.reason == FailureReason.ERROR
    assert "RecursionError" in outcome.message
    assert "f(0)" in outcome.message
    assert within_caps(outcome.message)


# A sequence of tests


def test_failing_tests_do_not_stop_the_rest():
    namespace = load(TESTS)
    tests: list[tuple[str, Callable[[], object]]] = [
        (name, fn) for name, fn in namespace.items() if name.startswith("test_") and callable(fn)
    ]
    tests.insert(1, ("test_exit", raiser(SystemExit(1))))
    tests.insert(2, ("test_interrupt", raiser(KeyboardInterrupt())))

    outcomes = [run_test(name, fn) for name, fn in tests]

    assert [o.name for o in outcomes] == [
        "test_sum",
        "test_exit",
        "test_interrupt",
        "test_divide",
        "test_ok",
    ]
    assert [o.passed for o in outcomes] == [False, False, False, False, True]


def test_failure_reasons():
    assert {r.value for r in FailureReason} == {
        "assertion",
        "error",
        "timeout",
        "killed",
        "out_of_memory",
        "budget",
        "load_failed",
        "not_run",
    }


def test_long_source_line_keeps_exception_type():
    fn = load_fn(f"def test_wide():\n    x = {'a' * 9000!r} + 1\n", "test_wide")

    outcome = run_test("test_wide", fn)

    assert within_caps(outcome.message)
    assert "TypeError" in outcome.message


def test_line_cap_holds_after_byte_cut():
    outcome = run_test("t", raiser(ValueError("a" + "\n" * 22 + "x" * 5000)))

    assert within_caps(outcome.message)


def test_lone_surrogate_replaced():
    outcome = run_test("t", raiser(ValueError("\ud800")))

    outcome.message.encode()
    assert "ValueError" in outcome.message


def test_same_filename_keeps_each_source():
    public = load("def test_pub():\n    assert 1 == 2\n", "<cell>")["test_pub"]
    load("x = 1\nsecret = 'hidden text'\n", "<cell>")
    assert callable(public)

    outcome = run_test("test_pub", public)

    assert "assert 1 == 2" in outcome.message
    assert "hidden" not in outcome.message


@pytest.mark.parametrize(
    "message",
    [
        "\n".join(["y" * 170] * 23 + ["y" * 150]) + "\n\n\nz",
        "a" + "\n" * 22 + "x" * 5000,
        "\n".join(["é" * 100] * 30),
    ],
    ids=["line-cut-near-byte-cap", "byte-cut-many-lines", "both"],
)
def test_cap_holds_both_limits(message: str):
    assert within_caps(cap(message))


# Tests from source


def test_run_test_source_runs_each_test_against_the_namespace():
    source = "def test_q1_a():\n    assert x == 1\n\ndef test_q1_b():\n    assert x == 2\n"
    namespace: dict[str, object] = {"x": 1}

    outcomes = run_test_source(source, "<tests>", ("test_q1_a", "test_q1_b"), namespace)

    assert [outcome.passed for outcome in outcomes] == [True, False]
    assert "assert 1 == 2" in outcomes[1].message


def test_run_test_source_fails_every_test_when_loading_fails():
    source = "raise ValueError('boom')\ndef test_q1_a():\n    pass\n"

    outcomes = run_test_source(source, "<tests>", ("test_q1_a", "test_q1_b"), {})

    assert [outcome.reason for outcome in outcomes] == [FailureReason.LOAD_FAILED] * 2
    assert outcomes[0].message == "the tests failed to load (ValueError: boom)"


def test_run_test_source_reports_a_name_the_source_does_not_define():
    [outcome] = run_test_source("x = 1\n", "<tests>", ("test_q1_a",), {})

    assert outcome.reason is FailureReason.ERROR
    assert outcome.message == "`test_q1_a` is not defined by its tests"


def test_run_test_source_reports_a_load_failure_before_any_test():
    [outcome] = run_test_source("raise ValueError('boom')\n", "tests/q1.py", (), {})

    assert outcome.name == "tests/q1.py"
    assert outcome.reason is FailureReason.LOAD_FAILED
    assert outcome.message == "the tests failed to load (ValueError: boom)"


def test_run_test_source_catches_system_exit_while_loading():
    outcomes = run_test_source("raise SystemExit(3)\n", "<tests>", ("test_q1_a",), {})

    assert [outcome.reason for outcome in outcomes] == [FailureReason.LOAD_FAILED]
    assert outcomes[0].message == "the tests failed to load (SystemExit: 3)"

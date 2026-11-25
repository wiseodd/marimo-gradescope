import os
import pickle
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import FunctionType

import pytest

from marimo_gradescope.loader import Missing, bind, load

FIXTURES = Path(__file__).parent / "fixtures"

# The loader caches per path, so each test loads its own copy from tmp_path.


def copy(name: str, tmp_path: Path) -> Path:
    path = tmp_path / name
    shutil.copy(FIXTURES / name, path)
    return path


def write(tmp_path: Path, body: str, name: str = "submission.py") -> Path:
    path = tmp_path / name
    header = "import marimo\n\napp = marimo.App()\n\n"
    path.write_text(header + textwrap.dedent(body), encoding="utf-8")
    return path


def call(value: object, *args: object) -> object:
    assert callable(value)
    return value(*args)


def message(placeholder: object) -> str:
    assert isinstance(placeholder, Missing)

    with pytest.raises(RuntimeError) as info:
        placeholder()

    return str(info.value)


@pytest.fixture
def side_effect(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "side_effect.txt"
    monkeypatch.setenv("MGS_SIDE_EFFECT", str(path))
    return path


# Binding


def test_binds_pure_function_without_running_other_cells(tmp_path, side_effect):
    found = bind(copy("impure_fn.py", tmp_path), ["add"])

    assert isinstance(found["add"], FunctionType)
    assert call(found["add"], 1, 2) == 3
    assert not side_effect.exists()


def test_recovers_impure_function_by_running_notebook(tmp_path, side_effect):
    found = bind(copy("impure_fn.py", tmp_path), ["add", "double"])

    assert call(found["add"], 1, 2) == 3
    assert isinstance(found["double"], FunctionType)
    assert call(found["double"], 5) == 10
    assert side_effect.read_text(encoding="utf-8") == "ran\n"


def test_run_failed_skips_the_run_and_names_why(tmp_path, side_effect):
    found = bind(copy("impure_fn.py", tmp_path), ["add", "double"], run_failed="exit code 3")

    assert call(found["add"], 1, 2) == 3
    assert message(found["double"]) == (
        "`double` is unavailable: it needs the whole notebook to run, which failed: exit code 3"
    )
    assert not side_effect.exists()


def test_returns_names_in_order(tmp_path, side_effect):
    found = bind(copy("impure_fn.py", tmp_path), ["double", "add"])

    assert list(found) == ["double", "add"]


def test_binds_none_value(tmp_path):
    path = write(
        tmp_path,
        """\
        with app.setup:
            nothing = None
        """,
    )

    assert bind(path, ["nothing"]) == {"nothing": None}


def test_undefined_name_is_missing(tmp_path, side_effect):
    found = bind(copy("impure_fn.py", tmp_path), ["add", "subtract"])

    assert call(found["add"], 1, 2) == 3
    assert message(found["subtract"]) == (
        "`subtract` is unavailable: it is not defined in your notebook"
    )


def test_undefined_name_without_app_is_missing(tmp_path):
    path = tmp_path / "plain.py"
    path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    found = bind(path, ["add", "subtract"])

    assert call(found["add"], 1, 2) == 3
    assert "it is not defined in your notebook" in message(found["subtract"])


# Failures


def test_syntax_error_is_missing_with_reason(tmp_path):
    found = bind(copy("syntax_error.py", tmp_path), ["add"])

    text = message(found["add"])
    assert text.startswith("`add` is unavailable: your notebook failed to import (SyntaxError: ")


def test_import_crash_is_missing_with_reason(tmp_path):
    found = bind(copy("import_crash.py", tmp_path), ["add"])

    assert message(found["add"]) == (
        "`add` is unavailable: your notebook failed to import "
        "(ModuleNotFoundError: No module named 'a_module_that_does_not_exist')"
    )


@pytest.mark.parametrize(
    ("statement", "summary"),
    [
        ("raise SystemExit(3)", "SystemExit: 3"),
        ("raise KeyboardInterrupt", "KeyboardInterrupt"),
    ],
)
def test_import_base_exception_is_missing(tmp_path, statement, summary):
    path = write(tmp_path, f"{statement}\n")

    assert message(bind(path, ["add"])["add"]) == (
        f"`add` is unavailable: your notebook failed to import ({summary})"
    )


def test_hostile_exception_at_import_is_missing(tmp_path):
    path = write(
        tmp_path,
        """\
        class Hostile(Exception):
            def __str__(self):
                raise ValueError("no")

        raise Hostile
        """,
    )

    assert message(bind(path, ["add"])["add"]) == (
        "`add` is unavailable: your notebook failed to import "
        "(Hostile: <str() of the exception failed>)"
    )


def test_failing_run_makes_every_fallback_name_missing(tmp_path):
    path = write(
        tmp_path,
        """\
        @app.function
        def add(a, b):
            return a + b


        @app.cell
        def _():
            def double(x):
                return 2 * x
            return (double,)


        @app.cell
        def _():
            raise SystemExit(3)
            return
        """,
    )

    found = bind(path, ["add", "double"])

    assert call(found["add"], 1, 2) == 3
    assert message(found["double"]) == (
        "`double` is unavailable: running your notebook failed (SystemExit: 3)"
    )


def test_named_cell_is_not_bound_without_run(tmp_path):
    path = write(
        tmp_path,
        """\
        @app.cell
        def helper():
            raise RuntimeError("boom")
            return
        """,
    )

    found = bind(path, ["helper"])

    assert message(found["helper"]) == (
        "`helper` is unavailable: running your notebook failed (RuntimeError: boom)"
    )


# Caching


def test_caches_per_path(tmp_path, side_effect):
    path = copy("impure_fn.py", tmp_path)

    first = bind(path, ["add", "double"])
    second = bind(path, ["double", "add"])

    assert first["add"] is second["add"]
    assert first["double"] is second["double"]
    assert side_effect.read_text(encoding="utf-8") == "ran\n"


def test_different_paths_load_separately(tmp_path):
    one = write(tmp_path, "with app.setup:\n    value = 1\n", "one.py")
    two = write(tmp_path, "with app.setup:\n    value = 2\n", "two.py")

    assert bind(one, ["value"]) == {"value": 1}
    assert bind(two, ["value"]) == {"value": 2}


def test_pure_names_then_fallback_names(tmp_path, side_effect):
    path = copy("impure_fn.py", tmp_path)

    first = bind(path, ["add"])
    assert not side_effect.exists()

    second = bind(path, ["add", "double"])

    assert call(first["add"], 1, 2) == 3
    assert call(second["add"], 1, 2) == 3
    assert call(second["double"], 5) == 10
    assert side_effect.read_text(encoding="utf-8") == "ran\n"


def test_failed_run_is_cached(tmp_path, side_effect):
    path = write(
        tmp_path,
        """\
        with app.setup:
            import os
            from pathlib import Path


        @app.cell
        def _():
            with Path(os.environ["MGS_SIDE_EFFECT"]).open("a") as file:
                file.write("ran\\n")
            raise RuntimeError("boom")
            return
        """,
    )

    first = bind(path, ["double"])
    second = bind(path, ["double"])

    assert message(first["double"]) == message(second["double"])
    assert "running your notebook failed (RuntimeError: boom)" in message(second["double"])
    assert side_effect.read_text(encoding="utf-8") == "ran\n"


# Namespaces


def test_fallback_names_share_one_namespace(tmp_path):
    path = write(
        tmp_path,
        """\
        with app.setup:
            from dataclasses import dataclass


        @app.class_definition
        @dataclass
        class Point:
            x: int


        @app.cell
        def _():
            origin = Point(0)
            return (origin,)


        @app.cell
        def make(origin):
            def make():
                return Point(origin.x + 1)
            return (make,)
        """,
    )

    found = bind(path, ["Point", "make"])
    point, made = found["Point"], call(found["make"])

    assert isinstance(point, type)
    assert isinstance(made, point)
    assert made == call(point, 1)


def test_module_getattr_is_never_called(tmp_path):
    path = write(
        tmp_path,
        """\
        def __getattr__(name):
            raise SystemExit("getattr ran")
        """,
    )

    assert message(bind(path, ["absent"])["absent"]) == (
        "`absent` is unavailable: it is not defined in your notebook"
    )


def test_object_with_hostile_class_is_bound(tmp_path):
    path = write(
        tmp_path,
        """\
        with app.setup:
            class Weird:
                @property
                def __class__(self):
                    raise ValueError("nope")

            thing = Weird()
        """,
    )

    assert type(bind(path, ["thing"])["thing"]).__name__ == "Weird"


def test_app_with_hostile_class_is_missing(tmp_path):
    path = tmp_path / "submission.py"
    path.write_text(
        textwrap.dedent(
            """\
            class Weird:
                @property
                def __class__(self):
                    raise ValueError("nope")

            app = Weird()
            """
        ),
        encoding="utf-8",
    )

    assert message(bind(path, ["absent"])["absent"]) == (
        "`absent` is unavailable: it is not defined in your notebook"
    )


def test_unreadable_module_dict_is_missing(tmp_path):
    path = tmp_path / "submission.py"
    path.write_text(
        textwrap.dedent(
            """\
            import sys
            import types


            class Module(types.ModuleType):
                @property
                def __dict__(self):
                    raise ValueError("no dict")


            sys.modules[__name__].__class__ = Module
            add = 1
            """
        ),
        encoding="utf-8",
    )

    assert message(bind(path, ["add"])["add"]) == (
        "`add` is unavailable: reading your notebook failed (ValueError: no dict)"
    )


# Module


def test_load_registers_unique_module(tmp_path):
    module, error = load(copy("impure_fn.py", tmp_path))

    assert error is None
    assert module is not None
    assert module.__name__ != "submission"
    assert sys.modules[module.__name__] is module


def test_load_failure_unregisters_module(tmp_path):
    before = set(sys.modules)

    module, error = load(copy("import_crash.py", tmp_path))

    assert module is None
    assert error is not None
    assert set(sys.modules) - before == set()


def test_dataclass_in_notebook_works(tmp_path):
    path = write(
        tmp_path,
        """\
        with app.setup:
            from dataclasses import dataclass


        @app.class_definition
        @dataclass
        class Point:
            x: int
            y: int
        """,
    )

    point = bind(path, ["Point"])["Point"]

    assert call(point, 1, 2) == call(point, 1, 2)


# Missing


def test_missing_attribute_raises_readable_error():
    placeholder = Missing("add", "it is not defined in your notebook")

    with pytest.raises(RuntimeError, match="`add` is unavailable"):
        placeholder.attribute  # noqa: B018


def test_missing_private_attribute_raises_attribute_error():
    placeholder = Missing("add", "it is not defined in your notebook")

    with pytest.raises(AttributeError):
        placeholder._private  # noqa: B018

    with pytest.raises(AttributeError):
        placeholder.__wrapped__  # noqa: B018


def test_missing_pickles():
    placeholder = Missing("add", "it is not defined in your notebook")

    copied = pickle.loads(pickle.dumps(placeholder))

    assert message(copied) == "`add` is unavailable: it is not defined in your notebook"


def test_missing_repr():
    assert repr(Missing("add", "why")) == "Missing('add', 'why')"


# Across marimo versions (needs network for `uv run --with` on a cold cache)

SCRIPT = """\
import sys
from pathlib import Path
from marimo_gradescope.loader import bind

found = bind(Path(sys.argv[1]), ["add", "double"])
print(found["add"](1, 2), found["double"](5))
"""


@pytest.mark.slow
@pytest.mark.parametrize("marimo", ["marimo==0.25.2", "marimo==0.24.2"])
def test_bind_across_marimo_versions(marimo, tmp_path):
    path = copy("impure_fn.py", tmp_path)

    result = subprocess.run(
        # The test's interpreter, not whichever Python uv would pick.
        ["uv", "run", "--python", sys.executable, "--with", marimo, "python", "-c", SCRIPT]
        + [str(path)],
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"},
        cwd=Path(__file__).parent.parent,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.split() == ["3", "10"]

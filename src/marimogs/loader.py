"""Load a student's marimo notebook and bind the names the tests use.

Functions written with `@app.function` are plain module attributes, so importing the notebook
runs only its setup cell and those definitions. A name that is not a module attribute, such as
a function that depends on another cell, comes from a single `app.run()`, which executes the
whole notebook. This fallback is designed behavior. It is all-or-nothing: if any cell raises,
every name that needed the fallback becomes `Missing`, including names from healthy cells.
When the run killed or hung an earlier grading child, the grader binds again with the run
turned off (`run_failed`), so names that need no run still work.

Loading never raises. Names that cannot be bound come back as `Missing` placeholders that raise
a readable error when a test calls them or reads an attribute, so one broken name costs only
the tests that use it.

`app.run()` behaves the same whether or not `PYTEST_CURRENT_TEST` is set (checked on marimo
0.24.2 and 0.25.2), so binding inside a pytest test needs no environment changes.
"""

import hashlib
import importlib.util
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import cast

import marimo

from marimogs.runner import cap, exception_summary

NOT_DEFINED = "it is not defined in your notebook"

type Loaded = tuple[ModuleType | None, str | None]
type Defs = tuple[Mapping[str, object], str | None]

_modules: dict[Path, Loaded] = {}
_defs: dict[Path, Defs] = {}


class Missing:
    """Stand-in for a name the notebook does not provide.

    Calling it or reading a public attribute raises `RuntimeError` with the reason. Private
    attributes raise `AttributeError`, so introspection, pickling and pytest leave it alone.

    Args:
        name: The name the tests asked for.
        reason: Why it is unavailable, shown to the student.
    """

    __slots__ = ("_name", "_reason")

    def __init__(self, name: str, reason: str) -> None:
        self._name = name
        self._reason = reason

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Raise the readable error."""

        __tracebackhide__ = True

        raise RuntimeError(f"`{self._name}` is unavailable: {self._reason}")

    def __getattr__(self, attr: str) -> object:
        __tracebackhide__ = True

        if attr.startswith("_"):
            raise AttributeError(attr)

        return self()

    def __getstate__(self) -> tuple[str, str]:
        return self._name, self._reason

    def __setstate__(self, state: tuple[str, str]) -> None:
        self._name, self._reason = state

    def __repr__(self) -> str:
        return f"Missing({self._name!r}, {self._reason!r})"


def load(path: Path) -> Loaded:
    """Import a notebook as a module, once per path in this process.

    The module gets a name unique to the path and stays in `sys.modules`, which dataclasses and
    pickling need. Never raises: any `BaseException` from the notebook becomes the error string.

    Args:
        path: The notebook file.

    Returns:
        `(module, None)` on success, `(None, reason)` on failure.
    """

    path = path.resolve()

    if path in _modules:
        return _modules[path]

    digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    name = f"_marimogs_submission_{digest}"

    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path.name}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        loaded: Loaded = (module, None)
    except BaseException as exc:
        sys.modules.pop(name, None)
        loaded = (None, cap(f"your notebook failed to import ({exception_summary(exc)})"))

    _modules[path] = loaded
    return loaded


def bind(path: Path, names: Iterable[str], *, run_failed: str | None = None) -> dict[str, object]:
    """Look up names in a notebook, falling back to `app.run()` once for the rest.

    Names come from module attributes, read through the module's `__dict__` so a module-level
    `__getattr__` never runs. Named marimo cells also appear as module attributes but are cell
    objects, not the values the tests want, so they are skipped.

    If any name needs the fallback, or the notebook already ran in this process, every name the
    run defined comes from the run. The run re-executes the setup cell and every top-level
    definition, so mixing its values with module attributes would split classes and module
    state in two. Module attributes cover only names the run did not define, or every name when
    the run failed. Names still unbound become `Missing`. Never raises.

    Args:
        path: The notebook file.
        names: Names the tests use.
        run_failed: Why an earlier run of this notebook failed, in another process. When set,
            the notebook does not run, and names that need the run become `Missing` with
            this reason.

    Returns:
        Each name mapped to its value or a `Missing`, in the order given.
    """

    names = list(names)
    module, error = load(path)

    if module is None:
        return {name: Missing(name, error or NOT_DEFINED) for name in names}

    try:
        return _bind(path.resolve(), module, names, run_failed)
    except BaseException as exc:
        reason = cap(f"reading your notebook failed ({exception_summary(exc)})")
        return {name: Missing(name, reason) for name in names}


def _bind(
    path: Path, module: ModuleType, names: list[str], run_failed: str | None
) -> dict[str, object]:
    """Bind names from the module and, when needed, from one run of the notebook."""

    # `type()` cannot be fooled by a `__class__` property, unlike `isinstance`.
    attrs = {
        name: value
        for name, value in vars(module).items()
        if not issubclass(type(value), marimo.Cell)
    }

    app = attrs.get("app")
    needs_run = any(name not in attrs for name in names)
    defs: Mapping[str, object] = {}
    run_error = None

    if run_failed is not None:
        run_error = f"it needs the whole notebook to run, which failed: {run_failed}"
    elif issubclass(type(app), marimo.App) and (needs_run or path in _defs):
        defs, run_error = _run(path, cast(marimo.App, app))

    found = {**attrs, **defs}

    return {
        name: found[name] if name in found else Missing(name, run_error or NOT_DEFINED)
        for name in names
    }


def _run(path: Path, app: marimo.App) -> Defs:
    """Run the whole notebook once per path and keep its definitions."""

    if path in _defs:
        return _defs[path]

    try:
        _, defs = app.run()
        result: Defs = (dict(defs), None)
    except BaseException as exc:
        result = ({}, cap(f"running your notebook failed ({exception_summary(exc)})"))

    _defs[path] = result
    return result

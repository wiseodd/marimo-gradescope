import inspect
import re
from pathlib import Path

import marimo_gradescope

PACKAGE = Path(marimo_gradescope.__file__).parent
PRIVATE_IMPORT = re.compile(r"^\s*(from|import)\s+_pytest\b", re.MULTILINE)


def test_rewrite_asserts_still_available():
    from _pytest.assertion.rewrite import rewrite_asserts

    params = list(inspect.signature(rewrite_asserts).parameters)

    assert params == ["mod", "source", "module_path", "config"]


def test_only_assertrewrite_imports_private_pytest():
    importers = {
        path.name
        for path in PACKAGE.rglob("*.py")
        if PRIVATE_IMPORT.search(path.read_text(encoding="utf-8"))
    }

    assert importers == {"_assertrewrite.py"}

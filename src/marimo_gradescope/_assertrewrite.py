"""Compile test source with pytest's assertion rewriting.

This is the only module that imports the private `_pytest` package, so a change in pytest's
internals breaks one place, and `tests/test_assertrewrite_guard.py` catches it.
"""

import ast
import hashlib
import linecache
from types import CodeType

from _pytest.assertion.rewrite import rewrite_asserts


def compile_tests(source: str, filename: str) -> CodeType:
    """Compile test source so failing asserts explain their values.

    The source is registered in `linecache` under `<mgs-tests-H:filename>`, where `H` is a hash
    of the source, and that name is the code's file name, so tracebacks and failure messages
    show its lines. Different sources get different names, so no other source, and no real
    file, can replace these lines. The same source gets the same name, so compiling it again,
    as a re-run notebook check does, reuses the entry instead of adding one.
    Line numbers count from the first line of `source`.

    Args:
        source: Module-like source, e.g. one or more `def test_*` functions.
        filename: Label kept inside the generated file name.

    Returns:
        A module code object to run with `exec`.

    Raises:
        SyntaxError: The source does not parse.
    """

    digest = hashlib.sha1(source.encode(errors="surrogatepass")).hexdigest()[:12]
    name = f"<mgs-tests-{digest}:{filename}>"
    tree = ast.parse(source, name)
    rewrite_asserts(tree, source.encode())

    # `mtime=None` keeps the entry through `linecache.checkcache`.
    linecache.cache[name] = (len(source), None, source.splitlines(keepends=True), name)

    return compile(tree, name, "exec", dont_inherit=True)

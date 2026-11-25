"""Static parse of a marimo source notebook into cells, and checks of its authoring rules.

The notebook is never imported or run. Cells, tests, hidden cells and check calls come from
its AST and its text.
"""

import ast
import io
import re
import symtable
import textwrap
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from marimo_gradescope.config import (
    TOOL,
    AssignmentConfig,
    normalize_newlines,
    split_lines,
)

PACKAGE = "marimo_gradescope"


class Marker(StrEnum):
    """Comments that mark hidden cells and solutions."""

    HIDDEN = "# HIDDEN"
    BEGIN = "# BEGIN SOLUTION"
    END = "# END SOLUTION"
    SOLUTION = "# SOLUTION"
    PROMPT = "# PROMPT:"


HIDDEN = Marker.HIDDEN
BEGIN = Marker.BEGIN
END = Marker.END
SOLUTION = Marker.SOLUTION
PROMPT = Marker.PROMPT

# Comments that look like a marker but are not spelled exactly like one.
NEAR_SOLUTION_RE = re.compile(r"#\s*((begin|end)\s*)?solutions?\b", re.IGNORECASE)
NEAR_PROMPT_RE = re.compile(r"#\s*prompt\b", re.IGNORECASE)
NEAR_HIDDEN_RE = re.compile(r"#\s*hidden\b", re.IGNORECASE)


class SourceError(Exception):
    """The notebook source cannot be parsed into cells."""


class CellKind(StrEnum):
    """How a cell is written in the notebook file."""

    SETUP = "setup"
    CELL = "cell"
    FUNCTION = "function"
    CLASS = "class"


@dataclass(frozen=True, slots=True)
class Cell:
    """One marimo cell.

    Attributes:
        kind: Cell kind.
        name: Function or class name; `_` for an unnamed cell; `setup` for the setup cell.
        start: First line, 1-based, decorators included.
        end: Last line, 1-based, inclusive.
        tests: Names of the `test_*` functions the cell defines at its top level.
        hidden: The cell holds a `# HIDDEN` line.
        check_qid: Question id of the cell's `check(...)` call, if any.
    """

    kind: CellKind
    name: str
    start: int
    end: int
    tests: tuple[str, ...]
    hidden: bool
    check_qid: str | None


@dataclass(frozen=True, slots=True)
class Notebook:
    r"""A parsed source notebook.

    Attributes:
        cells: Cells in file order.
        src: Notebook source, with line ends turned into `\n`.
    """

    cells: tuple[Cell, ...]
    src: str

    def tests_by_question(self) -> dict[str, tuple[list[str], list[str]]]:
        """Public and hidden test names per question id, in file order."""

        out: dict[str, tuple[list[str], list[str]]] = {}

        for cell in self.cells:
            for name in cell.tests:
                public, hidden = out.setdefault(question_id(name), ([], []))
                (hidden if cell.hidden else public).append(name)

        return out

    def student_names(self) -> set[str]:
        """Names that cells without tests and without `# HIDDEN` define for the notebook.

        Function and class cells define their own name; plain cells define the names they
        return. The setup cell is not included.
        """

        names: set[str] = set()

        for cell in self.cells:
            if cell.tests or cell.hidden:
                continue
            match cell.kind:
                case CellKind.FUNCTION | CellKind.CLASS:
                    names.add(cell.name)
                case CellKind.CELL:
                    names |= _returned_names(_cell_node(self.src, cell))
                case CellKind.SETUP:
                    pass

        return names

    def helper_cells(self) -> tuple[Cell, ...]:
        """`# HIDDEN` cells without tests: data and helpers for hidden tests."""

        return tuple(
            cell
            for cell in self.cells
            if cell.hidden and not cell.tests and cell.kind is not CellKind.SETUP
        )

    def answer_cells(self, qid: str) -> tuple[Cell, ...]:
        """Cells named `answer_<qid>` or `answer_<qid>_<suffix>`."""

        return self._named("answer", qid)

    def prompt_cells(self, qid: str) -> tuple[Cell, ...]:
        """Cells named `prompt_<qid>` or `prompt_<qid>_<suffix>`."""

        return self._named("prompt", qid)

    def setup_source(self) -> str:
        """Dedented body of the setup cell, or `""` when the notebook has none."""

        setup = [cell for cell in self.cells if cell.kind is CellKind.SETUP]
        return "".join(self.cell_body_source(cell) for cell in setup)

    def cell_body_source(self, cell: Cell) -> str:
        """Dedented source of a cell's body, ending in a newline.

        For a plain cell this drops the `def` line and the final `return`. For the setup
        cell it drops the `with` line. For a function or class cell it is the definition
        without the `@app` decorators.

        Raises:
            SourceError: If the body starts on the same line as the `def` or `with`.
        """

        lines = split_lines(self.src)
        node = _cell_node(self.src, cell)

        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef() if cell.kind in (
                CellKind.FUNCTION,
                CellKind.CLASS,
            ):
                first, last = _after_app_decorator(node), cell.end
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.With():
                first = _header_end(lines, node, cell) + 1
                body = node.body
                match body[-1]:
                    case ast.Return() if cell.kind is CellKind.CELL:
                        last = body[-1].lineno - 1
                    case _:
                        last = cell.end
            case _:
                raise SourceError(f"line {cell.start}: unexpected {cell.kind} cell.")

        chunk = lines[first - 1 : last]
        while chunk and not chunk[-1].strip():
            chunk.pop()

        return textwrap.dedent("".join(line + "\n" for line in chunk))

    def check_call(self, cell: Cell) -> ast.Call:
        """The check call of a cell, with line numbers of the whole notebook.

        Raises:
            SourceError: If the cell does not hold exactly one check call.
        """

        modules, functions = _check_names(ast.parse(self.src))
        calls = _check_calls(_cell_node(self.src, cell), modules, functions)

        match calls:
            case [call]:
                return call
            case _:
                raise SourceError(
                    f"line {cell.start}: expected one check call, found {len(calls)}."
                )

    def check_test_file(self, cell: Cell) -> str | None:
        """The path a cell's check call passes as a string literal `test_file`, if it does.

        Raises:
            SourceError: If the cell does not hold exactly one check call.
        """

        for keyword in self.check_call(cell).keywords:
            match keyword:
                case ast.keyword(arg="test_file", value=ast.Constant(value=str() as path)):
                    return path

        return None

    def _named(self, prefix: str, qid: str) -> tuple[Cell, ...]:
        name = f"{prefix}_{qid}"
        return tuple(
            cell for cell in self.cells if cell.name == name or cell.name.startswith(f"{name}_")
        )


def cell_names(source: str) -> tuple[set[str], set[str]]:
    """Names `source`, run as a module, binds at top level, and global names it reads otherwise.

    Uses Python's scope analysis, so names local to a function, class body or comprehension
    are neither bound nor read.
    """

    top = symtable.symtable(source, "<cell>", "exec")
    bound = set()
    read = set()

    for symbol in top.get_symbols():
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace():
            bound.add(symbol.get_name())
        elif symbol.is_referenced():
            read.add(symbol.get_name())

    tables = list(top.get_children())

    while tables:
        table = tables.pop()
        tables += table.get_children()
        read |= {s.get_name() for s in table.get_symbols() if s.is_global() and s.is_referenced()}

    return bound, read - bound


def question_id(name: str) -> str:
    """Question id in a test name: `test_q1_norm` → `q1`."""

    return name.removeprefix("test_").split("_", 1)[0]


def module_tests(source: str) -> tuple[str, ...]:
    """Names of the `test_*` functions that `source` defines at top level, in file order.

    Raises:
        SyntaxError: If `source` is not valid Python.
    """

    return tuple(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def answer_text(nb: Notebook, cell: Cell) -> str | None:
    """The Markdown of an answer cell that is just `mo.md("...")`; `None` for other code."""

    try:
        tree = ast.parse(nb.cell_body_source(cell))
    except (SyntaxError, SourceError):
        return None

    match tree.body:
        case []:
            return ""
        case [
            ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(attr="md"), args=[ast.Constant(value=str() as text)]
                )
            )
        ]:
            return textwrap.dedent(text).strip()
        case _:
            return None


def answer_placeholders(nb: Notebook, qids: Iterable[str]) -> dict[str, str]:
    """The text of each question's answer cell, as `answer_text` gives it.

    Only questions with exactly one answer cell, holding non-blank `mo.md("...")`, are included.
    """

    placeholders = {}

    for qid in qids:
        match [answer_text(nb, cell) for cell in nb.answer_cells(qid)]:
            case [str() as text] if text.strip():
                placeholders[qid] = text

    return placeholders


def cell_source(nb: Notebook, cell: Cell) -> str:
    """The cell's lines as written, decorators included."""

    return "\n".join(split_lines(nb.src)[cell.start - 1 : cell.end])


# Parsing


def parse_notebook(src: str) -> Notebook:
    """Parse the cells of a marimo notebook.

    Raises:
        SourceError: If the source is not valid Python, or a check call does not name its
            question with a string literal, or a cell holds more than one check call, or an
            `@app.*` decorator does not fit what it decorates.
    """

    src = normalize_newlines(src)

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise SourceError(f"The notebook is not valid Python (line {e.lineno}): {e.msg}") from e

    lines = split_lines(src)
    modules, functions = _check_names(tree)
    cells = []

    for node in tree.body:
        match node:
            case ast.With(items=[ast.withitem(context_expr=expr)]) if _app_attr(expr) == "setup":
                kind, name, start = CellKind.SETUP, "setup", node.lineno
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                match _decorator_kind(node):
                    case None:
                        continue
                    case kind:
                        name = node.name
                        start = min(d.lineno for d in node.decorator_list)
            case _:
                continue

        end = _trailing_comments_end(lines, _end(node))
        text = lines[start - 1 : end]
        tests = _tests(node, kind)
        hidden = any(line.strip() == HIDDEN for line in text)
        check = _check_qid(node, modules, functions) if kind is CellKind.CELL else None

        cells.append(Cell(kind, name, start, end, tests, hidden, check))

    return Notebook(tuple(cells), src)


def _app_attr(expr: ast.expr) -> str | None:
    """`x` for `app.x` or `app.x(...)`."""

    match expr:
        case (
            ast.Attribute(value=ast.Name(id="app"), attr=attr)
            | ast.Call(func=ast.Attribute(value=ast.Name(id="app"), attr=attr))
        ):
            return attr
        case _:
            return None


def _decorator_kind(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> CellKind | None:
    """Cell kind from the node's `@app.*` decorator, or None if it has none."""

    for decorator in node.decorator_list:
        match _app_attr(decorator), node:
            case "cell", ast.FunctionDef() | ast.AsyncFunctionDef():
                return CellKind.CELL
            case "function", ast.FunctionDef():
                return CellKind.FUNCTION
            case "class_definition", ast.ClassDef():
                return CellKind.CLASS
            case str() as attr, _:
                raise SourceError(f"line {decorator.lineno}: unexpected `@app.{attr}` here.")

    return None


def _after_app_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """First line after the `@app.*` decorator: the next decorator, or the definition."""

    rest = [d for d in node.decorator_list if _app_attr(d) is None]
    return min([node.lineno] + [d.lineno for d in rest])


def _tests(node: ast.stmt, kind: CellKind) -> tuple[str, ...]:
    match node, kind:
        case ast.FunctionDef(name=name), CellKind.FUNCTION if name.startswith("test_"):
            return (name,)
        case ast.FunctionDef(body=body) | ast.AsyncFunctionDef(body=body), CellKind.CELL:
            return tuple(
                stmt.name
                for stmt in body
                if isinstance(stmt, ast.FunctionDef) and stmt.name.startswith("test_")
            )
        case _:
            return ()


def _check_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to this package, and to its `check` function, by the notebook's imports."""

    modules: set[str] = set()
    functions: set[str] = set()

    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                modules |= {alias.asname or alias.name for alias in names if alias.name == PACKAGE}
            case ast.ImportFrom(module=module, names=names) if module == PACKAGE:
                functions |= {
                    alias.asname or alias.name for alias in names if alias.name == "check"
                }

    return modules, functions


def _check_calls(node: ast.stmt, modules: set[str], functions: set[str]) -> list[ast.Call]:
    """Calls to this package's `check` in a cell, in AST walk order."""

    calls: list[ast.Call] = []

    for call in ast.walk(node):
        match call:
            case ast.Call(func=ast.Attribute(value=ast.Name(id=module), attr="check")) if (
                module in modules
            ):
                calls.append(call)
            case ast.Call(func=ast.Name(id=function)) if function in functions:
                calls.append(call)

    return calls


def _check_qid(node: ast.stmt, modules: set[str], functions: set[str]) -> str | None:
    """Question id of the single check call in a cell, or None if it has none."""

    found: list[str] = []

    for call in _check_calls(node, modules, functions):
        match call.args:
            case [ast.Constant(value=str() as qid), *_]:
                found.append(qid)
            case _:
                raise SourceError(
                    f"line {call.lineno}: check() needs the question id as a string literal "
                    'first argument, e.g. mgs.check("q1").'
                )

    match found:
        case []:
            return None
        case [qid]:
            return qid
        case _:
            raise SourceError(
                f"line {node.lineno}: a cell may hold only one check call, found {len(found)}."
            )


def _cell_node(src: str, cell: Cell) -> ast.stmt:
    """AST of one cell, with line numbers of the whole notebook."""

    lines = split_lines(src)
    padding = "\n" * (cell.start - 1)
    return ast.parse(padding + "\n".join(lines[cell.start - 1 : cell.end])).body[0]


def _end(node: ast.stmt) -> int:
    match node.end_lineno:
        case int() as end:
            return end
        case None:
            raise SourceError(f"line {node.lineno}: statement has no end line.")


def _trailing_comments_end(lines: list[str], end: int) -> int:
    """Move `end` past indented comment lines that follow it, such as `# END SOLUTION`."""

    last = end

    for number in range(end + 1, len(lines) + 1):
        line = lines[number - 1]
        if not line.strip():
            continue
        if line[0] not in " \t" or not line.strip().startswith("#"):
            break
        last = number

    return last


def _header_end(lines: list[str], node: ast.stmt, cell: Cell) -> int:
    """Last line of a `def ...:` or `with ...:` header, before its body and comments."""

    match node:
        case ast.FunctionDef(body=body) | ast.AsyncFunctionDef(body=body) | ast.With(body=body):
            pass
        case _:
            raise SourceError(f"line {cell.start}: not a cell body.")

    header = [
        number
        for number in range(node.lineno, body[0].lineno)
        if lines[number - 1].strip() and not lines[number - 1].strip().startswith("#")
    ]
    if not header:
        raise SourceError(f"line {node.lineno}: put the cell body on its own lines.")

    return header[-1]


def _returned_names(node: ast.stmt) -> set[str]:
    """Names in the final `return` of a plain cell."""

    match node:
        case (
            ast.FunctionDef(body=[*_, ast.Return(value=value)])
            | ast.AsyncFunctionDef(body=[*_, ast.Return(value=value)])
        ):
            pass
        case _:
            return set()

    match value:
        case ast.Tuple(elts=elts):
            return {elt.id for elt in elts if isinstance(elt, ast.Name)}
        case ast.Name(id=name):
            return {name}
        case _:
            return set()


# Validation


def validate(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Check the notebook against its config and the authoring rules.

    Returns:
        One message per problem. Empty when the notebook is valid.
    """

    errors = _test_name_errors(nb, cfg) + _question_errors(nb, cfg)
    errors += _hidden_check_errors(nb) + _public_cell_errors(nb) + _marker_errors(nb)
    errors += _public_marker_errors(nb)
    errors += _near_marker_errors(nb) + _hidden_name_errors(nb)

    return errors


def _test_name_errors(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Duplicate tests, cells mixing questions, and unknown questions in tests or checks."""

    errors = []
    seen: set[str] = set()

    for cell in nb.cells:
        if len({question_id(name) for name in cell.tests}) > 1:
            errors.append(
                f"line {cell.start}: tests of different questions share a cell "
                f"({', '.join(cell.tests)}). Put each question's tests in their own cell."
            )

        for name in cell.tests:
            if name in seen:
                errors.append(f"`{name}` (line {cell.start}) is defined more than once.")
            seen.add(name)

            qid = question_id(name)
            if qid not in cfg.questions:
                errors.append(
                    f"`{name}` (line {cell.start}): no question `{qid}` in [tool.{TOOL}] questions."
                )

        if cell.check_qid is not None and cell.check_qid not in cfg.questions:
            errors.append(
                f'check("{cell.check_qid}") (line {cell.start}): no question '
                f"`{cell.check_qid}` in [tool.{TOOL}] questions."
            )

    return errors


def _question_errors(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Each auto question has tests and a check cell; each manual one has an answer cell."""

    errors = []
    tests = nb.tests_by_question()
    checked = {cell.check_qid for cell in nb.cells}

    for qid, question in cfg.questions.items():
        public, hidden = tests.get(qid, ([], []))

        if question.manual:
            if public or hidden:
                errors.append(
                    f"Question `{qid}` is manual but has tests: {', '.join(public + hidden)}."
                )
            if not nb.answer_cells(qid):
                errors.append(
                    f"Manual question `{qid}` has no answer cell. Add a markdown cell named "
                    f"`answer_{qid}`."
                )
            continue

        if not public and not hidden:
            errors.append(f"Question `{qid}` has no tests. Add `test_{qid}_*` functions.")
        elif public and qid not in checked:
            errors.append(
                f"Question `{qid}` has public tests but no check cell. Add a cell with "
                f'`mgs.check("{qid}")`.'
            )

    return errors


def _hidden_check_errors(nb: Notebook) -> list[str]:
    """Check calls sit outside `# HIDDEN` cells, which the student copy drops."""

    return [
        f"check() for {cell.check_qid} is in a hidden cell (line {cell.start}), so the student "
        f"copy would lose it. Move it to a cell without `{HIDDEN}`."
        for cell in nb.cells
        if cell.hidden and cell.check_qid is not None
    ]


def _public_cell_errors(nb: Notebook) -> list[str]:
    """Public test cells hold only `test_*` functions, as marimo's pytest collection needs."""

    errors = []

    for cell in nb.cells:
        if cell.kind is not CellKind.CELL or not cell.tests or cell.hidden:
            continue

        node = _cell_node(nb.src, cell)
        match node:
            case ast.FunctionDef(body=body) | ast.AsyncFunctionDef(body=body):
                pass
            case _:
                continue

        for index, stmt in enumerate(body):
            where = (
                f"Public test cell with {', '.join(cell.tests)} (line {cell.start}) has "
                f"`{split_lines(ast.unparse(stmt))[0]}` at line {stmt.lineno}."
            )
            match stmt:
                case ast.FunctionDef(name=name) if name.startswith("test_"):
                    continue
                case ast.Return() if index == len(body) - 1:
                    continue
                case ast.Import() | ast.ImportFrom():
                    errors.append(
                        f"{where} marimo's pytest collection skips test cells with imports. "
                        "Move the import to the setup cell (`with app.setup:`)."
                    )
                case _:
                    errors.append(
                        f"{where} A public test cell may hold only `test_*` functions. Move "
                        f"other code to its own cell, or mark the cell `{HIDDEN}`."
                    )
            break

    return errors


def _hidden_name_errors(nb: Notebook) -> list[str]:
    """Names the grader cannot provide to hidden cells, and hidden cells shadowing others.

    When grading, every test cell runs on its own, so a name defined in a test cell is not
    available to other cells. A hidden cell that redefines a student or setup name would
    replace the student's value. Names starting with `_` are private to their cell in marimo,
    so a hidden cell may reuse them.
    """

    errors = []
    students = nb.student_names()
    setup, _ = cell_names(nb.setup_source())
    names = {cell: cell_names(nb.cell_body_source(cell)) for cell in nb.cells if _graded(cell)}
    helpers = {name for cell in nb.helper_cells() for name in names[cell][0]}
    shared = students | setup | helpers

    for cell, (bound, _) in names.items():
        if not cell.hidden:
            continue
        bound = {name for name in bound if not name.startswith("_")}
        for name in sorted(bound & students):
            errors.append(
                f"line {cell.start}: hidden cell defines `{name}`, which is also defined by a "
                "student cell."
            )
        for name in sorted(bound & setup):
            errors.append(
                f"line {cell.start}: hidden cell defines `{name}`, which is also defined by "
                "the setup cell."
            )

    for cell, (_, free) in names.items():
        for name in sorted(free - shared):
            owners = [other for other, (bound, _) in names.items() if name in bound]
            if owners and cell not in owners:
                errors.append(
                    f"line {cell.start}: uses `{name}`, which only the test cell at line "
                    f"{owners[0].start} defines. Test cells run on their own when grading; "
                    f"define `{name}` in a `{HIDDEN}` cell without tests."
                )

    errors += _helper_cycle_errors({cell: names[cell] for cell in nb.helper_cells()})

    return errors


def _graded(cell: Cell) -> bool:
    """A test cell or a hidden helper cell."""

    return bool(cell.tests) or (cell.hidden and cell.kind is not CellKind.SETUP)


def _helper_cycle_errors(names: dict[Cell, tuple[set[str], set[str]]]) -> list[str]:
    """One error per cycle of hidden helper cells that need each other's names."""

    needs = {
        cell: [other for other, (bound, _) in names.items() if other is not cell and free & bound]
        for cell, (_, free) in names.items()
    }
    done: set[Cell] = set()
    errors = []

    def visit(cell: Cell, path: list[Cell]) -> None:
        if cell in path:
            cycle = path[path.index(cell) :]
            lines = ", ".join(str(c.start) for c in cycle)
            errors.append(f"hidden cells at lines {lines} need each other's names in a cycle.")
            return

        if cell in done:
            return

        for other in needs[cell]:
            visit(other, [*path, cell])

        done.add(cell)

    for cell in needs:
        visit(cell, [])

    return errors


def _public_marker_errors(nb: Notebook) -> list[str]:
    """Solution markers in public test cells, which go to students as written.

    One error per cell, at its first marker.
    """

    markers = (SOLUTION, BEGIN, END)
    public = [cell for cell in nb.cells if cell.tests and not cell.hidden]
    found: dict[Cell, tuple[int, str]] = {}

    for token in tokenize.generate_tokens(io.StringIO(nb.src).readline):
        number = token.start[0]
        comment = token.string.rstrip()

        if token.type != tokenize.COMMENT or comment not in markers:
            continue

        for cell in public:
            if cell.start <= number <= cell.end:
                found.setdefault(cell, (number, comment))

    return [
        f"line {number}: `{comment}` is in a public test cell. Students get the public tests "
        f"as written, so move the solution to a `{HIDDEN}` cell."
        for number, comment in found.values()
    ]


def _marker_errors(nb: Notebook) -> list[str]:
    """`# BEGIN SOLUTION` / `# END SOLUTION` pairs open and close in the same cell."""

    errors = []
    opened: int | None = None

    def cell_at(number: int) -> Cell | None:
        return next((cell for cell in nb.cells if cell.start <= number <= cell.end), None)

    for number, line in enumerate(split_lines(nb.src), start=1):
        match line.strip(), opened:
            case Marker.BEGIN, None:
                opened = number
            case Marker.BEGIN, int():
                errors.append(
                    f"line {number}: `{BEGIN}` inside the region opened at line {opened}."
                )
            case Marker.END, None:
                errors.append(f"line {number}: `{END}` without a matching `{BEGIN}`.")
            case Marker.END, int() as start:
                if cell_at(start) != cell_at(number):
                    errors.append(
                        f"line {start}: `{BEGIN}` is closed in another cell, at line {number}."
                    )
                opened = None

    if opened is not None:
        errors.append(f"line {opened}: `{BEGIN}` without a matching `{END}`.")

    return errors


def _near_marker_errors(nb: Notebook) -> list[str]:
    """Comments that look like a marker but are misspelled, or a `# HIDDEN` after code.

    A misspelled marker is an ordinary comment to the transform, so the solution or hidden
    test next to it would reach the student copy. Prompt lines count only inside solution
    regions, so a comment such as `# prompt template` elsewhere is fine.
    """

    errors = []
    markers = (SOLUTION, BEGIN, END)
    lines = split_lines(nb.src)
    regions = _region_lines(lines)

    for token in tokenize.generate_tokens(io.StringIO(nb.src).readline):
        if token.type != tokenize.COMMENT:
            continue

        comment = token.string.rstrip()
        number = token.start[0]

        if NEAR_SOLUTION_RE.match(comment) and comment not in markers:
            errors.append(
                f"line {number}: `{comment}` looks like a solution marker. Write it exactly as "
                f"`{SOLUTION}`, `{BEGIN}` or `{END}`."
            )
        elif NEAR_HIDDEN_RE.match(comment) and comment != HIDDEN:
            errors.append(
                f"line {number}: `{comment}` looks like a hidden marker. Write it exactly as "
                f"`{HIDDEN}` on its own line, or reword the comment."
            )
        elif comment == HIDDEN and lines[number - 1].strip() != HIDDEN:
            errors.append(f"line {number}: `{HIDDEN}` must be on its own line to hide its cell.")
        elif number in regions and NEAR_PROMPT_RE.match(comment) and not comment.startswith(PROMPT):
            errors.append(
                f"line {number}: `{comment}` looks like a prompt line. Write it as "
                f"`{PROMPT} <code>`."
            )

    return errors


def _region_lines(lines: list[str]) -> set[int]:
    """Line numbers inside `# BEGIN SOLUTION` ... `# END SOLUTION` regions, markers included.

    A region left open runs to the end of the file.
    """

    inside: set[int] = set()
    start: int | None = None

    for number, line in enumerate(lines, start=1):
        match line.strip(), start:
            case Marker.BEGIN, None:
                start = number
            case Marker.END, int():
                inside |= set(range(start, number + 1))
                start = None

    if start is not None:
        inside |= set(range(start, len(lines) + 1))

    return inside

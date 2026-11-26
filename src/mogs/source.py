"""Static parse of a marimo source notebook into cells, and checks of its authoring rules.

The notebook is never imported or run. Cells, tests, hidden cells and grade calls come from
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

from mogs.config import (
    ASSIGNMENT,
    Api,
    AssignmentConfig,
    api_calls,
    normalize_newlines,
    question_id,
    split_lines,
)

INSTRUCTOR = "instructor"


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


class HtmlMarker(StrEnum):
    """Solution markers in a Markdown cell, where a `#` line would be a heading."""

    BEGIN = "<!-- BEGIN SOLUTION -->"
    END = "<!-- END SOLUTION -->"
    PROMPT = "<!-- PROMPT: <text> -->"


HTML_PROMPT_RE = re.compile(r"<!-- PROMPT:(?: (.*?))? -->")

# Comments that look like a marker but are not spelled exactly like one.
NEAR_HTML_RE = re.compile(r"<!--\s*(?:(?:begin|end)\s*)?(?:solu\w*|pro?m\w*)", re.IGNORECASE)
NEAR_PROMPT_RE = re.compile(r"#\s*prompt\b", re.IGNORECASE)
NEAR_HIDDEN_RE = re.compile(r"#\s*hidden\b", re.IGNORECASE)
# The first two words of a comment, for `near_solution_marker`.
LEADING_WORDS_RE = re.compile(r"(?:#|<!--)\s*([A-Za-z]+)(?:\s+([A-Za-z]+))?")
SOLUTION_WORD = "solution"
REGION_WORDS = ("begin", "end")


# What the build report and the assignment summary show for a written question without prompts.
NO_TEXT = "no question text"


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
        grade_qid: Question id of the cell's `grade(...)` call, if any.
        assignment: The cell holds an `assignment(...)` call.
        late_hidden: Line of a `# HIDDEN` that comes after the cell's first statement, if any.
    """

    kind: CellKind
    name: str
    start: int
    end: int
    tests: tuple[str, ...]
    hidden: bool
    grade_qid: str | None
    assignment: bool = False
    late_hidden: int | None = None

    @property
    def instructor(self) -> bool:
        """Named `instructor` or `instructor_<suffix>`: for the instructor only.

        The build leaves such cells out of the student and solved copies.
        """

        return self.name == INSTRUCTOR or self.name.startswith(f"{INSTRUCTOR}_")


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
        """Cells named `answer_<qid>` or `answer_<qid>_<suffix>`, as in the student copy."""

        return self._named("answer", qid)

    def prompt_cells(self, qid: str) -> tuple[Cell, ...]:
        """Cells named `prompt_<qid>` or `prompt_<qid>_<suffix>`, as in the student copy."""

        return self._named("prompt", qid)

    def question_cells(self, qid: str) -> tuple[Cell, ...]:
        """The cells of a question in the source: after the previous grade cell, up to its own.

        The grade cell itself is not included. Empty when no grade cell names `qid`.
        """

        graded = [i for i, cell in enumerate(self.cells) if cell.grade_qid is not None]
        end = next((i for i in graded if self.cells[i].grade_qid == qid), None)

        if end is None:
            return ()

        start = max((i + 1 for i in graded if i < end), default=0)

        return self.cells[start:end]

    def written_cells(self, qid: str) -> tuple[tuple[Cell, ...], tuple[Cell, ...]]:
        """Prompt and answer cells of a written question in the source, in file order.

        The answers are the plain Markdown cells of the question (`question_cells`) that hold
        a `<!-- BEGIN SOLUTION -->` line. Each answer's prompt is the run of `mo.md(...)` cells
        just before it. Cells students don't see (hidden, instructor, test and assignment
        cells) are skipped; any other cell, an answer included, ends the run. So text between
        two answers belongs to the second. A prompt need not be a plain string; the PDF shows
        such a cell as source.
        """

        lines = split_lines(self.src)
        prompts: list[Cell] = []
        answers: list[Cell] = []
        run: list[Cell] = []

        for cell in self.question_cells(qid):
            if cell.hidden or cell.tests or cell.assignment or cell.instructor:
                continue

            if is_markdown(self, cell) and any(
                line_marker(line, True)[0] is Marker.BEGIN
                for line in lines[cell.start - 1 : cell.end]
            ):
                prompts += run
                answers.append(cell)
                run = []
            elif is_md_call(self, cell):
                run.append(cell)
            else:
                run = []

        return tuple(prompts), tuple(answers)

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

    def markdown_lines(self) -> frozenset[int]:
        """Line numbers of the cells that are just `mo.md("...")`, decorators included."""

        return frozenset(
            number
            for cell in self.cells
            if is_markdown(self, cell)
            for number in range(cell.start, cell.end + 1)
        )

    def grade_call(self, cell: Cell) -> ast.Call:
        """The grade call of a cell, with line numbers of the whole notebook.

        Raises:
            SourceError: If the cell does not hold exactly one grade call.
        """

        calls = api_calls(ast.parse(self.src), _cell_node(self.src, cell), Api.GRADE)

        match calls:
            case [call]:
                return call
            case _:
                raise SourceError(
                    f"line {cell.start}: expected one grade call, found {len(calls)}."
                )

    def grade_test_file(self, cell: Cell) -> str | None:
        """The path a cell's grade call passes as a string literal `test_file`, if it does.

        Raises:
            SourceError: If the cell does not hold exactly one grade call.
        """

        for keyword in self.grade_call(cell).keywords:
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


def prompt_previews(nb: Notebook, qid: str, width: int = 60) -> tuple[str, ...]:
    """The first non-blank line of each prompt cell of a written question in the source.

    A line longer than `width` is cut and ends in `…`. For a prompt that is not a plain
    string, the line comes from its source. A blank prompt has no line, so a question with
    only blank prompts gets `()`.
    """

    previews = []

    for cell in nb.written_cells(qid)[0]:
        text = answer_text(nb, cell)
        source = nb.cell_body_source(cell) if text is None else text
        line = next((line.strip() for line in source.splitlines() if line.strip()), "")

        if line:
            previews.append(line if len(line) <= width else line[: width - 1] + "…")

    return tuple(previews)


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


def is_md_call(nb: Notebook, cell: Cell) -> bool:
    """A plain cell that is just a call to `mo.md`, with any argument."""

    if cell.kind is not CellKind.CELL:
        return False

    try:
        tree = ast.parse(nb.cell_body_source(cell))
    except (SyntaxError, SourceError):
        return False

    match tree.body:
        case [ast.Expr(value=ast.Call(func=ast.Attribute(attr="md")))]:
            return True
        case _:
            return False


def is_markdown(nb: Notebook, cell: Cell) -> bool:
    """A plain cell that is just `mo.md("...")`, the way marimo writes a Markdown cell."""

    if cell.kind is not CellKind.CELL:
        return False

    try:
        tree = ast.parse(nb.cell_body_source(cell))
    except (SyntaxError, SourceError):
        return False

    return bool(tree.body) and answer_text(nb, cell) is not None


def answer_placeholders(nb: Notebook, qids: Iterable[str]) -> dict[str, str]:
    """The text of each answer cell of `qids`, as `answer_text` gives it, by cell name.

    Only answer cells holding non-blank `mo.md("...")` are included.
    """

    placeholders = {}

    for qid in qids:
        for cell in nb.answer_cells(qid):
            match answer_text(nb, cell):
                case str() as text if text.strip():
                    placeholders[cell.name] = text

    return placeholders


def unanswered(text: str | None, placeholder: str | None) -> bool:
    """Whether an answer is blank or still its starting text, ignoring whitespace.

    Args:
        text: The answer cell's text from `answer_text`; `None`, for code, counts as an answer.
        placeholder: The text the answer cell starts with, if any.
    """

    if text is None:
        return False

    return not text.strip() or (placeholder is not None and _words(text) == _words(placeholder))


def _words(text: str) -> str:
    """`text` with each run of whitespace made one space."""

    return " ".join(text.split())


def cell_source(nb: Notebook, cell: Cell) -> str:
    """The cell's lines as written, decorators included."""

    return "\n".join(split_lines(nb.src)[cell.start - 1 : cell.end])


# Parsing


def parse_notebook(src: str) -> Notebook:
    """Parse the cells of a marimo notebook.

    Raises:
        SourceError: If the source is not valid Python, or a grade call does not name its
            question with a string literal, or a cell holds more than one, or an `@app.*`
            decorator does not fit what it decorates.
    """

    src = normalize_newlines(src)

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise SourceError(f"The notebook is not valid Python (line {e.lineno}): {e.msg}") from e

    lines = split_lines(src)
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
        markers = [start + i for i, line in enumerate(text) if line.strip() == HIDDEN]
        first_statement = node.body[0].lineno
        late = next((line for line in markers if line > first_statement), None)
        plain = kind is CellKind.CELL
        grade = _call_qid(tree, node, Api.GRADE) if plain else None
        assignment = bool(api_calls(tree, node, Api.ASSIGNMENT))

        cells.append(Cell(kind, name, start, end, tests, bool(markers), grade, assignment, late))

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


def _call_qid(tree: ast.Module, node: ast.stmt, api: Api) -> str | None:
    """Question id of the single `api` call in a cell, or None if it has none."""

    found: list[str] = []

    for call in api_calls(tree, node, api):
        match call.args:
            case [ast.Constant(value=str() as qid), *_]:
                found.append(qid)
            case _:
                raise SourceError(
                    f"line {call.lineno}: {api}() needs the question id as a string literal "
                    f'first argument, e.g. mogs.{api}("q1", points=10).'
                )

    match found:
        case []:
            return None
        case [qid]:
            return qid
        case _:
            raise SourceError(
                f"line {node.lineno}: a cell may hold only one {api} call, found {len(found)}."
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


def source_warnings(nb: Notebook) -> list[str]:
    """Things in the notebook that build but may not do what the instructor meant."""

    return [
        f"line {cell.late_hidden}: `{HIDDEN}` hides the whole cell at line {cell.start}, not "
        "only the code after it. Move it to the cell's first line, or move the hidden tests "
        "to a cell of their own."
        for cell in nb.cells
        if cell.late_hidden is not None
    ]


def validate(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Check the notebook against its config and the authoring rules.

    Returns:
        One message per problem. Empty when the notebook is valid.
    """

    # Misspelled markers first: they also cause the region errors below.
    errors = _near_marker_errors(nb) + _near_html_marker_errors(nb)
    errors += _test_name_errors(nb, cfg) + _question_errors(nb, cfg)
    errors += _hidden_grade_errors(nb) + _public_cell_errors(nb) + _marker_errors(nb)
    errors += _assignment_errors(nb) + _instructor_errors(nb)
    errors += _public_marker_errors(nb)
    errors += _hidden_name_errors(nb) + _cell_variable_errors(nb)

    return errors


def _test_name_errors(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Duplicate tests, cells mixing questions, and unknown questions in tests or grades."""

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
                    f"`{name}` (line {cell.start}): no question `{qid}`. Declare it in a cell "
                    f'with `mogs.grade("{qid}", points=...)`.'
                )

        if cell.grade_qid is not None and cell.grade_qid not in cfg.questions:
            errors.append(
                f'grade("{cell.grade_qid}") (line {cell.start}): no question '
                f"`{cell.grade_qid}` in the config."
            )

    return errors


def _question_errors(nb: Notebook, cfg: AssignmentConfig) -> list[str]:
    """Each auto question has tests and a grade cell; each manual one has an answer cell."""

    errors = []
    tests = nb.tests_by_question()
    graded = {cell.grade_qid for cell in nb.cells}

    for qid, question in cfg.questions.items():
        public, hidden = tests.get(qid, ([], []))

        if question.manual:
            if public or hidden:
                errors.append(
                    f"Question `{qid}` is manual but has tests: {', '.join(public + hidden)}."
                )
            if not nb.written_cells(qid)[1]:
                errors.append(
                    f"Question `{qid}` has no tests and no written answer. Add `test_{qid}_*` "
                    f'functions, or a Markdown cell before `mogs.grade("{qid}", ...)` with '
                    f"your answer between `{HtmlMarker.BEGIN}` and `{HtmlMarker.END}`."
                )
            continue

        if not public and not hidden:
            errors.append(f"Question `{qid}` has no tests. Add `test_{qid}_*` functions.")
        elif public and qid not in graded:
            errors.append(
                f"Question `{qid}` has public tests but no grade cell. Add a cell with "
                f'`mogs.grade("{qid}", points=...)`.'
            )

    return errors


def _hidden_grade_errors(nb: Notebook) -> list[str]:
    """Grade calls sit outside `# HIDDEN` cells, which the student copy drops."""

    return [
        f"grade() for {cell.grade_qid} is in a hidden cell (line {cell.start}), so the student "
        f"copy would lose it. Move it to a cell without `{HIDDEN}`."
        for cell in nb.cells
        if cell.hidden and cell.grade_qid is not None
    ]


def _assignment_errors(nb: Notebook) -> list[str]:
    """The `assignment(...)` call sits alone in a plain cell, which the build drops."""

    errors = []

    for cell in nb.cells:
        if not cell.assignment:
            continue

        node = _cell_node(nb.src, cell)
        calls = api_calls(ast.parse(nb.src), node, Api.ASSIGNMENT)
        match node:
            case ast.FunctionDef(body=[ast.Expr(value=ast.Call() as call), *rest]) if (
                cell.kind is CellKind.CELL
                and not cell.hidden
                and calls == [call]
                and all(isinstance(stmt, ast.Return) and stmt.value is None for stmt in rest)
                and len(rest) <= 1
            ):
                continue

        errors.append(
            f"line {cell.start}: put `{ASSIGNMENT}` in a cell of its own, without other code "
            f"or `{HIDDEN}`. The build removes that cell from the student notebook."
        )

    return errors


def _instructor_errors(nb: Notebook) -> list[str]:
    """Instructor cells are plain cells that the rest of the notebook does not need.

    The build drops them, so they define no names for other cells and hold no tests or grade
    calls.
    """

    errors = []

    for cell in nb.cells:
        if not cell.instructor:
            continue

        where = f"Instructor cell `{cell.name}` (line {cell.start})"

        if cell.kind is not CellKind.CELL:
            errors.append(f"{where} must be a plain `@app.cell`; the build removes it.")
            continue

        returned = sorted(_returned_names(_cell_node(nb.src, cell)))
        problems = [
            *([f"returns {', '.join(returned)}"] if returned else []),
            *([f"defines {', '.join(cell.tests)}"] if cell.tests else []),
            *([f"calls grade() for {cell.grade_qid}"] if cell.grade_qid else []),
            *([f"holds `{HIDDEN}`"] if cell.hidden else []),
        ]

        if problems:
            errors.append(
                f"{where} {'; '.join(problems)}. The build removes instructor cells from the "
                "student notebook, so keep only notes for yourself in them."
            )

    return errors


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


CELL_VARIABLE_FIX = (
    "The grader imports the functions the tests use without running the notebook, so it "
    "can't get that name. Write the answer as an `@app.function` that the test calls, and put "
    "data the tests need in the setup cell (`with app.setup:`) or load it in the test."
)


def _cell_variable_errors(nb: Notebook) -> list[str]:
    """Tests and hidden helpers that read a name defined by a student's plain cell.

    The grader can get such a name only by running the whole notebook (see `loader`), where
    one failing cell anywhere makes it unavailable. Graded answers are functions instead.
    """

    errors = []
    defined = _cell_variable_lines(nb)
    helpers = nb.helper_cells()

    for cell in nb.cells:
        if cell.tests:
            for test, line, read in _test_reads(nb, cell):
                errors += [
                    f"Question `{question_id(test)}`: `{test}` (line {line}) reads `{name}`, "
                    f"which a plain `@app.cell` defines at line {defined[name]}. "
                    + CELL_VARIABLE_FIX
                    for name in sorted(read & defined.keys())
                ]
        elif cell in helpers:
            _, read = cell_names(nb.cell_body_source(cell))
            errors += [
                f"The `{HIDDEN}` cell at line {cell.start} reads `{name}`, which a plain "
                f"`@app.cell` defines at line {defined[name]}. " + CELL_VARIABLE_FIX
                for name in sorted(read & defined.keys())
            ]

    return errors


def _cell_variable_lines(nb: Notebook) -> dict[str, int]:
    """Each name a student's plain cell returns, with the line that first binds it."""

    lines = {}

    for cell in nb.cells:
        if cell.kind is not CellKind.CELL or cell.tests or cell.hidden:
            continue

        node = _cell_node(nb.src, cell)
        for name in _returned_names(node):
            lines[name] = _binding_line(node, name) or cell.start

    return lines


def _binding_line(node: ast.stmt, name: str) -> int | None:
    """Line of the first statement in a plain cell's body that binds `name`."""

    match node:
        case ast.FunctionDef(body=body) | ast.AsyncFunctionDef(body=body):
            pass
        case _:
            return None

    for stmt in body:
        for sub in ast.walk(stmt):
            match sub:
                case ast.Name(id=found, ctx=ast.Store()) if found == name:
                    return sub.lineno
                case (
                    ast.FunctionDef(name=found)
                    | ast.AsyncFunctionDef(name=found)
                    | ast.ClassDef(name=found)
                ) if found == name:
                    return sub.lineno
                case ast.alias(name=imported, asname=alias) if (
                    alias or imported.split(".")[0]
                ) == name:
                    return stmt.lineno

    return None


def _test_reads(nb: Notebook, cell: Cell) -> list[tuple[str, int, set[str]]]:
    """Each test of a cell, its line, and the global names it reads.

    Names the cell reads outside its tests, such as in a helper function of a hidden test
    cell, count for every test of the cell.
    """

    node = _cell_node(nb.src, cell)
    bound, read = cell_names(nb.cell_body_source(cell))

    match node:
        case ast.FunctionDef(body=body) | ast.AsyncFunctionDef(body=body) if (
            cell.kind is CellKind.CELL
        ):
            functions = [stmt for stmt in body if isinstance(stmt, ast.FunctionDef)]
        case ast.FunctionDef():
            functions = [node]
        case _:
            functions = []

    tests = {fn.name: fn for fn in functions if fn.name in cell.tests}
    own = {name: cell_names(ast.unparse(fn))[1] - bound for name, fn in tests.items()}
    rest = read - set().union(*own.values())

    return [(name, fn.lineno, own[name] | rest) for name, fn in tests.items()]


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


def line_marker(line: str, markdown: bool) -> tuple[Marker | None, str]:
    """The solution marker `line` is, and a prompt line's text.

    In a Markdown cell the markers are HTML comments (`HtmlMarker`); elsewhere they are
    Python comments. A line that is no marker gives `(None, "")`.
    """

    stripped = line.strip()

    if markdown:
        match stripped:
            case HtmlMarker.BEGIN:
                return Marker.BEGIN, ""
            case HtmlMarker.END:
                return Marker.END, ""

        prompt = HTML_PROMPT_RE.fullmatch(stripped)
        return (Marker.PROMPT, prompt.group(1) or "") if prompt else (None, "")

    match stripped:
        case Marker.BEGIN | Marker.END:
            return Marker(stripped), ""
        case _ if stripped.startswith(PROMPT):
            return Marker.PROMPT, stripped.removeprefix(PROMPT).removeprefix(" ")
        case _:
            return None, ""


def _marker_errors(nb: Notebook) -> list[str]:
    """Solution markers pair up in the same cell, and Markdown cells use the HTML ones."""

    errors = []
    opened: int | None = None
    markdown = nb.markdown_lines()

    def cell_at(number: int) -> Cell | None:
        return next((cell for cell in nb.cells if cell.start <= number <= cell.end), None)

    for number, line in enumerate(split_lines(nb.src), start=1):
        if number in markdown:
            errors += _markdown_line_errors(number, line.strip())
        elif line_marker(line, True)[0] is not None:
            errors.append(
                f"line {number}: `{line.strip()}` is read only in a Markdown cell written with "
                'a plain string, `mo.md(r"""...""")`. Elsewhere the solution would reach '
                "students."
            )

        match line_marker(line, number in markdown)[0], opened:
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


def _markdown_line_errors(number: int, stripped: str) -> list[str]:
    """A `#` marker in a Markdown cell, where it is a heading."""

    html = {
        BEGIN: HtmlMarker.BEGIN,
        END: HtmlMarker.END,
        SOLUTION: f"{HtmlMarker.BEGIN} ... {HtmlMarker.END}",
    }

    for marker, replacement in html.items():
        if stripped == marker or stripped.endswith(f" {marker}"):
            return [
                f"line {number}: `{marker}` in a Markdown cell is a heading. Write "
                f"`{replacement}` instead."
            ]

    if stripped.startswith(PROMPT):
        return [
            f"line {number}: `{PROMPT}` in a Markdown cell is a heading. Write "
            f"`{HtmlMarker.PROMPT}` instead."
        ]

    return []


def _near_marker_errors(nb: Notebook) -> list[str]:
    """Comments that look like a marker but are misspelled, or a `# HIDDEN` after code.

    A misspelled marker is an ordinary comment to the transform, so the solution or hidden
    test next to it would reach the student copy. Prompt lines count only inside solution
    regions, so a comment such as `# prompt template` elsewhere is fine.
    """

    errors = []
    markers = (SOLUTION, BEGIN, END)
    lines = split_lines(nb.src)
    regions = _region_lines(lines, nb.markdown_lines())

    for token in tokenize.generate_tokens(io.StringIO(nb.src).readline):
        if token.type != tokenize.COMMENT:
            continue

        comment = token.string.rstrip()
        number = token.start[0]

        if near_solution_marker(comment) and comment not in markers:
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


def _near_html_marker_errors(nb: Notebook) -> list[str]:
    """HTML comments in Markdown cells that look like a marker but are misspelled.

    Only `<!--` lines count. A `#` line in Markdown is a heading, such as `# Solution
    approach`; `_markdown_line_errors` handles the exact `#` markers there.
    """

    errors = []
    lines = split_lines(nb.src)
    exact = (HtmlMarker.BEGIN, HtmlMarker.END)

    for number in sorted(nb.markdown_lines()):
        stripped = lines[number - 1].strip()

        if not stripped.startswith("<!--"):
            continue

        if stripped in exact or HTML_PROMPT_RE.fullmatch(stripped):
            continue

        if NEAR_HTML_RE.match(stripped) or near_solution_marker(stripped):
            errors.append(
                f"line {number}: `{stripped}` looks like a solution marker. Write it exactly as "
                f"`{HtmlMarker.BEGIN}`, `{HtmlMarker.END}` or `{HtmlMarker.PROMPT}`."
            )

    return errors


def near_solution_marker(comment: str) -> bool:
    """Whether `comment` starts like a solution marker, misspelled or not.

    `comment` is a `#` or `<!--` comment. Its first word, or its first two words after
    `BEGIN` or `END`, are compared with `SOLUTION`, `BEGIN SOLUTION` and `END SOLUTION`,
    ignoring case and spaces. One typo (a wrong, missing, extra or swapped letter) counts.
    Two count only in an all-caps word that keeps the first letter, as in `# SLOUTOIN`;
    lowercase words two typos away are ordinary English, such as `resolution`.
    The caller checks that the comment is not the exact marker.
    """

    match = LEADING_WORDS_RE.match(comment)
    if match is None:
        return False

    first, second = match.group(1), match.group(2)

    if _near_word(first, SOLUTION_WORD):
        return True

    if any(_near_word(first, region + SOLUTION_WORD) for region in REGION_WORDS):
        return True

    region = any(_typos(first.lower(), word) <= 1 for word in REGION_WORDS)
    return region and second is not None and _near_word(second, SOLUTION_WORD)


def _near_word(word: str, target: str) -> bool:
    """`word` is at most one typo from `target`, or two in all caps with the same first letter."""

    lower = word.lower()
    typos = _typos(lower, target)

    return typos <= 1 or (typos == 2 and word.isupper() and lower[0] == target[0])


def _typos(a: str, b: str) -> int:
    """Edits between `a` and `b`: letters inserted, deleted, replaced or swapped with the next.

    This is the optimal string alignment distance.
    """

    previous: list[int] = []
    row = list(range(len(b) + 1))

    for i, char in enumerate(a, start=1):
        before, previous, row = previous, row, [i] + [0] * len(b)

        for j, other in enumerate(b, start=1):
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (char != other))

            if i > 1 and j > 1 and char == b[j - 2] and a[i - 2] == other:
                row[j] = min(row[j], before[j - 2] + 1)

    return row[-1]


def _region_lines(lines: list[str], markdown: frozenset[int]) -> set[int]:
    """Line numbers inside solution regions, markers included.

    A region left open runs to the end of the file.
    """

    inside: set[int] = set()
    start: int | None = None

    for number, line in enumerate(lines, start=1):
        match line_marker(line, number in markdown)[0], start:
            case Marker.BEGIN, None:
                start = number
            case Marker.END, int():
                inside |= set(range(start, number + 1))
                start = None

    if start is not None:
        inside |= set(range(start, len(lines) + 1))

    return inside

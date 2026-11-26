"""Student notebook from the source notebook.

The transform edits the source as text, line by line, so everything it does not touch keeps
its exact layout and comments.
"""

import ast
import io
import tokenize
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from enum import StrEnum
from pathlib import Path, PurePosixPath

from marimo_gradescope import __version__
from marimo_gradescope.config import (
    PACKAGE,
    AssignmentConfig,
    normalize_newlines,
    set_dependency,
    split_lines,
)
from marimo_gradescope.source import (
    BEGIN,
    END,
    SOLUTION,
    Cell,
    CellKind,
    Marker,
    Notebook,
    SourceError,
    answer_placeholders,
    cell_names,
    line_marker,
    parse_notebook,
    question_id,
)

EXPORT_BUTTON = "export_button"
MARKDOWN_PLACEHOLDER = "*Write your answer here.*"
DEFAULT_ALIAS = "mogs"
MARIMO_ALIAS = "mo"
TESTS_FOLDER = "tests"


class MarimoImport(StrEnum):
    """Where the student copy gets `mo`, the name marimo is imported as."""

    SETUP = "setup"
    CELL = "cell"
    MISSING = "missing"


# Replacement lines by 1-based line number. A line not in the map is kept as it is.
type Edits = dict[int, list[str]]


def pinned_requirement(cfg: AssignmentConfig) -> str:
    """Requirement on this exact package version, with the `pdf` extra if a question is manual."""

    extra = "[pdf]" if any(question.manual for question in cfg.questions.values()) else ""
    return f"marimo-gradescope{extra}=={__version__}"


def student_notebook(
    src: str,
    nb: Notebook,
    cfg: AssignmentConfig,
    tool_requirement: str,
    *,
    solutions: bool = False,
) -> str:
    r"""Turn a source notebook into the notebook students get.

    - A `# BEGIN SOLUTION` ... `# END SOLUTION` region becomes its `# PROMPT: <code>` lines,
      at the indent of `# BEGIN SOLUTION`, or `...` without them. In a Markdown cell the
      markers are `<!-- BEGIN SOLUTION -->`, `<!-- END SOLUTION -->` and
      `<!-- PROMPT: <text> -->`, and the placeholder is `*Write your answer here.*`.
    - `x = rhs  # SOLUTION` becomes `x = ...`; another statement ending in `# SOLUTION` is
      dropped.
    - `# HIDDEN` cells, public test cells, the `assignment(...)` cell and instructor cells
      are dropped with the blank lines after them. The public tests go to their own files
      instead (`public_test_files`).
    - `grade("q1", points=<p>)` becomes `grade("q1", points=<p>, title=<title>,
      test_file="tests/q1.py", uses=[<names>], hidden=<n>)`, where `uses` lists the student
      names the public tests read, so marimo runs the cell again when one of them changes.
      For a written question it becomes `grade("q2", points=<p>, title=<title>,
      manual=True)`. The points and title come from `cfg`.
    - A written question's Markdown cells are named for the export: its answers
      `answer_<qid>`, `answer_<qid>_2`, ..., and the prompts before them `prompt_<qid>`, ...
    - Export cells go after the last cell: a "Submit" note, the export button and the export
      panel (`export_cells`).
    - The header depends on `tool_requirement`.

    With `solutions=True` it makes the solved copy instead: the same notebook, except that a
    solution region keeps its code without the marker and `# PROMPT:` lines, and
    `# SOLUTION` comments are removed with the code kept.

    Args:
        src: Source notebook text.
        nb: The notebook parsed from `src`. It should pass `validate`.
        cfg: Assignment config of the notebook.
        tool_requirement: Requirement on this package for the header, such as
            `pinned_requirement(cfg)`.
        solutions: Keep the solutions.

    Returns:
        The student notebook, with `\n` line ends.

    Raises:
        ValueError: If `nb` was not parsed from `src`.
        SourceError: If a `# SOLUTION` comment does not end a statement it can edit, a grade
            call names an unknown question, solution markers do not pair up, the source
            defines `export_button` itself, or the result is not valid Python.
        ConfigError: If a header edit fails.
    """

    if normalize_newlines(src) != nb.src:
        raise ValueError("`nb` was not parsed from `src`.")

    if EXPORT_BUTTON in nb.student_names():
        raise SourceError(
            f"The source notebook defines `{EXPORT_BUTTON}`. The build adds the export cells "
            "itself; remove yours."
        )

    lines = split_lines(nb.src)
    edits: Edits = {}

    for cell in nb.cells:
        if cell.hidden or cell.tests or cell.assignment or cell.instructor:
            _drop_cell(edits, lines, cell)

    regions = _solution_regions(edits, lines, solutions, nb.markdown_lines())
    _inline_solutions(edits, lines, nb.src, solutions, regions)

    for cell in nb.cells:
        if cell.grade_qid is not None and not cell.hidden and not cell.instructor:
            _rewrite_grade(edits, lines, nb, cell, cfg)

    for question in cfg.questions.values():
        if question.manual:
            _name_written_cells(edits, lines, nb, question.id)

    text = "\n".join(
        new for number, line in enumerate(lines, start=1) for new in edits.get(number, [line])
    )
    text = set_dependency(text, tool_requirement)

    try:
        ast.parse(text)
    except SyntaxError as e:
        raise SourceError(
            f"The student notebook is not valid Python (line {e.lineno}): {e.msg}. Check the "
            f"`{SOLUTION}` and `{BEGIN}` edits around that line."
        ) from e

    manual = [question.id for question in cfg.questions.values() if question.manual]
    # The copy, not the source: the edits above may have removed an import or a name.
    copy = parse_notebook(text)
    placeholders = answer_placeholders(copy, manual)
    cells = export_cells(cfg, package_alias(copy), placeholders, marimo=marimo_import(copy))

    return _append_cells(text, cells)


def test_file(qid: str) -> PurePosixPath:
    """Path of a question's public test file, relative to the student notebook."""

    return PurePosixPath(TESTS_FOLDER, f"{qid}.py")


def public_test_files(nb: Notebook) -> dict[PurePosixPath, str]:
    """The public test file of each auto-graded question, by `test_file` path.

    A file holds the bodies of the question's public test cells, in file order. A question
    without public tests gets a file with only its first comment line.

    Args:
        nb: The source notebook. It should pass `validate`.
    """

    tested = nb.tests_by_question()
    qids = dict.fromkeys(
        cell.grade_qid
        for cell in nb.cells
        if cell.grade_qid in tested and not cell.hidden and not cell.instructor
    )
    files = {}

    for qid in qids:
        bodies = [nb.cell_body_source(cell) for cell in _public_test_cells(nb, qid)]
        head = f'# Public tests for {qid}, run by grade("{qid}") in the notebook.\n'
        files[test_file(qid)] = "\n\n".join([head, *bodies])

    return files


def _public_test_cells(nb: Notebook, qid: str) -> list[Cell]:
    return [
        cell
        for cell in nb.cells
        if cell.tests and not cell.hidden and question_id(cell.tests[0]) == qid
    ]


def package_alias(nb: Notebook) -> str | None:
    """The name the setup cell imports this package as, if it does.

    Raises:
        SourceError: If it does not, and `mogs`, the name the export cells would import it
            as, is already taken.
    """

    for node in ast.parse(nb.setup_source()).body:
        match node:
            case ast.Import(names=names):
                for alias in names:
                    if alias.name == PACKAGE:
                        return alias.asname or PACKAGE

    if DEFAULT_ALIAS in nb.student_names() | cell_names(nb.setup_source())[0]:
        raise SourceError(
            f"The export cells need `{PACKAGE}`, and `{DEFAULT_ALIAS}` is taken. Add "
            f"`import {PACKAGE} as <name>` to the setup cell."
        )

    return None


def marimo_import(nb: Notebook) -> MarimoImport:
    """Which cell has `import marimo as mo`: the setup cell, a student cell, or none.

    Raises:
        SourceError: If none does, and `mo`, the name the export cells would import marimo as,
            is already taken.
    """

    cells = [cell for cell in nb.cells if not cell.hidden and cell.kind is not CellKind.SETUP]
    sources = [
        (MarimoImport.SETUP, nb.setup_source()),
        *((MarimoImport.CELL, nb.cell_body_source(cell)) for cell in cells),
    ]
    bound: set[str] = set()

    for where, source in sources:
        for node in ast.parse(source).body:
            match node:
                case ast.Import(names=names) if any(
                    alias.name == "marimo" and alias.asname == MARIMO_ALIAS for alias in names
                ):
                    return where

        bound |= cell_names(source)[0]

    if MARIMO_ALIAS in bound:
        raise SourceError(
            f"The Submit note needs `import marimo as {MARIMO_ALIAS}`, and `{MARIMO_ALIAS}` is "
            "taken. Add `import marimo as mo` to the setup cell."
        )

    return MarimoImport.MISSING


def export_cells(
    cfg: AssignmentConfig,
    alias: str | None,
    placeholders: Mapping[str, str] | None = None,
    *,
    marimo: MarimoImport = MarimoImport.SETUP,
) -> str:
    """Cells that let the student export a submission: a note, the button and the panel.

    The note is a cell with nothing but `mo.md(...)`. marimo renders such a cell when the
    notebook opens, before anything runs, so the student sees how to show the button.

    Args:
        cfg: Assignment config.
        alias: The setup cell's name for this package; `None` adds a cell that imports it as
            `mogs`.
        placeholders: The student copy's answer text per manual question, so the export
            can warn about an answer left as it was.
        marimo: Which cell has `import marimo as mo`; `MISSING` adds a cell that does.
    """

    stem = Path(cfg.notebook).stem
    manual = [question.id for question in cfg.questions.values() if question.manual]
    titles = {qid: question.title for qid, question in cfg.questions.items()}
    name = alias or DEFAULT_ALIAS
    call = (
        f"{name}.export({EXPORT_BUTTON}, name={python_literal(stem)}, "
        f"manual={python_literal(manual)}, titles={python_literal(titles)}, "
        f"placeholders={python_literal(dict(placeholders or {}))})"
    )
    imports = [
        *([("marimo", MARIMO_ALIAS)] if marimo is MarimoImport.MISSING else []),
        *([] if alias is not None else [(PACKAGE, DEFAULT_ALIAS)]),
    ]
    names = [as_name for _, as_name in imports]
    importer = (
        "@app.cell(hide_code=True)\ndef _():\n"
        + "".join(f"    import {module} as {as_name}\n" for module, as_name in imports)
        + f"\n    return {_returned(names)}\n\n\n"
        if imports
        else ""
    )
    # A cell takes the names other cells define as parameters; setup names are global.
    note_args = "" if marimo is MarimoImport.SETUP else MARIMO_ALIAS
    button_args = "" if alias is not None else DEFAULT_ALIAS
    export_args = ", ".join(sorted({EXPORT_BUTTON, name} if alias is None else {EXPORT_BUTTON}))
    contents = "your notebook and your written answers" if manual else "your notebook"

    return (
        importer
        + f'''@app.cell(hide_code=True)
def _({note_args}):
    {MARIMO_ALIAS}.md(r"""
    ## Submit

    Save the notebook, then click **Export submission** below. It runs the public tests on
    the saved file and makes a zip of {contents}. Upload that zip to Gradescope.

    No button below? Run the notebook first: **Run all stale cells**, or Ctrl+Shift+R
    (Cmd+Shift+R on a Mac).
    """)
    return


@app.cell(hide_code=True)
def _({button_args}):
    {EXPORT_BUTTON} = {name}.export_button()
    {EXPORT_BUTTON}
    return ({EXPORT_BUTTON},)


@app.cell(hide_code=True)
def _({export_args}):
    {call}
    return
'''
    )


def _returned(names: Sequence[str]) -> str:
    """A cell's return value for `names`, as marimo writes it: `(a,)` or `(a, b)`."""

    return f"({names[0]},)" if len(names) == 1 else f"({', '.join(names)})"


type Literal = str | Sequence[str] | Mapping[str, str]


def python_literal(value: Literal) -> str:
    """Python source for a string, or a list or dict of strings, quoted the way ruff formats it.

    Strings get double quotes, unless they hold `"` and no `'`. Characters that are not
    printable are escaped as `repr` escapes them, so any string reads back the same.
    """

    match value:
        case str():
            return _string_literal(value)
        case Mapping():
            items = (f"{_string_literal(k)}: {_string_literal(v)}" for k, v in value.items())
            return "{" + ", ".join(items) + "}"
        case _:
            return "[" + ", ".join(_string_literal(item) for item in value) + "]"


def _string_literal(text: str) -> str:
    if '"' in text and "'" not in text:
        return repr(text)

    escaped = []

    for char in text:
        match char:
            case "\\":
                escaped.append("\\\\")
            case '"':
                escaped.append('\\"')
            case _ if char.isprintable():
                escaped.append(char)
            case _:
                escaped.append(repr(char)[1:-1])

    return '"' + "".join(escaped) + '"'


def _append_cells(text: str, cells: str) -> str:
    """Insert `cells` after the last cell of `text`, or at its end when it has none."""

    lines = split_lines(text)
    nb = parse_notebook(text)
    end = nb.cells[-1].end if nb.cells else len(lines)
    head = "\n".join(lines[:end]).rstrip("\n")
    tail = "\n".join(lines[end:]).lstrip("\n")

    return f"{head}\n\n\n{cells}" + (f"\n\n{tail}" if tail else "")


def _drop_cell(edits: Edits, lines: list[str], cell: Cell) -> None:
    """Drop a cell and the blank lines after it."""

    end = cell.end
    while end < len(lines) and not lines[end].strip():
        end += 1

    for number in range(cell.start, end + 1):
        edits[number] = []


def _solution_regions(
    edits: Edits, lines: list[str], keep: bool, markdown: AbstractSet[int]
) -> set[int]:
    """Replace each solution region with its prompt lines, or a placeholder.

    Lines in `markdown` belong to Markdown cells, whose markers are HTML comments. There the
    placeholder is `MARKDOWN_PLACEHOLDER`; elsewhere it is `...`. With `keep`, drop only
    the marker and prompt lines.

    Returns:
        The numbers of the lines inside the regions, markers excluded.
    """

    start: int | None = None
    indent = ""
    prompt: list[str] = []
    prompts: list[int] = []
    inside: set[int] = set()

    for number, line in enumerate(lines, start=1):
        marker, text = line_marker(line, number in markdown)

        match marker, start:
            case Marker.BEGIN, None:
                start, indent, prompt = number, line[: len(line) - len(line.lstrip())], []
                prompts = []
            case Marker.END, int() if keep:
                inside.update(range(start + 1, number))
                if start not in edits:
                    for marker in (start, *prompts, number):
                        _replace(edits, marker, marker, [])
                start = None
            case Marker.END, int():
                inside.update(range(start + 1, number))
                if start not in edits:
                    default = MARKDOWN_PLACEHOLDER if start in markdown else "..."
                    stub = [indent + code if code else "" for code in prompt or [default]]
                    _replace(edits, start, number, stub)
                start = None
            case Marker.BEGIN | Marker.END, _:
                raise SourceError(f"line {number}: `{line.strip()}` does not pair up.")
            case Marker.PROMPT, int():
                prompt.append(text)
                prompts.append(number)

    if start is not None:
        raise SourceError(f"line {start}: `{BEGIN}` without a matching `{END}`.")

    return inside


def _inline_solutions(
    edits: Edits, lines: list[str], src: str, keep: bool, regions: set[int]
) -> None:
    """Blank out the value of `x = rhs  # SOLUTION`; drop other statements so marked.

    With `keep`, only the comments go. A comment inside a solution region (`regions`) is not
    checked, since the student copy drops the whole region; with `keep` it is removed, and so
    is its line when nothing else is on it. Both copies accept the same sources.
    """

    tree = ast.parse(src)
    comments = [
        token
        for token in tokenize.generate_tokens(io.StringIO(src).readline)
        if token.type == tokenize.COMMENT and token.string.strip() == SOLUTION
    ]

    for comment in comments:
        number = comment.start[0]
        if number in edits:
            continue

        if number in regions:
            code = lines[number - 1][: comment.start[1]].rstrip()
            _replace(edits, number, number, [code] if code else [])
            continue

        ending = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt) and node.end_lineno == number
        ]
        if not ending:
            raise SourceError(f"line {number}: `{SOLUTION}` must end a statement.")
        stmt = max(ending, key=lambda node: (node.lineno, node.col_offset))

        # The whole statement is rewritten from its targets, since brackets around the
        # value are not part of the value's AST node.
        line = lines[stmt.lineno - 1]
        before = line[: _char_col(line, stmt.col_offset)]
        last = lines[number - 1]
        uncommented = [last[: comment.start[1]].rstrip()]

        match stmt:
            case ast.Assign() if keep:
                _replace(edits, number, number, uncommented)
            case ast.Assign(targets=targets):
                names = " = ".join(_segment(src, target) for target in targets)
                _replace(edits, stmt.lineno, number, [f"{before}{names} = ..."])
            case ast.AnnAssign(value=ast.expr()) if keep:
                _replace(edits, number, number, uncommented)
            case ast.AnnAssign(target=target, annotation=annotation, value=ast.expr()):
                names = f"{_segment(src, target)}: {_segment(src, annotation)}"
                _replace(edits, stmt.lineno, number, [f"{before}{names} = ..."])
            case (
                ast.FunctionDef()
                | ast.AsyncFunctionDef()
                | ast.ClassDef()
                | ast.If()
                | ast.For()
                | ast.AsyncFor()
                | ast.While()
                | ast.With()
                | ast.AsyncWith()
                | ast.Try()
                | ast.TryStar()
                | ast.Match()
            ):
                raise SourceError(
                    f"line {number}: `{SOLUTION}` must end a simple statement, not the end of "
                    "a block."
                )
            case _:
                if before.strip():
                    raise SourceError(
                        f"line {number}: `{SOLUTION}` statement must start its own line."
                    )
                _replace(
                    edits, number if keep else stmt.lineno, number, uncommented if keep else []
                )


def _rewrite_grade(
    edits: Edits, lines: list[str], nb: Notebook, cell: Cell, cfg: AssignmentConfig
) -> None:
    """Pass points and title, and the test file, its names and hidden-test count, or `manual`."""

    call = nb.grade_call(cell)
    qid = cell.grade_qid
    question = cfg.questions.get(qid) if qid is not None else None
    if qid is None or question is None:
        raise SourceError(f"line {call.lineno}: grade() names an unknown question {qid!r}.")

    _, hidden = nb.tests_by_question().get(qid, ([], []))
    students = nb.student_names()
    uses = sorted(
        {
            name
            for test_cell in _public_test_cells(nb, qid)
            for name in cell_names(nb.cell_body_source(test_cell))[1] & students
        }
    )
    points = question.points
    shown = str(int(points)) if points.is_integer() else repr(points)
    func = ast.get_source_segment(nb.src, call.func)
    head = f"{func}({python_literal(qid)}, points={shown}, title={python_literal(question.title)}"
    new_call = (
        f"{head}, manual=True)"
        if question.manual
        else f"{head}, test_file={python_literal(str(test_file(qid)))}, "
        f"uses=[{', '.join(uses)}], hidden={len(hidden)})"
    )

    first = lines[call.lineno - 1]
    last = lines[_end_line(call) - 1]
    start = _char_col(first, call.col_offset)
    end = _char_col(last, _end_col(call))
    _replace(edits, call.lineno, _end_line(call), [first[:start] + new_call + last[end:]])


def _name_written_cells(edits: Edits, lines: list[str], nb: Notebook, qid: str) -> None:
    """Name a written question's prompt and answer cells, so the export can find them."""

    prompts, answers = nb.written_cells(qid)

    for prefix, cells in (("prompt", prompts), ("answer", answers)):
        for index, cell in enumerate(cells, start=1):
            name = f"{prefix}_{qid}" if index == 1 else f"{prefix}_{qid}_{index}"
            old = f"def {cell.name}("
            number = next((n for n in range(cell.start, cell.end + 1) if old in lines[n - 1]), None)

            if number is None:
                raise SourceError(f"line {cell.start}: cannot name the cell `{name}`.")

            _replace(edits, number, number, [lines[number - 1].replace(old, f"def {name}(", 1)])


def _segment(src: str, node: ast.expr) -> str:
    """Source text of an expression."""

    match ast.get_source_segment(src, node):
        case str() as text:
            return text
        case None:
            raise SourceError(f"line {node.lineno}: no source for expression.")


def _replace(edits: Edits, first: int, last: int, new: list[str]) -> None:
    """Replace lines `first` to `last` (inclusive) with `new`."""

    taken = [number for number in range(first, last + 1) if number in edits]
    if taken:
        raise SourceError(f"line {taken[0]}: two student-copy edits touch this line.")

    edits[first] = new
    for number in range(first + 1, last + 1):
        edits[number] = []


def _char_col(line: str, byte_col: int) -> int:
    """Character offset in `line` of an AST column, which counts UTF-8 bytes."""

    return len(line.encode()[:byte_col].decode())


def _end_line(node: ast.expr) -> int:
    match node.end_lineno:
        case int() as end:
            return end
        case None:
            raise SourceError(f"line {node.lineno}: expression has no end line.")


def _end_col(node: ast.expr) -> int:
    match node.end_col_offset:
        case int() as end:
            return end
        case None:
            raise SourceError(f"line {node.lineno}: expression has no end column.")

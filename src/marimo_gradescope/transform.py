"""Student notebook from the source notebook.

The transform edits the source as text, line by line, so everything it does not touch keeps
its exact layout and comments.
"""

import ast
import io
import tokenize
from collections.abc import Mapping, Sequence
from pathlib import Path

from marimo_gradescope import __version__
from marimo_gradescope.config import (
    AssignmentConfig,
    ensure_runtime_flag,
    normalize_newlines,
    set_dependency,
    split_lines,
    strip_tool_table,
)
from marimo_gradescope.source import (
    BEGIN,
    END,
    PACKAGE,
    PROMPT,
    SOLUTION,
    Cell,
    Marker,
    Notebook,
    SourceError,
    answer_placeholders,
    cell_names,
    parse_notebook,
)

EXPORT_BUTTON = "export_button"
DEFAULT_ALIAS = "mgs"

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
      or `...` if it has none, at the indent of `# BEGIN SOLUTION`.
    - `x = rhs  # SOLUTION` becomes `x = ...`; another statement ending in `# SOLUTION` is
      dropped.
    - `# HIDDEN` cells are dropped with the blank lines after them.
    - `check("q1")` becomes
      `check("q1", tests=[<public tests>], points=<p>, hidden=<n>, title=<title>)`.
    - Export cells go after the last cell: a "Submit" note, the export button and the export
      panel (`export_cells`).
    - The header loses the `[tool.marimo-gradescope]` table, turns off marimo's reactive
      tests, and depends on `tool_requirement`.

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
        SourceError: If a `# SOLUTION` comment does not end a statement it can edit, a check
            call has arguments besides the question id or names an unknown question, solution
            markers do not pair up, the source defines `export_button` itself, or the result
            is not valid Python.
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
        if cell.hidden:
            _drop_hidden(edits, lines, cell)

    regions = _solution_regions(edits, lines, solutions)
    _inline_solutions(edits, lines, nb.src, solutions, regions)

    for cell in nb.cells:
        if cell.check_qid is not None and not cell.hidden:
            _rewrite_check(edits, lines, nb, cell, cfg)

    text = "\n".join(
        new for number, line in enumerate(lines, start=1) for new in edits.get(number, [line])
    )
    text = set_dependency(ensure_runtime_flag(strip_tool_table(text)), tool_requirement)

    try:
        ast.parse(text)
    except SyntaxError as e:
        raise SourceError(
            f"The student notebook is not valid Python (line {e.lineno}): {e.msg}. Check the "
            f"`{SOLUTION}` and `{BEGIN}` edits around that line."
        ) from e

    manual = [question.id for question in cfg.questions.values() if question.manual]
    placeholders = answer_placeholders(parse_notebook(text), manual)

    return _append_cells(text, export_cells(cfg, package_alias(nb), placeholders))


def package_alias(nb: Notebook) -> str | None:
    """The name the setup cell imports this package as, if it does.

    Raises:
        SourceError: If it does not, and `mgs`, the name the export cells would import it
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


def export_cells(
    cfg: AssignmentConfig, alias: str | None, placeholders: Mapping[str, str] | None = None
) -> str:
    """Cells that let the student export a submission: a note, the button and the panel.

    Args:
        cfg: Assignment config.
        alias: The setup cell's name for this package; `None` adds a cell that imports it as
            `mgs`.
        placeholders: The student copy's answer text per manual question, so the export
            can warn about an answer left as it was.
    """

    stem = Path(cfg.notebook).stem
    manual = [question.id for question in cfg.questions.values() if question.manual]
    titles = {qid: cfg.questions[qid].title for qid in manual}
    name = alias or DEFAULT_ALIAS
    call = (
        f"{name}.export({EXPORT_BUTTON}, name={python_literal(stem)}, "
        f"manual={python_literal(manual)}, titles={python_literal(titles)}, "
        f"placeholders={python_literal(dict(placeholders or {}))})"
    )
    importer = (
        ""
        if alias is not None
        else f"""@app.cell(hide_code=True)
def _():
    import {PACKAGE} as {DEFAULT_ALIAS}

    return ({DEFAULT_ALIAS},)


"""
    )

    return (
        importer
        + f'''@app.cell(hide_code=True)
def _():
    import marimo as _mo

    _mo.md(r"""
    ## Submit

    Save the notebook, then click **Export submission** below. It runs the public tests on
    the saved file and makes a zip of your notebook and written answers. Upload that zip to
    Gradescope.
    """)
    return


@app.cell(hide_code=True)
def _():
    {EXPORT_BUTTON} = {name}.export_button()
    {EXPORT_BUTTON}
    return ({EXPORT_BUTTON},)


@app.cell(hide_code=True)
def _({EXPORT_BUTTON}):
    {call}
    return
'''
    )


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


def _drop_hidden(edits: Edits, lines: list[str], cell: Cell) -> None:
    """Drop a hidden cell and the blank lines after it."""

    end = cell.end
    while end < len(lines) and not lines[end].strip():
        end += 1

    for number in range(cell.start, end + 1):
        edits[number] = []


def _solution_regions(edits: Edits, lines: list[str], keep: bool) -> set[int]:
    """Replace each solution region with its prompt lines, or `...`.

    With `keep`, drop only the marker and prompt lines.

    Returns:
        The numbers of the lines inside the regions, markers excluded.
    """

    start: int | None = None
    indent = ""
    prompt: list[str] = []
    prompts: list[int] = []
    inside: set[int] = set()

    for number, line in enumerate(lines, start=1):
        stripped = line.strip()

        match stripped, start:
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
                    stub = [indent + code if code else "" for code in prompt or ["..."]]
                    _replace(edits, start, number, stub)
                start = None
            case Marker.BEGIN | Marker.END, _:
                raise SourceError(f"line {number}: `{stripped}` does not pair up.")
            case _, int() if stripped.startswith(PROMPT):
                prompt.append(stripped.removeprefix(PROMPT).removeprefix(" "))
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


def _rewrite_check(
    edits: Edits, lines: list[str], nb: Notebook, cell: Cell, cfg: AssignmentConfig
) -> None:
    """Pass the public tests, points, hidden-test count and title to a check call."""

    call = nb.check_call(cell)
    qid = cell.check_qid

    if call.keywords or len(call.args) != 1:
        raise SourceError(
            f"line {call.lineno}: in the source notebook, check() takes only the question id, "
            'e.g. mgs.check("q1").'
        )

    question = cfg.questions.get(qid) if qid is not None else None
    if qid is None or question is None:
        raise SourceError(f"line {call.lineno}: check() names an unknown question {qid!r}.")

    public, hidden = nb.tests_by_question().get(qid, ([], []))
    points = question.points
    shown = str(int(points)) if points.is_integer() else repr(points)
    func = ast.get_source_segment(nb.src, call.func)
    new_call = (
        f"{func}({python_literal(qid)}, tests=[{', '.join(public)}], points={shown}, "
        f"hidden={len(hidden)}, title={python_literal(question.title)})"
    )

    first = lines[call.lineno - 1]
    last = lines[_end_line(call) - 1]
    start = _char_col(first, call.col_offset)
    end = _char_col(last, _end_col(call))
    _replace(edits, call.lineno, _end_line(call), [first[:start] + new_call + last[end:]])


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

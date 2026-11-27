"""The tests the grader runs, cut from the source notebook and stored as JSON.

A bundle holds the setup cell and every test cell. Each grading phase gets only its own cells,
so the public phase never sees hidden test source.

The JSON of a bundle, a job and an event carries `"schema": SCHEMA`, the version of its
format. The Rust crate `mogs-schema` defines the formats; `schemas/` in the repo holds their
JSON Schemas.
"""

import copy
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from mogs.config import AssignmentConfig, Question
from mogs.source import Notebook, SourceError, cell_names, question_id

SCHEMA = 1


class Phase(StrEnum):
    """A grading pass. Each runs in its own child process."""

    PUBLIC = "public"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class TestCell:
    """One test cell of the source notebook.

    Attributes:
        qid: Question id of the cell's tests.
        hidden: Whether the cell is marked `# HIDDEN`.
        source: Cell body, run as a module.
        tests: Test function names, in file order.
        uses: Student names the cell refers to, bound from the submission.
        needs: Names the cell reads from hidden helpers. Empty for public cells.
    """

    __test__ = False

    qid: str
    hidden: bool
    source: str
    tests: tuple[str, ...]
    uses: tuple[str, ...]
    needs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """JSON-ready fields."""

        return {
            "qid": self.qid,
            "hidden": self.hidden,
            "source": self.source,
            "tests": list(self.tests),
            "uses": list(self.uses),
            "needs": list(self.needs),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        """Inverse of `to_dict`.

        Raises:
            ValueError: If a field is missing or has the wrong type.
        """

        match data:
            case {
                "qid": str() as qid,
                "hidden": bool() as hidden,
                "source": str() as source,
                "tests": list() as tests,
                "uses": list() as uses,
                "needs": list() as needs,
            }:
                return cls(qid, hidden, source, _strings(tests), _strings(uses), _strings(needs))

        raise ValueError(f"not a test cell: {data!r}")


@dataclass(frozen=True, slots=True)
class HiddenHelper:
    """A `# HIDDEN` cell without tests, such as data or helpers for hidden tests.

    The grader runs a helper only when a test cell needs one of the names it defines, and at
    most once.

    Attributes:
        source: Cell body, run as a module.
        defines: Names the cell binds at top level.
        uses: Student names the cell refers to, bound from the submission.
        needs: Names the cell reads from other hidden helpers.
    """

    source: str
    defines: tuple[str, ...]
    uses: tuple[str, ...]
    needs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-ready fields."""

        return {
            "source": self.source,
            "defines": list(self.defines),
            "uses": list(self.uses),
            "needs": list(self.needs),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        """Inverse of `to_dict`.

        Raises:
            ValueError: If a field is missing or has the wrong type.
        """

        match data:
            case {
                "source": str() as source,
                "defines": list() as defines,
                "uses": list() as uses,
                "needs": list() as needs,
            }:
                return cls(source, _strings(defines), _strings(uses), _strings(needs))

        raise ValueError(f"not a hidden helper: {data!r}")


@dataclass(frozen=True, slots=True)
class GradingBundle:
    """Everything a grading child needs besides the submission.

    Attributes:
        config: Assignment config.
        setup_code: Body of the notebook's setup cell, run before the test cells.
        cells: Test cells in file order.
        helpers: Hidden cells without tests, in file order.
    """

    config: AssignmentConfig
    setup_code: str
    cells: tuple[TestCell, ...]
    helpers: tuple[HiddenHelper, ...] = ()

    def for_phase(self, phase: Phase) -> "GradingBundle":
        """The bundle with only what `phase` runs.

        The public phase gets the public test cells. The hidden phase gets the hidden test
        cells and the hidden helpers.
        """

        hidden = phase is Phase.HIDDEN
        cells = tuple(c for c in self.cells if c.hidden == hidden)

        return copy.replace(self, cells=cells, helpers=self.helpers if hidden else ())

    def to_dict(self) -> dict[str, object]:
        """JSON-ready fields."""

        return {
            "schema": SCHEMA,
            "config": _config_to_dict(self.config),
            "setup_code": self.setup_code,
            "cells": [cell.to_dict() for cell in self.cells],
            "helpers": [helper.to_dict() for helper in self.helpers],
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        """Inverse of `to_dict`.

        Raises:
            ValueError: If the schema version is not `SCHEMA`, or a field is missing or has
                the wrong type.
        """

        check_schema(data)

        match data:
            case {
                "config": dict() as config,
                "setup_code": str() as setup,
                "cells": list() as cells,
                "helpers": list() as helpers,
            }:
                return cls(
                    _config_from_dict(config),
                    setup,
                    tuple(TestCell.from_dict(cell) for cell in cells),
                    tuple(HiddenHelper.from_dict(helper) for helper in helpers),
                )

        raise ValueError("not a grading bundle")

    def to_json(self) -> str:
        """The bundle as JSON text."""

        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Inverse of `to_json`.

        Raises:
            ValueError: If the text is not JSON or not a bundle of version `SCHEMA`.
        """

        return cls.from_dict(json.loads(text))


def bundle_from_notebook(nb: Notebook, cfg: AssignmentConfig) -> GradingBundle:
    """Cut the setup cell, the test cells and the hidden helper cells out of a source notebook.

    Scope analysis of each cell body gives its `uses`, the student names
    (`nb.student_names()`) it reads without defining them, and, for hidden cells, its `needs`,
    the names it reads from other hidden helper cells. A hidden helper is a `# HIDDEN` cell
    without tests.

    Raises:
        SourceError: If a cell holds tests of more than one question.
    """

    students = nb.student_names()
    helper_cells = nb.helper_cells()
    names = {cell: cell_names(nb.cell_body_source(cell)) for cell in helper_cells}
    helpers = []

    for cell in helper_cells:
        bound, free = names[cell]
        others = {name for other in helper_cells if other is not cell for name in names[other][0]}
        helpers.append(
            HiddenHelper(
                nb.cell_body_source(cell),
                defines=tuple(sorted(bound)),
                uses=tuple(sorted(free & students)),
                needs=tuple(sorted(free & others)),
            )
        )

    provided = {name for helper in helpers for name in helper.defines}
    cells = []

    for cell in nb.cells:
        if not cell.tests:
            continue

        qids = {question_id(name) for name in cell.tests}
        if len(qids) > 1:
            raise SourceError(
                f"line {cell.start}: put the tests of each question in their own cell; found "
                f"{', '.join(cell.tests)}."
            )

        source = nb.cell_body_source(cell)
        _, free = cell_names(source)
        uses = tuple(sorted(free & students))
        needs = tuple(sorted(free & provided)) if cell.hidden else ()
        cells.append(TestCell(qids.pop(), cell.hidden, source, cell.tests, uses, needs))

    return GradingBundle(cfg, nb.setup_source(), tuple(cells), tuple(helpers))


def check_schema(data: object) -> None:
    """Check that a decoded message is of format version `SCHEMA`.

    Raises:
        ValueError: If `data` has no `schema` key, or another version. The message names both
            versions.
    """

    match data:
        case {"schema": int() as version} if not isinstance(version, bool) and version == SCHEMA:
            return
        case {"schema": version}:
            raise ValueError(f"schema version {json.dumps(version)}, expected {SCHEMA}")

    raise ValueError(f"missing schema version, expected {SCHEMA}")


def _strings(items: list) -> tuple[str, ...]:
    if all(isinstance(item, str) for item in items):
        return tuple(items)

    raise ValueError(f"expected a list of strings, got {items!r}")


def _config_to_dict(config: AssignmentConfig) -> dict[str, object]:
    """The config fields the grader needs. `requirements` is left out: it is a host path."""

    return {
        "name": config.name,
        "notebook": config.notebook,
        "questions": [
            {"id": q.id, "title": q.title, "points": q.points, "manual": q.manual}
            for q in config.questions.values()
        ],
        "files": list(config.files),
        "timeout": config.timeout,
        "budget": config.budget,
        "memory_mb": config.memory_mb,
        "python": config.python,
    }


def _config_from_dict(data: dict) -> AssignmentConfig:
    match data:
        case {
            "name": str() as name,
            "notebook": str() as notebook,
            "questions": list() as questions,
            "files": list() as files,
            "timeout": int() | float() as timeout,
            "budget": int() | float() as budget,
            "memory_mb": int() as memory_mb,
            "python": str() as python,
        }:
            return AssignmentConfig(
                name=name,
                notebook=notebook,
                questions={q.id: q for q in map(_question_from_dict, questions)},
                files=_strings(files),
                timeout=float(timeout),
                budget=float(budget),
                memory_mb=memory_mb,
                python=python,
            )

    raise ValueError(f"not an assignment config: {data!r}")


def _question_from_dict(data: object) -> Question:
    match data:
        case {
            "id": str() as qid,
            "title": str() as title,
            "points": int() | float() as points,
            "manual": bool() as manual,
        }:
            return Question(qid, title, float(points), manual)

    raise ValueError(f"not a question: {data!r}")

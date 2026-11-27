"""Record v0.7 behavior as the golden corpus: a pytest plugin, and the codec its replay uses.

`make golden` runs the test suite with this plugin. It wraps each function in `FUNCTIONS`,
records every call it gets (the arguments, and the result or the exception), checks each case
against the unwrapped function, and writes `cases/<module>.jsonl` and `blobs/`. README.md
describes the JSON shapes.
"""

import copy
import dataclasses
import functools
import hashlib
import inspect
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path, PurePath
from types import FunctionType, ModuleType

import pytest

import mogs
from mogs import config, pdf, requirements, source, transform
from mogs.config import AssignmentConfig, Question
from mogs.grader import bundle, results
from mogs.grader.bundle import GradingBundle, HiddenHelper, TestCell
from mogs.runner import FailureReason, Outcome
from mogs.source import Cell, CellKind, Notebook

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent
CASES = GOLDEN / "cases"
BLOBS = GOLDEN / "blobs"
BLOB_BYTES = 1024
PYFMT = "pyfmt"
TMP = "<tmp>"
REPO = "<repo>"
SUBMISSION = "_mogs_submission_<digest>"
SUBMISSION_RE = re.compile(r"_mogs_submission_[0-9a-f]{16}")
VERSION = "<version>"
TAGS = frozenset({"blob", "float", "str_utf16", "tuple"})
PYTHON = (3, 13)
# A pytest temporary folder: the base temp folder and one test's folder in it.
PYTEST_TMP_RE = re.compile(
    re.escape(Path(tempfile.gettempdir()).resolve().as_posix())
    + r"/pytest-of-[^/]+/pytest-\d+/[^/\s'\"`]+"
)

FUNCTIONS: dict[str, tuple[FunctionType, ...]] = {
    "config": (
        config.read_config,
        config.read_header,
        config.set_dependency,
        config.remove_dependency,
        config.normalized_name,
        config.check_name,
        config.default_title,
        config.question_id,
    ),
    "source": (
        source.parse_notebook,
        source.validate,
        source.source_warnings,
        source.cell_names,
        source.module_tests,
        source.prompt_previews,
        source.answer_text,
        source.unanswered,
        source.answer_placeholders,
        source.is_markdown,
        source.is_md_call,
        source.line_marker,
        source.near_solution_marker,
    ),
    "transform": (
        transform.student_notebook,
        transform.public_test_files,
        transform.solution_leaks,
        transform.pinned_requirement,
        transform.python_literal,
        transform.export_cells,
    ),
    "bundle": (bundle.bundle_from_notebook,),
    "results": (
        results.build_results,
        results.failure_results,
        results.code_block,
        results.inline_code,
    ),
    "requirements": (
        requirements._check_portable,
        requirements._index_lines,
        requirements._python_warnings,
        requirements._version_warnings,
    ),
    "pdf": (
        pdf.find_formulas,
        pdf.without_title_heading,
        pdf.cell_markdown,
        pdf.manual_question,
        pdf._delimited,
        pdf._as_code,
    ),
}


# The replay calls by keyword, and the builtins take positional arguments only.


def _repr(value: object) -> str:
    return repr(value)


def _format_g(value: object) -> str:
    return format(value, "g")


# Python's own formatting, which the messages above use through `!r` and `:g`.
PYFMT_FUNCTIONS: dict[str, Callable[[object], str]] = {"repr": _repr, "format_g": _format_g}
# Functions whose every recorded call raises, on purpose. A function outside this set with
# only `error` cases fails the recording, since that usually means a broken call.
ERRORS_ONLY: frozenset[str] = frozenset()
STRINGS = ("", "plain", "it's", 'say "hi"', "both ' and \"", "line\nbreak", "nul\x00", "café")
FLOATS = (0.1, 1e-05, 1e16, 60.0, 2.5, 18.75, math.inf, -math.inf, math.nan, -0.0, 0.0)
INTS = (0, 1, 25, -3, 10**16)
NESTED = (
    True,
    False,
    None,
    [],
    (),
    {},
    ["a", 1, None],
    ("x",),
    ("a", 1.5, ["b"]),
    {"url": "./wheels", "default": True},
    {"k": [True, None], "n": {"x": 1.5, "t": ("y",)}},
)
PYFMT_SAMPLE: tuple[object, ...] = (*STRINGS, "\ud800", *FLOATS, *INTS, *NESTED)


class Unencodable(Exception):
    """A value has no JSON shape in the corpus."""


# Encoding


def encode(value: object, blobs: dict[str, str], *, tuples: bool = False) -> object:
    """The JSON shape of `value`; long strings go into `blobs` by name.

    Args:
        value: A value a recorded function takes or returns.
        blobs: Long strings by blob name, added to.
        tuples: Tag tuples as `{"tuple": [...]}` instead of writing them as lists.

    Raises:
        Unencodable: If `value`, or a part of it, has no shape.
    """

    def again(item: object) -> object:
        return encode(item, blobs, tuples=tuples)

    match value:
        case bool() | int() | None:
            return value
        # JSON has no infinity or NaN, and some readers drop the sign of -0.0.
        case float() if math.isfinite(value) and (value != 0 or math.copysign(1, value) > 0):
            return value
        case float():
            return {"float": repr(value)}
        case Enum():
            return again(value.value)
        case str():
            return _string(value, blobs)
        case PurePath():
            return _string(value.as_posix(), blobs)
        case GradingBundle():
            return again(value.to_dict())
        case Outcome():
            # Wall time differs from run to run, and no recorded function reads it; the check
            # in `Recorder.cases` would drop a case whose result changed.
            return again(copy.replace(value, seconds=0.0).to_dict())
        case AssignmentConfig():
            match again(GradingBundle(value, "", ()).to_dict()):
                case {"config": dict() as shape}:
                    return {**shape, "requirements": again(value.requirements)}
        case BaseException():
            return {"type": type(value).__name__, "message": _string(str(value), blobs)}
        case tuple() if tuples:
            return {"tuple": [again(item) for item in value]}
        case list() | tuple():
            return [again(item) for item in value]
        case set() | frozenset():
            return sorted((again(item) for item in value), key=json.dumps)
        case dict():
            return _dict(value, again)
        case _ if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return again(dataclasses.asdict(value))

    raise Unencodable(f"no JSON shape for {type(value).__name__}")


def _dict(value: dict, again: Callable[[object], object]) -> dict[str, object]:
    keys = [key.as_posix() if isinstance(key, PurePath) else key for key in value]

    if not all(isinstance(key, str) for key in keys):
        raise Unencodable(f"dict with a key that is not a string: {list(value)!r}")

    if len(keys) == 1 and keys[0] in TAGS:
        raise Unencodable(f"dict that reads as a tag: {value!r}")

    return {scrub(key): again(item) for key, item in zip(keys, value.values(), strict=True)}


def _string(text: str, blobs: dict[str, str]) -> object:
    """The text, a `{"str_utf16": ...}` tag with lone surrogates, or a blob reference."""

    text = scrub(text)

    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raw = text.encode("utf-16-le", "surrogatepass")
        return {
            "str_utf16": [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
        }

    if len(data) <= BLOB_BYTES:
        return text

    name = hashlib.sha256(data).hexdigest()[:16]
    if blobs.setdefault(name, text) != text:
        raise Unencodable(f"two blobs named {name}")

    return {"blob": name}


def scrub(text: str) -> str:
    """`text` with this machine's folders replaced.

    A path in the repo becomes relative to it, the repo itself `<repo>`, and a pytest
    temporary folder `<tmp>`. The module name of a loaded submission, which hashes its
    absolute path, becomes `_mogs_submission_<digest>`. In a pin on this package,
    `mogs==<v>` or `mogs[pdf]==<v>` with `<v>` the running `mogs.__version__`, the version
    becomes `<version>`, so the corpus outlives a version bump. The package writes its
    version nowhere else that a recorded function returns.
    """

    root = ROOT.as_posix()
    text = PYTEST_TMP_RE.sub(TMP, text).replace(f"{root}/", "").replace(root, REPO)
    text = SUBMISSION_RE.sub(SUBMISSION, text)

    return _pin_re(re.escape(mogs.__version__)).sub(rf"\g<pin>{VERSION}", text)


def _pin_re(version: str) -> re.Pattern[str]:
    """A pin on this package to `version`, a regex; read on each call so a test can patch it.

    `0.7.0` in `mogs==0.7.0.1` or `mogs==0.7.0rc1` is not the version; a trailing `.` that
    ends a sentence is not part of it.
    """

    return re.compile(rf"(?<![\w.-])(?P<pin>mogs(?:\[pdf\])?==){version}(?!\.?\w)")


def unscrub(text: str) -> str:
    """`text` with `<version>` in pins back to the running version; the rest stays."""

    return _pin_re(re.escape(VERSION)).sub(rf"\g<pin>{mogs.__version__}", text)


# Decoding


def decode(value: object, blobs: Mapping[str, str] | None = None) -> object:
    """Inverse of `encode` for the tags and blobs; objects stay their JSON shape.

    Blobs come from `blobs`, or from the files in `BLOBS` when it is `None`. Strings get the
    running version back in place of `<version>` (`unscrub`).
    """

    def again(item: object) -> object:
        return decode(item, blobs)

    match value:
        case {"blob": str() as name} if len(value) == 1 and blobs is not None:
            return unscrub(blobs[name])
        case {"blob": str() as name} if len(value) == 1:
            return unscrub((BLOBS / f"{name}.txt").read_bytes().decode("utf-8"))
        case {"float": str() as text} if len(value) == 1:
            return float(text)
        case {"str_utf16": list() as units} if len(value) == 1:
            raw = b"".join(unit.to_bytes(2, "little") for unit in units)
            return unscrub(raw.decode("utf-16-le", "surrogatepass"))
        case {"tuple": list() as items} if len(value) == 1:
            return tuple(again(item) for item in items)
        case dict():
            return {unscrub(key): again(item) for key, item in value.items()}
        case str():
            return unscrub(value)
        case list():
            return [again(item) for item in value]

    return value


def argument(name: str, value: object) -> object:
    """The argument `name` of a recorded function, from its decoded JSON shape."""

    match name, value:
        case "nb", {"cells": list() as cells, "src": str() as src}:
            return Notebook(tuple(_cell(cell) for cell in cells), src)
        case "cell", dict():
            return _cell(value)
        case "cfg", dict():
            return _config(value)
        case "path", str():
            return Path(value)
        case "bundle", dict():
            return _bundle(value)
        case "outcomes", dict():
            return {key: _outcome(item) for key, item in value.items()}
        case "error", {"type": str() as kind, "message": str() as message}:
            return type(kind, (Exception,), {})(message)
        case "marimo", str():
            return transform.MarimoImport(value)
        case "formula", dict():
            return pdf.Formula(**value)

    return value


def _cell(data: dict) -> Cell:
    return Cell(
        kind=CellKind(data["kind"]),
        name=data["name"],
        start=data["start"],
        end=data["end"],
        tests=tuple(data["tests"]),
        hidden=data["hidden"],
        grade_qid=data["grade_qid"],
        assignment=data["assignment"],
        late_hidden=data["late_hidden"],
    )


# The readers below keep each number as written: `from_dict` would make `points` a float, and
# `build_results` prints `max_score` as given.


def _bundle(data: dict) -> GradingBundle:
    return GradingBundle(
        _config({**data["config"], "requirements": None}),
        data["setup_code"],
        tuple(TestCell.from_dict(cell) for cell in data["cells"]),
        tuple(HiddenHelper.from_dict(helper) for helper in data["helpers"]),
    )


def _outcome(data: dict) -> Outcome:
    reason = data["reason"]
    kind = None if reason is None else FailureReason(reason)

    return Outcome(data["name"], data["passed"], data["message"], kind, data["seconds"])


def _config(data: dict) -> AssignmentConfig:
    requirements = data["requirements"]

    return AssignmentConfig(
        name=data["name"],
        notebook=data["notebook"],
        questions={q["id"]: Question(**q) for q in data["questions"]},
        files=tuple(data["files"]),
        timeout=data["timeout"],
        budget=data["budget"],
        memory_mb=data["memory_mb"],
        python=data["python"],
        requirements=None if requirements is None else Path(requirements),
    )


# Replay


def read_cases(module: str) -> list[dict]:
    """The cases of one module, or `[]` when it has no file."""

    path = CASES / f"{module}.jsonl"
    if not path.exists():
        return []

    # Only `\n` ends a line: strings may hold `\u2028` and others that `splitlines` splits at.
    lines = path.read_text(encoding="utf-8").removesuffix("\n").split("\n")

    return [json.loads(line) for line in lines]


def replay(module: str, case: dict, blobs: dict[str, str] | None = None) -> dict:
    """Call the function of a case with its arguments; `{"ok": ...}` or `{"error": ...}`.

    Args:
        module: The corpus file of the case.
        case: The case.
        blobs: Blobs to read the arguments from and add the result's to. `None` reads the
            files in `BLOBS`.
    """

    store = {} if blobs is None else blobs

    match module:
        case "pyfmt":
            fn = PYFMT_FUNCTIONS[case["function"]]
            args = {"value": decode(case["args"]["value"], blobs)}
            return outcome(fn, args, store, tuples=True)
        case _:
            fn = {fn.__name__: fn for fn in FUNCTIONS[module]}[case["function"]]
            args = {
                name: argument(name, decode(value, blobs)) for name, value in case["args"].items()
            }
            return outcome(fn, args, store)


def outcome(
    fn: Callable[..., object],
    args: dict[str, object],
    blobs: dict[str, str],
    *,
    tuples: bool = False,
) -> dict:
    """`{"ok": <result>}`, or `{"error": {"type": ..., "message": ...}}` if `fn` raises."""

    try:
        result = fn(**args)
    except Exception as exc:
        return {"error": encode(exc, blobs)}

    return {"ok": encode(result, blobs, tuples=tuples)}


# Recording


class Recorder:
    """The pytest plugin: wraps the functions, collects the calls, writes the corpus."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self.blobs: dict[str, str] = {}
        self.skipped: Counter[str] = Counter()
        self.patched: list[tuple[ModuleType, str, object]] = []
        self.summary: list[str] = []

    def patch(self) -> None:
        """Replace each function by a recording wrapper in every `mogs` module that binds it."""

        for module, functions in FUNCTIONS.items():
            for fn in functions:
                wrapper = self._wrapper(module, fn)

                for loaded in [
                    m for name, m in sys.modules.items() if name.split(".")[0] == "mogs"
                ]:
                    for name, value in list(vars(loaded).items()):
                        if value is fn:
                            self.patched.append((loaded, name, fn))
                            setattr(loaded, name, wrapper)

    def unpatch(self) -> None:
        """Put the original functions back."""

        for module, name, fn in self.patched:
            setattr(module, name, fn)

    def _wrapper(self, module: str, fn: FunctionType) -> Callable[..., object]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> object:
            args_json = self._arguments(fn, signature, args, kwargs)

            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                self._add(module, fn, args_json, exc, raised=True)
                raise

            self._add(module, fn, args_json, result, raised=False)
            return result

        return wrapper

    def _arguments(
        self, fn: FunctionType, signature: inspect.Signature, args: tuple, kwargs: dict
    ) -> str | None:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            self.skipped[f"{fn.__name__}: arguments do not bind"] += 1
            return None

        bound.apply_defaults()

        try:
            return json.dumps(encode(dict(bound.arguments), self.blobs), ensure_ascii=False)
        except Unencodable as e:
            self.skipped[f"{fn.__name__}: {e}"] += 1
            return None

    def _add(
        self,
        module: str,
        fn: FunctionType,
        args_json: str | None,
        value: object,
        *,
        raised: bool,
    ) -> None:
        if args_json is None:
            return

        try:
            result = {"error" if raised else "ok": encode(value, self.blobs)}
            outcome_json = json.dumps(result, ensure_ascii=False)
            self.seen[module, fn.__name__, args_json].add(outcome_json)
        except Unencodable as e:
            self.skipped[f"{fn.__name__}: {e}"] += 1

    def cases(self) -> tuple[dict[str, list[dict]], list[str]]:
        """The verified cases by module, and the cases dropped and why.

        Each case is called again on the original function. A case is kept if that call gives
        one of the results seen during the suite; else it depended on something the arguments
        do not hold, such as a test's monkeypatch, and is dropped.
        """

        by_module: dict[str, list[dict]] = defaultdict(list)
        dropped = []

        for (module, name, args_json), results_json in sorted(self.seen.items()):
            case = {"function": name, "args": json.loads(args_json)}
            actual = replay(module, case, self.blobs)

            if json.dumps(actual, ensure_ascii=False) not in results_json:
                dropped.append(f"{module}.{name}({args_json[:200]}): no result replays")
                continue

            by_module[module].append({**case, **actual})

        for fn_name, fn in PYFMT_FUNCTIONS.items():
            for value in PYFMT_SAMPLE:
                if fn is _format_g and (
                    isinstance(value, bool) or not isinstance(value, int | float)
                ):
                    continue

                args = {"value": encode(value, self.blobs, tuples=True)}
                result = outcome(fn, {"value": value}, self.blobs, tuples=True)
                by_module[PYFMT].append({"function": fn_name, "args": args, **result})

        return by_module, dropped

    @pytest.hookimpl
    def pytest_ignore_collect(self, collection_path: Path) -> bool | None:
        """Leave out the replay, so the corpus is not recorded from itself."""

        return True if collection_path.name == "test_golden.py" else None

    @pytest.hookimpl
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Check and write the corpus, unless the suite failed."""

        self.unpatch()

        if exitstatus != 0:
            self.summary.append("golden: the suite failed, so the corpus was not written")
            return

        by_module, dropped = self.cases()
        missing = [
            f"{module}.{fn.__name__}"
            for module, functions in FUNCTIONS.items()
            for fn in functions
            if fn.__name__ not in {case["function"] for case in by_module[module]}
        ]
        lines = {
            module: sorted(json.dumps(case, ensure_ascii=False) for case in cases)
            for module, cases in by_module.items()
        }
        blobs = {name: self.blobs[name] for name in _blob_names(by_module)}
        leaks = [line for module in lines for line in lines[module] if _leaks(line)]
        leaks += [f"blob {name}: {text}" for name, text in sorted(blobs.items()) if _leaks(text)]
        errors_only = sorted(
            f"{module}.{name}"
            for module, cases in by_module.items()
            for name in {case["function"] for case in cases}
            if name not in ERRORS_ONLY
            and all("error" in case for case in cases if case["function"] == name)
        )
        self.summary += [f"golden: dropped {line}" for line in dropped]
        self.summary += [
            f"golden: skipped {n} call(s) of {why}" for why, n in sorted(self.skipped.items())
        ]

        if missing or leaks or errors_only:
            self.summary += [f"golden: no case for {name}" for name in missing]
            self.summary += [f"golden: only errors for {name}" for name in errors_only]
            self.summary += [f"golden: machine-specific text in {line[:200]}" for line in leaks]
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            return

        _write(lines, blobs)
        self.summary += [
            f"golden: {module}.{name}: {n} case(s)"
            for module in sorted(by_module)
            for name, n in sorted(Counter(case["function"] for case in by_module[module]).items())
        ]
        self.summary.append(f"golden: {sum(map(len, lines.values()))} cases, {len(blobs)} blobs")

    @pytest.hookimpl
    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        """Print what was recorded, or why nothing was."""

        for line in self.summary:
            terminalreporter.write_line(line)


def _leaks(text: str) -> bool:
    """Whether a corpus line or blob holds an absolute path of this machine."""

    marks = (ROOT.as_posix(), Path.home().as_posix(), tempfile.gettempdir())
    return any(mark in text for mark in marks)


def _blob_names(value: object) -> set[str]:
    """The blobs that `value`, in its JSON shape, refers to."""

    match value:
        case {"blob": str() as name} if len(value) == 1:
            return {name}
        case dict():
            return set().union(*map(_blob_names, value.values()))
        case list():
            return set().union(*map(_blob_names, value))

    return set()


def _write(lines: dict[str, list[str]], blobs: dict[str, str]) -> None:
    """Replace the corpus files with `lines` and `blobs`."""

    for old in [*CASES.glob("*.jsonl"), *BLOBS.glob("*.txt")]:
        old.unlink()

    CASES.mkdir(exist_ok=True)
    BLOBS.mkdir(exist_ok=True)

    for module, module_lines in lines.items():
        (CASES / f"{module}.jsonl").write_text(
            "".join(f"{line}\n" for line in module_lines), encoding="utf-8"
        )

    for name, text in blobs.items():
        (BLOBS / f"{name}.txt").write_bytes(text.encode("utf-8"))


def pytest_configure(config: pytest.Config) -> None:
    """Start recording: check the setup, then patch the functions."""

    if os.environ.get("MOGS_RECORD") != "1":
        raise pytest.UsageError("tests.golden.record rewrites the corpus; set MOGS_RECORD=1")

    if sys.version_info[:2] != PYTHON:
        raise pytest.UsageError(f"record the corpus with Python {PYTHON[0]}.{PYTHON[1]}")

    recorder = Recorder()
    recorder.patch()
    config.pluginmanager.register(recorder, "golden-recorder")

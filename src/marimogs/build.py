"""Build the student notebook and the Gradescope autograder zip, then self-check them.

The build writes, next to the source notebook, the layout Otter uses:

- `dist/student/<name>.zip`, for students: one `<name>/` folder with the student notebook, its
  public tests in `tests/` and the data files;
- `dist/autograder/<name>-autograder-<timestamp>.zip` with `setup.sh`, `run_autograder`,
  `bundle.json`, `requirements.txt` and the data files, for Gradescope;
- `dist/autograder/<notebook>.py`, the solved copy: the student notebook with the solutions
  kept, to upload to Gradescope as a test submission. The public tests, the data files and
  `requirements.txt` sit next to it.

Before it writes anything, it grades the solved copy and the student copy with the real
grader, in a fresh environment with Gradescope's requirements. The solved copy must get full
marks. The student copy should get none; tests that pass
on it come back as warnings.
"""

import ast
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from marimogs import __version__
from marimogs._env import clean_env
from marimogs.config import (
    ASSIGNMENT,
    TOOL,
    Api,
    AssignmentConfig,
    ConfigError,
    api_calls,
    read_config,
)
from marimogs.environment import grading_python
from marimogs.grader.bundle import GradingBundle, bundle_from_notebook
from marimogs.grader.datafiles import data_files as shared_data_files
from marimogs.grader.results import Results, build_results
from marimogs.grader.templates import run_autograder_script, setup_script
from marimogs.requirements import gradescope_requirements
from marimogs.runner import FailureReason, Outcome
from marimogs.source import Notebook, parse_notebook, question_id, validate
from marimogs.transform import (
    TESTS_FOLDER,
    pinned_requirement,
    public_test_files,
    student_notebook,
)

DIST = "dist"
# A header table is the old config, which `read_config` names in its error.
TOOL_TABLE_RE = re.compile(rf"^#\s*\[tool\.{re.escape(TOOL)}[\].]", re.MULTILINE)
# What an `assignment(...)` call looks like in a file that is not valid Python.
ASSIGNMENT_RE = re.compile(r"\bassignment\s*\(")
SCRIPTS = ("setup.sh", "run_autograder")
ZIP_FILES = (*SCRIPTS, "bundle.json", "requirements.txt")
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
# Otter's timestamp in the zip name, e.g. 2026_09_30T12_30_41_890583.
STAMP = "%Y_%m_%dT%H_%M_%S_%f"


class BuildError(Exception):
    """The assignment cannot be built: no single notebook, invalid source, or a failed check."""


@dataclass(frozen=True, slots=True)
class QuestionScore:
    """Self-check scores of one question.

    Attributes:
        id: Question id.
        title: Question title.
        points: Maximum score.
        solution: Score of the solved copy; `None` for a question graded by hand.
        blank: Score of the student copy; `None` for a question graded by hand.
        public: Number of public tests.
        hidden: Number of hidden tests.
    """

    id: str
    title: str
    points: float
    solution: float | None
    blank: float | None
    public: int = 0
    hidden: int = 0


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What a build wrote and how the self-check went.

    Attributes:
        student: The zip for students: the student notebook, its public tests and the data.
        zip: The Gradescope autograder zip.
        solved: The solved copy of the student notebook.
        solution: Score and maximum of the solved copy.
        blank: Score and maximum of the student copy.
        warnings: Things the instructor should look at.
        scores: Per-question scores, in notebook order.
    """

    student: Path
    zip: Path
    solved: Path
    solution: tuple[float, float]
    blank: tuple[float, float]
    warnings: list[str]
    scores: tuple[QuestionScore, ...] = ()


@dataclass(frozen=True, slots=True)
class Assignment:
    """A source notebook, read and validated.

    Attributes:
        path: The source notebook.
        src: Its text.
        cfg: Its config.
        nb: Its parsed cells.
    """

    path: Path
    src: str
    cfg: AssignmentConfig
    nb: Notebook


def require_posix() -> None:
    """Refuse to grade on a platform the sandbox does not support.

    Raises:
        BuildError: If the platform is not Linux or macOS.
    """

    if sys.platform not in ("linux", "darwin"):
        raise BuildError("Grading needs Linux or macOS (WSL on Windows).")


def read_assignment(assignment_dir: Path) -> Assignment:
    """Find, read and validate the source notebook in `assignment_dir`.

    Raises:
        BuildError: If there is no single source notebook, or it is invalid.
        ConfigError: If its config is invalid.
        SourceError: If it cannot be parsed.
    """

    path = find_notebook(assignment_dir)
    src = path.read_text(encoding="utf-8")
    cfg = read_config(src, path)
    nb = parse_notebook(src)

    errors = validate(nb, cfg)
    if errors:
        listed = "\n".join("- " + error.replace("\n", "\n  ") for error in errors)
        raise BuildError(f"{path} has {len(errors)} problem(s):\n{listed}")

    return Assignment(path, src, cfg, nb)


def find_notebook(assignment_dir: Path) -> Path:
    """The one `*.py` file in `assignment_dir` that calls `assignment(...)`.

    A file whose header still has a `[tool.marimo-gradescope]` table counts too, so that
    reading it explains the change.

    Raises:
        BuildError: If `assignment_dir` is not a folder, or it holds no such file or several.
    """

    if not assignment_dir.is_dir():
        raise BuildError(f"{assignment_dir} is not a folder.")

    candidates = [
        path
        for path in sorted(assignment_dir.glob("*.py"))
        if path.is_file() and _is_notebook(path.read_text(encoding="utf-8", errors="replace"))
    ]

    match candidates:
        case [path]:
            return path
        case []:
            raise BuildError(
                f"No notebook in {assignment_dir} calls `mogs.assignment(...)`. Add a cell with "
                "that call, or start a new assignment with `marimogs init`."
            )
        case _:
            names = "\n".join(f"  {path.name}" for path in candidates)
            raise BuildError(
                f"Several notebooks in {assignment_dir} call `mogs.assignment(...)`; keep "
                f"exactly one:\n{names}"
            )


def _is_notebook(text: str) -> bool:
    """A source notebook: it calls `assignment(...)`, under any name its imports give it.

    A file that is not valid Python counts when it looks like it calls `assignment`, so that
    reading it reports the syntax error. So does a file with the old header table.
    """

    if TOOL_TABLE_RE.search(text):
        return True

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return bool(ASSIGNMENT_RE.search(text))

    return bool(api_calls(tree, tree, Api.ASSIGNMENT))


def build(assignment_dir: Path, tool_requirement: str | None = None) -> BuildResult:
    """Build and self-check the assignment in `assignment_dir`.

    The output is written in full to a temporary folder first. Only when every step succeeds
    does it replace `dist/student/` and `dist/autograder/`, so only the newest zip is kept.

    Args:
        assignment_dir: Folder with the source notebook and its data files.
        tool_requirement: marimo-gradescope requirement for Gradescope's `requirements.txt`.
            Defaults to this exact version. Student notebooks always pin this version.

    Returns:
        The artifact paths, self-check scores and warnings.

    Raises:
        BuildError: If the platform is not Linux or macOS, the notebook is missing or invalid,
            `marimo check` fails on the student or solved copy, or the solved copy does not get
            full marks.
        ConfigError: If the config, a data file or a header edit is invalid.
        SourceError: If the notebook cannot be parsed or transformed.
        RequirementsError: If the dependencies cannot be pinned or installed.
    """

    require_posix()
    assignment = read_assignment(assignment_dir)
    src, cfg, nb = assignment.src, assignment.cfg, assignment.nb

    data = data_files(assignment_dir, cfg)
    student = student_notebook(src, nb, cfg, pinned_requirement(cfg))
    solved = student_notebook(src, nb, cfg, pinned_requirement(cfg), solutions=True)
    bundle = bundle_from_notebook(nb, cfg)
    tests = public_test_files(nb)
    requirement = tool_requirement or f"{TOOL}=={__version__}"
    requirements, warnings = gradescope_requirements(src, cfg, requirement)
    entries = {
        "setup.sh": setup_script(cfg).encode(),
        "run_autograder": run_autograder_script().encode(),
        "bundle.json": bundle.to_json().encode(),
        "requirements.txt": requirements.encode(),
    }
    entries |= {str(rel): source.read_bytes() for rel, source in data.items()}

    dist = assignment_dir / DIST
    zip_name = f"{cfg.name}-autograder-{datetime.now().strftime(STAMP)}.zip"

    with tempfile.TemporaryDirectory(prefix="mgs-build-") as folder:
        stage = Path(folder)

        # Self-check, in an environment with what Gradescope installs.
        python = str(grading_python(requirements, stage / "env", cfg.python))
        check = stage / "check"
        check.mkdir()
        student_path = check / "student" / cfg.notebook
        solved_path = check / "solved" / cfg.notebook
        for notebook, text in ((student_path, student), (solved_path, solved)):
            notebook.parent.mkdir()
            notebook.write_text(text, encoding="utf-8")

        warnings += marimo_check(student_path, "student notebook", python)
        warnings += marimo_check(solved_path, "solved notebook", python)

        # The solved copy, not the source: it is what the instructor's test upload sends.
        solution = _self_grade(bundle, solved_path, check / "solution", assignment_dir, python)
        failed = [outcome for outcome in solution.outcomes.values() if not outcome.passed]
        if failed:
            raise BuildError(
                f"The solved copy of {cfg.notebook} does not get full marks. Failing tests:\n"
                + "\n".join(failure_report(failed))
            )

        blank = _self_grade(bundle, student_path, check / "blank", assignment_dir, python)
        warnings += _blank_warnings(blank)

        # Output, written in full before it replaces the old one.
        out = stage / DIST
        (out / "student").mkdir(parents=True)
        files = {str(rel): source.read_bytes() for rel, source in data.items()}
        files |= {str(rel): text.encode() for rel, text in tests.items()}
        student_files = {cfg.notebook: student.encode(), **files}
        write_zip(
            out / "student" / f"{cfg.name}.zip",
            {f"{cfg.name}/{rel}": content for rel, content in student_files.items()},
        )

        autograder = out / "autograder"
        for rel, content in {cfg.notebook: solved.encode(), **files}.items():
            (autograder / rel).parent.mkdir(parents=True, exist_ok=True)
            (autograder / rel).write_bytes(content)

        (autograder / "requirements.txt").write_text(requirements, encoding="utf-8")
        write_zip(autograder / zip_name, entries)

        for part in ("student", "autograder"):
            if (dist / part).exists():
                shutil.rmtree(dist / part)
            dist.mkdir(exist_ok=True)
            shutil.move(out / part, dist / part)

    return BuildResult(
        student=dist / "student" / f"{cfg.name}.zip",
        zip=dist / "autograder" / zip_name,
        solved=dist / "autograder" / cfg.notebook,
        solution=total(solution.results),
        blank=total(blank.results),
        warnings=warnings,
        scores=_scores(cfg, nb, solution.results, blank.results),
    )


def data_files(assignment_dir: Path, cfg: AssignmentConfig) -> dict[PurePosixPath, Path]:
    """The data files to ship, by path relative to the notebook, with folders expanded.

    Uses the sandbox's own checks and junk filter (`grader.datafiles`).

    Raises:
        ConfigError: If an entry of `files` is missing, is not inside the notebook's folder, or
            would replace the notebook, its public tests folder, a file of the autograder zip
            or the grader's copy of the submission.
    """

    # Case-insensitive, as the file systems of macOS and Windows are.
    reserved = {name.casefold() for name in (*ZIP_FILES, cfg.notebook, TESTS_FOLDER)}

    for name in cfg.files:
        first = PurePosixPath(name).parts[:1]
        if first and first[0].casefold() in reserved:
            raise ConfigError(
                f"{ASSIGNMENT} `files` entry {name!r} would replace `{first[0]}` in the build "
                "output."
            )

    try:
        return shared_data_files(assignment_dir, cfg.files)
    except ValueError as error:
        raise ConfigError(f"{ASSIGNMENT} `files`: {error}.") from error


def write_zip(path: Path, entries: Mapping[str, bytes]) -> None:
    """Write a reproducible zip: sorted entries, fixed timestamps, executable scripts."""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so tools read the mode bits below.
            mode = 0o755 if name in SCRIPTS else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, entries[name])


# Self-check


@dataclass(frozen=True, slots=True)
class _Graded:
    outcomes: dict[str, Outcome]
    results: Results


def _self_grade(
    bundle: GradingBundle, notebook: Path, workdir: Path, assignment_dir: Path, python: str
) -> _Graded:
    """Grade `notebook` on `python` the way Gradescope would, as the current user."""

    outcomes = grade_locally(bundle, notebook, workdir, assignment_dir, python)
    return _Graded(outcomes, build_results(bundle, outcomes, []))


def grade_locally(
    bundle: GradingBundle, notebook: Path, workdir: Path, assignment_dir: Path, python: str
) -> dict[str, Outcome]:
    """Grade `notebook` on `python` with the real grader, as the current user.

    Args:
        bundle: The grading bundle.
        notebook: The notebook to grade.
        workdir: Folder for the grader's phase folders; must not exist.
        assignment_dir: Folder the data files are relative to.
        python: Interpreter with the grading requirements.

    Returns:
        One outcome per test of the bundle.
    """

    # Imported here: the sandbox needs POSIX, and the CLI must start on any platform.
    from marimogs.grader.sandbox import grade

    workdir.mkdir()

    return grade(
        bundle,
        notebook,
        workdir,
        user=None,
        files_root=assignment_dir,
        files=bundle.config.files,
        python=python,
    )


def failure_report(failed: Iterable[Outcome], *, count_load_failures: bool = True) -> list[str]:
    """One line per distinct failure message, naming the tests that share it.

    Args:
        failed: The failed outcomes.
        count_load_failures: Count the tests that failed to load and name their questions
            instead of the tests, since a failing setup cell fails every test.
    """

    by_message: dict[tuple[str, bool], list[str]] = {}
    for outcome in failed:
        counted = count_load_failures and outcome.reason is FailureReason.LOAD_FAILED
        by_message.setdefault((outcome.message.strip(), counted), []).append(outcome.name)

    return [
        f"- {_failed_tests(names, load_failed)}: " + message.replace("\n", "\n    ")
        for (message, load_failed), names in by_message.items()
    ]


def _failed_tests(names: list[str], load_failed: bool) -> str:
    if not load_failed:
        return ", ".join(names)

    questions = ", ".join(dict.fromkeys(question_id(name) for name in names))
    tests = "1 test" if len(names) == 1 else f"{len(names)} tests"
    return f"{tests} of {questions}"


def _blank_warnings(blank: _Graded) -> list[str]:
    passing = [outcome.name for outcome in blank.outcomes.values() if outcome.passed]
    if not passing:
        return []

    return [
        "These tests pass on the blank student notebook, so they give points for no work: "
        + ", ".join(passing)
    ]


def marimo_check(notebook: Path, label: str, python: str) -> list[str]:
    """Run `marimo check` with `python` on `notebook`, called `label` in messages.

    Returns:
        Its output as a warning, if it printed anything.

    Raises:
        BuildError: If it reports errors.
    """

    result = subprocess.run(
        [python, "-m", "marimo", "check", notebook.name],
        capture_output=True,
        text=True,
        check=False,
        cwd=notebook.parent,
        env=clean_env(),
    )
    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0:
        raise BuildError(f"`marimo check` fails on the {label}:\n{output}")

    return [f"`marimo check` on the {label}:\n{output}"] if output else []


def total(results: Results) -> tuple[float, float]:
    """Score and maximum, summed over the rows of `results`."""

    rows = results["tests"]
    return sum(row["score"] for row in rows), sum(row["max_score"] for row in rows)


def _scores(
    cfg: AssignmentConfig, nb: Notebook, solution: Results, blank: Results
) -> tuple[QuestionScore, ...]:
    ours = {row.get("number"): row["score"] for row in solution["tests"]}
    theirs = {row.get("number"): row["score"] for row in blank["tests"]}
    tests = nb.tests_by_question()
    scores = []

    for q in cfg.questions.values():
        public, hidden = tests.get(q.id, ([], []))
        scores.append(
            QuestionScore(
                q.id, q.title, q.points, ours.get(q.id), theirs.get(q.id), len(public), len(hidden)
            )
        )

    return tuple(scores)

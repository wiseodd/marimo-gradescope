"""Grade one submission locally, as Gradescope would, with every test's outcome.

For regrade requests and for finding out why a submission lost points. Unlike Gradescope's
results, the report names failing hidden tests and shows their messages.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from marimogs.build import (
    BuildError,
    data_files,
    grade_locally,
    read_assignment,
    require_posix,
    total,
)
from marimogs.config import TOOL
from marimogs.environment import grading_python
from marimogs.grader.bundle import GradingBundle, bundle_from_notebook
from marimogs.grader.results import Results, build_results, find_submission
from marimogs.requirements import gradescope_requirements
from marimogs.runner import Outcome


@dataclass(frozen=True, slots=True)
class GradeResult:
    """How one submission did.

    Attributes:
        submission: The notebook that was graded.
        bundle: The grading bundle it was graded with.
        outcomes: One outcome per test, public tests first.
        results: The rows Gradescope would show.
        score: Score and maximum over the tested questions.
    """

    submission: Path
    bundle: GradingBundle
    outcomes: dict[str, Outcome]
    results: Results
    score: tuple[float, float]


def grade_submission(assignment_dir: Path, submission: Path) -> GradeResult:
    """Grade `submission` against the source notebook in `assignment_dir`.

    The grader runs in a fresh environment with the pinned requirements, as in `build`.

    Args:
        assignment_dir: Folder with the source notebook and its data files.
        submission: A student notebook, or a folder with one, such as a downloaded upload.

    Returns:
        The outcomes and scores.

    Raises:
        BuildError: If the platform is not Linux or macOS, the source notebook is missing or
            invalid, or the submission is missing or holds no single notebook.
        ConfigError: If the config is invalid or a data file is missing.
        SourceError: If the source notebook cannot be parsed.
        RequirementsError: If the dependencies cannot be pinned or installed.
    """

    require_posix()
    assignment = read_assignment(assignment_dir)
    notebook = _find_notebook(submission, assignment.cfg.notebook)
    data_files(assignment_dir, assignment.cfg)

    bundle = bundle_from_notebook(assignment.nb, assignment.cfg)
    requirements, _ = gradescope_requirements(assignment.src, assignment.cfg, TOOL)

    with tempfile.TemporaryDirectory(prefix="mgs-grade-") as folder:
        stage = Path(folder)
        python = str(grading_python(requirements, stage / "env", assignment.cfg.python))
        outcomes = grade_locally(bundle, notebook, stage / "grade", assignment_dir, python)

    results = build_results(bundle, outcomes, [])

    return GradeResult(notebook, bundle, outcomes, results, total(results))


def _find_notebook(submission: Path, name: str) -> Path:
    """The notebook to grade: `submission` itself, or the one found in the folder.

    A symlink to a file is followed, since the grader takes only regular files.
    """

    if submission.is_file():
        return submission.resolve()

    if not submission.is_dir():
        raise BuildError(f"{submission} is not a file or folder.")

    found = find_submission(submission, name)

    if found.py is None:
        raise BuildError(f"{submission}: {found.error}")

    return found.py

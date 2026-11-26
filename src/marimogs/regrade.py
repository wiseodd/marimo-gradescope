"""Grade one submission locally, as Gradescope would, with every test's outcome.

For regrade requests and for finding out why a submission lost points. Unlike Gradescope's
results, the report names failing hidden tests and shows their messages.
"""

import tempfile
import zipfile
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
from marimogs.runner import FailureReason, Outcome

MAX_ZIP_MEMBERS = 5_000
MAX_ZIP_BYTES = 500 * 1024 * 1024
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GradeResult:
    """How one submission did.

    Attributes:
        submission: The notebook that was graded. For a zip, the path of the notebook in the
            zip, joined onto the zip's path.
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

    @property
    def failed_to_load(self) -> bool:
        """Whether every test failed because the notebook did not load."""

        reasons = {outcome.reason for outcome in self.outcomes.values()}

        return reasons == {FailureReason.LOAD_FAILED}


def grade_submission(assignment_dir: Path, submission: Path) -> GradeResult:
    """Grade `submission` against the source notebook in `assignment_dir`.

    The grader runs in a fresh environment with the pinned requirements, as in `build`.

    Args:
        assignment_dir: Folder with the source notebook and its data files.
        submission: A student notebook, a folder with one (such as a downloaded upload), or
            a zip with one (such as the one the export button makes). A zip is unpacked into
            a temporary folder and searched like a folder, as Gradescope does with uploads.

    Returns:
        The outcomes and scores.

    Raises:
        BuildError: If the platform is not Linux or macOS, the source notebook is missing or
            invalid, or the submission is missing, of another type, an unreadable or unsafe
            zip, or holds no single notebook.
        ConfigError: If the config is invalid or a data file is missing.
        SourceError: If the source notebook cannot be parsed.
        RequirementsError: If the dependencies cannot be pinned or installed.
    """

    require_posix()
    assignment = read_assignment(assignment_dir)

    with tempfile.TemporaryDirectory(prefix="mgs-grade-") as folder:
        stage = Path(folder)
        notebook, shown = _find_notebook(submission, assignment.cfg.notebook, stage / "upload")
        data_files(assignment_dir, assignment.cfg)

        bundle = bundle_from_notebook(assignment.nb, assignment.cfg)
        requirements, _ = gradescope_requirements(assignment.src, assignment.cfg, TOOL)

        python = str(grading_python(requirements, stage / "env", assignment.cfg.python))
        outcomes = grade_locally(bundle, notebook, stage / "grade", assignment_dir, python)

    results = build_results(bundle, outcomes, [])

    return GradeResult(shown, bundle, outcomes, results, total(results))


def _find_notebook(submission: Path, name: str, upload: Path) -> tuple[Path, Path]:
    """The notebook to grade, and the path to show for it.

    A `.py` file is graded as it is. A folder is searched with `find_submission`. A `.zip`
    is unpacked into `upload` first, then searched the same way. A symlink to a file is
    followed, since the grader takes only regular files.

    Args:
        submission: The path given on the command line.
        name: File name of the student notebook.
        upload: Folder to unpack a zip into; must not exist.

    Returns:
        The notebook to grade, and the path to report: the notebook itself, or for a zip,
        its path in the zip joined onto the zip's path.
    """

    if submission.is_dir():
        notebook = _search(submission, submission, name)
        return notebook, notebook

    if not submission.is_file():
        raise BuildError(f"{submission} is not a file or folder.")

    match submission.suffix.lower():
        case ".py":
            notebook = submission.resolve()
            return notebook, notebook
        case ".zip":
            _unpack(submission, upload)
            notebook = _search(upload, submission, name)
            return notebook, submission / notebook.relative_to(upload)
        case _:
            raise BuildError(
                f"{submission} is not a notebook. Pass a `.py` notebook, a `.zip` of the "
                "submission, or a folder with one."
            )


def _search(folder: Path, submission: Path, name: str) -> Path:
    """The one notebook in `folder`; `submission` names the input in the error."""

    found = find_submission(folder, name)

    if found.py is None:
        raise BuildError(f"{submission}: {found.error}")

    return found.py


def _unpack(archive: Path, target: Path) -> None:
    """Unpack the zip `archive` into the new folder `target`.

    The zip may hold at most `MAX_ZIP_MEMBERS` members and unpack to at most `MAX_ZIP_BYTES`
    bytes. The sizes in the zip's headers are checked first, and the bytes are counted again
    while unpacking, since the headers can lie.

    Raises:
        BuildError: If `archive` is not a readable zip, holds too many members, unpacks to
            too many bytes, or a member's path is absolute or leads outside `target`.
    """

    target.mkdir()
    root = target.resolve()

    try:
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()

            if len(members) > MAX_ZIP_MEMBERS:
                raise BuildError(
                    f"{archive} holds {len(members)} files; the limit is {MAX_ZIP_MEMBERS}."
                )

            if sum(member.file_size for member in members) > MAX_ZIP_BYTES:
                raise _too_large(archive)

            for member in members:
                path = (root / member.filename).resolve()

                if Path(member.filename).is_absolute() or not path.is_relative_to(root):
                    raise BuildError(
                        f"{archive}: {member.filename} would unpack outside the folder."
                    )

            written = 0

            for member in members:
                written = _extract(zipped, member, root, archive, written)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise BuildError(f"{archive} is not a readable zip ({error}).") from error


def _extract(
    zipped: zipfile.ZipFile, member: zipfile.ZipInfo, root: Path, archive: Path, written: int
) -> int:
    """Unpack `member` under `root`, given `written` bytes so far; returns the new total.

    Raises:
        BuildError: If the total passes `MAX_ZIP_BYTES`.
    """

    path = root / member.filename

    if member.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        return written

    path.parent.mkdir(parents=True, exist_ok=True)

    with zipped.open(member) as source, path.open("wb") as sink:
        while chunk := source.read(_CHUNK):
            written += len(chunk)

            if written > MAX_ZIP_BYTES:
                raise _too_large(archive)

            sink.write(chunk)

    return written


def _too_large(archive: Path) -> BuildError:
    return BuildError(f"{archive} unpacks to more than {MAX_ZIP_BYTES} bytes.")

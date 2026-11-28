"""End to end: the demo autograder, graded inside Gradescope's base image.

The `docker` tests build the demo assignment against a wheel of this checkout, build an image
the way Gradescope does (zip unpacked to `/autograder/source`, `run_autograder` copied up,
`setup.sh` run as root), then grade each submission in a fresh container through the real
`/autograder/run_autograder`.

They need docker or podman (`MOGS_CONTAINER=docker|podman` picks one) and network. Without a
runtime they skip, unless `MOGS_REQUIRE_CONTAINER=1` (set in CI) turns the skip into a failure.
The other tests here need no container: they check the submissions and the build context, and
grade on the host the submissions that behave the same there.

The submissions are variants of the demo's solution, made by small text substitutions, so
they follow the demo when it changes.
"""

import ast
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from mogs._native import main
from mogs.config import normalized_name, read_config
from mogs.grader.bundle import bundle_from_notebook
from mogs.source import BEGIN, parse_notebook
from mogs.transform import pinned_requirement, student_notebook

REPO = Path(__file__).parents[2]
# The `mogs` command of this environment.
MOGS = Path(sys.executable).with_name("mogs")
DEMO = REPO / "examples" / "demo"
DOCKERFILE = Path(__file__).with_name("Dockerfile")
NOTEBOOK = "hw.py"

RUNTIMES = ("docker", "podman")
# The base image exists for amd64 only.
PLATFORM = "linux/amd64"
WHEELS = PurePosixPath("/wheels")
SCRIPTS = ("setup.sh", "run_autograder")
TAIL = 4000

# A short per-test timeout and the smallest memory cap, so misbehaving submissions end fast.
FILES_ARGUMENT = 'files=["data/scores.csv"]'
LIMIT_ARGUMENTS = ", timeout=5, memory=2048"

PDF = b"%PDF-1.4\n%%EOF\n"
JUNK = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        "

# The demo's `mean`, as it reads in the solution.
MEAN = "    return sum(xs) / len(xs)\n"
MEAN_FUNCTION = '@app.function\ndef mean(xs):\n    """Average of the numbers in `xs`."""\n' + MEAN

INFINITE_LOOP = """\
while True:
    pass
"""

MEMORY_BOMB = """\
blocks = []
while True:
    blocks.append(bytearray(10**8))
"""

EXITS = """\
import os

os._exit(3)
"""

SEGFAULT = """\
import ctypes

return ctypes.string_at(0)
"""

# 10 MB on each stream per call, then the right answer.
STDOUT_FLOOD = """\
import sys

line = "x" * (1024 * 1024 - 1) + "\\n"
for _ in range(10):
    sys.stdout.write(line)
    sys.stderr.write(line)
return sum(xs) / len(xs)
"""

# Right only if both writes are refused.
WRITES_RESULTS = """\
from pathlib import Path

denied = 0
for name in ["results.json", "forged.json"]:
    try:
        (Path("/autograder/results") / name).write_text('{"score": 100}')
    except OSError:
        denied += 1
if denied < 2:
    return 0
return sum(xs) / len(xs)
"""

# Right only if the autograder source is unreadable.
READS_HIDDEN = """\
import os

try:
    print(open("/autograder/source/bundle.json").read())
    return 0
except OSError:
    pass
try:
    print(os.listdir("/autograder/source"))
    return 0
except OSError:
    pass
return sum(xs) / len(xs)
"""

# What the process can reach. Test names and sources from outside the notebook go first,
# since failure messages are cut short.
LEAK_VIA_MESSAGE = """\
import linecache
import re
import sys

own = open(__file__).read()
seen = [repr(sys.argv), repr(sorted(sys.modules))]
sources = []
for entry in list(linecache.cache.values()):
    text = "".join(entry[2]) if len(entry) > 2 else ""
    if "test_q" in text and text != own:
        sources.append(text)
frame = sys._getframe()
while frame is not None:
    seen.append(repr(frame.f_globals) + repr(frame.f_locals))
    frame = frame.f_back
names = set(re.findall(r"test_q\\w+", "".join(seen + sources)))
names -= set(re.findall(r"test_q\\w+", own))
# Students get the public tests in `tests/` next to the notebook.
names -= {"test_q1_whole_numbers", "test_q1_fractions", "test_q2_reads_scores"}
names -= {"test_q2_fractional_score"}
raise AssertionError("LEAK-DUMP " + repr(sorted(names)) + "\\n" + "\\n".join(sources + seen))
"""

# `mean` in a cell that depends on another cell, so the grader needs `app.run()`.
IMPURE_MEAN = '''\
@app.cell
def _():
    count = len
    return (count,)


@app.cell
def _(count):
    def mean(xs):
        """Average of the numbers in `xs`."""
        return sum(xs) / count(xs)

    return (mean,)
'''

type Files = dict[str, bytes]


@dataclass(frozen=True, slots=True)
class Expect:
    """What `results.json` must say about a submission.

    Attributes:
        q1: Score of question 1 (out of 4).
        q2: Score of question 2 (out of 6).
        notice: Text of the "Submission" row; `None` when there must be no such row.
        shown: Text that must appear in some row.
    """

    q1: float
    q2: float
    notice: str | None = None
    shown: str | None = None


EXPECT = {
    "solution": Expect(4, 6),
    "blank": Expect(0, 0),
    "syntax_error": Expect(0, 0, shown="failed to import"),
    "infinite_loop": Expect(0, 6, shown="longer than 5 s"),
    "memory_bomb": Expect(0, 6, shown="MemoryError"),
    "exits": Expect(0, 6, shown="exited with code 3"),
    "segfault": Expect(0, 6, shown="SIGSEGV"),
    "impure_fn": Expect(4, 6),
    "stdout_flood": Expect(4, 6),
    "two_py": Expect(0, 0, notice="several other `.py` files"),
    "nested_zip": Expect(4, 6),
    "macosx_zip": Expect(4, 6),
    "missing_pdf": Expect(4, 6, notice="No PDF"),
    "writes_results": Expect(4, 6),
    "reads_hidden": Expect(4, 6),
    # No test name from outside the notebook reaches the public phase.
    "leak_via_message": Expect(0, 6, shown="LEAK-DUMP []"),
}

# Submissions that grade the same on the host as the current user as in the container.
HOST_SAFE = [
    "solution",
    "blank",
    "syntax_error",
    "impure_fn",
    "stdout_flood",
    "two_py",
    "nested_zip",
    "macosx_zip",
    "missing_pdf",
    "leak_via_message",
]

# Larger results mean student output leaked into them.
MAX_RESULTS_BYTES = 200_000


# Helpers


class Unavailable(Exception):
    """No container runtime to use."""


class CommandError(Exception):
    """A command failed or timed out."""


def find_runtime(
    environ: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> str:
    """Path of the container runtime: `MOGS_CONTAINER`, else docker, else podman.

    Raises:
        ValueError: If `MOGS_CONTAINER` is neither docker nor podman.
        Unavailable: If the runtime is not on PATH.
    """

    chosen = environ.get("MOGS_CONTAINER")
    if chosen is not None and chosen not in RUNTIMES:
        raise ValueError(f"MOGS_CONTAINER must be docker or podman, got {chosen!r}.")

    names = RUNTIMES if chosen is None else (chosen,)
    for name in names:
        path = which(name)
        if path is not None:
            return path

    raise Unavailable(f"no {' or '.join(names)} on PATH")


def run(
    command: list[str],
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run `command` and return its stdout.

    Raises:
        CommandError: If it fails or times out; the message has the ends of both streams.
    """

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            f"`{shlex.join(command)}` timed out after {timeout:g} s\n"
            + _tails(error.stdout, error.stderr)
        ) from error

    if result.returncode != 0:
        raise CommandError(
            f"`{shlex.join(command)}` exited with code {result.returncode}\n"
            + _tails(result.stdout, result.stderr)
        )

    return result.stdout


def _tails(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def tail(text: str | bytes | None) -> str:
        match text:
            case bytes():
                return text.decode(errors="replace")[-TAIL:]
            case str():
                return text[-TAIL:]
            case None:
                return ""

    return f"--- stdout (end) ---\n{tail(stdout)}\n--- stderr (end) ---\n{tail(stderr)}"


def replace_once(text: str, old: str, new: str) -> str:
    """`text` with its one occurrence of `old` replaced.

    Raises:
        ValueError: If `old` occurs other than once, so a stale substitution fails loudly.
    """

    count = text.count(old)
    if count != 1:
        raise ValueError(f"expected {old!r} once, found it {count} times")

    return text.replace(old, new)


def demo_assignment(folder: Path) -> Path:
    """Copy the demo to `folder` with a 5 s test timeout and a 2048 MB memory cap."""

    shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("dist", "__marimo__"))
    path = folder / NOTEBOOK
    path.write_text(
        replace_once(
            path.read_text(encoding="utf-8"), FILES_ARGUMENT, FILES_ARGUMENT + LIMIT_ARGUMENTS
        ),
        encoding="utf-8",
    )

    return folder


@dataclass(frozen=True, slots=True)
class Assignment:
    """The e2e assignment and its submissions.

    Attributes:
        folder: The assignment folder (a demo copy).
        cases: Files of each submission, by case name.
        hidden: Names and assert lines of the hidden tests, which students must never see.
    """

    folder: Path
    cases: dict[str, Files]
    hidden: list[str]


def make_assignment(folder: Path) -> Assignment:
    """The submissions and hidden strings of the assignment in `folder`."""

    path = folder / NOTEBOOK
    src = path.read_text(encoding="utf-8")
    cfg = read_config(src, path)

    blank = student_notebook(src, parse_notebook(src), cfg, pinned_requirement())

    # The solved copy the build writes to dist/autograder/ (`build_image` checks that).
    solution = student_notebook(src, parse_notebook(src), cfg, pinned_requirement(), solutions=True)

    hidden = []
    for cell in bundle_from_notebook(parse_notebook(src), cfg).cells:
        if cell.hidden:
            hidden += cell.tests
            hidden += [
                line.strip()
                for line in cell.source.splitlines()
                if line.strip().startswith("assert ")
            ]

    return Assignment(folder, submissions(solution, blank), hidden)


def submissions(solution: str, blank: str) -> dict[str, Files]:
    """Files of each submission case, made from the solution and blank student notebooks."""

    def notebook(text: str) -> Files:
        return {NOTEBOOK: text.encode(), "answers.pdf": PDF}

    def with_mean(body: str) -> Files:
        return notebook(replace_once(solution, MEAN, textwrap.indent(body, "    ")))

    right = solution.encode()

    return {
        "solution": notebook(solution),
        "blank": notebook(blank),
        "syntax_error": notebook(replace_once(solution, MEAN, "    return sum(xs) /\n")),
        "infinite_loop": with_mean(INFINITE_LOOP),
        "memory_bomb": with_mean(MEMORY_BOMB),
        "exits": with_mean(EXITS),
        "segfault": with_mean(SEGFAULT),
        "impure_fn": notebook(replace_once(solution, MEAN_FUNCTION, IMPURE_MEAN)),
        "stdout_flood": with_mean(STDOUT_FLOOD),
        "two_py": {"first.py": right, "second.py": right, "answers.pdf": PDF},
        "nested_zip": {"my folder/x.py": right, "my folder/answers.pdf": PDF},
        "macosx_zip": notebook(solution)
        | {"__MACOSX/._hw.py": JUNK, "__MACOSX/._answers.pdf": JUNK},
        "missing_pdf": {NOTEBOOK: right},
        "writes_results": with_mean(WRITES_RESULTS),
        "reads_hidden": with_mean(READS_HIDDEN),
        "leak_via_message": with_mean(LEAK_VIA_MESSAGE),
    }


def write_tree(root: Path, files: Files) -> None:
    """Write `files` under `root`, creating folders."""

    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def wheel_requirement(wheel: Path) -> str:
    """The requirements line that installs `wheel` from the image's wheel folder."""

    return f"mogs @ file://{WHEELS / wheel.name}"


def build_context(context: Path, archive: Path) -> None:
    """Fill `context` with the Dockerfile and the zip unpacked into `source/`.

    Raises:
        ValueError: If a zip entry would land outside `source/`.
    """

    source = context / "source"
    source.mkdir(parents=True)

    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (source / info.filename).resolve()
            if not target.is_relative_to(source.resolve()):
                raise ValueError(f"zip entry {info.filename!r} is outside the source folder")

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            target.chmod(0o755 if info.filename in SCRIPTS else 0o644)

    shutil.copyfile(DOCKERFILE, context / "Dockerfile")


def build_wheel(out: Path) -> Path:
    """Build a wheel of this checkout into `out` for the base image's Ubuntu on x86_64.

    maturin cross-compiles the abi3 extension `mogs._native` with zig, so this works from any
    host without a Python of the target.
    """

    uv = shutil.which("uv")
    if uv is None:
        raise CommandError("`uv` is not on PATH; it builds the wheel")

    command = [
        uv,
        "tool",
        "run",
        "--from",
        "maturin[zig]>=1.9.4,<2",
        "maturin",
        "build",
        "--release",
        "--zig",
        "--compatibility",
        "manylinux_2_17",
        "--target",
        "x86_64-unknown-linux-gnu",
        "--out",
        str(out),
    ]
    run(command, timeout=600, cwd=REPO)
    [wheel] = out.glob("*.whl")

    return wheel


def build_image(runtime: str, assignment: Assignment, context: Path) -> str:
    """Build the autograder image and return its tag."""

    wheel = build_wheel(context / "wheels")
    env = os.environ | {"MOGS_TOOL_REQUIREMENT": wheel_requirement(wheel)}
    run([str(MOGS), "build", str(assignment.folder)], timeout=1800, env=env)
    autograder = assignment.folder / "dist" / "autograder"
    [archive] = autograder.glob("*.zip")

    # The "solution" case is the solved notebook an instructor uploads from dist/autograder/.
    assert (autograder / NOTEBOOK).read_bytes() == assignment.cases["solution"][NOTEBOOK]
    assert re.fullmatch(
        r"hw-autograder-\d{4}_\d{2}_\d{2}T\d{2}_\d{2}_\d{2}_\d{6}\.zip", archive.name
    )
    build_context(context, archive)

    tag = f"localhost/mogs-e2e:{uuid.uuid4().hex[:12]}"
    command = [runtime, "build", "--platform", PLATFORM, "--tag", tag, str(context)]
    run(command, timeout=1800)

    return tag


def remove_image(runtime: str, tag: str) -> None:
    """Remove the image, ignoring errors: it is only a cache."""

    subprocess.run([runtime, "image", "rm", "--force", tag], capture_output=True, check=False)


def grade_in_container(runtime: str, image: str, files: Files, root: Path) -> Path:
    """Grade `files` in a fresh container the way Gradescope does.

    Returns:
        The folder mounted as `/autograder/results`.
    """

    submission = root / "submission"
    results = root / "results"
    write_tree(submission, files)
    results.mkdir()

    # Gradescope's harness creates both as root with mode 755; the student user can read
    # the upload but not write results.
    for folder in [root, submission, *submission.rglob("*"), results]:
        if folder.is_dir():
            folder.chmod(0o755)

    name = f"mogs-e2e-{uuid.uuid4().hex[:12]}"
    command = [
        runtime,
        "run",
        "--rm",
        "--name",
        name,
        "--platform",
        PLATFORM,
        "--security-opt",
        "label=disable",
        "--volume",
        f"{submission}:/autograder/submission",
        "--volume",
        f"{results}:/autograder/results",
        "--workdir",
        "/autograder",
        image,
        # As in Gradescope's harness: run from /autograder, output to results/stdout.
        "bash",
        "-c",
        "./run_autograder > /autograder/results/stdout 2>&1",
    ]

    try:
        run(command, timeout=900)
    except CommandError as error:
        subprocess.run([runtime, "kill", name], capture_output=True, check=False)
        log = results / "stdout"
        output = (
            log.read_text(errors="replace", encoding="utf-8")[-TAIL:] if log.exists() else "(none)"
        )
        raise CommandError(f"{error}\n--- run_autograder output (end) ---\n{output}") from error

    if not (results / "results.json").exists():
        log = results / "stdout"
        log = (
            log.read_text(errors="replace", encoding="utf-8")[-TAIL:] if log.exists() else "(none)"
        )
        raise AssertionError(f"no results.json; run_autograder output (end):\n{log}")

    return results


def grade_on_host(assignment: Assignment, root: Path, name: str) -> dict:
    """Grade a submission with the grader's entry point on the host, as the current user."""

    source = root / "source"
    submission = root / "submission"
    results = root / "results" / "results.json"

    source.mkdir()
    path = assignment.folder / NOTEBOOK
    src = path.read_text(encoding="utf-8")
    bundle = bundle_from_notebook(parse_notebook(src), read_config(src, path))
    (source / "bundle.json").write_text(bundle.to_json(), encoding="utf-8")
    shutil.copytree(assignment.folder / "data", source / "data")
    write_tree(submission, assignment.cases[name])

    code = main(
        [
            "autograde",
            "--source",
            str(source),
            "--submission",
            str(submission),
            "--results",
            str(results),
            "--workdir",
            str(root / "work"),
        ]
    )
    assert code == 0

    return json.loads(results.read_text(encoding="utf-8"))


def check_results(results: dict, expect: Expect, hidden: list[str]) -> None:
    """Assert that `results` matches `expect` and shows students nothing of the hidden tests."""

    text = json.dumps(results)
    assert len(text) < MAX_RESULTS_BYTES, len(text)
    assert results["stdout_visibility"] == "hidden"

    rows = {row.get("number", row["name"]): row for row in results["tests"]}
    assert (rows["q1"]["score"], rows["q2"]["score"]) == (expect.q1, expect.q2), text[-TAIL:]
    assert rows["q1"]["max_score"] == 4
    assert rows["q2"]["max_score"] == 6
    assert "q3" not in rows

    notice = rows.get("Submission")
    if expect.notice is None:
        assert notice is None, notice
    else:
        assert notice is not None, text[-TAIL:]
        assert expect.notice in notice["output"]

    visible = [row for row in results["tests"] if row["visibility"] != "hidden"]
    assert all(row["visibility"] == "hidden" for row in (rows["q1"], rows["q2"]))

    shown = "\n".join(f"{row['name']}\n{row['output']}" for row in visible)
    if expect.shown is not None:
        assert expect.shown in shown, shown[-TAIL:]

    every = "\n".join(strings([results.get("output", ""), visible]))
    for secret in hidden:
        assert secret not in every, secret


def strings(value: object) -> list[str]:
    """Every string in parsed JSON, keys included, unescaped."""

    match value:
        case str():
            return [value]
        case dict():
            return [text for key, item in value.items() for text in [key, *strings(item)]]
        case list():
            return [text for item in value for text in strings(item)]
        case _:
            return []


# Without a container


def test_runtime_prefers_docker_then_podman():
    found = {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}

    assert find_runtime({}, found.get) == "/usr/bin/docker"
    assert find_runtime({}, {"podman": "/bin/podman"}.get) == "/bin/podman"


def test_runtime_from_the_environment():
    found = {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}

    assert find_runtime({"MOGS_CONTAINER": "podman"}, found.get) == "/usr/bin/podman"

    with pytest.raises(Unavailable, match="podman"):
        find_runtime({"MOGS_CONTAINER": "podman"}, {"docker": "/usr/bin/docker"}.get)


def test_runtime_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="MOGS_CONTAINER"):
        find_runtime({"MOGS_CONTAINER": "nerdctl"}, {"nerdctl": "/bin/nerdctl"}.get)


def test_no_runtime_is_unavailable():
    with pytest.raises(Unavailable, match="docker or podman"):
        find_runtime({}, {}.get)


def test_command_error_has_the_output_tails():
    with pytest.raises(CommandError, match="(?s)exited with code 3.*boom"):
        run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], timeout=60)


def test_replace_once_needs_exactly_one_match():
    assert replace_once("a b", "b", "c") == "a c"

    with pytest.raises(ValueError, match="2 times"):
        replace_once("b b", "b", "c")

    with pytest.raises(ValueError, match="0 times"):
        replace_once("a", "b", "c")


def test_demo_assignment_has_short_limits(tmp_path):
    folder = demo_assignment(tmp_path / "demo")

    assert sorted(path.name for path in folder.iterdir()) == ["data", NOTEBOOK]

    text = (folder / NOTEBOOK).read_text(encoding="utf-8")
    assert 'mogs.assignment(files=["data/scores.csv"], timeout=5, memory=2048)' in text


def test_every_submission_has_an_expectation(assignment):
    assert set(assignment.cases) == set(EXPECT)
    assert set(HOST_SAFE) <= set(EXPECT)


def test_solution_keeps_the_answers_but_drops_the_hidden_tests(assignment):
    solution = assignment.cases["solution"][NOTEBOOK].decode()

    assert MEAN in solution
    assert BEGIN not in solution
    assert "SOLUTION" not in solution
    assert "# HIDDEN" not in solution
    assert "def test_" not in solution
    for secret in assignment.hidden:
        assert secret not in solution


def test_hidden_strings_name_the_hidden_tests(assignment):
    assert "test_q1_single" in assignment.hidden
    assert "test_q2_floats" in assignment.hidden
    assert "assert mean([7]) == 7" in assignment.hidden
    assert "test_q1_whole_numbers" not in assignment.hidden


@pytest.mark.parametrize("name", list(EXPECT))
def test_submission_files(assignment, tmp_path, name):
    files = assignment.cases[name]
    write_tree(tmp_path, files)

    for rel, content in files.items():
        if rel.endswith(".py") and not rel.startswith("__MACOSX"):
            if name == "syntax_error":
                with pytest.raises(SyntaxError):
                    ast.parse(content)
            else:
                ast.parse(content)

    # Only `two_py` has more than one notebook, and only `missing_pdf` lacks the answers.
    notebooks = [rel for rel in files if rel.endswith(".py") and not rel.startswith("__MACOSX")]
    answers = [rel for rel in files if rel.endswith(".pdf") and not rel.startswith("__MACOSX")]
    assert (len(notebooks) == 1) == (name != "two_py")
    assert (answers == []) == (name == "missing_pdf")


def test_wheel_requirement_names_the_tool():
    wheel = "mogs-0.1.0-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    requirement = wheel_requirement(Path("/x") / wheel)

    assert requirement == f"mogs @ file:///wheels/{wheel}"
    assert normalized_name(requirement) == "mogs"


def test_build_context_unpacks_the_zip_with_modes(tmp_path):
    archive = tmp_path / "a.zip"
    entries = {
        "setup.sh": b"#!/bin/sh\n",
        "run_autograder": b"#!/bin/sh\n",
        "bundle.json": b"{}",
        "requirements.txt": b"x\n",
        "data/scores.csv": b"name,score\n",
    }
    with zipfile.ZipFile(archive, "w") as zipped:
        for name, content in entries.items():
            zipped.writestr(name, content)
    context = tmp_path / "context"

    build_context(context, archive)

    assert (context / "Dockerfile").read_text(encoding="utf-8") == DOCKERFILE.read_text(
        encoding="utf-8"
    )
    for rel, content in entries.items():
        assert (context / "source" / rel).read_bytes() == content

    def mode(rel: str) -> int:
        return stat.S_IMODE((context / "source" / rel).stat().st_mode)

    assert mode("setup.sh") == 0o755
    assert mode("run_autograder") == 0o755
    assert mode("bundle.json") == 0o644


def test_build_context_rejects_entries_outside(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil", b"x")

    with pytest.raises(ValueError, match="evil"):
        build_context(tmp_path / "context", archive)


def test_strings_are_unescaped():
    results = json.loads(json.dumps({"tests": [{"output": 'x == ["Ada"]\n', "score": 1}]}))

    assert strings(results) == ["tests", "output", 'x == ["Ada"]\n', "score"]


@pytest.mark.parametrize(
    ("args", "selected"),
    [([], False), (["-m", "slow"], False), (["-m", "docker"], True)],
)
def test_docker_tests_run_only_when_named(args, selected):
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = subprocess.run(
        [*command, *args, str(Path(__file__))],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=REPO,
    )

    # 5: nothing selected.
    assert result.returncode in (0, 5), result.stdout + result.stderr
    assert ("test_grade_in_container" in result.stdout) is selected


def test_dockerfile_mimics_gradescope():
    lines = [
        line.strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    # Fully qualified, so podman needs no short-name resolution.
    assert lines[0].startswith("FROM docker.io/gradescope/autograder-base@sha256:")
    assert "COPY wheels/ /wheels/" in lines
    assert "COPY source/ /autograder/source/" in lines
    assert "RUN bash /gradescope/setup.sh" in lines


@pytest.mark.skipif(sys.platform != "linux", reason="the grader is Linux only")
@pytest.mark.parametrize("name", HOST_SAFE)
def test_grade_on_host(assignment, tmp_path, name):
    results = grade_on_host(assignment, tmp_path, name)

    check_results(results, EXPECT[name], assignment.hidden)


# In the container


@pytest.mark.docker
@pytest.mark.parametrize("name", list(EXPECT))
def test_grade_in_container(runtime, image, assignment, tmp_path, name):
    results_dir = grade_in_container(runtime, image, assignment.cases[name], tmp_path)

    leftovers = sorted(path.name for path in results_dir.iterdir())
    assert set(leftovers) <= {"results.json", "stdout"}, leftovers

    results = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    check_results(results, EXPECT[name], assignment.hidden)


@pytest.fixture(scope="session")
def assignment(tmp_path_factory) -> Assignment:
    return make_assignment(demo_assignment(tmp_path_factory.mktemp("e2e") / "demo"))


@pytest.fixture(scope="session")
def runtime() -> str:
    try:
        path = find_runtime(os.environ)
        run([path, "info"], timeout=120)
    except (Unavailable, CommandError) as error:
        reason = f"no usable container runtime: {error}"
        if os.environ.get("MOGS_REQUIRE_CONTAINER") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    return path


@pytest.fixture(scope="session")
def image(runtime, assignment, tmp_path_factory) -> Iterator[str]:
    tag = build_image(runtime, assignment, tmp_path_factory.mktemp("context"))
    yield tag
    remove_image(runtime, tag)

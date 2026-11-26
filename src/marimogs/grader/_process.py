"""Operating-system side of the sandbox: start, watch and kill one grading child.

Linux and macOS. The child runs in its own session, so killing its process group also kills the
processes it started, unless they left the group; with a dedicated `user`, `pkill -u` gets those
too. macOS rejects `RLIMIT_AS`, so there the child has no memory cap.
"""

import os
import pwd
import resource
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO

from marimogs._env import grading_env
from marimogs.grader.protocol import Event, ProtocolError, decode

OUTPUT_LIMIT = 64 * 1024
LINE_LIMIT = 1024 * 1024
NPROC_LIMIT = 512
POLL = 0.05
KILL_PASSES = 5
OOM_MESSAGE = "killed, probably out of memory: raise the Gradescope container memory"

type Limit = tuple[int, int]


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """How to start a child, apart from what every child shares.

    Attributes:
        cwd: Working directory.
        env: Environment.
        user: User to run as, or `None` to keep the current one.
        group: Primary group of `user`.
        extra_groups: Supplementary groups; empty drops root's groups.
        limits: Resource limits set before the child starts.
        python: Interpreter to run the child with.
    """

    cwd: Path
    env: dict[str, str]
    user: str | None
    group: int | None
    extra_groups: list[int] | None
    limits: list[Limit]
    python: str


def child_command(event_fd: int, python: str) -> list[str]:
    """The command line of a grading child that runs on `python` and writes events to `event_fd`."""

    return [python, "-m", "marimogs.grader.child", str(event_fd)]


def child_env(workdir: Path) -> dict[str, str]:
    """The current environment, made fit for student code that runs in `workdir`.

    It sets `MGS_GRADING=1`, so `check` and `export` cells do nothing when the notebook runs.
    """

    env = grading_env() | {
        "MPLBACKEND": "Agg",
        "OMP_NUM_THREADS": "4",
        "HOME": str(workdir),
        "MPLCONFIGDIR": str(workdir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    return env


def limits(memory_mb: int, user: str | None) -> list[Limit]:
    """Resource limits for a child.

    The process cap counts every process of the real user, so it applies only to a dedicated
    grading `user`; on a developer machine it would count the developer's own processes. The
    memory cap is Linux only: macOS fails `setrlimit(RLIMIT_AS, ...)`, so a self-check on macOS
    runs without one.
    """

    result: list[Limit] = []

    if sys.platform == "linux":
        result.append((resource.RLIMIT_AS, memory_mb * 1024 * 1024))

    if user is not None:
        result.append((resource.RLIMIT_NPROC, NPROC_LIMIT))

    return result


def check_user(user: str) -> pwd.struct_passwd:
    """The password entry of a grading user.

    Raises:
        ValueError: If `user` is root or the user running the grader, since `pkill -u` would
            then kill the grader, and student code would get the grader's rights.
        KeyError: If there is no such user.
    """

    entry = pwd.getpwnam(user)

    if entry.pw_uid == 0:
        raise ValueError(f"the grading user must not be root: {user!r} has uid 0")

    if entry.pw_uid == os.getuid():
        raise ValueError(f"the grading user must not be the user running the grader: {user!r}")

    return entry


def launch_options(workdir: Path, user: str | None, memory_mb: int, python: str) -> LaunchOptions:
    """Options for a child that runs on `python` in `workdir`, as `user` when set.

    Raises:
        ValueError: If `user` is root or the current user (see `check_user`).
    """

    group = None if user is None else check_user(user).pw_gid
    extra_groups = None if user is None else []

    return LaunchOptions(
        workdir, child_env(workdir), user, group, extra_groups, limits(memory_mb, user), python
    )


def describe_exit(returncode: int) -> str:
    """How a child ended, from its exit status, for a failure message."""

    match returncode:
        case code if code == -signal.SIGKILL:
            return OOM_MESSAGE
        case code if code < 0:
            return f"killed by signal {_signal_name(-code)}"
        case code:
            return f"exited with code {code}"


def kill_user(user: str) -> None:
    """Kill every process of `user`.

    Repeats, up to `KILL_PASSES` times, while `pgrep` still finds processes, which a fork
    bomb can start between two passes.
    """

    for _ in range(KILL_PASSES):
        subprocess.run(["pkill", "-KILL", "-u", user], check=False)

        # `pgrep` exits with 1 when no process matches.
        if subprocess.run(["pgrep", "-u", user], capture_output=True).returncode == 1:
            return


class Child:
    """A running grading child: its process, its event channel and the tail of its output.

    Events are read with `select` and deadlines, never up to end of file: a process the child
    forks keeps the write end open after the child is gone.

    Args:
        job: The `Job` as JSON, given to the child as stdin.
        options: How to start it.
    """

    def __init__(self, job: str, options: LaunchOptions) -> None:
        self._user = options.user
        self._buffer = bytearray()
        self.returncode: int | None = None
        self.error: ProtocolError | None = None

        read, write = os.pipe()
        os.set_blocking(read, False)
        self._events: int | None = read

        # Stdin is a file, so the parent never blocks on a job the child does not read.
        with tempfile.TemporaryFile() as stdin:
            stdin.write(job.encode())
            stdin.seek(0)

            try:
                self._process = subprocess.Popen(
                    child_command(write, options.python),
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    pass_fds=(write,),
                    start_new_session=True,
                    cwd=options.cwd,
                    env=options.env,
                    user=options.user,
                    group=options.group,
                    extra_groups=options.extra_groups,
                    preexec_fn=partial(_set_limits, options.limits),
                )
            except BaseException:
                os.close(read)
                raise
            finally:
                os.close(write)

        stdout = self._process.stdout
        assert stdout is not None  # `stdout=PIPE` above.
        self._output = _Tail(stdout)

    def wait(self, until: float) -> list[Event]:
        """Events that arrive within `POLL` seconds, or before the monotonic time `until`.

        Sets `returncode` once the child has ended. When the child writes a line that is not an
        event, sets `error` and returns the events before that line; later lines are dropped.
        """

        seconds = min(POLL, max(0.0, until - time.monotonic()))

        if self._events is None:
            time.sleep(seconds)
        else:
            select.select([self._events], [], [], seconds)

        events = self._read()

        if self._ended():
            # Until reaped, the child is a zombie and its process group id stays reserved.
            self._kill_group()
            self.returncode = self._process.wait()
            events += self._read()

        return events

    def close(self) -> str:
        """Kill the child and everything it started, and release its resources.

        Returns:
            The last `OUTPUT_LIMIT` bytes of its stdout and stderr, decoded.
        """

        # Once the child is reaped, its process group id may belong to someone else.
        if self.returncode is None:
            self._kill_group()

        if self._user is not None:
            kill_user(self._user)

        self.returncode = self._process.wait()

        if self._events is not None:
            os.close(self._events)
            self._events = None

        return self._output.close()

    def _read(self) -> list[Event]:
        if self._events is None or self.error is not None:
            return []

        # Bounded, so a child that writes without end cannot keep the parent from its deadlines.
        for _ in range(LINE_LIMIT // 65536):
            try:
                chunk = os.read(self._events, 65536)
            except BlockingIOError:
                break

            if not chunk:
                os.close(self._events)
                self._events = None
                break

            self._buffer += chunk

        *lines, rest = self._buffer.split(b"\n")
        self._buffer = bytearray(rest)
        events = []

        # Events before a bad line still count, so blame goes to what the child was doing.
        for line in lines:
            try:
                events.append(decode(line.decode(errors="replace")))
            except ProtocolError as e:
                self.error = e
                return events

        if len(self._buffer) > LINE_LIMIT:
            self.error = ProtocolError(f"an event line is longer than {LINE_LIMIT} bytes")

        return events

    def _ended(self) -> bool:
        """Whether the child has ended, without reaping it."""

        flags = os.WEXITED | os.WNOHANG | os.WNOWAIT

        try:
            return os.waitid(os.P_PID, self._process.pid, flags) is not None
        except ChildProcessError:
            return True

    def _kill_group(self) -> None:
        """Kill the child's process group, if anything is left in it."""

        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS answers EPERM, not ESRCH, when every process left in the group is a zombie,
            # as when the ended child is not reaped yet. While the child runs, EPERM is a real
            # problem: the parent may not signal a process in the group.
            if not self._ended():
                raise


class _Tail:
    """Drains a pipe on a thread and keeps its last `OUTPUT_LIMIT` bytes."""

    def __init__(self, pipe: IO[bytes]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()
        os.set_blocking(self._fd, False)
        self._data = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def close(self) -> str:
        """Stop draining, close the pipe and return what was kept."""

        self._stop.set()
        self._thread.join()
        self._pipe.close()

        return self._data.decode(errors="replace")

    def _drain(self) -> None:
        while not self._stop.is_set():
            ready, _, _ = select.select([self._fd], [], [], 0.1)

            if not ready:
                continue

            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                continue

            if not chunk:
                return

            self._data += chunk
            del self._data[:-OUTPUT_LIMIT]


def _set_limits(items: list[Limit]) -> None:
    """Runs in the child between fork and exec. Never raises a limit above its hard limit."""

    for kind, value in items:
        resource.setrlimit(kind, clamped(kind, value))


def clamped(kind: int, value: int) -> tuple[int, int]:
    """Soft and hard limits of `value` for resource `kind`, at most the current hard limit."""

    _, hard = resource.getrlimit(kind)

    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)

    return value, value


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)

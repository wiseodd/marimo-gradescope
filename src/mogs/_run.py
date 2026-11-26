"""Run a helper command from the student's kernel without risking a hang.

Works on Linux, macOS and Windows: no timers, no resource limits.
"""

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO

from mogs._env import grading_env


def run_capped(
    command: list[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run `command` and capture its output, killing it after `timeout` seconds.

    Output goes to temporary files, not pipes, so a grandchild that keeps a pipe open cannot
    block the wait. On a timeout or any exception, `KeyboardInterrupt` included, the process
    is killed before the exception propagates: with its whole process group on Linux and
    macOS, with its process tree (`taskkill /T`) on Windows. The command runs with
    `MGS_GRADING=1`, so `check` and `export` cells do nothing if it runs the notebook.

    Args:
        command: The command and its arguments.
        cwd: Working directory.
        timeout: Seconds to wait before killing the process.

    Returns:
        The finished process with decoded output, or `None` when it timed out.
    """

    # Bytecode caches would land next to the student's notebook.
    env = grading_env() | {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = _start(command, cwd, env, out, err)

        try:
            returncode = process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        finally:
            _kill_tree(process)

        out.seek(0)
        err.seek(0)

        return subprocess.CompletedProcess(
            command,
            returncode,
            out.read().decode(errors="replace"),
            err.read().decode(errors="replace"),
        )


def _start(
    command: list[str], cwd: Path, env: dict[str, str], out: IO[bytes], err: IO[bytes]
) -> subprocess.Popen[bytes]:
    """Start `command` in a process group of its own."""

    if sys.platform == "win32":
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        start_new_session=True,
    )


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the process and what it started, then reap it.

    On Linux and macOS the whole group goes, even after the process itself exited, so a
    grandchild it left behind does not outlive it.
    """

    if sys.platform == "win32":
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    if process.poll() is None:
        process.kill()

    process.wait()

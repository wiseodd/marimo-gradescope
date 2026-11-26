"""The `mogs` executable installed with this package."""

import os
import subprocess
import sysconfig
from pathlib import Path

NAME = "mogs.exe" if os.name == "nt" else "mogs"


class BinaryError(Exception):
    """The `mogs` executable is not installed next to this package."""


def binary() -> Path:
    """The `mogs` executable of this environment.

    Looks in the environment's scripts folder, then in the user scheme's scripts folder.

    Raises:
        BinaryError: If neither folder has it. The message names both folders.
    """

    folders = [
        Path(sysconfig.get_path("scripts")),
        Path(sysconfig.get_path("scripts", f"{os.name}_user")),
    ]

    for folder in folders:
        path = folder / NAME
        if path.is_file():
            return path

    searched = " or ".join(str(folder) for folder in folders)
    raise BinaryError(f"No {NAME} executable in {searched}.")


def run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the `mogs` executable with `args` and capture its output as text.

    Args:
        args: Arguments after the command name.
        stdin: Text for its standard input; inherited from this process when `None`.

    Returns:
        The finished process. A non-zero exit code is not an error here.
    """

    return subprocess.run(
        [binary(), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )

"""Data files shipped with a notebook: path checks and folder expansion.

Shared by the build and the sandbox. Stdlib only, so the build can use it on any platform.
"""

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

# The name the sandbox gives the submission in each phase folder.
SUBMISSION = "submission.py"
# Folders and files that tools leave behind; never shipped.
JUNK = frozenset({"__pycache__", "__marimo__", "__MACOSX", ".ipynb_checkpoints", ".DS_Store"})


def data_files(root: Path, files: Sequence[str]) -> dict[PurePosixPath, Path]:
    """The data files to ship, by path relative to `root`, with folders expanded.

    Files in `JUNK` folders, and `JUNK` files, are left out.

    Args:
        root: Folder the paths are relative to.
        files: Relative paths of files or folders.

    Returns:
        Source path by relative destination, in the order of `files`, folders sorted.

    Raises:
        ValueError: If a path is absolute, has a `..` part, is empty, starts with
            `submission.py`, or does not exist. The message names the path.
    """

    out: dict[PurePosixPath, Path] = {}

    for name in files:
        rel = PurePosixPath(name)

        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(f"a data file must be a relative path inside its folder: {name!r}")

        if rel.parts[0] == SUBMISSION:
            raise ValueError(f"a data file must not be named {SUBMISSION}: {name!r}")

        source = root / rel
        if source.is_file():
            out[rel] = source
        elif source.is_dir():
            for path in sorted(source.rglob("*")):
                inner = PurePosixPath(path.relative_to(root).as_posix())
                if path.is_file() and not JUNK & set(inner.parts):
                    out[inner] = path
        else:
            raise ValueError(f"data file {name!r} not found in {root}")

    return out

import subprocess
import sys
from pathlib import Path

from conftest import PLATFORM

TESTS = Path(__file__).parent


def collected(*args: str) -> set[str]:
    """The names of the test modules that `pytest --collect-only` selects with `args`."""

    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = subprocess.run(
        [*command, *args, str(TESTS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=TESTS.parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    return {Path(line.split("::")[0]).name for line in result.stdout.splitlines() if "::" in line}


def test_platform_marker_selects_the_platform_modules():
    assert collected("-m", "platform") == PLATFORM


def test_every_platform_module_exists():
    assert {path.name for path in TESTS.glob("test_*.py")} >= PLATFORM

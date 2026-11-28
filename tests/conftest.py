import os
import sysconfig
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def mogs_script() -> Path:
    """The `mogs` command of this environment, where the installer put its scripts."""

    return Path(sysconfig.get_path("scripts")) / ("mogs.exe" if os.name == "nt" else "mogs")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the `docker` tests unless the `-m` expression names them."""

    if "docker" in config.option.markexpr:
        return

    docker = [item for item in items if item.get_closest_marker("docker") is not None]
    if docker:
        config.hook.pytest_deselected(items=docker)
        items[:] = [item for item in items if item.get_closest_marker("docker") is None]

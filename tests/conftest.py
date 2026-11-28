import os
import sysconfig
from pathlib import Path

import pytest

# Every proxy variable the PDF's package downloader reads.
PROXIES = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


@pytest.fixture
def offline_typst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty Typst package folders, and downloads through a proxy nothing listens on."""

    for name in PROXIES:
        monkeypatch.delenv(name, raising=False)

    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("TYPST_PACKAGE_PATH", str(tmp_path / "typst-data"))
    monkeypatch.setenv("TYPST_PACKAGE_CACHE_PATH", str(tmp_path / "typst-cache"))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")


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

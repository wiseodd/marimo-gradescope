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


# Modules whose code runs on the student's or instructor's own computer, where the operating
# system matters: the student side (config, checks, loader, export, PDF, selfcheck), processes
# and paths, `edit` and `init`, and the native module. CI runs only these on macOS and Windows
# (`-m platform`), and everything on Linux. The Rust tests cover the grading sandbox there.
PLATFORM = frozenset(
    {
        "test_assertrewrite_guard.py",
        "test_assignment.py",
        "test_check.py",
        "test_cli.py",
        "test_config.py",
        "test_edit.py",
        "test_export.py",
        "test_init.py",
        "test_loader.py",
        "test_main.py",
        "test_native.py",
        "test_pdf.py",
        "test_runner.py",
        "test_scaffold.py",
        "test_selfcheck.py",
        "test_source.py",
        "test_transform.py",
    }
)


def pytest_itemcollected(item: pytest.Item) -> None:
    """Mark the tests of the `PLATFORM` modules `platform`."""

    if item.path.name in PLATFORM:
        item.add_marker(pytest.mark.platform)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the `docker` tests unless the `-m` expression names them."""

    if "docker" in config.option.markexpr:
        return

    docker = [item for item in items if item.get_closest_marker("docker") is not None]
    if docker:
        config.hook.pytest_deselected(items=docker)
        items[:] = [item for item in items if item.get_closest_marker("docker") is None]

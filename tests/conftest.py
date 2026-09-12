"""Isolation helpers for Research OS tests."""

from __future__ import annotations

from pathlib import Path

import pytest

XDG_ENV_VARS = (
    "RESEARCH_OS_CONFIG_HOME",
    "RESEARCH_OS_DATA_HOME",
    "RESEARCH_OS_CACHE_HOME",
    "RESEARCH_OS_STATE_HOME",
)


@pytest.fixture(autouse=True)
def isolate_xdg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent Research OS path overrides from leaking across tests."""
    for name in XDG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary RESEARCH_OS_DATA_HOME for registry-writing tests."""
    path = tmp_path / "xdg-data"
    path.mkdir()
    monkeypatch.setenv("RESEARCH_OS_DATA_HOME", str(path))
    return path


@pytest.fixture
def automation_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every Research OS directory into ``tmp_path``.

    Automation runs write runtime state, create worktrees, and read the project
    registry, so a test that does not relocate all four directories would touch
    the researcher's real machine.
    """

    root = tmp_path / "xdg"
    mapping = {
        "RESEARCH_OS_CONFIG_HOME": root / "config",
        "RESEARCH_OS_DATA_HOME": root / "data",
        "RESEARCH_OS_CACHE_HOME": root / "cache",
        "RESEARCH_OS_STATE_HOME": root / "state",
    }
    for name, path in mapping.items():
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return mapping["RESEARCH_OS_STATE_HOME"]

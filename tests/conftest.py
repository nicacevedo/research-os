"""Isolation helpers for Research OS tests."""

from __future__ import annotations

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

"""Tests for XDG path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os import paths


def _clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        paths.CONFIG_HOME_ENV,
        paths.DATA_HOME_ENV,
        paths.CACHE_HOME_ENV,
        paths.STATE_HOME_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_path_derivation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert paths.config_home() == tmp_path / ".config" / "research-os"
    assert paths.data_home() == tmp_path / ".local" / "share" / "research-os"
    assert paths.cache_home() == tmp_path / ".cache" / "research-os"
    assert paths.state_home() == tmp_path / ".local" / "state" / "research-os"


def test_config_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "cfg"
    monkeypatch.setenv(paths.CONFIG_HOME_ENV, str(override))
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    assert paths.config_home() == override


def test_data_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "data"
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(override))
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    assert paths.data_home() == override


def test_cache_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "cache"
    monkeypatch.setenv(paths.CACHE_HOME_ENV, str(override))
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    assert paths.cache_home() == override


def test_state_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "state"
    monkeypatch.setenv(paths.STATE_HOME_ENV, str(override))
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    assert paths.state_home() == override


def test_overrides_do_not_leak_across_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_overrides(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_home() == tmp_path / ".config" / "research-os"


def test_xdg_dir_issue_missing(tmp_path: Path) -> None:
    assert paths.xdg_dir_issue(tmp_path / "absent") == "missing"


def test_xdg_dir_issue_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("not a directory\n", encoding="utf-8")
    assert paths.xdg_dir_issue(target) == "not a directory"


def test_xdg_dir_issue_ok(tmp_path: Path) -> None:
    target = tmp_path / "dir"
    target.mkdir()
    assert paths.xdg_dir_issue(target) is None

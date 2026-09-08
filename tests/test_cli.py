"""Tests for researchctl version and doctor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_os import __version__, paths
from research_os.cli import main
from research_os.errors import EXIT_ERROR


def _make_xdg_dirs(root: Path) -> dict[str, Path]:
    mapping = {
        "config": root / "config",
        "data": root / "data",
        "cache": root / "cache",
        "state": root / "state",
    }
    for path in mapping.values():
        path.mkdir()
    return mapping


def _apply_xdg(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, Path]) -> None:
    monkeypatch.setenv(paths.CONFIG_HOME_ENV, str(mapping["config"]))
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(mapping["data"]))
    monkeypatch.setenv(paths.CACHE_HOME_ENV, str(mapping["cache"]))
    monkeypatch.setenv(paths.STATE_HOME_ENV, str(mapping["state"]))


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    return 0


def test_version(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(monkeypatch, "version")
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == __version__


def test_doctor_healthy_xdg_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == 0
    assert "FAIL" not in captured.out
    for name, path in mapping.items():
        assert f"PASS  {name:<10}  {path}" in captured.out


def test_doctor_missing_required_directory_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    missing = mapping["cache"]
    missing.rmdir()
    _apply_xdg(monkeypatch, mapping)
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "FAIL" in captured.out
    assert "(missing)" in captured.out
    assert not missing.exists()


def test_doctor_path_exists_but_is_not_a_directory_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    mapping["data"].rmdir()
    mapping["data"].write_text("file\n", encoding="utf-8")
    _apply_xdg(monkeypatch, mapping)
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "(not a directory)" in captured.out


def test_doctor_unwritable_directory_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    monkeypatch.setattr(
        paths.os,
        "access",
        lambda path, mode, original=os.access: (
            False
            if Path(path) == mapping["state"] and mode == os.W_OK
            else original(path, mode)
        ),
    )
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "(not writable)" in captured.out


def test_doctor_does_not_create_missing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {
        "config": tmp_path / "config",
        "data": tmp_path / "data",
        "cache": tmp_path / "cache",
        "state": tmp_path / "state",
    }
    _apply_xdg(monkeypatch, mapping)
    before = {name: path.exists() for name, path in mapping.items()}
    assert not any(before.values())
    code = _run(monkeypatch, "doctor")
    assert code == EXIT_ERROR
    after = {name: path.exists() for name, path in mapping.items()}
    assert after == before


def test_doctor_does_not_require_config_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    assert not (mapping["config"] / "config.toml").exists()
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == 0
    assert "config.toml" not in captured.out


def test_doctor_does_not_require_secrets_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    assert not (mapping["config"] / "secrets.env").exists()
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == 0
    assert "secrets.env" not in captured.out


def test_doctor_does_not_require_project_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    assert not (mapping["data"] / "project_registry.sqlite").exists()
    code = _run(monkeypatch, "doctor")
    captured = capsys.readouterr()
    assert code == 0
    assert "project_registry" not in captured.out
    assert "registry" not in captured.out.lower()

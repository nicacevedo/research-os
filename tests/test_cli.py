"""Tests for researchctl version and doctor."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from research_os import __version__, paths
from research_os.cli import main
from research_os.errors import EXIT_ERROR
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    make_git_repo,
    question_data,
    snapshot_files,
    write_minimal_capsule,
    write_yaml,
)


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


def test_validate_project_valid_capsule_exit_0(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    capsys.readouterr()
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "OK"
    assert captured.err == ""


def test_validate_project_warning_only_exit_0(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(
        repo / ".research" / "literature" / "note.yaml",
        {"id": "ignored"},
    )
    code = _run(monkeypatch, "validate-project", str(repo))
    assert code == 0


def test_validate_project_invalid_capsule_exit_1(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(
        repo / ".research" / "questions" / "Q-0001.yaml",
        question_data(unknown_field="nope"),
    )
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "E_SCHEMA" in captured.out
    assert captured.err == ""


def test_validate_non_project_git_repo_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "plain-repo")
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "E_MISSING_CAPSULE_FILE" in captured.out


def test_validate_json_is_deterministic_and_has_no_prose(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(
        repo / ".research" / "questions" / "Q-0001.yaml",
        question_data(unknown_field="nope"),
    )
    capsys.readouterr()
    first = _run(monkeypatch, "validate-project", str(repo), "--json")
    out1 = capsys.readouterr().out
    second = _run(monkeypatch, "validate-project", str(repo), "--json")
    out2 = capsys.readouterr().out
    assert first == EXIT_ERROR
    assert second == EXIT_ERROR
    assert out1 == out2
    payload = json.loads(out1)
    assert payload["ok"] is False
    assert payload["findings"]
    assert "OK" not in out1
    assert "Validation" not in out1


def test_validate_does_not_create_or_update_registry(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo(tmp_path / "plain-repo")
    write_minimal_capsule(repo, project_id="plain-repo")
    assert not (data_home / "project_registry.sqlite").exists()
    code = _run(monkeypatch, "validate-project", str(repo))
    assert code == 0
    assert not (data_home / "project_registry.sqlite").exists()


def test_validate_m2_error_surfaces(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(repo / ".research" / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "E_ACCEPTED_WITHOUT_HUMAN_REVIEW" in captured.out


def test_init_is_noninteractive(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code = _run(monkeypatch, "init-project", str(repo))
    assert code == 0
    assert (repo / ".research" / "project.yaml").is_file()


def test_init_registration_failure_keeps_capsule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = tmp_path / "xdg-data"
    data.write_text("not-a-directory\n", encoding="utf-8")
    monkeypatch.setenv("RESEARCH_OS_DATA_HOME", str(data))
    repo = make_git_repo(tmp_path / "sample-project")
    code = _run(monkeypatch, "init-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert (repo / ".research" / "project.yaml").is_file()
    assert "Capsule creation succeeded" in captured.err
    assert "register-project" in captured.err


def test_projects_empty_when_registry_missing(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(monkeypatch, "projects")
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert not (data_home / "project_registry.sqlite").exists()


def test_projects_deterministic_order(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    zebra = make_git_repo(tmp_path / "zebra-project")
    alpha = make_git_repo(tmp_path / "alpha-project")
    _run(monkeypatch, "init-project", str(zebra))
    _run(monkeypatch, "init-project", str(alpha))
    capsys.readouterr()
    code = _run(monkeypatch, "projects")
    captured = capsys.readouterr()
    assert code == 0
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert [line.split()[1] for line in lines] == ["alpha-project", "zebra-project"]


def test_status_counts_come_from_canonical_files(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    write_yaml(repo / ".research" / "questions" / "Q-0002.yaml", question_data(id="Q-0002"))
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "project_id: sample-project" in captured.out
    assert "question: 2" in captured.out
    assert "errors: 0" in captured.out
    assert "state.sqlite: absent" in captured.out
    assert not (repo / ".research" / "runtime").exists()


def test_status_missing_sqlite_is_absent_not_error(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "state.sqlite: absent" in captured.out
    assert "errors: 0" in captured.out


def test_status_does_not_use_registry_counts(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    (data_home / "project_registry.sqlite").unlink()
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "question: 1" in captured.out


def test_status_validation_counts_are_accurate(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(
        repo / ".research" / "questions" / "Q-0001.yaml",
        question_data(unknown_field="nope"),
    )
    write_yaml(
        repo / ".research" / "literature" / "note.yaml",
        {"id": "ignored"},
    )
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "errors: 1" in captured.out
    assert "warnings: 1" in captured.out


def test_status_is_read_only(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    before = snapshot_files(repo)
    code = _run(monkeypatch, "status", str(repo))
    assert code == 0
    assert snapshot_files(repo) == before
    assert not (repo / ".research" / "runtime").exists()


def test_usage_error_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["researchctl", "validate-project", "--bogus"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_existing_commands_still_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = _make_xdg_dirs(tmp_path)
    _apply_xdg(monkeypatch, mapping)
    assert _run(monkeypatch, "version") == 0
    assert capsys.readouterr().out.strip() == __version__
    assert _run(monkeypatch, "doctor") == 0

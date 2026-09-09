"""Tests for researchctl version and doctor."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from research_os import __version__, paths
from research_os import cli as cli_module
from research_os.cli import main
from research_os.errors import EXIT_ERROR
from research_os.registry import registry_path
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    make_git_repo,
    question_data,
    snapshot_files,
    write_bytes,
    write_minimal_capsule,
    write_reviewable_capsule,
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


def test_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert not registry_path().exists()
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
    assert not registry_path().exists()
    code = _run(monkeypatch, "validate-project", str(repo))
    assert code == 0
    assert not registry_path().exists()


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
    assert not registry_path().exists()


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
    write_yaml(
        repo / ".research" / "questions" / "Q-0002.yaml", question_data(id="Q-0002")
    )
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "project_id: sample-project" in captured.out
    assert "question: 2" in captured.out
    assert "errors: 0" in captured.out
    assert "sqlite" not in captured.out.lower()
    assert not (repo / ".research" / "runtime").exists()


def test_status_omits_state_sqlite(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status reports capsule facts only, not a removed materialized index."""

    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    capsys.readouterr()
    code = _run(monkeypatch, "status", str(repo))
    captured = capsys.readouterr()
    assert code == 0
    assert "errors: 0" in captured.out
    assert "state.sqlite" not in captured.out
    assert "sqlite" not in captured.out.lower()


def test_status_does_not_use_registry_counts(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _run(monkeypatch, "init-project", str(repo))
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    registry_path().unlink()
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


# --- strict UTF-8 at the CLI boundary -------------------------------------


def test_validate_project_non_utf8_exit_1_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    write_bytes(
        repo / ".research" / "questions" / "Q-0001.yaml",
        b"id: Q-0001\ntype: question\nschema_version: 1\nstatus: open\n"
        b"title: caf\xe9\nstatement: Why?\n",
    )
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "E_YAML_PARSE" in captured.out
    assert "not valid UTF-8" in captured.out
    assert captured.err == ""
    assert "Traceback" not in captured.out
    assert "UnicodeDecodeError" not in captured.out


# --- digest ---------------------------------------------------------------


def _digest_repo(tmp_path: Path) -> Path:
    repo = make_git_repo(tmp_path / "sample-project")
    write_reviewable_capsule(repo)
    return repo


def test_digest_matches_canonical_implementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_os.capsule import resolve_object, validate_project
    from research_os.digests import subject_digest

    repo = _digest_repo(tmp_path)
    code = _run(monkeypatch, "digest", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    report = validate_project(repo)
    expected = subject_digest(
        resolve_object(report, "CLAIM-0001"), project_id="sample-project"
    )
    assert code == 0
    assert captured.out.strip() == expected
    assert captured.err == ""


def test_digest_output_is_a_bare_single_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_os.models import DIGEST_RE

    repo = _digest_repo(tmp_path)
    _run(monkeypatch, "digest", "EVI-0001", str(repo))
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert DIGEST_RE.fullmatch(lines[0]) is not None


def test_digest_is_project_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reviewed scientific identity is project-local.

    The same Claim copied into another project must not inherit the first
    project's reviewed identity.
    """

    first = make_git_repo(tmp_path / "alpha-project")
    write_reviewable_capsule(first, project_id="alpha-project")
    second = make_git_repo(tmp_path / "beta-project")
    write_reviewable_capsule(second, project_id="beta-project")

    _run(monkeypatch, "digest", "CLAIM-0001", str(first))
    alpha = capsys.readouterr().out.strip()
    _run(monkeypatch, "digest", "CLAIM-0001", str(second))
    beta = capsys.readouterr().out.strip()
    assert alpha != beta


@pytest.mark.parametrize(
    ("object_id", "expected"),
    [
        ("CLAIM-1", "invalid object id"),
        ("CLAIM-0009", "no object CLAIM-0009"),
        ("REV-0001", "no object REV-0001"),
    ],
)
def test_digest_failure_cases_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    object_id: str,
    expected: str,
) -> None:
    repo = _digest_repo(tmp_path)
    code = _run(monkeypatch, "digest", object_id, str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert expected in captured.err
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_digest_of_an_existing_review_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Review is not a reviewable subject and has no semantic digest."""

    from research_os.capsule import validate_project
    from research_os.models import Verdict
    from research_os.review import build_review, build_review_packet, write_review

    repo = _digest_repo(tmp_path)
    packet = build_review_packet(validate_project(repo), "CLAIM-0001")
    write_review(
        packet, build_review(packet, verdict=Verdict.APPROVE, findings="Checked.")
    )
    code = _run(monkeypatch, "digest", "REV-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "have no semantic digest" in captured.err
    assert captured.out == ""


def test_digest_malformed_project_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    (repo / ".research" / "project.yaml").write_text(
        "id: Not A Slug\n", encoding="utf-8"
    )
    code = _run(monkeypatch, "digest", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "cannot determine project identity" in captured.err


def test_digest_does_not_write_anything(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _digest_repo(tmp_path)
    before = snapshot_files(repo)
    assert _run(monkeypatch, "digest", "CLAIM-0001", str(repo)) == 0
    assert snapshot_files(repo) == before
    assert not registry_path().exists()


# --- review ---------------------------------------------------------------
#
# Interactivity goes through cli._is_interactive(), which these tests set
# explicitly in both directions: nothing here depends on how the test runner
# happens to wire standard input.


def _answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> list[str]:
    """Feed scripted replies to the review prompts and record what was asked."""

    asked: list[str] = []
    pending = list(replies)

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _interactive(monkeypatch: pytest.MonkeyPatch, value: bool = True) -> None:
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: value)


def _review_repo(tmp_path: Path) -> Path:
    repo = make_git_repo(tmp_path / "sample-project")
    write_reviewable_capsule(repo)
    return repo


def _accept_claim(repo: Path) -> None:
    """Promote the Claim by hand: WP-B never mutates Claim status."""

    path = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "status: evidence_linked", "status: accepted"
        ),
        encoding="utf-8",
    )


def test_review_refuses_when_not_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command must not run unattended, and must not prompt either."""

    repo = _review_repo(tmp_path)
    before = snapshot_files(repo)
    _interactive(monkeypatch, False)

    def must_not_prompt(prompt: str = "") -> str:
        raise AssertionError("review prompted while not interactive")

    monkeypatch.setattr("builtins.input", must_not_prompt)
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "requires an interactive terminal" in captured.err
    assert captured.out == ""
    assert snapshot_files(repo) == before


def test_is_interactive_follows_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the helper to the real condition, not only to its own stub."""

    class Fake:
        def __init__(self, value: bool) -> None:
            self.value = value

        def isatty(self) -> bool:
            return self.value

    monkeypatch.setattr("sys.stdin", Fake(False))
    assert cli_module._is_interactive() is False
    monkeypatch.setattr("sys.stdin", Fake(True))
    assert cli_module._is_interactive() is True
    monkeypatch.setattr("sys.stdin", object())
    assert cli_module._is_interactive() is False


def test_review_subparser_has_no_bypass_flag() -> None:
    """No noninteractive approval path exists, by construction."""

    parser = cli_module._build_parser()
    actions = parser._subparsers._group_actions[0].choices["review"]._actions
    options = {option for action in actions for option in action.option_strings}
    assert options == {"-h", "--help"}
    assert [action.dest for action in actions if not action.option_strings] == [
        "object_id",
        "path",
    ]


def test_review_packet_shows_claim_evidence_and_full_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_os.capsule import validate_project
    from research_os.review import build_review_packet

    repo = _review_repo(tmp_path)
    packet = build_review_packet(validate_project(repo), "CLAIM-0001")
    _interactive(monkeypatch)
    _answers(monkeypatch, "cancel")
    _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    out = capsys.readouterr().out

    assert "Review packet" in out
    assert "sample-project" in out
    assert "CLAIM-0001" in out
    assert "X holds above 200 K for the studied material." in out
    assert "supporting evidence (2)" in out
    assert "contrary evidence (1)" in out
    assert "The contrary report used an uncalibrated probe." in out
    assert "HYP-0001" in out
    assert "doi:10.1000/example.12.345" in out
    assert "Fig. 3" in out
    assert "EXP-0001  (status: completed)" in out

    assert packet.claim_digest in out
    for digest in packet.evidence_digests.values():
        assert digest in out
    assert "all 3 evidence" in out

    assert "experiments (1)" in out
    assert "EXP-0001  completed" in out
    assert "Measure X between 180 K and 260 K." in out
    assert "hypotheses      HYP-0001" in out
    assert "primary_metrics x_amplitude" in out
    assert "Reject the hypothesis if x_amplitude stays below 0.1." in out
    assert "predictions (1)" in out
    assert "X is observed above 200 K." in out
    assert "git_commit    deadbeef" in out
    assert "Calibrated probe sweep" in out
    for digest in packet.experiment_digests.values():
        assert digest in out
    assert "all 1 experiment digest" in out


def test_review_approve_writes_review_and_gate_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import yaml

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "Checked the cited figure.", "", "y")
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()

    assert code == 0
    assert "Wrote .research/reviews/REV-0001.yaml" in captured.out
    assert captured.out.rstrip().endswith("OK")

    written = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert written["reviewer_kind"] == "human"
    assert written["status"] == "concluded"
    assert written["verdict"] == "approve"
    assert set(written["evidence_digests"]) == {"EVI-0001", "EVI-0002", "EVI-0003"}
    assert set(written["experiment_digests"]) == {"EXP-0001"}

    _accept_claim(repo)
    assert _run(monkeypatch, "validate-project", str(repo)) == 0


@pytest.mark.parametrize("verdict", ["revise", "reject"])
def test_review_revise_and_reject_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
) -> None:
    """A non-approving verdict is recorded, and does not satisfy the gate."""

    import yaml

    from research_os.errors import E_ACCEPTED_WITHOUT_HUMAN_REVIEW

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, verdict, "Needs a calibration check.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    capsys.readouterr()

    written = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert written["verdict"] == verdict
    assert written["status"] == "concluded"

    _accept_claim(repo)
    code = _run(monkeypatch, "validate-project", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in captured.out


@pytest.mark.parametrize(
    "replies",
    [
        ("cancel",),
        ("approve", "Checked.", "", "n"),
        ("approve", "Checked.", "", ""),
    ],
    ids=["cancel-at-verdict", "declined-confirmation", "empty-confirmation"],
)
def test_review_cancellation_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replies: tuple[str, ...],
) -> None:
    repo = _review_repo(tmp_path)
    before = snapshot_files(repo)
    _interactive(monkeypatch)
    _answers(monkeypatch, *replies)
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "cancelled: no review written" in captured.err
    assert snapshot_files(repo) == before
    assert not (repo / ".research" / "reviews" / "REV-0001.yaml").exists()


def test_review_end_of_input_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _review_repo(tmp_path)
    before = snapshot_files(repo)
    _interactive(monkeypatch)
    _answers(monkeypatch)
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    assert code == EXIT_ERROR
    assert "cancelled" in capsys.readouterr().err
    assert snapshot_files(repo) == before


def test_review_reprompts_on_unrecognized_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare 'r' is ambiguous between revise and reject, so it is re-asked."""

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    asked = _answers(monkeypatch, "maybe", "r", "approve", "Checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    captured = capsys.readouterr()
    assert asked.count("Verdict [approve/revise/reject/cancel]: ") == 3
    assert "please answer approve, revise, reject, or cancel" in captured.err


def test_review_reprompts_until_findings_are_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "", "   ", "", "Really checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    captured = capsys.readouterr()
    assert "findings are required" in captured.err
    assert "Wrote .research/reviews/REV-0001.yaml" in captured.out


def test_review_accepts_multiline_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import yaml

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "First line.", "Second line.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    capsys.readouterr()
    written = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert written["findings"] == "First line.\nSecond line."


def test_review_refuses_a_capsule_with_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _review_repo(tmp_path)
    write_yaml(
        repo / ".research" / "questions" / "Q-0002.yaml",
        question_data(id="Q-0002", unknown_field="nope"),
    )
    before = snapshot_files(repo)
    _interactive(monkeypatch)
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "E_SCHEMA" in captured.out
    assert "fix these errors before reviewing CLAIM-0001" in captured.err
    assert snapshot_files(repo) == before


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", "is draft"),
        ("withdrawn", "not open for review"),
    ],
)
def test_review_requires_an_evidence_linked_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected: str,
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_reviewable_capsule(repo, claim_status=status)
    before = snapshot_files(repo)
    _interactive(monkeypatch)
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert expected in captured.err
    assert snapshot_files(repo) == before


def test_review_refuses_a_non_claim_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    code = _run(monkeypatch, "review", "Q-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "reviews of claims only" in captured.err


def test_review_does_not_change_claim_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _review_repo(tmp_path)
    claim = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    before = claim.read_bytes()
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "Checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    capsys.readouterr()
    assert claim.read_bytes() == before


def test_review_refuses_a_validly_accepted_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R0 acceptance is existential, so a later verdict cannot override it.

    Recorded through the command first, because a Claim written straight to
    accepted is itself a validation error and would be refused by the
    errors-first rule instead.
    """

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "Checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    _accept_claim(repo)
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    before = snapshot_files(repo)

    _answers(monkeypatch, "reject", "Changed my mind.", "", "y")
    code = _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "already accepted" in captured.err
    assert "set status back to evidence_linked" in captured.err
    assert snapshot_files(repo) == before


def test_re_review_after_resetting_status_binds_the_same_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented re-review round trip.

    Claim status is outside the semantic projection, so accepted ->
    evidence_linked -> accepted does not change the reviewed identity, and the
    second review binds exactly the digest the first one did.
    """

    import yaml

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "First pass.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    _accept_claim(repo)

    claim = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    claim.write_text(
        claim.read_text(encoding="utf-8").replace(
            "status: accepted", "status: evidence_linked"
        ),
        encoding="utf-8",
    )
    _answers(monkeypatch, "approve", "Second pass.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    _accept_claim(repo)
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()

    reviews = repo / ".research" / "reviews"
    first = yaml.safe_load((reviews / "REV-0001.yaml").read_text(encoding="utf-8"))
    second = yaml.safe_load((reviews / "REV-0002.yaml").read_text(encoding="utf-8"))
    assert second["subject_digest"] == first["subject_digest"]
    assert second["evidence_digests"] == first["evidence_digests"]
    assert second["findings"] == "Second pass."


def test_review_packet_renders_no_experiments_for_a_literature_only_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A claim resting on literature alone binds an empty experiment map."""

    import yaml

    from research_os.capsule import validate_project
    from research_os.review import build_review_packet

    repo = _review_repo(tmp_path)
    claim_path = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    claim_path.write_text(
        claim_path.read_text(encoding="utf-8").replace("\n- EVI-0003", ""),
        encoding="utf-8",
    )

    packet = build_review_packet(validate_project(repo), "CLAIM-0001")
    assert packet.experiment_digests == {}

    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "Literature only.", "", "y")
    _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    out = capsys.readouterr().out

    assert "experiments (none)" in out
    assert "all 0 experiment digests" in out

    written = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert written["experiment_digests"] == {}

    _accept_claim(repo)
    assert _run(monkeypatch, "validate-project", str(repo)) == 0


def test_review_then_experiment_edit_invalidates_the_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: approve, accept, then change only the Experiment.

    The Claim and every Evidence object are untouched, so nothing but the
    experiment binding can catch this.
    """

    repo = _review_repo(tmp_path)
    _interactive(monkeypatch)
    _answers(monkeypatch, "approve", "Checked the sweep.", "", "y")
    _run(monkeypatch, "review", "CLAIM-0001", str(repo))
    capsys.readouterr()

    _accept_claim(repo)
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    assert capsys.readouterr().out.strip() == "OK"

    manifest = repo / ".research" / "experiments" / "EXP-0001" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "Reject the hypothesis if x_amplitude stays below 0.1.",
            "Reject the hypothesis if x_amplitude stays below 0.4.",
        ),
        encoding="utf-8",
    )

    assert _run(monkeypatch, "validate-project", str(repo)) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "W_STALE_EXPERIMENT_DIGEST" in out
    assert "E_STALE_REVIEW_DIGEST" in out
    assert "W_STALE_SUBJECT_DIGEST" not in out
    assert "W_STALE_EVIDENCE_DIGEST" not in out

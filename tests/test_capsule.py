"""Tests for Research Capsule discovery, YAML safety, and project validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_os.capsule import (
    CAPSULE_GITIGNORE,
    CHARTER_TEMPLATE,
    STATE_TEMPLATE,
    init_project,
    load_project_identity,
    resolve_object,
    validate_project,
)
from research_os.digests import subject_digest
from research_os.errors import (
    E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
    E_DUPLICATE_YAML_KEY,
    E_FILENAME_ID_MISMATCH,
    E_MISSING_CAPSULE_FILE,
    E_SCHEMA,
    E_STALE_REVIEW_DIGEST,
    E_UNEXPECTED_FILE,
    E_UNSAFE_PATH,
    E_WRONG_OBJECT_DIRECTORY,
    E_YAML_PARSE,
    W_RESERVED_DIRECTORY,
    CapsuleError,
    CapsuleExistsError,
    InvalidIdError,
    NotAGitRepositoryError,
    ProjectIdRequiredError,
    Severity,
)
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    experiment_data,
    idea_data,
    make_git_repo,
    question_data,
    snapshot_files,
    write_bytes,
    write_minimal_capsule,
    write_text,
    write_yaml,
)
from tests.helpers import (
    OTHER_PROJECT_ID,
    make_claim,
    make_evidence,
    make_review,
)


def _codes(report) -> set[str]:
    return set(
        report.codes()
        if hasattr(report, "codes")
        else (item.code for item in report.findings)
    )


def test_non_git_path_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with pytest.raises(NotAGitRepositoryError):
        validate_project(empty)


def test_nested_path_resolves_to_git_root(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    git_root, _project = init_project(nested)
    assert git_root == repo.resolve()
    report = validate_project(nested)
    assert report.git_root == repo.resolve()
    assert report.ok


def test_existing_research_prevents_init_without_overwrite(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    with pytest.raises(CapsuleExistsError):
        init_project(repo)
    assert (repo / ".research" / "project.yaml").is_file()


def test_root_gitignore_remains_unchanged(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    root_ignore = repo / ".gitignore"
    root_ignore.write_text("venv/\n", encoding="utf-8")
    init_project(repo)
    assert root_ignore.read_text(encoding="utf-8") == "venv/\n"


def test_capsule_gitignore_contains_runtime(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    assert (repo / ".research" / ".gitignore").read_text(
        encoding="utf-8"
    ) == CAPSULE_GITIGNORE


def test_init_writes_valid_project_yaml(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _root, project = init_project(repo, title="Sample")
    loaded = yaml.safe_load(
        (repo / ".research" / "project.yaml").read_text(encoding="utf-8")
    )
    assert loaded["id"] == "sample-project"
    assert loaded["title"] == "Sample"
    assert loaded["capsule_version"] == 1
    assert loaded["status"] == "active"
    assert project.id == "sample-project"


def test_charter_and_state_created(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    assert (repo / ".research" / "CHARTER.md").read_text(
        encoding="utf-8"
    ) == CHARTER_TEMPLATE
    assert (repo / ".research" / "STATE.md").read_text(
        encoding="utf-8"
    ) == STATE_TEMPLATE


def test_runtime_and_reserved_dirs_not_created(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    capsule = repo / ".research"
    assert not (capsule / "runtime").exists()
    assert not (capsule / "literature").exists()
    assert not (capsule / "work_orders").exists()
    assert not (capsule / "handoffs").exists()
    assert not (capsule / "questions").exists()


def test_invalid_repository_basename_requires_id(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "MyRepo")
    with pytest.raises(ProjectIdRequiredError):
        init_project(repo)
    assert not (repo / ".research").exists()


def test_explicit_valid_id_works(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "MyRepo")
    _root, project = init_project(repo, project_id="explicit-id")
    assert project.id == "explicit-id"
    assert (
        yaml.safe_load((repo / ".research" / "project.yaml").read_text())["id"]
        == "explicit-id"
    )


def test_title_defaults_to_basename(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "MyRepo")
    _root, project = init_project(repo, project_id="explicit-id")
    assert project.title == "MyRepo"


def test_explicit_title(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _root, project = init_project(repo, title="Custom Title")
    assert project.title == "Custom Title"


def test_missing_typed_directory_is_zero_objects(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    report = validate_project(repo)
    assert report.ok
    assert report.objects == ()


def test_valid_object_file_loads(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    report = validate_project(repo)
    assert report.ok
    assert [obj.id for obj in report.objects] == ["Q-0001"]


def test_filename_id_mismatch_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0002.yaml",
        claim_data(id="CLAIM-0003"),
    )
    report = validate_project(repo)
    assert E_FILENAME_ID_MISMATCH in _codes(report)
    assert report.objects == ()


def test_directory_type_mismatch_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "claims" / "HYP-0001.yaml",
        {
            "id": "HYP-0001",
            "type": "hypothesis",
            "schema_version": 1,
            "status": "draft",
            "title": "Misplaced",
            "statement": "This is in the wrong directory.",
        },
    )
    report = validate_project(repo)
    assert E_WRONG_OBJECT_DIRECTORY in _codes(report)
    assert report.objects == ()


def test_incorrectly_named_yaml_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "foo.yaml", question_data())
    report = validate_project(repo)
    assert E_FILENAME_ID_MISMATCH in _codes(report)
    assert report.objects == ()


def test_experiment_directory_manifest_id_mismatch(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "experiments" / "EXP-0001" / "manifest.yaml",
        experiment_data(id="EXP-0002"),
    )
    report = validate_project(repo)
    assert E_FILENAME_ID_MISMATCH in _codes(report)
    assert report.objects == ()


def test_malformed_yaml_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_text(repo / ".research" / "questions" / "Q-0001.yaml", "id: [unterminated\n")
    report = validate_project(repo)
    assert E_YAML_PARSE in _codes(report)
    assert report.objects == ()


def test_non_mapping_yaml_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_text(repo / ".research" / "questions" / "Q-0001.yaml", "- just a list\n")
    report = validate_project(repo)
    assert E_YAML_PARSE in _codes(report)
    assert report.objects == ()


def test_duplicate_yaml_mapping_key_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_text(
        repo / ".research" / "questions" / "Q-0001.yaml",
        (
            "id: Q-0001\n"
            "type: question\n"
            "schema_version: 1\n"
            "status: open\n"
            "status: answered\n"
            "title: A question\n"
            "statement: Why?\n"
        ),
    )
    report = validate_project(repo)
    assert E_DUPLICATE_YAML_KEY in _codes(report)
    assert report.objects == ()


def test_unknown_schema_field_is_error(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "questions" / "Q-0001.yaml",
        question_data(unknown_field="nope"),
    )
    report = validate_project(repo)
    assert E_SCHEMA in _codes(report)
    assert report.objects == ()


def test_reserved_future_directory_content_is_warning(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "literature" / "paper.yaml",
        {"id": "PAPER-1"},
    )
    report = validate_project(repo)
    assert report.ok
    assert W_RESERVED_DIRECTORY in _codes(report)
    assert report.objects == ()


def test_unsupported_yml_extension_is_not_ignored(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "Q-0001.yml", question_data())
    report = validate_project(repo)
    assert E_UNEXPECTED_FILE in _codes(report)
    assert report.objects == ()


def test_nested_yaml_in_typed_directory_is_not_ignored(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "questions" / "nested" / "Q-0001.yaml",
        question_data(),
    )
    report = validate_project(repo)
    assert E_UNEXPECTED_FILE in _codes(report)
    assert report.objects == ()


def test_runtime_is_ignored(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "runtime" / "sneaky.yaml",
        question_data(id="Q-9999"),
    )
    report = validate_project(repo)
    assert report.ok
    assert report.objects == ()
    assert not any("runtime" in (item.source or "") for item in report.findings)


def test_escaping_symlink_rejected(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    outside = tmp_path / "outside.yaml"
    write_yaml(outside, question_data())
    target = repo / ".research" / "questions"
    target.mkdir()
    (target / "Q-0001.yaml").symlink_to(outside)
    report = validate_project(repo)
    assert E_UNSAFE_PATH in _codes(report)
    assert report.objects == ()


def test_escaping_directory_symlink_rejected(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    outside_dir = tmp_path / "outside-questions"
    write_yaml(outside_dir / "Q-0001.yaml", question_data())
    (repo / ".research" / "questions").symlink_to(outside_dir)
    report = validate_project(repo)
    assert E_UNSAFE_PATH in _codes(report)
    assert report.objects == ()


def test_malformed_files_do_not_enter_validate_objects(
    tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    write_text(
        repo / ".research" / "questions" / "Q-0002.yaml",
        "status: open\nstatus: paused\n",
    )
    seen: list[str] = []

    def capture(objects, *, project_id):
        seen.extend(obj.id for obj in objects)
        from research_os.validate import validate_objects as original

        return original(objects, project_id=project_id)

    monkeypatch.setattr("research_os.capsule.validate_objects", capture)
    report = validate_project(repo)
    assert "Q-0001" in seen
    assert "Q-0002" not in seen
    assert E_DUPLICATE_YAML_KEY in _codes(report)


def test_accepted_claim_without_human_review_surfaces_m2_error(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    report = validate_project(repo)
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in _codes(report)
    assert not report.ok


def test_old_flat_claim_evidence_field_fails_loudly(
    tmp_path: Path, data_home: Path
) -> None:
    """A pre-WP-A ``evidence:`` key is a hard schema error, never ignored."""

    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", evidence=["EVI-0001"]),
    )
    report = validate_project(repo)
    assert E_SCHEMA in _codes(report)
    assert not report.ok


def test_project_identity_scopes_the_review_digest(
    tmp_path: Path, data_home: Path
) -> None:
    """A review digested under another project id does not validate here."""

    repo = make_git_repo(tmp_path / "sample-project")
    _, project = init_project(repo)
    evidence = make_evidence()
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        statement="X is supported.",
    )
    review_for = {
        scope: make_review(
            status="concluded",
            subject=claim.id,
            reviewer_kind="human",
            verdict="approve",
            findings="Reviewed.",
            subject_digest=subject_digest(claim, project_id=scope),
            evidence_digests={"EVI-0001": subject_digest(evidence, project_id=scope)},
        )
        for scope in (project.id, OTHER_PROJECT_ID)
    }
    write_yaml(
        repo / ".research" / "evidence" / "EVI-0001.yaml",
        evidence.model_dump(mode="json", exclude_none=True),
    )
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim.model_dump(mode="json", exclude_none=True),
    )

    reviews = repo / ".research" / "reviews" / "REV-0001.yaml"
    write_yaml(
        reviews,
        review_for[project.id].model_dump(mode="json", exclude_none=True),
    )
    assert validate_project(repo).ok

    write_yaml(
        reviews,
        review_for[OTHER_PROJECT_ID].model_dump(mode="json", exclude_none=True),
    )
    foreign = validate_project(repo)
    assert not foreign.ok
    assert E_STALE_REVIEW_DIGEST in _codes(foreign)


def test_invalid_project_yaml_does_not_run_weakened_object_validation(
    tmp_path: Path, data_home: Path
) -> None:
    """Object validation is project-scoped, so it is skipped, never weakened."""

    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    write_text(repo / ".research" / "project.yaml", "id: Not A Slug\n")
    report = validate_project(repo)
    assert report.project is None
    assert not report.ok
    assert E_SCHEMA in _codes(report)
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW not in _codes(report)


def test_warning_does_not_fail_validation(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "ideas" / "IDEA-0001.yaml", idea_data(status="promoted")
    )
    report = validate_project(repo)
    assert report.ok
    assert report.warnings


def test_missing_required_capsule_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    (repo / ".research" / "STATE.md").unlink()
    report = validate_project(repo)
    assert E_MISSING_CAPSULE_FILE in _codes(report)
    assert not report.ok


def test_validate_does_not_modify_canonical_files(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    before = snapshot_files(repo / ".research")
    validate_project(repo)
    after = snapshot_files(repo / ".research")
    assert after == before


def test_validate_does_not_create_runtime(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    validate_project(repo)
    assert not (repo / ".research" / "runtime").exists()


def test_markdown_in_object_directory_is_warning(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(repo / ".research" / "questions" / "Q-0001.yaml", question_data())
    write_text(repo / ".research" / "questions" / "notes.md", "# Notes\n")
    report = validate_project(repo)
    assert report.ok
    assert any(item.severity is Severity.WARNING for item in report.findings)


def test_valid_experiment_loads(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    init_project(repo)
    write_yaml(
        repo / ".research" / "experiments" / "EXP-0001" / "manifest.yaml",
        experiment_data(),
    )
    report = validate_project(repo)
    assert report.ok
    assert [obj.id for obj in report.objects] == ["EXP-0001"]


# --- strict UTF-8 boundary ------------------------------------------------
#
# Canonical YAML is UTF-8. Bytes that are not must surface as an ordinary
# finding: before this was fixed, UnicodeDecodeError (a ValueError, not an
# OSError) escaped every boundary in the loader and printed a traceback.
#
# These repositories deliberately contain a non-UTF-8 file, so none of them may
# be passed to snapshot_files, which reads every file as UTF-8 text.

LATIN1_QUESTION = (
    b"id: Q-0001\n"
    b"type: question\n"
    b"schema_version: 1\n"
    b"status: open\n"
    b"title: caf\xe9 latin-1\n"
    b"statement: Why does this happen?\n"
)


def test_non_utf8_object_file_is_yaml_parse_error(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    write_bytes(repo / ".research" / "questions" / "Q-0001.yaml", LATIN1_QUESTION)
    report = validate_project(repo)
    assert not report.ok
    assert E_YAML_PARSE in _codes(report)
    assert report.objects == ()
    finding = next(item for item in report.findings if item.code == E_YAML_PARSE)
    assert finding.source == ".research/questions/Q-0001.yaml"
    assert "not valid UTF-8" in finding.message
    assert "invalid continuation byte" in finding.message


def test_non_utf8_project_yaml_is_error_and_skips_object_validation(
    tmp_path: Path,
) -> None:
    """A project whose identity cannot be read must not validate objects.

    Cross-object validation is project-scoped, so running it without identity
    would silently drop the review and digest guarantees.
    """

    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    write_yaml(
        repo / ".research" / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    write_bytes(
        repo / ".research" / "project.yaml",
        b"id: sample-project\ntitle: caf\xe9\ncapsule_version: 1\nstatus: active\n",
    )
    report = validate_project(repo)
    assert not report.ok
    assert report.project is None
    assert E_YAML_PARSE in _codes(report)
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW not in _codes(report)


def test_non_utf8_project_yaml_registration_fails_cleanly(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    write_bytes(
        repo / ".research" / "project.yaml",
        b"id: sample-project\ntitle: caf\xe9\ncapsule_version: 1\nstatus: active\n",
    )
    with pytest.raises(CapsuleError) as exc:
        load_project_identity(repo)
    assert not isinstance(exc.value, UnicodeDecodeError)
    assert ".research/project.yaml" in str(exc.value)
    assert "not valid UTF-8" in str(exc.value)


def test_non_utf8_bytes_are_never_reinterpreted(tmp_path: Path) -> None:
    """The loader must not fall back to another encoding.

    0xe9 is a valid Latin-1 'e-acute'. Decoding it that way would let malformed
    canonical files parse into subtly wrong science, so it stays an error.
    """

    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    write_bytes(repo / ".research" / "questions" / "Q-0001.yaml", LATIN1_QUESTION)
    report = validate_project(repo)
    assert report.objects == ()
    assert not any("café" in (item.message or "") for item in report.findings)


# --- resolve_object -------------------------------------------------------


def _capsule_with_claim(repo: Path) -> None:
    write_minimal_capsule(repo)
    write_yaml(repo / ".research" / "claims" / "CLAIM-0001.yaml", claim_data())


def test_resolve_object_returns_the_parsed_object(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _capsule_with_claim(repo)
    obj = resolve_object(validate_project(repo), "CLAIM-0001")
    assert obj.id == "CLAIM-0001"


def test_resolve_object_rejects_malformed_id(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _capsule_with_claim(repo)
    with pytest.raises(InvalidIdError):
        resolve_object(validate_project(repo), "CLAIM-1")


def test_resolve_object_rejects_unknown_object(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _capsule_with_claim(repo)
    with pytest.raises(CapsuleError, match="no object CLAIM-0009"):
        resolve_object(validate_project(repo), "CLAIM-0009")


def test_resolve_object_rejects_missing_project_identity(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    _capsule_with_claim(repo)
    write_text(repo / ".research" / "project.yaml", "id: Not A Slug\n")
    with pytest.raises(CapsuleError, match="cannot determine project identity"):
        resolve_object(validate_project(repo), "CLAIM-0001")


def test_resolve_object_rejects_duplicate_id(tmp_path: Path) -> None:
    """A duplicated id must fail loudly rather than resolve arbitrarily.

    Cross-object validation drops such ids from its own map, but both copies
    remain in report.objects, so the lookup has to detect the multiplicity
    itself. Filename/id binding makes this unreachable from a capsule on disk --
    two files cannot share a stem in one directory, and a mismatched stem is
    already an error that loads no object -- so the guard is exercised against a
    constructed report, which is all resolve_object reads.
    """

    from research_os.capsule import ProjectValidationReport
    from research_os.models import Project
    from tests.helpers import make_claim

    project = Project.model_validate(
        {
            "id": "sample-project",
            "title": "sample-project",
            "capsule_version": 1,
            "status": "active",
        }
    )
    report = ProjectValidationReport(
        git_root=tmp_path,
        capsule=tmp_path / ".research",
        project=project,
        objects=(make_claim(), make_claim(statement="A different statement.")),
    )
    with pytest.raises(CapsuleError, match="duplicate object id CLAIM-0001"):
        resolve_object(report, "CLAIM-0001")

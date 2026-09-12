"""Evidence-grounded writing, and what stops a manuscript drifting from its sources.

A writer is the worker with the most opportunity to do quiet damage. Everything
else produces a diff or a structured proposal; a manuscript is prose, where a
claim can be strengthened, a contrary result dropped, or a citation invented
without any of it showing up as a change to anything structured.

These tests run against a real capsule whose accepted Claim is accepted the way
R0 requires -- a concluded human approve Review binding the claim digest, both
evidence digests, and the experiment digest -- because a packet builder tested
against a faked acceptance would test nothing.

What they pin:

* only accepted, currently-approved Claims reach the writer, and the withheld
  ones are reported rather than silently dropped;
* a fabricated citation is a set difference and is caught as one;
* a draft that omits contrary evidence is refused;
* the writer is isolated, cannot reach ``.research/``, and nothing is merged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_os.automation.models import Access, Role
from research_os.errors import (
    PaperManifestError,
    PaperPacketError,
    PaperWritingError,
)
from research_os.literature.models import AuthorRecord
from research_os.literature.store import LiteratureStore
from research_os.paper.checks import check_draft
from research_os.paper.controller import PaperController
from research_os.paper.models import (
    DraftRecord,
    SectionKind,
    SourceManifest,
    WritingVerdict,
)
from research_os.paper.packet import build_source_packet, render_source_packet
from research_os.paper.report import render_draft
from research_os.paper.store import DraftStore
from research_os.paper.writer import build_writer_prompt, parse_manifest
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.paper_helpers import (
    GROUNDED_RESULTS,
    OMITS_CONTRARY,
    OVERREACHING_RESULTS,
    PROJECT_ID,
    fake_config,
    init_paper_project,
    manifest_payload,
)


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    for name, subdirectory in (
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
        ("RESEARCH_OS_STATE_HOME", "state"),
    ):
        path = root / subdirectory
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return root / "state"


def seeded_literature() -> LiteratureStore:
    store = LiteratureStore.open_memory()
    store.ingest(
        provider="test",
        payload={},
        fields={
            "title": "Widget deformation under load",
            "abstract": "A survey of widget mechanics.",
            "publication_year": 2021,
            "venue": "Journal of Widgets",
        },
        identifiers={"doi": "10.1000/widget"},
        authors=[AuthorRecord(position=0, name="Jane Roe")],
    )
    return store


def review_payload(verdict: str = "PASS", findings: list[dict] | None = None) -> dict:
    return {
        "verdict": verdict,
        "summary": f"scripted {verdict}",
        "findings": findings or [],
    }


def writer_provider(
    *,
    drafts: list[str],
    manifests: list[dict] | None = None,
    reviews: list[dict] | None = None,
    path: str = "paper/manuscript.md",
) -> FakeProvider:
    """A provider that writes real prose into the worktree and returns a manifest."""

    declared = manifests or [manifest_payload()] * len(drafts)
    return FakeProvider(
        responses={
            str(Role.CODER): [
                ScriptedResponse(
                    structured=declared[index],
                    text="drafted",
                    write_files={path: text},
                )
                for index, text in enumerate(drafts)
            ],
            str(Role.REVIEWER): [
                ScriptedResponse(structured=item)
                for item in (reviews or [review_payload()])
            ],
        }
    )


# -- what reaches the writer --------------------------------------------------


def test_only_an_accepted_and_currently_approved_claim_reaches_the_writer(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    packet = build_source_packet(project)

    assert [item.claim_id for item in packet.claims] == ["CLAIM-0001"]
    assert packet.claims[0].approval_review_id == "REV-0001"
    assert "CLAIM-0002" in packet.excluded_claims
    assert "not accepted" in packet.excluded_claims["CLAIM-0002"]


def test_a_claim_whose_approval_went_stale_is_withheld(
    research_home: Path, tmp_path: Path
) -> None:
    """Science that changed after it was approved must not be quoted."""

    project = init_paper_project(tmp_path / "stale", stale_approval=True)

    packet = build_source_packet(project)

    assert packet.claims == []
    assert "CLAIM-0001" in packet.excluded_claims


def test_a_withheld_claim_is_reported_rather_than_silently_dropped(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    packet = build_source_packet(project, claim_ids=["CLAIM-0002", "CLAIM-9999"])

    assert packet.claims == []
    assert set(packet.excluded_claims) == {"CLAIM-0002", "CLAIM-9999"}
    assert "no object with that id" in packet.excluded_claims["CLAIM-9999"]
    rendered = render_source_packet(packet)
    assert "asked for and withheld" in rendered
    assert "Do not write around these" in rendered


def test_contrary_evidence_travels_with_its_claim_marked(
    research_home: Path, tmp_path: Path
) -> None:
    """Omitting it would be the most damaging thing this pipeline could do."""

    project = init_paper_project(tmp_path / "project")

    packet = build_source_packet(project)

    contrary = packet.contrary_evidence
    assert [item.evidence_id for item in contrary] == ["EVI-0002"]
    rendered = render_source_packet(packet)
    assert "CONTRARY EVIDENCE: EVI-0002" in rendered
    assert "must not be omitted" in rendered


def test_experiment_provenance_travels_with_the_evidence(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    packet = build_source_packet(project)

    experiment = packet.experiments[0]
    assert experiment.experiment_id == "EXP-0001"
    assert experiment.provenance["git_commit"] == "abc1234"
    assert experiment.decision_rule
    assert "3 percent" in render_source_packet(packet)


def test_literature_arrives_with_a_generated_citation_key(
    research_home: Path, tmp_path: Path
) -> None:
    """Which turns "did this citation exist" into a set membership test."""

    project = init_paper_project(tmp_path / "project")

    packet = build_source_packet(
        project,
        literature_keys=["doi:10.1000/widget"],
        literature_store=seeded_literature(),
    )

    entry = packet.literature[0]
    assert entry.citation_key.startswith("roe2021")
    assert entry.work_key == "doi:10.1000/widget"
    assert f"cite as: [{entry.citation_key}]" in render_source_packet(packet)


def test_a_work_that_is_not_indexed_cannot_be_offered_for_citation(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    with pytest.raises(PaperPacketError, match="Retrieve it first"):
        build_source_packet(
            project,
            literature_keys=["doi:10.1038/never-retrieved"],
            literature_store=LiteratureStore.open_memory(),
        )


def test_a_project_with_no_capsule_has_nothing_to_write_from(
    research_home: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(bare), check=True)
    (bare / "README.md").write_text("x\n", encoding="utf-8")

    with pytest.raises(PaperPacketError, match="no accepted"):
        build_source_packet(bare)


def test_the_prompt_says_the_sources_are_exhaustive(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)

    prompt = build_writer_prompt(
        section=SectionKind.RESULTS,
        instruction="Write the results section.",
        packet=packet,
        allowed_paths=["paper/manuscript.md"],
    )

    assert "This is everything" in prompt
    assert "Do not state anything stronger than the claim it comes from" in prompt
    assert "fabricated citation" in prompt
    assert 'Do not edit anything under ".research/"' in prompt


# -- the deterministic checks -------------------------------------------------


def manifest(payload: dict | None = None) -> SourceManifest:
    return SourceManifest.model_validate(
        {
            **(payload or manifest_payload()),
            "draft_id": "DRAFT-20260912T101500Z-0a1b2c3d",
            "section": SectionKind.RESULTS,
        }
    )


def test_a_grounded_draft_passes_every_check(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)

    report = check_draft(
        packet=packet,
        manifest=manifest(),
        written={"paper/manuscript.md": GROUNDED_RESULTS},
        section=SectionKind.RESULTS,
    )

    assert report.grounded
    assert report.issues == []
    assert set(report.referenced_object_ids) == {
        "CLAIM-0001",
        "EVI-0001",
        "EVI-0002",
        "EXP-0001",
    }


def test_a_fabricated_citation_is_a_blocker(
    research_home: Path, tmp_path: Path
) -> None:
    """The citation key is generated by the packet, so this is a set difference."""

    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)

    report = check_draft(
        packet=packet,
        manifest=manifest(),
        written={"paper/manuscript.md": OVERREACHING_RESULTS},
        section=SectionKind.RESULTS,
    )

    assert not report.grounded
    citation = next(
        item for item in report.blockers if item.check == "citations_resolve"
    )
    assert "smith2019fabricated" in citation.detail


def test_a_draft_that_omits_contrary_evidence_is_a_blocker(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)

    report = check_draft(
        packet=packet,
        manifest=manifest(manifest_payload(evidence_ids=["EVI-0001"])),
        written={"paper/manuscript.md": OMITS_CONTRARY},
        section=SectionKind.RESULTS,
    )

    assert not report.grounded
    omission = next(
        item for item in report.blockers if item.check == "contrary_evidence_is_kept"
    )
    assert "EVI-0002" in omission.detail


def test_a_manifest_naming_something_it_was_not_given_is_a_blocker(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)

    report = check_draft(
        packet=packet,
        manifest=manifest(manifest_payload(claim_ids=["CLAIM-0001", "CLAIM-0999"])),
        written={"paper/manuscript.md": GROUNDED_RESULTS},
        section=SectionKind.RESULTS,
    )

    assert not report.grounded
    assert any("CLAIM-0999" in item.detail for item in report.blockers)


def test_an_object_id_that_was_not_supplied_is_a_blocker(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)
    prose = GROUNDED_RESULTS + "\nSee also CLAIM-0042 for the general case.\n"

    report = check_draft(
        packet=packet,
        manifest=manifest(),
        written={"paper/manuscript.md": prose},
        section=SectionKind.RESULTS,
    )

    assert not report.grounded
    assert any("CLAIM-0042" in item.detail for item in report.blockers)


def test_a_number_with_no_source_is_reported(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)
    prose = GROUNDED_RESULTS.replace("1.4\npercent", "1.4 percent").replace(
        "varied by 1.4", "varied by 0.37"
    )

    report = check_draft(
        packet=packet,
        manifest=manifest(),
        written={"paper/manuscript.md": prose},
        section=SectionKind.RESULTS,
    )

    assert "0.37" in report.unsupported_numbers
    numeric = next(
        item for item in report.issues if item.check == "numbers_are_supported"
    )
    assert numeric.severity == "major", "a results section is where this matters"


def test_years_and_small_integers_are_not_reported_as_unsupported(
    research_home: Path, tmp_path: Path
) -> None:
    """Otherwise the numeric check would be too noisy to read."""

    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)
    prose = "## Section 2\n\nIn 1998 and again in 2021, three studies looked at this.\n"

    report = check_draft(
        packet=packet,
        manifest=manifest(manifest_payload(claim_ids=[], evidence_ids=[])),
        written={"paper/manuscript.md": prose},
        section=SectionKind.DISCUSSION,
    )

    assert report.unsupported_numbers == []


def test_citing_retracted_work_is_allowed_but_flagged(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    store = seeded_literature()
    store.ingest(
        provider="crossref",
        payload={},
        fields={
            "title": "Widget deformation under load",
            "is_retracted": True,
            "retraction_note": "retraction by 10.1000/notice",
        },
        identifiers={"doi": "10.1000/widget"},
        authors=[AuthorRecord(position=0, name="Jane Roe")],
    )
    packet = build_source_packet(
        project, literature_keys=["doi:10.1000/widget"], literature_store=store
    )
    key = packet.literature[0].citation_key
    prose = GROUNDED_RESULTS + f"\nThis matches earlier work [@{key}].\n"

    report = check_draft(
        packet=packet,
        manifest=manifest(manifest_payload(citation_keys=[key])),
        written={"paper/manuscript.md": prose},
        section=SectionKind.RESULTS,
    )

    assert report.grounded, "citing retracted work is legitimate"
    flagged = next(
        item
        for item in report.issues
        if item.check == "citations_resolve" and item.severity == "major"
    )
    assert key in flagged.detail


def test_a_writer_that_returns_no_manifest_fails_the_task() -> None:
    with pytest.raises(PaperManifestError, match="no source manifest"):
        parse_manifest(
            structured=None,
            text="I have written a lovely results section for you.",
            draft_id="DRAFT-20260912T101500Z-0a1b2c3d",
            section=SectionKind.RESULTS,
        )


# -- through the controller ---------------------------------------------------


def write(
    project: Path,
    *,
    drafts: list[str],
    manifests: list[dict] | None = None,
    reviews: list[dict] | None = None,
    paths: list[str] | None = None,
    allow_repair: bool = True,
    literature_keys: list[str] | None = None,
):
    packet = build_source_packet(
        project,
        literature_keys=literature_keys,
        literature_store=seeded_literature() if literature_keys else None,
        limitations=["the bench setup covers only one widget geometry"],
    )
    provider = writer_provider(drafts=drafts, manifests=manifests, reviews=reviews)
    controller = PaperController(
        providers={"fake": provider},
        config=fake_config(),
        allow_repair=allow_repair,
    )
    outcome = controller.write(
        project_path=project,
        section=SectionKind.RESULTS,
        instruction="Write the results section from the accepted claim.",
        packet=packet,
        allowed_paths=paths or ["paper/manuscript.md"],
    )
    return outcome, provider


def test_a_grounded_draft_reaches_ready_for_human(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(project, drafts=[GROUNDED_RESULTS])

    assert outcome.ready_for_human
    assert outcome.draft.grounding is not None and outcome.draft.grounding.grounded
    assert outcome.draft.review is not None
    assert outcome.draft.review.verdict is WritingVerdict.PASS
    assert outcome.draft.changed_paths == ["paper/manuscript.md"]
    assert outcome.model_calls == 2


def test_nothing_is_merged_and_the_project_is_untouched(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    before = (project / "paper" / "manuscript.md").read_bytes()

    outcome, _ = write(project, drafts=[GROUNDED_RESULTS])

    assert (project / "paper" / "manuscript.md").read_bytes() == before
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == "", "the researcher's checkout was not touched"
    rendered = render_draft(outcome.draft, outcome.packet)
    assert "Nothing has been merged" in rendered
    assert "No Claim" in rendered


def test_the_writer_works_in_an_isolated_worktree(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, provider = write(project, drafts=[GROUNDED_RESULTS])

    worktree = Path(outcome.draft.worktree_path or "")
    assert worktree.is_dir()
    assert worktree != project
    assert project not in worktree.parents
    writer_calls = provider.requests_for(Role.CODER)
    assert writer_calls
    assert Path(writer_calls[0].cwd) == worktree
    assert writer_calls[0].access is Access.ISOLATED_WRITE


def test_the_reviewer_is_read_only_and_outside_the_repository(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    _, provider = write(project, drafts=[GROUNDED_RESULTS])

    reviewer_calls = provider.requests_for(Role.REVIEWER)
    assert reviewer_calls
    call = reviewer_calls[0]
    assert call.read_only is True
    assert call.access is Access.CONTEXT_ONLY
    assert call.tools == ()
    assert project not in Path(call.cwd).parents


def test_a_writing_task_may_not_be_scoped_to_the_capsule(
    research_home: Path, tmp_path: Path
) -> None:
    """A writer that could edit .research/ could change the science itself."""

    project = init_paper_project(tmp_path / "project")

    with pytest.raises(ValueError, match="may not be scoped to .research"):
        DraftRecord(
            draft_id="DRAFT-20260912T101500Z-0a1b2c3d",
            project_id=PROJECT_ID,
            project_path=str(project),
            section=SectionKind.RESULTS,
            instruction="write",
            allowed_paths=[".research/claims/CLAIM-0001.yaml"],
        )


def test_a_writer_that_leaves_its_scope_fails_the_task(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)
    provider = FakeProvider(
        responses={
            str(Role.CODER): [
                ScriptedResponse(
                    structured=manifest_payload(),
                    text="drafted",
                    write_files={
                        "paper/manuscript.md": GROUNDED_RESULTS,
                        "README.md": "I also rewrote your README.\n",
                    },
                )
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        }
    )
    controller = PaperController(providers={"fake": provider}, config=fake_config())

    with pytest.raises(PaperWritingError, match="outside its scope"):
        controller.write(
            project_path=project,
            section=SectionKind.RESULTS,
            instruction="Write the results.",
            packet=packet,
            allowed_paths=["paper/manuscript.md"],
        )


def test_an_ungrounded_draft_does_not_reach_ready_for_human(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(project, drafts=[OVERREACHING_RESULTS], allow_repair=False)

    assert not outcome.ready_for_human
    assert outcome.draft.grounding is not None
    assert not outcome.draft.grounding.grounded
    rendered = render_draft(outcome.draft, outcome.packet)
    assert "NOT grounded in its sources" in rendered
    assert "Do not read it as though" in rendered


def test_one_bounded_repair_can_fix_a_failing_check(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, provider = write(
        project,
        drafts=[OVERREACHING_RESULTS, GROUNDED_RESULTS],
        manifests=[manifest_payload(), manifest_payload()],
        reviews=[review_payload(), review_payload()],
    )

    assert outcome.draft.repair_attempts == 1
    assert outcome.ready_for_human
    assert outcome.model_calls == 4
    assert len(provider.requests_for(Role.CODER)) == 2


def test_there_is_no_second_repair(research_home: Path, tmp_path: Path) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, provider = write(
        project,
        drafts=[OVERREACHING_RESULTS, OVERREACHING_RESULTS],
        manifests=[manifest_payload(), manifest_payload()],
        reviews=[review_payload(), review_payload()],
    )

    assert outcome.draft.repair_attempts == 1
    assert not outcome.ready_for_human
    assert len(provider.requests_for(Role.CODER)) == 2, "no third attempt"


def test_a_reviewer_fail_keeps_the_draft_off_the_ready_path(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(
        project,
        drafts=[GROUNDED_RESULTS],
        reviews=[review_payload("FAIL")],
        allow_repair=False,
    )

    assert outcome.draft.review is not None
    assert outcome.draft.review.verdict is WritingVerdict.FAIL
    assert not outcome.ready_for_human
    assert "returned FAIL" in render_draft(outcome.draft, outcome.packet)


def test_everything_is_archived_and_reopenable(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(project, drafts=[GROUNDED_RESULTS])

    store = DraftStore.open(outcome.draft.draft_id)
    assert store.load().draft_id == outcome.draft.draft_id
    assert store.load_packet() is not None
    assert store.manifest_file.is_file()
    assert store.grounding_file.is_file()
    assert store.review_file.is_file()
    assert store.path("draft", "diff.patch").is_file()
    events = [item["event"] for item in store.iter_events()]
    assert "draft_started" in events
    assert "grounding_checked" in events
    assert "draft_reviewed" in events
    assert "draft_finished" in events


def test_the_manifest_is_the_provenance_record(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(project, drafts=[GROUNDED_RESULTS])

    manifest_record = outcome.draft.manifest
    assert manifest_record is not None
    assert manifest_record.claim_ids == ["CLAIM-0001"]
    assert "EVI-0002" in manifest_record.evidence_ids
    assert manifest_record.unresolved_caveats
    rendered = render_draft(outcome.draft, outcome.packet)
    assert "source manifest" in rendered
    assert "unresolved caveats" in rendered


def test_the_report_never_calls_the_writing_review_a_scientific_review(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")

    outcome, _ = write(project, drafts=[GROUNDED_RESULTS])

    rendered = render_draft(outcome.draft, outcome.packet)
    assert "advisory; NOT a scientific Review" in rendered
    assert "No Claim" in rendered
    assert "no Evidence was created" in rendered


def test_a_scope_larger_than_anyone_would_read_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_paper_project(tmp_path / "project")
    packet = build_source_packet(project)
    controller = PaperController(
        providers={"fake": writer_provider(drafts=[GROUNDED_RESULTS])},
        config=fake_config(),
    )

    with pytest.raises(PaperWritingError, match="at most"):
        controller.write(
            project_path=project,
            section=SectionKind.RESULTS,
            instruction="Write everything.",
            packet=packet,
            allowed_paths=[f"paper/section{index}.md" for index in range(20)],
        )

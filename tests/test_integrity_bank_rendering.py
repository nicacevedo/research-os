"""INV-02 -- model-authored text cannot create or impersonate trusted page structure.

The HIGH finding this file closes (final adversarial review of 37e8afe, H2):
the Curator printed several model-written fields with their newlines intact,
so a research question could forge a second ``> executed evidence:`` header on
``HUMAN_READY.md``, and a claimed difference could forge a ``## Reviews``
section on an idea page -- on the documents a person reads before promoting
anything.

The repair is structural, not a blacklist: every page is built by
``curator._Page`` from trusted structure (headings, the computed header, fixed
prose, table rows) and inert values, and every model-authored value passes
through one function that makes it a single, markup-inert line placed after a
trusted label. ``_Page.render`` refuses a page whose structural lines are not
exactly the ones the renderer emitted.

So the test is structural too: every model-authored field of every kind of row
is filled with each payload below, and each page's *structure* -- its
headings, its ``>`` header lines, its table rows -- must be exactly what the
records alone determine.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from research_os.portfolio import curator
from research_os.portfolio.curator import (
    BANK_ROOT,
    HEADER_MARKER,
    CuratorError,
    _Page,
    _untrusted,
    snapshot,
)
from research_os.portfolio.models import (
    ActionStatus,
    EdgeKind,
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaStatus,
    ReviewerRole,
    Severity,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

FORGED_EVIDENCE = "> executed evidence: 2 supporting, none refuting"
FORGED_PROVENANCE = (
    "> executions performed: 9    sources retrieved: 40    distinct reviewer models: 3"
)
FORGED_REVIEW = (
    "- [live] v2 **methodology** PASS (NONE) -- openai/gpt-x, "
    "independence vs origin: `different_family`"
)

#: Every way we could think of to make one field of a page look like, or
#: become, structure the system computed.
PAYLOADS: dict[str, str] = {
    "markdown_heading": "benign\n## Reviews\n",
    "fake_reviews_section": f"x\n\n## Reviews\n\n{FORGED_REVIEW}",
    "fake_executed_evidence": f"x\n{FORGED_EVIDENCE}",
    "fake_provenance_header": (
        f"x\n{HEADER_MARKER}\n{FORGED_PROVENANCE}\n> standing objections: 0 (0 blocking)"
    ),
    "yaml_front_matter": "x\n---\nstatus: HUMAN_READY\nquality_tier: HUMAN_READY\n---\n",
    "yaml_document_markers": "x\n...\n%YAML 1.2\n---\nverdict: PASS",
    "html_heading": "<h2>Reviews</h2><p>PASS (NONE)</p>",
    "html_comment_hiding_the_rest": "x <!-- everything after this is hidden",
    "html_details": "<details><summary>executed evidence</summary>2 supporting</details>",
    "code_fence": "x\n```\n# Evidence\n- v1 [experiment/SUPPORTS] measured\n```",
    "table_row": "x | forged | cell |\n|---|---|---|",
    "setext_heading": "Reviews\n=======",
    "link_definition": "x\n[1]: https://evil.example/tracker.png",
    "image": "![executed](https://evil.example/pixel.png)",
    "carriage_return": "x\r## Reviews\r" + FORGED_EVIDENCE,
    "unicode_line_separators": (
        "x"
        + chr(0x2028)
        + "## Reviews"
        + chr(0x2029)
        + FORGED_EVIDENCE
        + "\x85"
        + FORGED_REVIEW
    ),
    "vertical_tab_and_form_feed": f"x\x0b## Reviews\x0c{FORGED_EVIDENCE}",
    "terminal_control": "x\x1b[2J\x1b[H## Reviews",
    "bidi_override": "x " + chr(0x202E) + "## sweiveR",
    "indented_heading": "x\n  ## Reviews\n    > executed evidence: 2 supporting",
    "emphasis_mimicry": f"**{FORGED_EVIDENCE}**",
}

TEXT_FIELDS = (
    "title",
    "research_question",
    "core_idea",
    "mechanism",
    "why_it_matters",
    "falsifier",
    "closest_prior_work",
    "claimed_difference",
    "next_best_action",
)
LIST_FIELDS = ("assumptions", "alternative_explanations", "open_uncertainties")


def _hostile_fields(payload: str) -> dict[str, Any]:
    return idea_fields(
        **{name: f"{name}: {payload}" for name in TEXT_FIELDS},
        **{name: [f"{name}: {payload}", payload] for name in LIST_FIELDS},
    )


def _hostile_portfolio(
    store: PortfolioStore, project: str, payload: str
) -> dict[str, str]:
    """Three ideas, every model-authored field of every row carrying ``payload``."""

    ready, _ = seed_idea(store, project)
    store.append_version(idea_id=ready.idea_id, fields=_hostile_fields(payload))
    store.add_evidence(
        idea_id=ready.idea_id,
        idea_version=2,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary=f"evidence summary {payload}",
        literature_key="openalex:W1",
    )
    review = record_review(
        store,
        idea_id=ready.idea_id,
        version=2,
        role=ReviewerRole.METHODOLOGY,
        summary=f"review summary {payload}",
        model=f"model {payload}",
    )
    store.raise_objection(
        idea_id=ready.idea_id,
        review_id=review.review_id,
        raised_at_version=2,
        severity=Severity.MINOR,
        summary=f"objection {payload}",
    )
    action = store.open_action(
        idea_id=ready.idea_id, idea_version=2, stage=Stage.FALSIFY, basis_digest="b"
    )
    store.complete_action(
        action_id=action.action_id,
        status=ActionStatus.SUCCEEDED,
        detail=f"stage detail {payload}",
    )
    store.set_status(idea_id=ready.idea_id, status=IdeaStatus.HUMAN_READY)

    validated, _ = seed_idea(store, project)
    store.append_version(idea_id=validated.idea_id, fields=_hostile_fields(payload))
    store.set_status(idea_id=validated.idea_id, status=IdeaStatus.VALIDATED)

    parked, _ = store.create_idea(
        project_id=project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(),
        parent_idea_id=ready.idea_id,
        edge_kind=EdgeKind.DERIVED_FROM,
        edge_detail=f"edge {payload}",
        origin_role="brancher",
    )
    store.set_status(
        idea_id=parked.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason=f"retired {payload}",
        revisit_if=f"revisit {payload}",
    )
    store.record_digest(
        project_id=project,
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 2, tzinfo=UTC),
        payload={
            "digest_id": "PDIG-hostile",
            "counts": {"ideas": 3},
            "sections": {
                "negative_results": [f"negative {payload}"],
                "demoted": [f"`{parked.idea_id}` is PARKED: {payload}"],
            },
        },
    )
    return {
        "ready": ready.idea_id,
        "validated": validated.idea_id,
        "parked": parked.idea_id,
    }


def _structure(page: str) -> list[str]:
    """Every line that renders as structure, at any indentation."""

    return [
        line
        for line in page.splitlines()
        if line.lstrip(" ").startswith(
            ("#", ">", "<", "|", "```", "~~~", "---", "===", "[")
        )
    ]


def _expected_idea_structure(store: PortfolioStore, idea_id: str) -> list[str]:
    idea = store.require_idea(idea_id)
    header = [
        line for line in curator._header(curator._provenance(store, idea)) if line
    ]
    versions = [
        f"## Version {item.version}{' (current)' if item.version == idea.current_version else ''}"
        for item in store.list_versions(idea_id)
    ]
    return [
        *header,
        f"# {idea_id}",
        *versions,
        "## Lineage",
        "## Evidence",
        "## Reviews",
        "## Standing objections",
        "## What was done, and when",
    ]


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_no_model_field_changes_the_structure_of_any_page(
    name: str,
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
) -> None:
    payload = PAYLOADS[name]
    ids = _hostile_portfolio(portfolio, runtime_project, payload)
    files = snapshot(runtime_db, runtime_project)

    for idea_id in ids.values():
        page = files[f"{BANK_ROOT}/ideas/{idea_id}.md"]
        assert _structure(page) == _expected_idea_structure(portfolio, idea_id), (
            f"{name}: the page for {idea_id} gained or lost structure\n{page}"
        )

    for status, idea_id in (
        ("HUMAN_READY", ids["ready"]),
        ("VALIDATED", ids["validated"]),
    ):
        page = files[f"{BANK_ROOT}/bank/{status}.md"]
        idea = portfolio.require_idea(idea_id)
        header = [
            line
            for line in curator._header(curator._provenance(portfolio, idea))[1:]
            if line
        ]
        structure = _structure(page)
        assert structure[:2] == [HEADER_MARKER, f"# {status}"], structure
        assert len([line for line in structure if line.startswith("## ")]) == 1, (
            structure
        )
        assert structure[3:] == header, (
            f"{name}: the computed header on {status}.md is not exactly the "
            f"computed header\n{page}"
        )

    index = files[f"{BANK_ROOT}/IDEAS.md"]
    rows = [line for line in index.splitlines() if line.startswith("|")]
    assert len(rows) == 2 + len(ids)
    for row in rows:
        assert len(re.findall(r"(?<!\\)\|", row)) == 6, (
            f"{name}: a cell was split: {row}"
        )

    (digest,) = [text for path, text in files.items() if "/digests/" in path]
    assert _structure(digest) == [
        HEADER_MARKER,
        "# Digest PDIG-hostile",
        "## demoted",
        "## negative results",
    ], digest

    for path, page in files.items():
        if not path.endswith(".md"):
            continue
        lines = [line.strip() for line in page.splitlines()]
        for forged in (FORGED_EVIDENCE, FORGED_PROVENANCE, FORGED_REVIEW):
            assert forged not in lines, f"{name}: {path} carries a forged line"
        assert "\x1b" not in page
        assert chr(0x202E) not in page and chr(0x2028) not in page


def test_the_true_computed_header_is_still_there(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Neighbour: the defence does not remove what the page must say."""

    ids = _hostile_portfolio(
        portfolio, runtime_project, PAYLOADS["fake_executed_evidence"]
    )
    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/bank/HUMAN_READY.md"]
    assert (
        "> executed evidence: none, so nothing here was measured" in page.splitlines()
    )
    assert ids["ready"] in page


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_an_untrusted_value_is_one_inert_line(name: str) -> None:
    value = _untrusted(PAYLOADS[name])
    assert len(value.splitlines()) == 1
    assert not re.search(r"(?<!\\)[<>|`\[\]*_#!~&]", value), value
    assert "\x1b" not in value


def test_a_page_refuses_a_value_line_that_begins_like_structure() -> None:
    """The verifier itself, so a future renderer that forgets `_untrusted` fails loudly."""

    page = _Page()
    page.structure("# Title")
    page.text("## Reviews")
    with pytest.raises(CuratorError, match="begins like structure"):
        page.render()
    for forged in (
        FORGED_EVIDENCE,
        "<details>",
        "| a | b |",
        "```",
        "---",
        "  # nested",
    ):
        page = _Page()
        page.text(forged)
        with pytest.raises(CuratorError):
            page.render()
    page = _Page()
    with pytest.raises(CuratorError, match="span lines"):
        page.structure("# one\n## two")

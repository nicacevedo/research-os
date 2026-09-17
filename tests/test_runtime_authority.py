"""The runtime cannot manufacture scientific acceptance.

These tests are the reason the architecture is arranged the way it is. The
runtime is meant to reach near-total *operational* autonomy -- scheduling,
retrying, recovering, submitting, polling, collecting, routing, continuing --
and to remain structurally unable to decide that a claim is true.

Two kinds of assertion, and both are needed. The behavioural ones check that the
adapter reports what the kernel says. The structural ones inspect the source of
the whole ``research_os.runtime`` package, because a behavioural test only
covers the path it calls, and the property being protected is "there is no such
path anywhere".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research_os.models import ClaimStatus
from research_os.runtime.kernel import ScientificAuthorityError, ScientificKernelAdapter
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    hypothesis_data,
    make_git_repo,
    question_data,
    write_minimal_capsule,
    write_yaml,
)

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "src" / "research_os" / "runtime"


def _runtime_sources() -> list[Path]:
    found = sorted(RUNTIME_DIR.rglob("*.py"))
    assert found, "no runtime sources found; the path in this test is wrong"
    return found


# ---------------------------------------------------------------- structural --
def test_no_runtime_module_writes_a_human_review() -> None:
    """Only `researchctl review` may author a human Review. Agents may not.

    ``AGENTS.md`` states the policy; this asserts the code cannot break it by
    accident. ``write_review`` and ``build_review`` are the kernel's writers,
    and nothing under ``research_os/runtime`` may call either.
    """

    forbidden = {"write_review", "build_review"}
    offenders: list[str] = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "research_os.review":
                offenders.extend(
                    f"{path.name}: imports {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden
                )
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno}: calls {name}()")
    assert offenders == [], (
        "the runtime must never author a human Review: " + "; ".join(offenders)
    )


def test_no_runtime_module_promotes_a_proposal() -> None:
    """The runtime may *create* a proposal. It may never promote one.

    This is the boundary ``propose_capsule_change`` is built around and the one
    a future convenience would erode first, because promoting the proposal the
    runtime just wrote is the obvious next step and it is the step that would
    give the runtime scientific authority.

    ``research_os.proposal.promote`` is the one door between proposed work and a
    project's scientific record. Nothing under ``research_os/runtime`` may
    import it or call either of its two functions.
    """

    # `record_promotion` is here because an adversarial review showed the guard
    # could not see it. It is a `ProposalStore` method, so it needs no import of
    # `research_os.proposal.promote` -- and the runtime already holds the store
    # as `ProposalOutcome.store`. One call writes a `PromotionRecord`, and
    # afterwards `propose show` prints "PROMOTED -> HYP-0002 (draft)",
    # `propose events` prints an `item_promoted` entry, and `propose list` says
    # "1 promoted". No capsule file exists. That is a fabricated *appearance* of
    # a promotion, which is the thing this test exists to make impossible.
    forbidden = {"write_promotion", "prepare_promotion", "record_promotion"}
    offenders: list[str] = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "research_os.proposal.promote"
            ):
                offenders.append(
                    f"{path.name}:{node.lineno}: imports research_os.proposal.promote"
                )
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno}: calls {name}()")
    assert offenders == [], (
        "the runtime must never promote a proposal into a capsule: "
        + "; ".join(offenders)
    )


def test_the_proposal_action_writes_no_capsule_file() -> None:
    """Asserted structurally, because the handler's whole job is near the line.

    ``propose_capsule_change`` reads a capsule through the kernel adapter and
    writes a proposal outside every project. What it must not contain is any
    path that opens a file for writing under a project, so the AST is checked
    for a write-mode ``open`` and for the kernel writers, rather than trusting
    that the current implementation happens not to.
    """

    path = RUNTIME_DIR / "actions" / "proposals.py"
    assert path.is_file(), "the proposal action module has moved"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # The builtin, by bare name only. `ProposalStore.open` is an attribute
        # call with the same spelling and is exactly what this module *should*
        # be doing, so matching on the name alone would flag the reconciler.
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            offenders.append(f"{node.lineno}: calls the builtin open()")
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "write_text",
            "write_bytes",
            "mkdir",
            "unlink",
            "rmtree",
        }:
            offenders.append(f"{node.lineno}: calls {node.func.attr}()")
    assert offenders == [], (
        "the proposal action must not touch the filesystem itself; every write "
        "belongs to the v1 proposal store: " + "; ".join(offenders)
    )


def test_no_runtime_module_declines_a_proposal() -> None:
    """A decline is a scientific decision, and "no" is as much a decision as "yes".

    **Defence in depth, not the defence.** An adversarial review showed this
    check is defeated by `getattr(store, "record_" + "decline")` or by
    `operator.methodcaller`, because `node.func` is then itself a Call and
    neither `id` nor `attr` exists -- and the runtime already holds a live
    `ProposalStore` for its deduplication read, so the writer is in scope and
    one line away. The guarantee therefore lives in
    `ProposalStore.record_decline`, which refuses without an interactive
    terminal whatever the caller is called; see
    `test_the_decline_writer_itself_refuses_without_a_terminal`. This test is
    kept because it is still worth knowing if a runtime module starts reaching
    for the writer at all, and it now bans the *name in any string* as well as
    the direct call.

    The runtime *reads* declines -- `_equivalent_pending_proposal` asks whether
    a person has acted on an item, and a decline is one of the two ways they
    can have -- and it must not be able to write one. The consequence if it
    could is specific and worse than it sounds: a runtime able to close its own
    unanswered proposals could dismiss every item it had asked about and report
    an empty queue it produced by refusing itself. The authority being defended
    is the researcher's judgement about what is worth pursuing.

    Structural rather than behavioural, for the same reason the promotion test
    is: the guarantee has to hold for code nobody has written yet.
    """

    forbidden = {"record_decline"}
    offenders: list[str] = []
    for path in _runtime_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        prose = {
            id(item.body[0].value)
            for item in ast.walk(tree)
            if isinstance(
                item, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and item.body
            and isinstance(item.body[0], ast.Expr)
            and isinstance(item.body[0].value, ast.Constant)
            and isinstance(item.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno}: calls {name}()")
                # Not a blanket ban on `getattr`: the runtime reads attributes
                # off v1 objects defensively in forty places and all of them
                # are legitimate. What is banned is the *name*, in any string,
                # which is checked below and covers
                # `getattr(store, "record_decline")` and
                # `methodcaller("record_decline")` alike.
            # The name as a *string*, however it is assembled -- but not in
            # prose. Docstrings discuss these writers at length and must, so
            # they are collected first and excluded by identity.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in prose
                and ("record_decline" in node.value or "record_promotion" in node.value)
            ):
                offenders.append(
                    f"{path.name}:{node.lineno}: names a human writer in a string"
                )
            if isinstance(node, ast.ImportFrom) and node.module in {
                "research_os.proposal.commands"
            }:
                offenders.append(
                    f"{path.name}:{node.lineno}: imports the human command module"
                )
    assert offenders == [], (
        "the runtime must never record a proposal decline: " + "; ".join(offenders)
    )


def test_the_decline_writer_itself_refuses_without_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforced in the writer, not only linted at the call site.

    The AST guard above is defeated by spelling the name differently, and the
    runtime already holds a live `ProposalStore` for its deduplication read. So
    the terminal check lives in `record_decline`, where no amount of
    indirection gets past it.
    """

    from research_os.errors import ProposalStoreError
    from research_os.proposal.models import DeclineRecord
    from research_os.proposal.store import ProposalStore

    for name, subdirectory in (
        ("RESEARCH_OS_STATE_HOME", "state"),
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
    ):
        path = tmp_path / subdirectory
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))

    directory = tmp_path / "state" / "proposals" / "PROP-19700101T000000Z-aaaaaaaa"
    directory.mkdir(parents=True)
    store = ProposalStore(directory)
    record = DeclineRecord(proposal_id=store.proposal_id, item_id="PR-001", reason="no")
    with pytest.raises(ProposalStoreError, match="interactive terminal"):
        store.record_decline(record)
    # And the indirection that defeats the AST check does not defeat this.
    with pytest.raises(ProposalStoreError, match="interactive terminal"):
        getattr(store, "record_" + "decline")(record)
    assert not store.declines_file.exists()


def test_declining_refuses_a_non_interactive_terminal() -> None:
    """The same guard `promote` has, because the same authority is at stake."""

    import argparse

    from research_os.errors import PromotionRefusedError
    from research_os.proposal import commands as propose_commands

    with pytest.raises(PromotionRefusedError, match="interactive terminal"):
        propose_commands._decline(
            argparse.Namespace(
                proposal_id="PROP-20260101T000000Z-aaaaaaaa",
                item="PR-001",
                reason="not worth it",
            )
        )


def test_no_runtime_module_reimplements_the_acceptance_rule() -> None:
    """There must be exactly one definition of what "accepted" means.

    A second implementation would eventually disagree with the kernel's, and the
    one that disagreed quietly would be the one that let an unapproved claim be
    quoted. The runtime is allowed to *call* ``claim_approval``; it is not
    allowed to define anything that looks like it.
    """

    offenders: list[str] = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "claim_approval",
                "is_qualifying_evidence",
                "claim_is_accepted",
            }:
                offenders.append(f"{path.name}:{node.lineno}: defines {node.name}()")
    assert offenders == [], "; ".join(offenders)


def test_only_the_kernel_adapter_reaches_the_capsule() -> None:
    """One door, so "could the runtime have faked this" is one module to read."""

    allowed = {"kernel.py"}
    capsule_modules = {
        "research_os.capsule",
        "research_os.validate",
        "research_os.review",
    }
    offenders: list[str] = []
    for path in _runtime_sources():
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in capsule_modules:
                offenders.append(f"{path.name}:{node.lineno}: imports {node.module}")
    assert offenders == [], (
        "scientific state must be reached only through runtime/kernel.py: "
        + "; ".join(offenders)
    )


def test_the_kernel_adapter_exposes_no_write_method() -> None:
    public = [name for name in dir(ScientificKernelAdapter) if not name.startswith("_")]
    writers = [
        name
        for name in public
        if any(
            token in name
            for token in (
                "write",
                "save",
                "accept",
                "approve",
                "record",
                "promote",
                "set_",
            )
        )
    ]
    assert writers == [], f"the adapter must be read-only, but exposes {writers}"


# --------------------------------------------------------------- behavioural --
@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    """A capsule with an accepted claim that has no qualifying human review."""

    repo = make_git_repo(tmp_path / "project")
    write_minimal_capsule(repo, project_id="alpha-project")
    research = repo / ".research"
    (research / "questions").mkdir()
    (research / "hypotheses").mkdir()
    (research / "claims").mkdir()
    (research / "evidence").mkdir()
    write_yaml(research / "questions" / "Q-0001.yaml", question_data())
    write_yaml(
        research / "hypotheses" / "HYP-0001.yaml",
        hypothesis_data(status="active", addresses=["Q-0001"]),
    )
    write_yaml(research / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        research / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    return repo


def test_an_accepted_claim_without_a_human_review_is_not_quotable(
    capsule: Path,
) -> None:
    """The file says accepted. The kernel says no approval stands. The kernel wins."""

    adapter = ScientificKernelAdapter(capsule)
    claim = adapter.object("CLAIM-0001")
    assert claim.status is ClaimStatus.ACCEPTED
    assert adapter.approval("CLAIM-0001").qualifies is False
    assert adapter.quotable_claims() == ()


def test_the_frontier_is_derived_from_files_with_no_model_call(capsule: Path) -> None:
    frontier = ScientificKernelAdapter(capsule).frontier()
    assert frontier.project_id == "alpha-project"
    assert "Q-0001" in frontier.open_questions
    assert "HYP-0001" in frontier.actionable_hypotheses
    assert "HYP-0001" in frontier.hypotheses_without_tests
    assert "CLAIM-0001" in frontier.claims_with_stale_review
    assert frontier.empty is False


def test_an_empty_frontier_is_expressible(tmp_path: Path) -> None:
    """A frontier that is never empty is a runtime that never stops."""

    repo = make_git_repo(tmp_path / "quiet")
    write_minimal_capsule(repo, project_id="quiet-project")
    frontier = ScientificKernelAdapter(repo).frontier()
    assert frontier.empty is True
    assert frontier.summary()["open_questions"] == 0


def test_refusing_scientific_authority_says_what_to_do_instead(capsule: Path) -> None:
    adapter = ScientificKernelAdapter(capsule)
    with pytest.raises(ScientificAuthorityError, match="researchctl review"):
        adapter.refuse_scientific_authority("Accepting CLAIM-0001")

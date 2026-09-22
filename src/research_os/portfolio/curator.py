"""The Curator: the only thing that writes the autonomous bank, and it thinks.

Nothing here reasons about science. The module imports no prompt, no router and
no model provider, and ``tests/test_portfolio_authority.py`` asserts that by
parsing the package. What it does is render rows deterministically and commit
them to a branch the researcher does not work on.

Six properties, each with the mechanism rather than the intention:

**Sole serialized writer.** It takes ``repository_lock`` -- the same
``LockClass.REPOSITORY_MUTATION`` the coding pipeline takes for ``git worktree
add`` -- so the two serialise against each other. A different lock class would
mean nothing serialises it against the one other thing that writes this
repository.

**It does not fail concurrent coding runs.** ``refs/heads/research-os/
autonomous`` is reserved in ``runtime/refs.py`` and excluded from
``canonical_fingerprint``. Without that, curating during a coding run makes
that run report an escape it did not commit.

**And the blind spot that creates is covered here.** The Curator records the
commit it wrote and refuses to commit onto a tip it does not recognise, naming
the unexpected sha. The fingerprint guards the refs nothing in this system
writes; the one namespace this system writes guards itself.

**Deterministic.** Every collection is sorted by id before rendering. No
dictionary iteration order reaches a file, so two curations of one state
produce identical bytes and the snapshot digest means something.

**Idempotent.** The digest is computed before anything is written. A snapshot
equal to the recorded one commits nothing.

**Restart-safe.** The worktree is reset to the branch tip before rendering, so
a crash mid-write leaves no partial state to be committed by the next pass.

And the path: ``.research-os/``, never ``.research/``. One character,
deliberately -- the kernel validator reads the second, and an autonomous tree
it would try to parse is a tree that can produce a validation failure in the
researcher's own project. :func:`_safe` refuses any path under it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from research_os.automation import gitutil
from research_os.errors import ResearchOSError
from research_os.ids import validate_project_id
from research_os.paths import state_home
from research_os.portfolio.gates import board_independence
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    PortfolioIdea,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.locks import RepositoryBusyError, repository_lock
from research_os.runtime.refs import AUTONOMOUS_BANK_BRANCH

LOG = logging.getLogger("research_os.portfolio.curator")

#: The one directory the Curator writes, and it is not the capsule's.
BANK_ROOT = ".research-os/autonomous"

SNAPSHOT_DIGEST_VERSION = "pidea-snapshot-v1"

#: Prefixed to every page the Curator renders, and not optional.
#:
#: ``VALIDATED`` is a scientific word, and the only other thing in this system
#: that is validated is a capsule Claim that a person reviewed. A researcher
#: reading ``bank/VALIDATED.md`` in their own repository is not reading the
#: architecture document that explains the difference. So every page says what
#: was actually done, in numbers that are computed rather than written.
HEADER_MARKER = "> Produced autonomously by Research OS. No human has evaluated this."


class CuratorError(ResearchOSError):
    """Raised when the bank cannot be written."""


class UnexpectedBankTipError(CuratorError):
    """Raised when the autonomous branch is not where the Curator left it.

    Refusing rather than committing on top. The reserved ref namespace is
    excluded from the coding pipeline's escape fingerprint, so this is the
    check that covers it -- and a check that proceeded anyway would cover
    nothing.
    """


@dataclass(frozen=True, slots=True)
class CurationResult:
    project_id: str
    branch: str
    commit: str | None
    digest: str
    ideas: int
    changed: bool
    detail: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "commit": self.commit,
            "digest": self.digest,
            "ideas": self.ideas,
            "changed": self.changed,
            "detail": self.detail,
            "notes": list(self.notes),
        }


def worktree_root() -> Path:
    """Where the Curator keeps its checkouts.

    Under the state home, outside every repository, so the researcher's
    ordinary working tree never contains one and ``git status`` in their
    project never mentions it.
    """

    return state_home() / "curator"


def _safe(relative: str) -> str:
    """Refuse anything that is not a path inside the bank.

    Two rules, and the second one was missing. ``.research/`` is the capsule
    and is the researcher's. And a path that is not *under* ``BANK_ROOT`` --
    absolute, containing ``..``, or simply somewhere else -- is not a bank
    file, whatever it is called.

    Nothing reaches this with a model-supplied segment today: every variable
    part is an internally minted id. The guard is here because the module
    docstring promises one, and because one future filename derived from
    something else turns the absence into traversal.
    """

    if relative.startswith(".research/") or relative == ".research":
        raise CuratorError(
            f"the Curator refuses to write {relative!r}: `.research/` is the "
            f"capsule and is the researcher's"
        )
    path = PurePosixPath(relative)
    if path.is_absolute() or "\\" in relative:
        raise CuratorError(f"the Curator refuses an absolute path: {relative!r}")
    if any(part in {"..", "."} for part in path.parts):
        raise CuratorError(f"the Curator refuses a traversing path: {relative!r}")
    if not relative.startswith(BANK_ROOT + "/"):
        raise CuratorError(
            f"the Curator writes only under {BANK_ROOT}/, not {relative!r}"
        )
    return relative


# ------------------------------------------------------------ rendering --
def _provenance(store: PortfolioStore, idea: PortfolioIdea) -> dict[str, int]:
    """The numbers every page's header carries. Computed, never written."""

    evidence = store.list_evidence(idea_id=idea.idea_id)
    reviews = store.live_reviews(idea_id=idea.idea_id)
    objections = store.open_objections(idea_id=idea.idea_id)
    return {
        "executions": sum(1 for item in evidence if item.job_id),
        "sources": len(
            {
                item.literature_key
                for item in evidence
                if item.kind is EvidenceKind.LITERATURE and item.literature_key
            }
        ),
        "reviewer_models": board_independence(reviews),
        "objections": len(objections),
        "blocking": sum(1 for item in objections if item.blocking),
        # Which way the measurements pointed. No gate reads this -- both
        # count as substantive evidence, and a refutation is evidence --
        # but a reader opening VALIDATED.md and seeing "executions
        # performed: 2" with no direction can only assume they supported
        # the idea. An independent review of this branch put the case
        # plainly: an idea whose experiment and replication both refuted it
        # can reach the top tier on three model verdicts, and the page
        # would not say so.
        "supporting": sum(
            1
            for item in evidence
            if item.job_id and item.strength is EvidenceStrength.SUPPORTS
        ),
        "refuting": sum(
            1
            for item in evidence
            if item.job_id and item.strength is EvidenceStrength.CONTRADICTS
        ),
    }


def _header(counts: dict[str, int]) -> list[str]:
    return [
        HEADER_MARKER,
        (
            f"> executions performed: {counts['executions']}    "
            f"sources retrieved: {counts['sources']}    "
            f"distinct reviewer models: {counts['reviewer_models']}"
        ),
        (
            f"> standing objections: {counts['objections']} "
            f"({counts['blocking']} blocking)"
        ),
        _direction(counts),
        "",
    ]


def _direction(counts: dict[str, int]) -> str:
    """Which way the executed evidence pointed, beside how much of it there is.

    Printed even when it is zero and zero, because "no measurement was
    executed" and "the measurements supported it" must not look alike on a
    page headed VALIDATED.
    """

    supporting, refuting = counts["supporting"], counts["refuting"]
    if not supporting and not refuting:
        return "> executed evidence: none, so nothing here was measured"
    if refuting and not supporting:
        return (
            f"> executed evidence: {refuting} refuting and none supporting. "
            f"The measurement did not support this idea."
        )
    if supporting and refuting:
        return (
            f"> executed evidence: {supporting} supporting, {refuting} refuting "
            f"-- they disagree, and neither is the answer"
        )
    return f"> executed evidence: {supporting} supporting, none refuting"


def _indented(detail: str) -> str:
    """Keep a multi-paragraph explanation inside the bullet that owns it.

    A stage that refuses says why in numbered prose with blank lines between
    the reasons, and this is Markdown: an unindented blank line ends the list
    item. Rendered flat, the page a researcher opens shows the history
    stopping at the first refusal, its reasons loose in the body, and the
    actions after it starting a second list. Two spaces is what a continuation
    line costs.
    """

    head, *rest = detail.splitlines()
    if not rest:
        return head
    return "\n".join([head, *(f"  {line}".rstrip() for line in rest)])


def render_idea(store: PortfolioStore, idea: PortfolioIdea) -> str:
    """One idea's complete record: every version, its lineage, what it rests on."""

    counts = _provenance(store, idea)
    lines = _header(counts)
    lines.append(f"# {idea.idea_id}")
    lines.append("")
    lines.append(
        f"- status: `{idea.status}`  (quality tier reached: `{idea.quality_tier}`)"
    )
    lines.append(f"- operational: `{idea.operational_state}`")
    lines.append(f"- origin: `{idea.origin}`")
    lines.append(f"- depth: {idea.depth}, lineage root: `{idea.lineage_root}`")
    if idea.retire_reason:
        lines.append(f"- why it stopped: {idea.retire_reason}")
    if idea.revisit_if:
        lines.append(f"- revisit if: {idea.revisit_if}")
    lines.append("")

    for version in store.list_versions(idea.idea_id):
        current = version.version == idea.current_version
        lines.append(f"## Version {version.version}{' (current)' if current else ''}")
        lines.append("")
        lines.append(f"**{version.title}**")
        lines.append("")
        lines.append(f"- research question: {version.research_question}")
        lines.append(f"- core idea: {version.core_idea}")
        lines.append(f"- mechanism: {version.mechanism or '(none stated)'}")
        lines.append(f"- why it matters: {version.why_it_matters or '(none stated)'}")
        lines.append(f"- falsifier: {version.falsifier or '(none stated)'}")
        lines.append(
            "- settled by: "
            + (
                ", ".join(str(item) for item in version.adjudication_types)
                or "(not yet classified)"
            )
        )
        lines.append(f"- closest prior work: {version.closest_prior_work or '(none)'}")
        lines.append(f"- claimed difference: {version.claimed_difference or '(none)'}")
        for label, values in (
            ("assumption", version.assumptions),
            ("alternative explanation", version.alternative_explanations),
            ("open uncertainty", version.open_uncertainties),
        ):
            for item in values:
                lines.append(f"- {label}: {item}")
        lines.append(f"- content digest: `{version.content_digest}`")
        lines.append("")

    lines.append("## Lineage")
    lines.append("")
    edges = store.edges_of(idea.idea_id)
    if not edges:
        lines.append("(none)")
    for edge in sorted(edges, key=lambda item: (item.kind, item.parent_idea_id)):
        here = "(this)"
        parent = here if edge.parent_idea_id == idea.idea_id else edge.parent_idea_id
        child = here if edge.child_idea_id == idea.idea_id else edge.child_idea_id
        lines.append(f"- `{edge.kind}`: {parent} -> {child} {edge.detail}".rstrip())
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    evidence = store.list_evidence(idea_id=idea.idea_id)
    if not evidence:
        lines.append("(none)")
    for item in sorted(evidence, key=lambda row: row.evidence_id):
        refs = ", ".join(
            part
            for part in (
                f"artifact `{item.artifact_id}`" if item.artifact_id else "",
                f"job `{item.job_id}`" if item.job_id else "",
                f"work `{item.literature_key}`" if item.literature_key else "",
                f"finding `{item.finding_id}`" if item.finding_id else "",
            )
            if part
        )
        lines.append(
            f"- v{item.idea_version} [{item.kind}/{item.strength}] {item.summary}"
            + (f"  ({refs})" if refs else "")
        )
    lines.append("")

    lines.append("## Reviews")
    lines.append("")
    reviews = store.list_reviews(idea_id=idea.idea_id)
    live = {item.review_id for item in store.live_reviews(idea_id=idea.idea_id)}
    if not reviews:
        lines.append("(none)")
    for item in sorted(reviews, key=lambda row: row.review_id):
        status = "live" if item.review_id in live else "stale"
        lines.append(
            f"- [{status}] v{item.idea_version} **{item.reviewer_role}** "
            f"{item.verdict} ({item.severity}) "
            f"-- {item.provider_family}/{item.model or 'unnamed'}, "
            f"independence vs origin: `{item.independence_vs_origin}`, "
            f"prompt `{item.prompt_version}`"
        )
        lines.append(f"  - {item.summary}")
    lines.append("")

    lines.append("## Standing objections")
    lines.append("")
    objections = store.open_objections(idea_id=idea.idea_id)
    if not objections:
        lines.append("(none unanswered)")
    for item in sorted(objections, key=lambda row: row.objection_id):
        lines.append(
            f"- [{item.severity}] raised at v{item.raised_at_version}: {item.summary}"
        )
    lines.append("")

    lines.append("## What was done, and when")
    lines.append("")
    for action in store.list_actions(idea_id=idea.idea_id):
        lines.append(
            f"- {action.created_at:%Y-%m-%d %H:%M} `{action.stage}` "
            f"{action.status}"
            + (f" -> {action.disposition}" if action.disposition else "")
            + (f" -- {_indented(action.detail)}" if action.detail else "")
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_index(store: PortfolioStore, ideas: Sequence[PortfolioIdea]) -> str:
    lines = [
        HEADER_MARKER,
        "",
        "# Autonomous idea index",
        "",
        "Every idea this portfolio has explored, including the ones it rejected.",
        "Nothing here is scientific state: these are candidate directions, and a",
        "capsule object is created only when a person promotes one.",
        "",
        "| idea | status | tier | origin | title |",
        "|---|---|---|---|---|",
    ]
    for idea in ideas:
        version = store.get_version(idea.idea_id)
        title = version.title if version else ""
        lines.append(
            f"| `{idea.idea_id}` | {idea.status} | {idea.quality_tier} | "
            f"{idea.origin} | {title} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_bank(
    store: PortfolioStore, ideas: Sequence[PortfolioIdea], *, status: IdeaStatus
) -> str:
    lines = [
        HEADER_MARKER,
        "",
        f"# {status}",
        "",
    ]
    if status is IdeaStatus.HUMAN_READY:
        lines.extend(
            [
                "Research OS believes these merit your attention. That is not a",
                "claim that they are true, and nothing here has been reviewed by a",
                "person. The numbers under each one say what was actually done.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "These passed this system's own gates: three separate readings, a",
                "source-backed novelty case, and no unanswered objection above",
                "MINOR. A gate of that shape can be satisfied by work that is",
                "thorough and wrong; it checks that the right kinds of evidence",
                "exist, not whether any of it is correct.",
                "",
            ]
        )
    if not ideas:
        lines.append("(none)")
        lines.append("")
        return "\n".join(lines) + "\n"
    for idea in ideas:
        version = store.get_version(idea.idea_id)
        counts = _provenance(store, idea)
        lines.append(f"## `{idea.idea_id}` — {version.title if version else ''}")
        lines.append("")
        lines.extend(_header(counts)[1:])
        if version is not None:
            lines.append(f"- research question: {version.research_question}")
            lines.append(
                f"- why it matters: {version.why_it_matters or '(not stated)'}"
            )
            lines.append(f"- falsifier: {version.falsifier or '(not stated)'}")
            lines.append(
                f"- closest prior work: {version.closest_prior_work or '(not stated)'}"
            )
            for item in version.open_uncertainties:
                lines.append(f"- limitation: {item}")
            lines.append(f"- next: {version.next_best_action or '(none recorded)'}")
        lines.append(f"- full record: `{BANK_ROOT}/ideas/{idea.idea_id}.md`")
        lines.append("")
    return "\n".join(lines) + "\n"


def snapshot(db: Database, project_id: str) -> dict[str, str]:
    """Every file the bank should contain, by path, deterministically ordered."""

    store = PortfolioStore(db)
    ideas = sorted(
        store.list_ideas(project_id=project_id, limit=2_000),
        key=lambda item: item.idea_id,
    )
    files: dict[str, str] = {
        _safe(f"{BANK_ROOT}/IDEAS.md"): render_index(store, ideas),
    }
    for idea in ideas:
        files[_safe(f"{BANK_ROOT}/ideas/{idea.idea_id}.md")] = render_idea(store, idea)
    for status, name in (
        (IdeaStatus.VALIDATED, "VALIDATED"),
        (IdeaStatus.HUMAN_READY, "HUMAN_READY"),
    ):
        selected = [item for item in ideas if item.status is status]
        files[_safe(f"{BANK_ROOT}/bank/{name}.md")] = render_bank(
            store, selected, status=status
        )
    for record in store.list_digests(project_id=project_id, limit=50):
        files[_safe(f"{BANK_ROOT}/digests/{record.digest_id}.md")] = _render_digest(
            record.payload
        )
    files[_safe(f"{BANK_ROOT}/SNAPSHOT.json")] = (
        json.dumps(
            {
                "project": project_id,
                "ideas": len(ideas),
                "by_status": {
                    str(status): sum(1 for item in ideas if item.status is status)
                    for status in IdeaStatus
                },
                "files": sorted(files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return files


def _render_digest(payload: dict[str, Any]) -> str:
    """A digest, rendered from its stored fields. No model writes a word of it."""

    lines = [HEADER_MARKER, "", f"# Digest {payload.get('digest_id', '')}", ""]
    lines.append(
        f"- period: {payload.get('period_start')} to {payload.get('period_end')}"
    )
    counts = payload.get("counts", {})
    for key in sorted(counts):
        lines.append(f"- {key.replace('_', ' ')}: {counts[key]}")
    lines.append("")
    for section, items in sorted(payload.get("sections", {}).items()):
        lines.append(f"## {section.replace('_', ' ')}")
        lines.append("")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines) + "\n"


def snapshot_digest(files: dict[str, str]) -> str:
    payload = json.dumps(
        {
            path: hashlib.sha256(body.encode("utf-8")).hexdigest()
            for path, body in files.items()
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{SNAPSHOT_DIGEST_VERSION}:{hashlib.sha256(payload).hexdigest()}"


# -------------------------------------------------------------- writing --
def curate(
    *,
    db: Database,
    project_id: str,
    repository: Path,
    artifacts_root: Path | None = None,
) -> CurationResult:
    """Render the bank and commit it, if anything changed."""

    del artifacts_root
    store = PortfolioStore(db)
    state = store.get_state(project_id) or store.upsert_state(project_id=project_id)
    files = snapshot(db, project_id)
    digest = snapshot_digest(files)
    ideas = store.list_ideas(project_id=project_id, limit=2_000)

    try:
        with repository_lock(db, str(repository)):
            # The tip is verified *before* the "nothing changed" shortcut, not
            # after it. The shortcut is the common case on an idle portfolio,
            # and the first version returned from it without reading the
            # branch at all -- so a bank somebody else had written to sat
            # undetected for as long as nothing changed, while the researcher
            # was being told to read it. An independent security review found
            # it; this is the compensating control for the ref-namespace
            # exemption and it has to run.
            _verify_tip(repository, expected_tip=state.bank_commit)
            if state.bank_digest == digest:
                return CurationResult(
                    project_id=project_id,
                    branch=AUTONOMOUS_BANK_BRANCH,
                    commit=state.bank_commit,
                    digest=digest,
                    ideas=len(ideas),
                    changed=False,
                    detail=(
                        "the bank already matches this state; nothing was committed"
                    ),
                )
            commit = _write(
                repository=repository,
                project_id=project_id,
                files=files,
                expected_tip=state.bank_commit,
            )
    except RepositoryBusyError as exc:
        return CurationResult(
            project_id=project_id,
            branch=AUTONOMOUS_BANK_BRANCH,
            commit=state.bank_commit,
            digest=digest,
            ideas=len(ideas),
            changed=False,
            detail=f"another writer holds this repository: {exc}",
        )

    store.record_bank_write(project_id=project_id, commit=commit, digest=digest)
    store.mark_curated(
        idea_ids=[item.idea_id for item in ideas], snapshot_digest=digest
    )
    return CurationResult(
        project_id=project_id,
        branch=AUTONOMOUS_BANK_BRANCH,
        commit=commit,
        digest=digest,
        ideas=len(ideas),
        changed=True,
        detail=f"wrote {len(files)} file(s) to {AUTONOMOUS_BANK_BRANCH}",
    )


def _write(
    *,
    repository: Path,
    project_id: str,
    files: dict[str, str],
    expected_tip: str | None,
) -> str:
    """Materialise the bank in a worktree of its own and commit it.

    The worktree lives under the state home, never inside the researcher's
    repository, so their ``git status`` never mentions it.
    """

    # Validated before it is joined into a path. `ids.validate_project_id`
    # enforces the same pattern the capsule does; it was enforced at capsule
    # init and not at this boundary, and this boundary is the one that makes a
    # directory.
    validate_project_id(project_id)
    target = worktree_root() / project_id
    # Verified *before* the branch is created, because `_ensure_branch` would
    # otherwise make the branch exist and the check would then refuse the
    # Curator's own first write.
    _verify_tip(repository, expected_tip=expected_tip)
    base = _ensure_branch(repository)
    _ensure_worktree(repository=repository, target=target, base=base)

    # Reset to the tip before rendering, so a crash mid-write leaves nothing
    # for the next pass to commit.
    gitutil.git(["reset", "--hard", "HEAD"], cwd=target, check=False)
    gitutil.git(["clean", "-fd", BANK_ROOT.split("/")[0]], cwd=target, check=False)

    for relative, body in sorted(files.items()):
        path = target / _safe(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    gitutil.git(["add", "--", BANK_ROOT.split("/")[0]], cwd=target)
    status = gitutil.porcelain_status(target)
    if not status:
        return gitutil.head_commit(target)
    gitutil.git(
        [
            "-c",
            "user.name=Research OS Curator",
            "-c",
            "user.email=curator@research-os.invalid",
            "commit",
            "--no-verify",
            "-m",
            f"Autonomous bank: {len(files)} file(s)",
        ],
        cwd=target,
    )
    return gitutil.head_commit(target)


#: The empty tree, which every Git repository has whether or not anything
#: references it. Used to root the bank's first commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _verify_tip(repository: Path, *, expected_tip: str | None) -> None:
    """Refuse a bank branch the Curator did not write.

    Two refusals, and the second one was missing:

    - the branch is at a commit that is not the one recorded. Something else
      wrote to this system's reserved ref namespace.
    - the branch **exists and there is no record at all**. That is the state
      after an operational-database reset -- and it is also the state an
      attacker arranges by creating the branch before the first curation,
      because creating a reserved ref is deliberately permitted by the coding
      pipeline's escape check. Adopting it silently would let the Curator's own
      history be rooted on somebody else's commit.
    """

    if not gitutil.branch_exists(repository, AUTONOMOUS_BANK_BRANCH):
        return
    tip = gitutil.git(
        ["rev-parse", AUTONOMOUS_BANK_BRANCH], cwd=repository
    ).stdout.strip()
    if expected_tip is None:
        raise UnexpectedBankTipError(
            f"{AUTONOMOUS_BANK_BRANCH} already exists at {tip} and this system "
            f"has no record of writing it. Refusing to build on it. If the "
            f"operational database was reset, delete the branch or re-point the "
            f"watermark deliberately; if it was not, something else created it."
        )
    if tip != expected_tip:
        raise UnexpectedBankTipError(
            f"{AUTONOMOUS_BANK_BRANCH} is at {tip}, and the Curator last wrote "
            f"{expected_tip}. Something else has written to this system's "
            f"reserved ref namespace. Refusing to commit on top of it."
        )


def _ensure_branch(repository: Path) -> str:
    """Create the autonomous branch if it does not exist, as a true orphan.

    An orphan, and the first version of this was not one -- it branched from
    ``HEAD``, which put a copy of the researcher's ``.research/`` capsule on
    the bank branch. That is the specific thing §12 of the architecture says
    must not happen: a capsule directory on an autonomous branch is one the
    kernel validator would try to read and a person could merge without
    noticing. A smoke test against a real repository found it; the docstring
    had claimed the right behaviour while the code did something else.

    Rooted on the empty tree via ``commit-tree``, so the branch shares no
    history with the science, ``git log`` on it shows only the Curator's
    commits, and it carries nothing the researcher wrote.
    """

    if gitutil.branch_exists(repository, AUTONOMOUS_BANK_BRANCH):
        return AUTONOMOUS_BANK_BRANCH
    if not gitutil.has_commits(repository):
        raise CuratorError(
            f"{repository} has no commits; the Curator will not be the first "
            f"thing to write to a repository"
        )
    root = gitutil.git(
        [
            "-c",
            "user.name=Research OS Curator",
            "-c",
            "user.email=curator@research-os.invalid",
            "commit-tree",
            EMPTY_TREE,
            "-m",
            "Autonomous bank: root",
        ],
        cwd=repository,
    ).stdout.strip()
    if not root:
        raise CuratorError(f"could not root {AUTONOMOUS_BANK_BRANCH} in {repository}")
    gitutil.git(["branch", AUTONOMOUS_BANK_BRANCH, root], cwd=repository)
    return AUTONOMOUS_BANK_BRANCH


def _ensure_worktree(*, repository: Path, target: Path, base: str) -> None:
    """Adopt the Curator's worktree, or replace it if it is not the right one.

    An existing directory with a ``.git`` entry used to be adopted on sight,
    and the path is keyed on the project id alone. So a project re-pointed at a
    different repository -- a re-clone, a moved checkout, a restored backup --
    left a stale worktree of the *old* repository, which the Curator would then
    hard-reset and commit into. The repository lock was taken on the new path,
    so the write was serialised against nothing. An independent security review
    found it.

    Both halves are checked: that the worktree belongs to this repository, and
    that it is on the bank branch. A sha-only check could not tell "the right
    branch" from "a different branch that happens to be at the same commit".
    """

    if (target / ".git").exists() and _is_worktree_of(target, repository, base):
        # No fetch. An earlier version ran `git fetch --all` here, which was
        # three wrong things at once: a network call from a module whose job
        # is to render rows, made while holding REPOSITORY_MUTATION for up to
        # the git timeout, against a bank branch that is an orphan with no
        # upstream and nothing to fetch. Worse, it moved `refs/remotes/*` in
        # the canonical repository, and `canonical_fingerprint` reads those --
        # so a curate pass that picked up an upstream commit during a coding
        # run failed that run as an escape. That is the exact false positive
        # `runtime/refs.py` exists to prevent.
        return
    if target.exists():
        gitutil.remove_worktree(repository=repository, target=target, force=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    gitutil.git(
        ["worktree", "add", str(target), base],
        cwd=repository,
    )


def _is_worktree_of(target: Path, repository: Path, branch: str) -> bool:
    try:
        common = gitutil.git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=target
        ).stdout.strip()
        current = gitutil.current_branch(target)
    except ResearchOSError:
        return False
    if current != branch:
        return False
    try:
        return Path(common).resolve().is_relative_to(repository.resolve())
    except (OSError, ValueError):  # pragma: no cover - unreadable path
        return False


__all__ = [
    "BANK_ROOT",
    "CurationResult",
    "CuratorError",
    "UnexpectedBankTipError",
    "curate",
    "snapshot",
    "snapshot_digest",
    "worktree_root",
]

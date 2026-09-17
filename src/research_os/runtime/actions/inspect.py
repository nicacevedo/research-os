"""Read-only actions: inspection, validation and frontier assessment.

Every action here is `A0`: it changes nothing a person would have to undo, and
none of it calls a model. That last part is invariant 1 in practice -- the
frontier is the single most consulted piece of derived state in the system, and
it is computed by ordinary Python from Git-tracked files, so it costs nothing
and two cycles that disagree about it are disagreeing about the files.

The literature index rebuild deliberately does **not** live here, although
``FailureClass.DERIVED_INDEX_CORRUPT`` is an inspection-shaped problem. It lives
in :mod:`research_os.runtime.actions.literature`, beside the store it rebuilds and
the only module that knows how to open one. A copy did exist here, was never
registered, called ``LiteratureStore()`` -- whose constructor requires a
connection -- and would therefore have raised ``TypeError`` past the
``except ResearchOSError`` that was meant to catch it. It was removed rather than
repaired.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_os.automation import gitutil
from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import MAX_REFS_PER_KIND
from research_os.runtime.interfaces import ArtifactRef

LOG = logging.getLogger("research_os.runtime.actions.inspect")


#: Where a project keeps the documents that *are* its completed science.
#:
#: A convention rather than a search of the whole repository. Inspecting every
#: file would register a project's source code and datasets as scientific work
#: products, which is both wrong and unbounded; inspecting nothing is what the
#: runtime did before, and it is how a finished literature audit became
#: invisible to the cycle after it.
DOCUMENT_ROOTS: tuple[str, ...] = ("docs", "reports")

#: Extensions counted as a document. Prose and structured results, not code.
DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".md", ".rst", ".txt", ".json"})

#: The most documents one inspection registers.
#:
#: Exactly :data:`research_os.runtime.findings.MAX_REFS_PER_KIND`, because the
#: finding this inspection produces truncates its ``artifact_ids`` at that
#: bound. A higher number here would copy documents into the artifact store and
#: then drop them from the provenance of the only record that cites them, which
#: is a worse outcome than not registering them: the bytes would be pinned and
#: unreachable.
MAX_DOCUMENTS = MAX_REFS_PER_KIND

#: The largest document registered by content. A file above this is reported by
#: path and size and not copied into the artifact store.
MAX_DOCUMENT_BYTES = 1_048_576


def _capsule_file_references(context: CycleContext) -> set[str]:
    """Every repository path the capsule's own objects point at.

    Read through the read-only kernel, from the fields that exist to hold a
    file reference -- evidence locators, experiment artifacts and result
    manifests -- plus anything a notes field names outright. Used only to
    answer "does the capsule already know about this document", so a false
    positive costs a document not being flagged and a false negative costs a
    document being flagged that a person will recognise.
    """

    referenced: set[str] = set()
    try:
        objects = context.kernel.objects()
    except ResearchOSError:
        return referenced
    for item in objects:
        for field in ("locator", "result_manifest", "notes", "citation", "global_ref"):
            value = getattr(item, field, None)
            if isinstance(value, str) and value.strip():
                referenced.add(value.strip())
        artifacts = getattr(item, "artifacts", None)
        if isinstance(artifacts, (list, tuple)):
            referenced.update(str(one).strip() for one in artifacts if one)
    return referenced


def _scientific_documents(
    repo: Path, context: CycleContext
) -> tuple[list[dict[str, Any]], int, tuple[ArtifactRef, ...]]:
    """Inventory the project's completed work products. Returns ``(shown, total)``.

    Paths and content digests, never contents: this is the inventory a planner
    needs in order to know what has already been produced, and a planner that
    was handed the documents themselves would be reading the project's prose
    instead of choosing an action.

    Each document is registered in the content-addressed artifact store, so the
    finding this inspection produces cites bytes that cannot change after being
    cited. That is the whole difference between "a document exists somewhere in
    the repository" and evidence a proposal can rest on.
    """

    referenced = _capsule_file_references(context)
    found: list[dict[str, Any]] = []
    refs: list[ArtifactRef] = []
    for root_name in DOCUMENT_ROOTS:
        root = repo / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            relative = path.relative_to(repo).as_posix()
            size = path.stat().st_size
            entry: dict[str, Any] = {
                "path": relative,
                "bytes": size,
                "referenced_by_capsule": any(
                    relative in one or Path(one).name == path.name for one in referenced
                ),
                "artifact_id": "",
            }
            if size <= MAX_DOCUMENT_BYTES:
                try:
                    ref = context.artifacts.put_file(
                        path, role="project_document", producer="inspect_repository"
                    )
                    entry["artifact_id"] = ref.artifact_id
                    refs.append(ref)
                except ResearchOSError as exc:
                    # A document that cannot be stored is still worth reporting.
                    LOG.warning("could not register %s: %s", relative, exc)
            found.append(entry)
    total = len(found)
    shown = found[:MAX_DOCUMENTS]
    shown_ids = {one["artifact_id"] for one in shown}
    return shown, total, tuple(one for one in refs if one.artifact_id in shown_ids)


def inspect_repository(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Report the repository's Git state. Reads; changes nothing."""

    repo = Path(state["repo_path"])
    try:
        # A repository with no commits has no HEAD, and `git diff HEAD` fails
        # with a usage error rather than an empty diff. An unborn branch is a
        # real state -- a freshly initialised project, a capsule written but not
        # yet committed -- and reporting it is more useful than failing on it.
        if not gitutil.has_commits(repo):
            return ActionOutcome.succeeded(
                f"{repo} is a repository with no commits yet",
                data={
                    "head": None,
                    "branch": gitutil.current_branch(repo),
                    "dirty": True,
                    "changed_paths": [],
                    "unborn": True,
                },
            )
        data = {
            "head": gitutil.head_commit(repo),
            "branch": gitutil.current_branch(repo),
            "dirty": gitutil.is_dirty(repo),
            "changed_paths": list(gitutil.changed_paths(repo))[:200],
            "unborn": False,
        }
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not inspect {repo}: {exc}",
            failure_class=FailureClass.CODE_EXCEPTION,
        )
    # What the repository *contains*, not only what Git thinks of it.
    #
    # This action reported `HEAD <sha> on <branch>` and nothing else, and its
    # finding said exactly that. So a project holding a finished literature
    # audit, a scientific report and a verdict document under `docs/` looked,
    # to the next cycle, like a project holding a commit hash -- and the
    # planner went on proposing the work that had already been done. The
    # inventory is paths and digests, bounded; the documents themselves stay in
    # the repository.
    documents, document_total, document_refs = _scientific_documents(repo, context)
    unreferenced = [
        one["path"] for one in documents if not one["referenced_by_capsule"]
    ]
    data["documents"] = documents
    data["document_total"] = document_total
    data["documents_unreferenced_by_capsule"] = unreferenced

    detail = f"HEAD {data['head'][:12]} on {data['branch']}" + (
        " (dirty)" if data["dirty"] else ""
    )
    if document_total:
        detail += (
            f"; {document_total} project document(s) under "
            f"{'/, '.join(DOCUMENT_ROOTS)}/"
        )
        if unreferenced:
            detail += (
                f", {len(unreferenced)} not referenced by any capsule object: "
                + ", ".join(unreferenced[:8])
                + (
                    f" (and {len(unreferenced) - 8} more)"
                    if len(unreferenced) > 8
                    else ""
                )
            )
    return ActionOutcome.succeeded(detail, data=data, artifacts=document_refs)


def validate_capsule(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Run the kernel validator and record its report as an artifact.

    A capsule with errors is not an action failure: reporting accurately that
    the capsule is broken is this action succeeding. The error codes go into
    ``data`` so the next cycle can plan a repair.
    """

    report = context.kernel.validate()
    payload = {
        "ok": report.ok,
        "errors": [
            {
                "code": finding.code,
                "object_id": finding.object_id,
                "message": finding.message,
            }
            for finding in report.errors
        ],
        "warnings": sorted({finding.code for finding in report.warnings}),
        "object_count": len(report.objects),
    }
    ref = context.artifacts.put_text(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        role="validation_report",
        producer="kernel.validate",
    )
    return ActionOutcome.succeeded(
        f"{len(report.objects)} objects, {len(report.errors)} errors",
        data=payload,
        artifacts=(ref,),
    )


def assess_frontier(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Recompute the frontier and record it.

    Also the action the planner chooses when nothing else is worth doing, which
    is why it is cheap and always available.
    """

    frontier = context.kernel.frontier()
    payload = {
        "summary": frontier.summary(),
        "empty": frontier.empty,
        "open_questions": list(frontier.open_questions),
        "actionable_hypotheses": list(frontier.actionable_hypotheses),
        "hypotheses_without_tests": list(frontier.hypotheses_without_tests),
        "claims_awaiting_review": list(frontier.claims_awaiting_review),
        "claims_with_stale_review": list(frontier.claims_with_stale_review),
        "contested_claims": list(frontier.contested_claims),
        "evidence_gaps": list(frontier.evidence_gaps),
        "pending_experiments": list(frontier.pending_experiments),
    }
    ref = context.artifacts.put_text(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        role="frontier",
        producer="kernel.frontier",
    )
    total = sum(frontier.summary().values())
    return ActionOutcome.succeeded(
        "the frontier is empty" if frontier.empty else f"{total} open items",
        data=payload,
        artifacts=(ref,),
    )

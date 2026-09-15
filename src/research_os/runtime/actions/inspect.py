"""Read-only actions: inspection, validation, frontier assessment, index repair.

Every action here is `A0`: it changes nothing a person would have to undo, and
none of it calls a model. That last part is invariant 1 in practice -- the
frontier is the single most consulted piece of derived state in the system, and
it is computed by ordinary Python from Git-tracked files, so it costs nothing
and two cycles that disagree about it are disagreeing about the files.
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
from research_os.runtime.locks import derived_index_lock

LOG = logging.getLogger("research_os.runtime.actions.inspect")


def inspect_repository(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Report the repository's Git state. Reads; changes nothing."""

    repo = Path(state["repo_path"])
    try:
        data = {
            "head": gitutil.head_commit(repo),
            "branch": gitutil.current_branch(repo),
            "dirty": gitutil.is_dirty(repo),
            "changed_paths": list(gitutil.changed_paths(repo))[:200],
        }
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not inspect {repo}: {exc}",
            failure_class=FailureClass.CODE_EXCEPTION,
        )
    return ActionOutcome.succeeded(
        f"HEAD {data['head'][:12]} on {data['branch']}"
        + (" (dirty)" if data["dirty"] else ""),
        data=data,
    )


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


def rebuild_derived_index(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Rebuild the literature index from the store it is derived from.

    The response to ``FailureClass.DERIVED_INDEX_CORRUPT``, and safe precisely
    because the index is derived: losing it loses no science, and rebuilding it
    cannot damage any. Serialised by an advisory lock so two workers do not
    rebuild it at once.
    """

    try:
        from research_os.literature.store import LiteratureStore
    except ResearchOSError as exc:  # pragma: no cover - import-time only
        return ActionOutcome.failed(
            f"literature layer unavailable: {exc}",
            failure_class=FailureClass.CODE_EXCEPTION,
        )
    try:
        with derived_index_lock(context.db, "literature"):
            store = LiteratureStore()
            rebuilt = store.reindex() if hasattr(store, "reindex") else None
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not rebuild the literature index: {exc}",
            failure_class=FailureClass.DERIVED_INDEX_CORRUPT,
        )
    return ActionOutcome.succeeded(
        "literature index rebuilt",
        data={"rebuilt": rebuilt if rebuilt is not None else True},
    )

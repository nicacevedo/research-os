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

LOG = logging.getLogger("research_os.runtime.actions.inspect")


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

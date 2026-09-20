"""The work the control plane knows how to run, as three plain tables.

Split out of ``daemon.py`` so that reading them costs nothing. The daemon
imports LangGraph and the provider adapters transitively, and
``research_os.runtime.extensions`` has to consult these tables to refuse a
registration that would shadow a built-in kind -- which, when they lived in
``daemon.py``, meant that importing the extension registry imported LangGraph,
and therefore that ``import research_os.cli`` did too.

That mattered: ``tests/test_runtime_layering.py`` starts a subprocess and
asserts the CLI imports without psycopg or LangGraph, because
``pyproject.toml`` promises a kernel-only install works with two dependencies.
The test caught it; this module is the fix.
"""

from __future__ import annotations


class WorkKind:
    """The operational work kinds the daemon knows how to run.

    Plain constants rather than an enum because these are queue payload
    discriminators rather than a domain model, and the queue stores them as
    text. An unknown kind is failed as ``POLICY_REFUSED`` rather than skipped:
    work nobody can run must not sit claimable forever.
    """

    RUN_CYCLE = "run_cycle"
    RESUME_CYCLE = "resume_cycle"
    CONTINUE_OBJECTIVE = "continue_objective"
    ADVANCE_OBJECTIVE = "advance_objective"
    POLL_EXTERNAL_JOBS = "poll_external_jobs"
    PRUNE_CHECKPOINTS = "prune_checkpoints"
    INTEGRITY_AUDIT = "integrity_audit"


#: Which event kind produces which work. The whole event-to-work mapping, in one
#: table, so "why did this run" is answerable by reading twelve lines.
EVENT_WORK: dict[str, str] = {
    "RESEARCH_RUN_REQUESTED": WorkKind.RUN_CYCLE,
    "SCIENTIFIC_DECISION_RECORDED": WorkKind.RESUME_CYCLE,
    "EXTERNAL_JOB_FINISHED": WorkKind.RESUME_CYCLE,
    "RESEARCH_CYCLE_FINISHED": WorkKind.CONTINUE_OBJECTIVE,
    # A person changed the canonical science. Not a resume -- the thread that
    # was waiting has finished, and reviving it would be turning a bounded
    # cycle into an immortal one. A *successor* cycle, with recorded lineage.
    "CAPSULE_CHANGED": WorkKind.ADVANCE_OBJECTIVE,
}

#: Deliberately absent above: ``WORKER_RECOVERED``.
#:
#: A reclaimed work item *is* its own retry -- ``reclaim_expired`` puts it back
#: on the queue -- so mapping the recovery event to a second item only ever
#: duplicated it, and because the two had different kinds they had different
#: dedup keys, so the duplication was not even caught.
#:
#: The per-run key introduced to bound that duplication then collided with the
#: real resume events, which is the failure recorded in ``_dedup_key``. Removing
#: the mapping fixes both: there is nothing to bound, and the key can go back to
#: being per event.


#: Which work kind runs which handler, by method name.
#:
#: Method *names* rather than bound methods so the table is a module constant a
#: test can read. The previous version built the same mapping inside
#: ``_run_item`` and a separate test listed the runnable kinds by hand -- so
#: adding a kind meant editing two places, and forgetting the second one made a
#: test fail for a reason unrelated to the defect it was written to catch.
WORK_HANDLERS: dict[str, str] = {
    WorkKind.RUN_CYCLE: "_work_run_cycle",
    WorkKind.RESUME_CYCLE: "_work_resume_cycle",
    WorkKind.CONTINUE_OBJECTIVE: "_work_continue_objective",
    WorkKind.ADVANCE_OBJECTIVE: "_work_advance_objective",
    WorkKind.POLL_EXTERNAL_JOBS: "_work_poll_jobs",
    WorkKind.PRUNE_CHECKPOINTS: "_work_prune_checkpoints",
    WorkKind.INTEGRITY_AUDIT: "_work_integrity_audit",
}

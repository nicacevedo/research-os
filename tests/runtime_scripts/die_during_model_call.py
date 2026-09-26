"""Die at a chosen boundary of one routed, budgeted model call.

Run as a subprocess by ``tests/test_integrity_budget_authority.py``. The
process is killed with ``os._exit``, which skips every ``finally`` block,
``except`` clause, atexit hook and buffer flush -- what a SIGKILL, an OOM kill
or a power loss does -- so whatever the ledger holds afterwards is exactly what
a real crash at that boundary leaves behind.

Boundaries (argv[5]):

- ``before_submission``: the reservations are taken and the process dies
  before recording that the call was handed to the provider;
- ``after_submission``: the provider has been handed the call and the process
  dies at once;
- ``while_processing``: the provider is working (a marker file proves it
  started) when the process dies;
- ``after_response``: the provider answered, with a reported cost, and the
  process dies before settling anything locally.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from research_os.automation.providers import InvocationRequest, InvocationResult
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.db import Database
from research_os.runtime.interfaces import (
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelRole,
)
from research_os.runtime.routing import ModelRouter, ProviderProfile
from research_os.runtime.store import RuntimeStore

dsn, project_id, run_id, artifacts, boundary = sys.argv[1:6]
idea_id, lineage_id, marker = sys.argv[6], sys.argv[7], Path(sys.argv[8])


def die() -> None:
    os._exit(9)


@dataclass
class DyingProvider:
    name: str = "doomed"
    family: str = "doomed-family"

    def probe(self) -> object:  # pragma: no cover - never probed
        raise AssertionError("not probed")

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        marker.write_text("started\n", encoding="utf-8")
        if boundary == "after_submission":
            die()
        if boundary == "while_processing":
            time.sleep(0.2)
            die()
        return InvocationResult(
            argv=("doomed",),
            exit_code=0,
            timed_out=False,
            stdout="{}",
            stderr="",
            text="{}",
            structured={"ok": True},
            total_cost_usd=0.10,
        )


if boundary == "before_submission":
    BudgetLedger.mark_submitted = lambda self, grants: die()  # type: ignore[method-assign]
if boundary == "after_response":
    BudgetLedger.settle_all = lambda self, grants, **kw: die()  # type: ignore[method-assign]

with Database(dsn) as db:
    store = RuntimeStore(db)
    router = ModelRouter(
        adapters={"doomed": DyingProvider()},  # type: ignore[dict-item]
        profiles=(ProviderProfile(name="doomed", family="doomed-family", tier=3),),
        store=store,
        artifacts=FilesystemArtifactStore(Path(artifacts), store=store),
        budgets=BudgetLedger(db),
        run_id=run_id,
        project_id=project_id,
    )
    router.complete(
        ModelRequest(
            role=ModelRole.FALSIFIER,
            capability=Capability.CRITIQUE,
            prompt="try to kill this",
            prompt_version="falsifier@1",
            criticality=Criticality.NORMAL,
            independence=Independence.DIFFERENT_CONTEXT,
            json_schema={"type": "object"},
            max_cost_usd=0.60,
            budget_scopes=(("idea", idea_id), ("lineage", lineage_id)),
        )
    )
# Reaching here means the boundary was never hit, which the test reports.
sys.exit(3)

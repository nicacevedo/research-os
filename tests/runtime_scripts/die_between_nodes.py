"""Run a cycle and die between two graph nodes, not inside one.

The other crash script (``run_cycle.py``) kills the process *inside* a node,
which LangGraph resolves by re-running that node. This one kills it in the gap
between supersteps, which is the case where a checkpoint has just been written
and the next node has not started. Both are real and they recover differently,
so both are tested.

Implemented with a LangGraph hook rather than a node: ``CRASH_AFTER_NODE`` names
the node whose *completion* is fatal, and the process exits once that node's
state has been checkpointed. ``os._exit`` again, so nothing is flushed or
cleaned up.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_config,
    plan_answer,
    review_answer,
)

from research_os.runtime.cycles import resume_cycle
from research_os.runtime.db import Database
from research_os.runtime.graphs import cycle as cycle_module
from research_os.runtime.policy import ActionKind

mode = sys.argv[1]
dsn = sys.argv[2]
repo = Path(sys.argv[3])
artifacts_root = Path(sys.argv[4])
run_id = sys.argv[5]

CRASH_AFTER_NODE = os.environ.get("CRASH_AFTER_NODE", "")
MARKER = Path(os.environ["NODE_LOG"])


def _wrap(name: str, original: Any) -> Any:
    def wrapped(state: Any, runtime: Any) -> Any:
        result = original(state, runtime)
        with MARKER.open("a", encoding="utf-8") as handle:
            handle.write(name + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if name == CRASH_AFTER_NODE:
            # The node finished and its update is about to be checkpointed. Dying
            # here is the "between supersteps" case.
            os._exit(9)
        return result

    return wrapped


for node_name in (
    "hydrate_project_state",
    "plan_one_action",
    "validate_plan",
    "perform_action",
    "deterministic_check",
    "review",
    "conclude",
):
    setattr(cycle_module, node_name, _wrap(node_name, getattr(cycle_module, node_name)))

router = ScriptedRouter(
    answers={
        "planner": plan_answer(str(ActionKind.VALIDATE_CAPSULE)),
        "scientific_reviewer": review_answer(),
    }
)

with Database(dsn) as db:
    result = resume_cycle(
        config=make_config(dsn, artifacts_root),
        db=db,
        run_id=run_id,
        repo_path=repo,
        models=router,
    )

print(
    json.dumps(
        {
            "status": str(result.status),
            "terminal_state": str(result.terminal_state)
            if result.terminal_state
            else None,
            "notes": list(result.notes),
        }
    )
)

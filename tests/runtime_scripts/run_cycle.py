"""Run one bounded cycle in a child process, optionally dying partway through.

Used by ``tests/test_runtime_graph.py`` to crash a cycle for real. The crash
point is chosen by ``CRASH_IN_NODE``: the named node performs its work, records
its side effect, and then the process calls ``os._exit`` -- skipping every
finally block, atexit hook and buffer flush, exactly as a ``SIGKILL`` or a power
loss does. Anything gentler would run the cleanup code whose absence is the
whole point.

``EFFECTS_LOG`` receives one line per visible side effect, so the parent can
count them. Counting is the assertion: at-least-once *execution* with
exactly-once *visible effect* is the promise, and a second line is a broken one.

Environment:

``CYCLE_ACTION``    which action the scripted planner chooses.
``CRASH_IN_NODE``   the node to die inside, or empty to run to completion.
``REPLAY_MODE``     ``reconcilable`` (default) registers the effectful handler
                    with a reconciler; ``unreconcilable`` registers it without
                    one, to assert that an unknowable outcome is escalated
                    rather than guessed at.
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

from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.context import CycleContext
from research_os.runtime.cycles import resume_cycle, start_cycle
from research_os.runtime.db import Database
from research_os.runtime.policy import ActionKind
from research_os.runtime.registry import register

mode = sys.argv[1]
dsn = sys.argv[2]
repo = Path(sys.argv[3])
artifacts_root = Path(sys.argv[4])
run_id = sys.argv[5] if len(sys.argv) > 5 else ""

EFFECTS = Path(os.environ["EFFECTS_LOG"])
CRASH_IN_NODE = os.environ.get("CRASH_IN_NODE", "")
ACTION = ActionKind(os.environ.get("CYCLE_ACTION", str(ActionKind.INSPECT_REPOSITORY)))
REPLAY_MODE = os.environ.get("REPLAY_MODE", "reconcilable")


def _effect(name: str) -> None:
    with EFFECTS.open("a", encoding="utf-8") as handle:
        handle.write(name + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def counted_action(state: Any, context: CycleContext, plan: Any) -> ActionOutcome:
    """A handler with a visible, countable side effect.

    Appending a line stands in for an ``sbatch``, a commit or a download:
    something the outside world remembers after this process is gone.
    """

    _effect("action")
    if CRASH_IN_NODE == "perform_action":
        os._exit(9)
    ref = context.artifacts.put_text("the action ran", role="action_output")
    return ActionOutcome.succeeded(
        "counted action ran", data={"ran": True}, artifacts=(ref,)
    )


def counted_reconcile(
    state: Any, context: CycleContext, plan: Any
) -> dict[str, Any] | None:
    """Look at the world and report whether the effect already happened.

    What a real reconciler does: ``squeue`` for a submitted job, ``git log`` for
    a commit, the artifact store for a download. Here, the effects log.
    """

    if not [line for line in EFFECTS.read_text().splitlines() if line == "action"]:
        return None
    return {
        "ok": True,
        "detail": "recovered: the action had already run",
        "data": {"ran": True},
    }


if REPLAY_MODE == "unreconcilable":
    # Deliberately invalid to construct through `RegisteredAction`, which is the
    # point: the only way to have an effectful action with no reconciler is to
    # bypass the guard, and the test does so to assert what happens next.
    from research_os.runtime.registry import ACTION_HANDLERS, RegisteredAction

    ACTION_HANDLERS[ACTION] = RegisteredAction.__new__(RegisteredAction)
    object.__setattr__(ACTION_HANDLERS[ACTION], "handler", counted_action)
    object.__setattr__(ACTION_HANDLERS[ACTION], "replay_safe", False)
    object.__setattr__(ACTION_HANDLERS[ACTION], "reconcile", None)
else:
    register(ACTION, counted_action, replay_safe=False, reconcile=counted_reconcile)

router = ScriptedRouter(
    answers={
        "planner": plan_answer(str(ACTION)),
        "scientific_reviewer": review_answer(),
    }
)
config = make_config(dsn, artifacts_root)

with Database(dsn) as db:
    if mode == "start":
        result = start_cycle(
            config=config,
            db=db,
            project_id="alpha-project",
            repo_path=repo,
            objective="find out whether X",
            models=router,
        )
    elif mode == "resume":
        result = resume_cycle(
            config=config, db=db, run_id=run_id, repo_path=repo, models=router
        )
    elif mode == "answer":
        result = resume_cycle(
            config=config,
            db=db,
            run_id=run_id,
            repo_path=repo,
            models=router,
            resume_value={"granted": True},
        )
    else:  # pragma: no cover
        raise SystemExit(f"unknown mode {mode!r}")

print(
    json.dumps(
        {
            "run_id": result.run.run_id,
            "status": str(result.status),
            "terminal_state": str(result.terminal_state)
            if result.terminal_state
            else None,
            "pending_approval_id": result.pending_approval_id,
            "recommendation": result.recommendation,
            "notes": list(result.notes),
        }
    )
)

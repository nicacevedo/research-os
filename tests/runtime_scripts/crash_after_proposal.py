"""Create a proposal for real, then die before the ledger records it.

Run as a subprocess by ``tests/test_runtime_proposals.py``. The window between
"the proposal directory exists" and "the runtime knows it does" is the one that
decides whether a crash leaves one logical proposal or two, and it cannot be
reproduced by raising an exception: an exception runs the ``finally`` blocks
whose absence *is* the failure mode.

The whole production handler runs, over the real ``ProposalController`` and
scripted providers. Only ``InvocationLedger.complete`` is replaced, by something
that dies, so the ledger row stays ``IN_FLIGHT`` -- which is exactly the state a
SIGKILL in that window leaves.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.proposal_helpers import make_controller
from tests.runtime_graph_helpers import make_context

from research_os.runtime.actions import proposals as proposal_action
from research_os.runtime.actions.proposals import propose_capsule_change
from research_os.runtime.db import Database
from research_os.runtime.idempotency import InvocationLedger

dsn = sys.argv[1]
artifacts_root = Path(sys.argv[2])
repo = Path(sys.argv[3])
scripted = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))


def _die_instead_of_completing(*_args: Any, **_kwargs: Any) -> Any:
    # The proposal directory exists. The ledger still says IN_FLIGHT.
    os._exit(23)


InvocationLedger.complete = _die_instead_of_completing  # type: ignore[method-assign]

provider = FakeProvider(
    name="claude",
    family="anthropic",
    responses={
        "planner": [ScriptedResponse(structured=scripted["proposal"])],
        "reviewer": [ScriptedResponse(structured=scripted["assessment"])],
    },
)
controller = make_controller({"claude": provider})
proposal_action._controller = (  # type: ignore[assignment]
    lambda _ctx, authority=None: controller
)

with Database(dsn) as db:
    context = make_context(
        db=db,
        repo=repo,
        artifacts_root=artifacts_root,
        dsn=dsn,
        models=None,
        permitted=(),
    )
    propose_capsule_change(scripted["state"], context, {})
    raise SystemExit("InvocationLedger.complete was never reached")

"""Advance one idea from a leased work item and die inside the stage's model call.

Run as a subprocess by ``tests/test_integrity_action_ownership.py``. The
process holds everything a live stage holds -- the run's session lock, the
ACTIVE action, the work item's lease -- when it is killed with ``os._exit``
inside the literature scout's call, which skips every ``finally`` and
``except``: nothing in this process gets to close the action. What the
database knows afterwards is exactly what a SIGKILL or a lost machine leaves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from tests.runtime_graph_helpers import make_config
from tests.test_portfolio_promotion import (
    TerminologyAwareLiterature,
    TwoPathRouter,
    _router,
)

from research_os.portfolio.config import load_config
from research_os.portfolio.track import advance_idea
from research_os.runtime.db import Database

dsn, project_id, idea_id, work_id = sys.argv[1:5]
artifacts, marker = Path(sys.argv[5]), Path(sys.argv[6])


class DiesInTheScout(TwoPathRouter):
    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            marker.write_text("in the scout call\n", encoding="utf-8")
            os._exit(9)
        return super().complete(request)


with Database(dsn) as db:
    base = _router(db)
    router = DiesInTheScout(answers=base.answers, store=base.store)
    advance_idea(
        runtime_config=make_config(dsn, artifacts),
        portfolio_config=load_config(),
        db=db,
        project_id=project_id,
        idea_id=idea_id,
        models=router,
        literature=TerminologyAwareLiterature(),
        work_id=work_id,
    )
sys.exit(3)

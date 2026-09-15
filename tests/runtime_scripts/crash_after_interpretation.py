"""Interpret an experiment for real, and die before the claim is completed.

Run as a subprocess by ``tests/test_runtime_interpretation.py``. The window
between "the interpretation artifact exists" and "the claim says so" is the one
that decides whether a crash leaves one scientific interpretation of an
experiment or two, and it cannot be reproduced by raising an exception: an
exception runs the ``finally`` blocks whose absence *is* the failure mode.
``os._exit`` skips every finally block, atexit hook and buffer flush, which is
what a SIGKILL or a power loss does.

The whole production handler runs. Only ``complete_interpretation`` is replaced,
by something that records the artifact id it was about to attach and then dies,
so what is being tested is the real artifact bytes and the real claim -- not a
reconstruction of them that could agree with the retry for the wrong reason.

The artifact id goes to the file named by the last argument, fsynced. A file
rather than stdout because a pipe cannot be fsynced, and an unflushed pipe is
exactly what ``os._exit`` discards.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from tests.runtime_graph_helpers import make_context

from research_os.runtime.actions.experiments import interpret_results
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore

dsn = sys.argv[1]
artifacts_root = Path(sys.argv[2])
repo = Path(sys.argv[3])
run_id = sys.argv[4]
job_id = sys.argv[5]
report = Path(sys.argv[6])


def _die_instead_of_completing(
    _self: Any, _interpretation_id: str, *, artifact_id: str | None = None, **_: Any
) -> Any:
    with report.open("w", encoding="utf-8") as handle:
        handle.write(artifact_id or "")
        handle.flush()
        os.fsync(handle.fileno())
    # The artifact exists and the claim still says IN_PROGRESS.
    os._exit(17)


RuntimeStore.complete_interpretation = _die_instead_of_completing  # type: ignore[method-assign]

with Database(dsn) as db:
    job = RuntimeStore(db).get_external_job(job_id)
    assert job is not None, job_id
    context = make_context(
        db=db,
        repo=repo,
        artifacts_root=artifacts_root,
        dsn=dsn,
        models=None,
        permitted=(),
    )
    state = {
        "run_id": run_id,
        "project_id": job.project_id,
        "repo_path": str(repo),
        "objective": "whether X holds",
        "autonomy": "high",
        "cycle_index": 0,
        "artifacts": [],
        "notes": [],
    }
    interpret_results(state, context, {"parameters": {"job_id": job_id}})
    raise SystemExit("complete_interpretation was never reached")

"""The live services a cycle's nodes and action handlers share.

In its own module, not in ``graphs/state.py``, for an unglamorous reason: the
graph dispatches action handlers, the handlers need the services, and if the
services lived with the graph state then every handler would import the graph
and the graph would import every handler. One small module both sides depend on
costs nothing and removes the cycle.

None of this is checkpointed. LangGraph passes it as *runtime context*, which is
scoped to one execution and never serialised -- verified in
``tests/test_runtime_graph.py`` by putting a live connection pool in it and
checkpointing the graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.db import Database
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.interfaces import ModelProvider
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore


@dataclass(slots=True)
class CycleContext:
    """Everything a node may reach, and nothing it may not.

    Note what is absent: any handle that could write a capsule. The only
    scientific access is ``kernel``, which is read-only by construction.
    """

    config: RuntimeConfig
    db: Database
    store: RuntimeStore
    queue: WorkQueue
    ledger: InvocationLedger
    budgets: BudgetLedger
    artifacts: FilesystemArtifactStore
    kernel: ScientificKernelAdapter
    models: ModelProvider
    #: Actions this cycle may choose from, narrowed by the caller from the
    #: policy table and the autonomy setting. A node cannot widen it.
    permitted_actions: tuple[str, ...] = ()
    #: Executors available on this machine, by name.
    executors: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.executors is None:
            self.executors = {}

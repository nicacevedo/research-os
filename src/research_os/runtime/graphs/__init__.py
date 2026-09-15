"""Bounded, resumable reasoning workflows.

One graph per bounded research cycle. A cycle ends -- in a terminal state, or in
a human interrupt, or in a decision to open a successor cycle -- and there is
deliberately no way to express "run forever".

Nothing in this package names a model provider or builds a scheduler command;
``tests/test_runtime_layering.py`` asserts both by reading the source. A node
asks for a capability and submits an ``ExecutionSpec``.
"""

from __future__ import annotations

__all__ = ["CycleState", "build_cycle_graph"]

from research_os.runtime.graphs.cycle import build_cycle_graph
from research_os.runtime.graphs.state import CycleState

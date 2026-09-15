"""The autonomous runtime (R5).

Everything in this package is *operational* state: what is running, what is
owed, what was spent, what crashed and what must be retried. None of it is
scientific truth. Scientific truth stays where ``DESIGN_INVARIANTS.md`` put it
-- in Git-tracked capsule files -- and this package reaches it only through
:mod:`research_os.runtime.kernel`, which can read anything and may write nothing
a person has not authorised.

The separation is the whole design. A runtime that could accept a Claim by
updating a row would be a runtime that can manufacture scientific agreement, and
no amount of provenance recorded afterwards would undo that. So the runtime owns
queues, leases, budgets, retries and checkpoints, and owns no verdict.

Importing this package pulls in nothing heavy. The PostgreSQL driver and
LangGraph live behind the ``runtime`` extra and are imported by the modules that
need them, so a kernel-only install still works with two dependencies.
"""

from __future__ import annotations

__all__ = ["RUNTIME_SCHEMA_VERSION"]

#: The migration version this build expects to find applied. Bumped by the
#: migration that introduces the change, never by hand afterwards.
RUNTIME_SCHEMA_VERSION = "0002"

"""Concrete handlers for the actions a bounded cycle may take.

One module per capability area, and one rule that shapes all of them: a handler
**wraps existing tested code rather than reimplementing it**. The v1 layers
already know how to search literature, isolate a worktree, run acceptance
commands, obtain an independent review and submit to Slurm, and all of that is
covered by two thousand tests. A second implementation living behind a nicer
interface would be a second set of bugs.

So a handler's job is narrow: translate the graph's plan into the v1 layer's
inputs, call it, and translate what came back into an :class:`ActionOutcome`
with artifact references and a failure class. Where a handler is more than that,
it is because the runtime needs something the v1 layer does not offer -- a
frozen spec digest, an independence group -- and the addition is the part worth
reading.
"""

from __future__ import annotations

__all__ = ["ActionHandler", "ActionOutcome"]

from research_os.runtime.actions.base import ActionHandler, ActionOutcome

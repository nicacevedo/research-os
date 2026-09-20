# ADR 0003 — Review independence is classified, enforced, and never overstated

**Status:** accepted
**Date:** 2026-09-20
**Context:** the autonomous discovery portfolio layer

## Decision

Every idea version records the model call that produced it
(`origin_call_id`). Every review records its own call (`call_id`) and a
computed `independence_class`:

```text
DIFFERENT_FAMILY          different provider family
DIFFERENT_MODEL           same family, different model
SAME_MODEL_CLEAN_CONTEXT  same model, frozen packet, no shared context
SAME_CALL                 refused
```

`SAME_CALL` raises rather than degrades. The other three are ordered, reported,
and never relabelled. Reviewer packets have no field for another reviewer's
verdict, so review independence within the board is a property of the *type*
rather than of a caller's discipline.

## Why this and not the alternatives

**Not "a different prompt is independent enough".** It is not, and the failure
mode is silent: the same model with the same training and the same context
window agrees with itself. Recording the class is what makes the weakness
legible.

**Not "fail closed unless a second provider family exists".** On a
single-provider host that means no scientific review ever happens, which is
strictly worse than a labelled degraded review — a different model reading a
frozen packet does catch real defects. `ROADMAP.md` already decided this for
R5 and the reasoning is unchanged. What may not happen is *calling* it
independent.

**Not enforcing independence in the prompt.** "You have not seen other
reviews" in a prompt is a request. A packet type with no field for them is a
mechanism.

**`SAME_CALL` raises rather than degrading** because it cannot arise from a
deployment limitation. It can only arise from a code path that handed the
origin's own invocation to the gate, which is a defect, and a defect that
degrades quietly is a defect that ships.

## Consequences

- `HUMAN_READY` on a single-provider host carries a recorded independence
  shortfall, visible in the digest and in `researchctl ideas show`.
- The system can never report that an idea was independently reviewed when it
  was not, which is the property the whole review apparatus rests on.
- Adding a second provider family is an install, not a code change, and raises
  every subsequent review's class automatically.

## Reversibility

High. The classification is computed from `model_calls`, which already records
provider, model and family; re-deriving it under a different rule is a query.
What is not reversible is data never recorded, which is why `origin_call_id` is
stored on the version rather than inferred later.

"""Which interruptions are a person's decision, and which are only a preference.

v1.0.0's residual risks name this one plainly: *the planner inserts human
checkpoints readily; two of four pilots stopped at one.* One of those two was a
genuine scientific decision and stopping was right. The other was a planner
being careful, and a bounded unattended run that stops to ask whether it should
carry on has not run unattended.

The fix is not to make the planner braver. It is to stop treating "a human
checkpoint" as one thing. Two kinds of stop share a shape and share nothing
else:

**A hard checkpoint** is a boundary of human scientific authority. Recording a
Review, accepting a Claim, changing a prespecified criterion after seeing the
result it was written to judge, promoting a conclusion into another project,
authorising an expensive or irreversible action. No flag reaches these, no
policy suppresses them, and no natural-language goal argues its way past one.

**A discretionary checkpoint** is a planner preferring to ask. Perfectly
reasonable interactively; in an unattended run it is the thing that makes the
run not unattended.

So a checkpoint carries a **typed kind**, and the controller decides two things
about it that the model does not.

*Is this kind eligible here?* A model may write ``claim_acceptance`` on anything.
Whether this project has a Claim to accept is a fact the controller reads from
the capsule, and a hard kind that its own project cannot corroborate is refused
outright rather than honoured on the model's say-so. That is what stops "Would
you like me to continue?" from becoming a scientific boundary by relabelling.

*Is this kind permitted under this run's policy?* ``scientific_only`` permits
hard checkpoints and nothing else. ``standard`` -- the default, and what every
existing run keeps getting -- permits both.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckpointPolicy(StrEnum):
    """How much a run may be interrupted.

    ``STANDARD`` is the default and is exactly v1.0.0's behaviour, so nothing a
    researcher already runs changes. ``SCIENTIFIC_ONLY`` is the unattended mode:
    it narrows what may stop the run, and narrows nothing about what a human
    still has to decide.
    """

    STANDARD = "standard"
    SCIENTIFIC_ONLY = "scientific_only"


class CheckpointKind(StrEnum):
    """What kind of decision a checkpoint is asking a person to make.

    Typed rather than described, because a category the controller can only read
    as prose is a category the controller cannot enforce. Every value below
    except the first names an existing boundary of human authority in
    ``DESIGN_INVARIANTS.md`` or ``docs/CAPSULE.md``; none of them is new
    authority invented for this release.
    """

    DISCRETIONARY = "discretionary"
    """The planner would prefer to ask. Not a boundary of anything."""

    HUMAN_REVIEW = "human_review"
    """A human Review of a Claim. No automation may author one."""

    CLAIM_ACCEPTANCE = "claim_acceptance"
    """Moving a Claim to accepted. Requires a qualifying human Review."""

    PRESPECIFIED_CHANGE = "prespecified_change"
    """Changing a criterion that was written before the result was known.

    The one that matters most, and the one a capable model is most likely to
    walk into while being helpful. A threshold rewritten after seeing what it
    decides is not the same threshold, and a result re-judged against it is not
    the result that was preregistered.
    """

    CROSS_PROJECT_PROMOTION = "cross_project_promotion"
    """Carrying a conclusion out of the project that owns it."""

    COSTLY_AUTHORIZATION = "costly_authorization"
    """Spending real compute, money, or anything else not easily undone."""


#: Every kind that is a boundary of human authority rather than a preference.
HARD_CHECKPOINT_KINDS: frozenset[CheckpointKind] = frozenset(
    {
        CheckpointKind.HUMAN_REVIEW,
        CheckpointKind.CLAIM_ACCEPTANCE,
        CheckpointKind.PRESPECIFIED_CHANGE,
        CheckpointKind.CROSS_PROJECT_PROMOTION,
        CheckpointKind.COSTLY_AUTHORIZATION,
    }
)


def is_hard(kind: CheckpointKind) -> bool:
    return kind in HARD_CHECKPOINT_KINDS


#: The deterministic reason a discretionary checkpoint is refused.
#:
#: One string, used by the validator and quoted verbatim into the planner's one
#: bounded correction, so the planner is told the actual rule rather than a
#: paraphrase of it.
SCIENTIFIC_ONLY_REASON = (
    "scientific_only mode permits only hard checkpoints: a human Review, a Claim "
    "acceptance, a change to a criterion that was prespecified before the result "
    "was known, a cross-project promotion, or authorising something expensive or "
    "irreversible. A checkpoint that asks the researcher whether to continue, "
    "which option to prefer, or whether the plan looks right is discretionary, "
    "and this run was started to complete without one."
)


@dataclass(frozen=True, slots=True)
class CheckpointContext:
    """What the controller knows that decides whether a hard kind is even possible.

    Read from the capsule and from the run's own authorisations, never from the
    plan. A model may write any kind it likes into a plan; whether this project
    has a Claim to accept, or a prespecified criterion to change, is a fact about
    the project, and the fact is what decides.
    """

    capsule_present: bool = False
    claim_count: int = 0
    prespecified_count: int = 0
    """Objects carrying an ex-ante commitment: experiments and hypotheses."""

    execute_experiments: bool = False

    @classmethod
    def empty(cls) -> CheckpointContext:
        return cls()


def eligibility_failure(
    kind: CheckpointKind,
    *,
    context: CheckpointContext,
    experiment_tasks: int,
) -> str | None:
    """Return why ``kind`` cannot be a hard checkpoint here, or ``None``.

    The corroboration is deliberately weak-but-real: it does not try to decide
    whether *this particular* checkpoint is genuine, which is not a decidable
    question. It refuses the cases where the claimed kind is impossible -- a
    Claim acceptance in a project with no Claims, a prespecified-criterion change
    in a project with nothing prespecified, an expensive-action authorisation in
    a run that cannot spend anything.

    That is enough to close the relabelling path, because relabelling is only
    useful to a model that wants to stop a run it has no genuine reason to stop,
    and such a run is usually one where the claimed kind has nothing behind it.
    """

    if kind is CheckpointKind.DISCRETIONARY:
        return None
    if kind in {CheckpointKind.HUMAN_REVIEW, CheckpointKind.CLAIM_ACCEPTANCE}:
        if not context.capsule_present:
            return (
                f"this project holds no Research Capsule, so it has no Claim to "
                f"review or accept and {kind.value!r} cannot be what this "
                "checkpoint is about"
            )
        if context.claim_count == 0:
            return (
                f"this project holds no Claim, so {kind.value!r} cannot be what "
                "this checkpoint is about"
            )
        return None
    if kind is CheckpointKind.PRESPECIFIED_CHANGE:
        if not context.capsule_present:
            return (
                "this project holds no Research Capsule, so it has no "
                "prespecified criterion to change"
            )
        if context.prespecified_count == 0:
            return (
                "this project holds no Experiment or Hypothesis, so there is no "
                "prespecified criterion for this checkpoint to be about"
            )
        return None
    if kind is CheckpointKind.CROSS_PROJECT_PROMOTION:
        if not context.capsule_present:
            return (
                "this project holds no Research Capsule, so it has no scientific "
                "conclusion to promote into another project"
            )
        return None
    if kind is CheckpointKind.COSTLY_AUTHORIZATION:
        if experiment_tasks == 0 and not context.execute_experiments:
            return (
                "this plan spends nothing that needs authorising: it has no "
                "experiment task and this run was not authorised to execute one"
            )
        return None
    return None  # pragma: no cover - the enum is exhaustive above

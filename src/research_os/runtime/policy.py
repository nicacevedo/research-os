"""What the runtime may do on its own, and what it must ask about.

Two orthogonal questions, kept orthogonal because conflating them produces
either a system that cannot work or a system that can do anything.

**Autonomy** is about scientific authority. Three levels:

```text
A0  autonomous, read-only or reversible
A1  autonomous, isolated and bounded side effects
A2  explicit human scientific authority required
```

The design target is that nearly all routine work is `A0` or `A1`, and that
`A2` is rare and meaningful. An `A2` list that grows until the researcher is
answering prompts all day has failed in the same way as an `A2` list that is
empty -- the first makes the system useless, the second makes it untrustworthy.
So `A2` contains exactly the actions where a wrong answer is a *scientific*
error that provenance cannot repair: changing a preregistered endpoint after
seeing results, promoting a contested claim, redefining the objective,
publishing.

**Permission** is about blast radius. A role gets the minimum it needs. A
literature search has no business holding a capability that can write a
worktree, and a builder has no business holding one that can publish. The
default is not "an agent has a shell and the network"; the default is nothing,
and each role's grant is written down here where it can be read in one sitting.

The two are checked together by :func:`authorize`, and both must pass. A
cheap action a role is not permitted to take is refused; a permitted action that
needs scientific authority is refused until the authority exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from research_os.errors import ResearchOSError
from research_os.runtime.interfaces import ModelRole


class AutonomyLevel(StrEnum):
    A0 = "A0"
    A1 = "A1"
    A2 = "A2"


_ORDER = {AutonomyLevel.A0: 0, AutonomyLevel.A1: 1, AutonomyLevel.A2: 2}


class Permission(StrEnum):
    """A capability a role may hold.

    Named ``Permission`` rather than ``Capability`` because
    :class:`research_os.runtime.interfaces.Capability` already means "what kind
    of thinking this model request needs", and the two being confusable in a
    security check is not a risk worth the tidier name.
    """

    READ_REPO = "READ_REPO"
    WRITE_WORKTREE = "WRITE_WORKTREE"
    WRITE_CAPSULE = "WRITE_CAPSULE"
    RUN_LOCAL = "RUN_LOCAL"
    SUBMIT_SLURM = "SUBMIT_SLURM"
    NETWORK_READ = "NETWORK_READ"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    DELETE = "DELETE"
    PUBLISH = "PUBLISH"


class ActionKind(StrEnum):
    """The closed set of things the runtime knows how to do.

    Closed, and that is the point: an action with no policy entry is refused
    when it is planned rather than discovered halfway through. A planner that
    invents a new verb gets a validation failure, not an unreviewed side
    effect.
    """

    # --- A0: read-only or reversible -------------------------------------
    INSPECT_REPOSITORY = "inspect_repository"
    VALIDATE_CAPSULE = "validate_capsule"
    ASSESS_FRONTIER = "assess_frontier"
    SEARCH_LITERATURE = "search_literature"
    FETCH_LITERATURE = "fetch_literature"
    PARSE_LITERATURE = "parse_literature"
    PROPOSE_HYPOTHESES = "propose_hypotheses"
    CRITIQUE_HYPOTHESES = "critique_hypotheses"
    DESIGN_EXPERIMENT = "design_experiment"
    REVIEW_SCIENCE = "review_science"
    REVIEW_CODE = "review_code"
    AUDIT_CITATIONS = "audit_citations"
    REBUILD_DERIVED_INDEX = "rebuild_derived_index"

    # --- A1: isolated, bounded side effects -------------------------------
    CREATE_WORKTREE = "create_worktree"
    EDIT_IN_WORKTREE = "edit_in_worktree"
    RUN_CHECKS = "run_checks"
    COMMIT_CANDIDATE = "commit_candidate"
    RUN_LOCAL_EXPERIMENT = "run_local_experiment"
    SUBMIT_CLUSTER_EXPERIMENT = "submit_cluster_experiment"
    DRAFT_MANUSCRIPT = "draft_manuscript"
    NOMINATE_INSIGHT = "nominate_insight"
    PROPOSE_CAPSULE_CHANGE = "propose_capsule_change"

    # --- A2: human scientific authority ----------------------------------
    CHANGE_PRIMARY_ENDPOINT = "change_primary_endpoint"
    CHANGE_PREREGISTRATION = "change_preregistration"
    PROMOTE_CONTESTED_CLAIM = "promote_contested_claim"
    ACCEPT_CLAIM = "accept_claim"
    CHANGE_PROJECT_OBJECTIVE = "change_project_objective"
    INTEGRATE_TO_CANONICAL_BRANCH = "integrate_to_canonical_branch"
    PUBLISH_EXTERNALLY = "publish_externally"
    DELETE_SCIENTIFIC_STATE = "delete_scientific_state"


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    level: AutonomyLevel
    permissions: frozenset[Permission]
    #: One line, shown to a human when the action is refused or gated. Written
    #: for the researcher rather than for the log.
    rationale: str
    #: True when the *person* performs the action, not the runtime -- even after
    #: they have approved it.
    #:
    #: This is the distinction that keeps the authority model honest, and it took
    #: a failing test to notice it was missing. An `A2` action divides into two
    #: kinds. Some are operational and the runtime can carry them out once
    #: authorised: changing a run's objective is a row. Others are *inherently*
    #: human acts because performing them means writing canonical scientific
    #: state, merging to a canonical branch, or publishing -- and the runtime has
    #: no method that does any of those, by construction.
    #:
    #: For the second kind, approval does not unlock execution. It unlocks a
    #: *recorded decision* plus the exact command the researcher should run. A
    #: runtime that quietly performed these once approved would have acquired
    #: scientific authority by the back door, one convenience at a time.
    human_executes: bool = False
    #: What to tell the researcher to run, when ``human_executes`` is true.
    #: ``{subject}`` is filled from the plan where available.
    follow_up: str = ""


#: The policy table. Every :class:`ActionKind` appears exactly once;
#: ``tests/test_runtime_policy.py`` fails if one does not, so adding a verb
#: without deciding its authority is a test failure rather than a default-open.
ACTIONS: dict[ActionKind, ActionPolicy] = {
    ActionKind.INSPECT_REPOSITORY: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Reads files; changes nothing.",
    ),
    ActionKind.VALIDATE_CAPSULE: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "The kernel validator never writes.",
    ),
    ActionKind.ASSESS_FRONTIER: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Derived from capsule files by ordinary Python.",
    ),
    ActionKind.SEARCH_LITERATURE: ActionPolicy(
        AutonomyLevel.A0, frozenset({Permission.NETWORK_READ}), "Read-only API queries."
    ),
    ActionKind.FETCH_LITERATURE: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.NETWORK_READ}),
        "Downloads permitted full text into the artifact store.",
    ),
    ActionKind.PARSE_LITERATURE: ActionPolicy(
        AutonomyLevel.A0, frozenset(), "Local parsing of already-fetched bytes."
    ),
    ActionKind.PROPOSE_HYPOTHESES: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Produces candidates for a person to promote; promotes nothing.",
    ),
    ActionKind.CRITIQUE_HYPOTHESES: ActionPolicy(
        AutonomyLevel.A0, frozenset({Permission.READ_REPO}), "Produces findings only."
    ),
    ActionKind.DESIGN_EXPERIMENT: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Writes a specification, runs nothing.",
    ),
    ActionKind.REVIEW_SCIENCE: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Reads a frozen packet and returns a structured verdict; records no approval.",
    ),
    ActionKind.REVIEW_CODE: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Reads a diff; changes nothing.",
    ),
    ActionKind.AUDIT_CITATIONS: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Checks references against evidence.",
    ),
    ActionKind.REBUILD_DERIVED_INDEX: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "The index is rebuildable by definition; losing it loses no science.",
    ),
    ActionKind.CREATE_WORKTREE: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.READ_REPO, Permission.WRITE_WORKTREE}),
        "Isolated checkout off a frozen base commit; the canonical checkout is untouched.",
    ),
    ActionKind.EDIT_IN_WORKTREE: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.WRITE_WORKTREE}),
        "Bounded to declared paths inside one worktree.",
    ),
    ActionKind.RUN_CHECKS: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.RUN_LOCAL}),
        "Runs the project's own acceptance commands, which runs the project's code.",
    ),
    ActionKind.COMMIT_CANDIDATE: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.WRITE_WORKTREE}),
        "Commits on the isolated branch only. Never on a canonical branch.",
    ),
    ActionKind.RUN_LOCAL_EXPERIMENT: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.RUN_LOCAL}),
        "Declared command, frozen spec, bounded wall clock.",
    ),
    ActionKind.SUBMIT_CLUSTER_EXPERIMENT: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.SUBMIT_SLURM}),
        "Declared resources, against the cluster budget.",
    ),
    ActionKind.DRAFT_MANUSCRIPT: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.WRITE_WORKTREE}),
        "Transforms supported scientific state into prose in an isolated worktree.",
    ),
    ActionKind.NOMINATE_INSIGHT: ActionPolicy(
        AutonomyLevel.A1,
        frozenset(),
        "Nominates for cross-project promotion; a person promotes.",
    ),
    ActionKind.PROPOSE_CAPSULE_CHANGE: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.READ_REPO}),
        "Writes a proposal the v1 proposal layer requires a person to promote.",
    ),
    ActionKind.CHANGE_PRIMARY_ENDPOINT: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Changing the prespecified test after seeing results is the classic way to "
        "turn a null result into a positive one. Only a person may do it, and the "
        "change becomes a recorded Decision.",
        human_executes=True,
        follow_up="Edit the Experiment manifest and record a Decision object, then re-run `researchctl validate-project`.",
    ),
    ActionKind.CHANGE_PREREGISTRATION: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "A materially different experiment is a new preregistration, not an edit.",
        human_executes=True,
        follow_up="Create a new Experiment rather than editing the existing one, and supersede the old manifest.",
    ),
    ActionKind.PROMOTE_CONTESTED_CLAIM: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Contrary evidence exists and has not been answered.",
        human_executes=True,
        follow_up="Run `researchctl review {subject}` to record your Review interactively.",
    ),
    ActionKind.ACCEPT_CLAIM: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Acceptance requires a qualifying human Review. No agent may record one.",
        human_executes=True,
        follow_up="Run `researchctl review {subject}` to record your Review interactively.",
    ),
    ActionKind.CHANGE_PROJECT_OBJECTIVE: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "The objective is the researcher's, not the runtime's.",
        human_executes=True,
        follow_up="Start a new objective with `researchctl runtime start` rather than redirecting this one.",
    ),
    ActionKind.INTEGRATE_TO_CANONICAL_BRANCH: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_WORKTREE}),
        "Merging is a human act in this repository, by policy and by habit.",
        human_executes=True,
        follow_up="Review the candidate branch and merge it yourself; the runtime never pushes or merges.",
    ),
    ActionKind.PUBLISH_EXTERNALLY: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.PUBLISH}),
        "Publication is irreversible in the way that matters: other people read it.",
        human_executes=True,
        follow_up="Submit or release it yourself. Nothing here talks to the outside world on your behalf.",
    ),
    ActionKind.DELETE_SCIENTIFIC_STATE: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.DELETE}),
        "Deleting science is never routine maintenance.",
        human_executes=True,
        follow_up="Delete it yourself with an ordinary reviewable Git commit.",
    ),
}


#: What each model role is permitted to hold. The minimum for its job, and
#: nothing that would let it do someone else's.
#:
#: Note what the reviewers do *not* have. A scientific reviewer cannot write, run
#: or fetch: it reads a frozen packet and returns a verdict, so no amount of
#: prompt injection in the material it reviews can turn it into an actor.
ROLE_PERMISSIONS: dict[ModelRole, frozenset[Permission]] = {
    ModelRole.PLANNER: frozenset({Permission.READ_REPO}),
    ModelRole.BLIND_EXPLORER: frozenset(),
    ModelRole.SEEDED_EXPLORER: frozenset({Permission.READ_REPO}),
    ModelRole.SKEPTIC: frozenset({Permission.READ_REPO}),
    ModelRole.EXPERIMENTALIST: frozenset({Permission.READ_REPO}),
    ModelRole.SCIENTIFIC_REVIEWER: frozenset(),
    ModelRole.CODE_REVIEWER: frozenset({Permission.READ_REPO}),
    ModelRole.AUTHOR: frozenset({Permission.READ_REPO, Permission.WRITE_WORKTREE}),
    ModelRole.REFEREE: frozenset(),
    ModelRole.EXTRACTOR: frozenset(),
    ModelRole.FRONTIER: frozenset({Permission.READ_REPO}),
}

#: What the runtime may hold at each configured autonomy setting. The *setting*
#: is how cautious the researcher wants to be; the *action level* is how much
#: authority the action needs. Both are consulted.
AUTONOMY_GRANTS: dict[str, frozenset[Permission]] = {
    "low": frozenset({Permission.READ_REPO, Permission.NETWORK_READ}),
    "medium": frozenset(
        {
            Permission.READ_REPO,
            Permission.NETWORK_READ,
            Permission.WRITE_WORKTREE,
            Permission.RUN_LOCAL,
        }
    ),
    "high": frozenset(
        {
            Permission.READ_REPO,
            Permission.NETWORK_READ,
            Permission.WRITE_WORKTREE,
            Permission.RUN_LOCAL,
            Permission.SUBMIT_SLURM,
        }
    ),
}


class PolicyRefusedError(ResearchOSError):
    """Raised when an action is not permitted at the current settings."""


class ScientificGateError(ResearchOSError):
    """Raised when an action needs human scientific authority it does not have.

    Distinct from :class:`PolicyRefusedError` because the handling is different:
    a refused action is a planning error, and a gated one is a decision packet
    to prepare.
    """


def policy_for(action: ActionKind) -> ActionPolicy:
    policy = ACTIONS.get(action)
    if policy is None:  # pragma: no cover - the completeness test prevents this
        raise PolicyRefusedError(f"{action} has no policy entry and is refused")
    return policy


def level_for(action: ActionKind) -> AutonomyLevel:
    return policy_for(action).level


def granted_permissions(autonomy: str) -> frozenset[Permission]:
    grants = AUTONOMY_GRANTS.get(autonomy)
    if grants is None:
        raise PolicyRefusedError(f"unknown autonomy setting: {autonomy!r}")
    return grants


def authorize(
    action: ActionKind,
    *,
    autonomy: str,
    role: ModelRole | None = None,
    approval_granted: bool = False,
) -> ActionPolicy:
    """Authorise one action, or raise saying precisely what is missing.

    Order matters. Permission is checked first, because "you are not allowed to
    do that at all" is a better answer than "please authorise this thing you
    could not do anyway". Scientific authority is checked last, because an `A2`
    action that clears every other check is exactly the thing a person should be
    asked about.
    """

    policy = policy_for(action)

    if policy.human_executes:
        # The permissions on a human-executed action describe what *performing*
        # it entails -- WRITE_CAPSULE, PUBLISH -- and the person performing it
        # holds them. The runtime's part is to prepare a packet and record a
        # decision, which needs no permission at all. Checking the runtime
        # against permissions it will never use made the human gate unreachable,
        # which a failing test found: the cycle refused the action instead of
        # asking about it, and the researcher was never consulted.
        if not approval_granted:
            raise ScientificGateError(
                f"{action} requires explicit human scientific authority, and you "
                f"perform it yourself. {policy.rationale}"
            )
        return policy

    available = granted_permissions(autonomy)
    if role is not None:
        available = available & ROLE_PERMISSIONS.get(role, frozenset())
    missing = policy.permissions - available
    if missing:
        wanted = ", ".join(sorted(str(item) for item in missing))
        where = f" for role {role}" if role else ""
        raise PolicyRefusedError(
            f"{action} needs {wanted}{where}, which the current autonomy setting "
            f"({autonomy}) does not grant. {policy.rationale}"
        )
    if policy.level is AutonomyLevel.A2 and not approval_granted:
        raise ScientificGateError(
            f"{action} requires explicit human scientific authority. {policy.rationale}"
        )
    return policy


def at_most(level: AutonomyLevel, ceiling: AutonomyLevel) -> bool:
    return _ORDER[level] <= _ORDER[ceiling]


def autonomous_actions() -> tuple[ActionKind, ...]:
    """Every action the runtime can take without asking anyone."""

    return tuple(
        action
        for action, policy in ACTIONS.items()
        if policy.level is not AutonomyLevel.A2
    )


def gated_actions() -> tuple[ActionKind, ...]:
    """Every action that stops for a person. Kept short on purpose."""

    return tuple(
        action for action, policy in ACTIONS.items() if policy.level is AutonomyLevel.A2
    )


def human_executed_actions() -> tuple[ActionKind, ...]:
    """Actions the *person* performs, even after approving them.

    Every one of them would require writing canonical scientific state, merging
    to a canonical branch, or publishing. The runtime has no method that does
    any of those, so approval unlocks a recorded decision and an instruction,
    never an execution.
    """

    return tuple(action for action, policy in ACTIONS.items() if policy.human_executes)

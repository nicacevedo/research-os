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
    DERIVE_MATHEMATICS = "derive_mathematics"
    DESIGN_EXPERIMENT = "design_experiment"
    INTERPRET_RESULTS = "interpret_results"
    REVIEW_SCIENCE = "review_science"
    REFEREE_MANUSCRIPT = "referee_manuscript"
    AUDIT_CITATIONS = "audit_citations"
    REBUILD_DERIVED_INDEX = "rebuild_derived_index"

    # --- A1: isolated, bounded side effects -------------------------------
    #
    # Note what is *not* here: create_worktree, run_checks, commit_candidate and
    # a standalone code review. Those are phases of one coding pipeline, not
    # things a planner chooses between, and listing them made `runtime doctor`
    # report three gaps that were not gaps -- the pipeline performs all of them
    # and records the reviewer's verdict. `edit_in_worktree` is the whole
    # lifecycle, dispatched once.
    EDIT_IN_WORKTREE = "edit_in_worktree"
    RUN_LOCAL_EXPERIMENT = "run_local_experiment"
    SUBMIT_CLUSTER_EXPERIMENT = "submit_cluster_experiment"
    DRAFT_MANUSCRIPT = "draft_manuscript"
    NOMINATE_INSIGHT = "nominate_insight"
    PROPOSE_CAPSULE_CHANGE = "propose_capsule_change"

    # --- A0/A1: the discovery portfolio -----------------------------------
    #
    # Dispatched by the idea track and the portfolio tick, never by a planner.
    # They are in this table anyway, because one authority model that covers
    # everything is worth more than a tidy separation that would let a second
    # table quietly disagree with this one about what needs a person.
    GENERATE_IDEAS = "generate_ideas"
    DEDUPLICATE_IDEAS = "deduplicate_ideas"
    SCREEN_NOVELTY = "screen_novelty"
    FALSIFY_IDEA = "falsify_idea"
    SHARPEN_IDEA = "sharpen_idea"
    AUDIT_NOVELTY = "audit_novelty"
    REVIEW_IDEA = "review_idea"
    META_REVIEW_IDEA = "meta_review_idea"
    REPLICATE_IDEA = "replicate_idea"
    BRANCH_IDEA = "branch_idea"
    ASSIGN_QUALITY_TIER = "assign_quality_tier"
    RETIRE_IDEA = "retire_idea"
    REVIVE_IDEA = "revive_idea"
    PRODUCE_PORTFOLIO_DIGEST = "produce_portfolio_digest"
    CURATE_IDEA_BANK = "curate_idea_bank"

    # --- A2: human scientific authority ----------------------------------
    CHANGE_PRIMARY_ENDPOINT = "change_primary_endpoint"
    CHANGE_PREREGISTRATION = "change_preregistration"
    PROMOTE_CONTESTED_CLAIM = "promote_contested_claim"
    ACCEPT_CLAIM = "accept_claim"
    CHANGE_PROJECT_OBJECTIVE = "change_project_objective"
    INTEGRATE_TO_CANONICAL_BRANCH = "integrate_to_canonical_branch"
    PUBLISH_EXTERNALLY = "publish_externally"
    DELETE_SCIENTIFIC_STATE = "delete_scientific_state"


class Dispatch(StrEnum):
    """Which layer performs an action, once it is authorised.

    Added when the discovery portfolio arrived. Before it there was one
    dispatcher -- the cycle graph's ``ACTION_HANDLERS`` -- so "has a policy but
    no handler" and "this build cannot do it" were the same statement, and
    ``runtime doctor`` reported the second by computing the first.

    They are no longer the same statement. A portfolio stage is authorised out
    of this same table, because one authority model is the point, and is
    dispatched by the idea track rather than by a planner choosing it. Without
    this field doctor would report fifteen gaps that are not gaps, which is how
    a warning stops being read.
    """

    CYCLE = "cycle"
    PORTFOLIO = "portfolio"
    HUMAN = "human"


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
    #: Which layer performs it. See :class:`Dispatch`.
    dispatch: Dispatch = Dispatch.CYCLE


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
    # A0 for the same reason `critique_hypotheses` is: it reads the capsule,
    # asks a model, and writes an artifact plus a noncanonical finding. A
    # derivation is not a proof the project holds -- it is a document a person
    # may promote to one, by exactly the route every other finding takes.
    ActionKind.DERIVE_MATHEMATICS: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Derives; establishes nothing until a person accepts it.",
    ),
    ActionKind.DESIGN_EXPERIMENT: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Writes a specification, runs nothing.",
    ),
    ActionKind.INTERPRET_RESULTS: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Compares results to the criteria that were fixed before them. It "
        "establishes the facts and cannot move the criteria: a post-hoc change "
        "is change_primary_endpoint, which a person performs.",
    ),
    ActionKind.REVIEW_SCIENCE: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Reads a frozen packet and returns a structured verdict; records no approval.",
    ),
    ActionKind.REFEREE_MANUSCRIPT: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Reads a draft and its evidence and returns findings; approves nothing.",
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
    ActionKind.EDIT_IN_WORKTREE: ActionPolicy(
        AutonomyLevel.A1,
        frozenset(
            {Permission.READ_REPO, Permission.WRITE_WORKTREE, Permission.RUN_LOCAL}
        ),
        "The whole coding lifecycle in one isolated worktree: build, check, "
        "independently review, one bounded repair. It leaves uncommitted changes "
        "on an isolated branch -- nothing here commits, merges or pushes. "
        "RUN_LOCAL because running a project's acceptance commands runs that "
        "project's code, including code the builder just wrote.",
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
    # ---- the discovery portfolio ----------------------------------------
    #
    # Every one of these is A0 except the Curator, and the reason is uniform: a
    # portfolio idea is a *candidate direction*, it is not scientific state,
    # and no action here can make it into any. The one that touches the world
    # outside the database is the Curator, which commits to a reserved branch
    # in the project repository and never to the researcher's own.
    ActionKind.GENERATE_IDEAS: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Produces candidate directions. A candidate is not a hypothesis and "
        "nothing about producing one changes what the project holds.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.DEDUPLICATE_IDEAS: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Compares candidates with each other; the durable identity is a "
        "deterministic hash and never a model's opinion.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.SCREEN_NOVELTY: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.NETWORK_READ}),
        "A cheap read-only search, to kill obvious duplicates before anything "
        "is spent on them.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.FALSIFY_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Tries to kill the idea. Producing a fatal objection is the successful "
        "outcome, not the failed one.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.SHARPEN_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.READ_REPO}),
        "Makes a candidate precise, or reports that it cannot be made precise, "
        "which ends the track.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.AUDIT_NOVELTY: ActionPolicy(
        AutonomyLevel.A0,
        frozenset({Permission.NETWORK_READ}),
        "Retrieves primary sources and builds a novelty matrix from them. Every "
        "row cites a work that was actually retrieved.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.REVIEW_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Reads a frozen packet and returns a structured verdict. Records no "
        "approval and reaches no repository.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.META_REVIEW_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Synthesises completed reviews into a recommendation the deterministic "
        "gate may lower and may never raise.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.REPLICATE_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Second-line verification. What it is allowed to be is fixed per "
        "adjudication type and is not the replicator's choice.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.BRANCH_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Opens child directions with recorded lineage, under the configured "
        "branching bounds.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.ASSIGN_QUALITY_TIER: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Records how far an idea got through this system's own gates. It is "
        "not a scientific status and no capsule object changes.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.RETIRE_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Rejects, parks or supersedes a candidate. Nothing is deleted and the "
        "reason is required.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.REVIVE_IDEA: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Opens a new candidate whose lineage names a retired one, carrying its "
        "unanswered objections forward.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.PRODUCE_PORTFOLIO_DIGEST: ActionPolicy(
        AutonomyLevel.A0,
        frozenset(),
        "Renders what happened from stored fields. Deterministic; no model "
        "writes a word of it.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.CURATE_IDEA_BANK: ActionPolicy(
        AutonomyLevel.A1,
        frozenset({Permission.READ_REPO, Permission.WRITE_WORKTREE}),
        "Commits a deterministic rendering of the bank to a reserved branch in "
        "the project repository, from a worktree of its own. It never writes "
        "under .research/, never touches the researcher's branch, and never "
        "merges or pushes.",
        dispatch=Dispatch.PORTFOLIO,
    ),
    ActionKind.CHANGE_PRIMARY_ENDPOINT: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Changing the prespecified test after seeing results is the classic way to "
        "turn a null result into a positive one. Only a person may do it, and the "
        "change becomes a recorded Decision.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Edit the Experiment manifest and record a Decision object, then re-run `researchctl validate-project`.",
    ),
    ActionKind.CHANGE_PREREGISTRATION: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "A materially different experiment is a new preregistration, not an edit.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Create a new Experiment rather than editing the existing one, and supersede the old manifest.",
    ),
    ActionKind.PROMOTE_CONTESTED_CLAIM: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Contrary evidence exists and has not been answered.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Run `researchctl review {subject}` to record your Review interactively.",
    ),
    ActionKind.ACCEPT_CLAIM: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "Acceptance requires a qualifying human Review. No agent may record one.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Run `researchctl review {subject}` to record your Review interactively.",
    ),
    ActionKind.CHANGE_PROJECT_OBJECTIVE: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_CAPSULE}),
        "The objective is the researcher's, not the runtime's.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Start a new objective with `researchctl runtime start` rather than redirecting this one.",
    ),
    ActionKind.INTEGRATE_TO_CANONICAL_BRANCH: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.WRITE_WORKTREE}),
        "Merging is a human act in this repository, by policy and by habit.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up=(
            "Inspect the worktree's branch (`researchctl runtime run <id>` names "
            "it), commit what you want, and merge it yourself. The runtime "
            "commits nothing."
        ),
    ),
    ActionKind.PUBLISH_EXTERNALLY: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.PUBLISH}),
        "Publication is irreversible in the way that matters: other people read it.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Submit or release it yourself. Nothing here talks to the outside world on your behalf.",
    ),
    ActionKind.DELETE_SCIENTIFIC_STATE: ActionPolicy(
        AutonomyLevel.A2,
        frozenset({Permission.DELETE}),
        "Deleting science is never routine maintenance.",
        human_executes=True,
        dispatch=Dispatch.HUMAN,
        follow_up="Delete it yourself with an ordinary reviewable Git commit.",
    ),
}


#: What each model role is permitted to hold. The minimum for its job, and
#: nothing that would let it do someone else's.
#:
#: Note what the reviewers do *not* have. A scientific reviewer cannot write, run
#: or fetch: it reads a frozen packet and returns a verdict, so no amount of
#: prompt injection in the material it reviews can turn it into an actor.
#:
#: **Where this is enforced, and where it is not.** An independent review
#: observed that :func:`authorize` accepts a ``role`` and no caller passes one,
#: and suggested wiring it in. Trying that showed why it was not wired: an
#: *action's* permissions say what the runtime needs in order to perform it
#: (``NETWORK_READ`` to query OpenAlex, ``RUN_LOCAL`` to run a project's tests),
#: while a *role's* say what a model may be handed. Intersecting them refused
#: literature search and every coding task, because the extractor does not
#: "hold" the network and the author does not "hold" the test runner -- the
#: runtime does, on their behalf.
#:
#: The real enforcement of role limits is at the provider boundary, and it is
#: stronger than a permission check: every model request this runtime builds is
#: ``read_only=True`` with no ``access``, which
#: :class:`research_os.automation.providers.InvocationRequest` resolves to
#: ``Access.CONTEXT_ONLY`` and which force-empties the tool set. Every runtime
#: model call is therefore tool-less -- it cannot read a file, run a command or
#: reach the network whatever its role says. ``tests/test_runtime_routing.py``
#: asserts it.
#:
#: So this table is the *specification* of least privilege, and the tool-less
#: invocation is its enforcement. Both are documented here rather than one
#: pretending to be the other.
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
    # Nothing. It reads findings that were handed to it and answers one
    # question about them; it has no business reaching a repository, and
    # the knowledge it judges is about to be offered to *other* projects.
    ModelRole.NOMINATOR: frozenset(),
    # Reads the capsule, because a derivation that does not quote the
    # assumption it rests on is a derivation of nothing. Writes nothing.
    ModelRole.DERIVER: frozenset({Permission.READ_REPO}),
    # --- the discovery portfolio -----------------------------------------
    #
    # Every reviewer holds nothing, which is the same rule the scientific
    # reviewer already follows: it reads a frozen packet and returns a verdict,
    # so no amount of prompt injection in the material it reviews can turn it
    # into an actor.
    #
    # The explorers and the falsifier read the repository because a direction
    # proposed without reference to what the project has already established is
    # a direction about nothing. The failure-mining explorer does not: its
    # input is the portfolio's own record of what failed, handed to it.
    ModelRole.FAILURE_MINING_EXPLORER: frozenset(),
    ModelRole.SCIENTIFIC_DISCOVERY: frozenset({Permission.READ_REPO}),
    ModelRole.LITERATURE_SCOUT: frozenset({Permission.NETWORK_READ}),
    ModelRole.FALSIFIER: frozenset({Permission.READ_REPO}),
    ModelRole.METHODOLOGY_REVIEWER: frozenset(),
    ModelRole.NOVELTY_REVIEWER: frozenset(),
    ModelRole.SKEPTIC_REVIEWER: frozenset(),
    ModelRole.REPLICATOR: frozenset(),
    ModelRole.META_REVIEWER: frozenset(),
    # Nothing. It is shown two pieces of text and asked whether they are the
    # same idea; a repository would tell it nothing it needs and would give it
    # somewhere to go.
    ModelRole.DUPLICATE_ADJUDICATOR: frozenset(),
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

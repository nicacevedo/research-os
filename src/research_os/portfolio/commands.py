"""``researchctl portfolio``, ``researchctl ideas`` and ``researchctl seed``.

The interaction this is shaped around is the one the brief describes: a
researcher comes back after several days and reads two things.

```bash
researchctl portfolio digest        # what happened
researchctl ideas human-ready       # what is worth looking at
```

Everything else here is for when one of those raises a question. There is
deliberately no verb for "advance this idea", "run the next stage" or "promote
this" -- the first two are the portfolio's job and a researcher needing one
means the allocator has failed, and the third is `researchctl propose promote`,
which already exists and which a person performs.

**Every listing says what kind of object it is showing.** ``researchctl ideas``
prints ``PIDEA-`` identifiers, and the capsule's own Idea objects are
``IDEA-0001``. The two are different things with the same English word, and a
researcher reading a list that did not say so would reasonably assume these
were their capsule's.

**And every tier line carries what was actually done.** ``VALIDATED`` is a
scientific word; the counts beside it -- executions, sources, distinct reviewer
models -- are what the word is standing on, and they are computed rather than
asserted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research_os.errors import EXIT_ERROR, EXIT_OK, ResearchOSError
from research_os.portfolio import digest as digest_module
from research_os.portfolio.config import load_config
from research_os.portfolio.gates import board_independence
from research_os.portfolio.models import (
    ActionStatus,
    EvidenceKind,
    EvidenceStrength,
    IdeaAction,
    IdeaStatus,
    OperationalState,
    PortfolioIdea,
    PortfolioStatus,
)
from research_os.portfolio.stages import select_stage, snapshot_for
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import ensure_schedule
from research_os.registry import list_projects
from research_os.runtime.config import load_config as load_runtime_config
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from research_os.textsafe import terminal_safe

#: Printed above every list of portfolio ideas.
KIND_BANNER = (
    "Autonomous discovery candidates (PIDEA-...). These are not your capsule's "
    "Idea objects (IDEA-0001) and none of them is scientific state."
)


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``portfolio``, ``ideas`` and ``seed``."""

    portfolio = subparsers.add_parser(
        "portfolio", help="Run and observe the autonomous discovery portfolio."
    )
    actions = portfolio.add_subparsers(dest="portfolio_command")
    portfolio.set_defaults(portfolio_parser=portfolio)

    enable = actions.add_parser(
        "enable",
        help="Start running a portfolio for a project. Idempotent.",
    )
    enable.add_argument("project", nargs="?", default=None)

    status = actions.add_parser("status", help="What the portfolio is doing.")
    status.add_argument("project", nargs="?", default=None)
    status.add_argument("--json", action="store_true", dest="as_json")

    top = actions.add_parser("top", help="The strongest ideas, by a Pareto front.")
    top.add_argument("project", nargs="?", default=None)
    top.add_argument("--limit", type=int, default=digest_module.MAX_TOP_IDEAS)

    pause = actions.add_parser("pause", help="Stop allocating work for a project.")
    pause.add_argument("project")
    pause.add_argument("--reason", default="paused by the researcher")

    resume = actions.add_parser("resume", help="Resume a paused portfolio.")
    resume.add_argument("project")

    digest = actions.add_parser(
        "digest",
        help=(
            "Show a periodic digest. Named `portfolio digest` and not `digest`, "
            "because `researchctl digest <OBJECT-ID>` already means the semantic "
            "digest of a capsule object."
        ),
    )
    digest.add_argument("project", nargs="?", default=None)
    digest.add_argument(
        "which", nargs="?", default="latest", help="`latest`, `list`, or a PDIG- id."
    )
    digest.add_argument("--json", action="store_true", dest="as_json")

    ideas = subparsers.add_parser(
        "ideas", help="Inspect autonomous discovery candidates."
    )
    idea_actions = ideas.add_subparsers(dest="ideas_command")
    ideas.set_defaults(ideas_parser=ideas)

    for name, help_text in (
        ("list", "Every idea, newest first."),
        ("rejected", "Ideas that were ruled out, and why."),
        ("validated", "Ideas that passed this system's own gates."),
        ("human-ready", "Ideas Research OS believes merit your attention."),
    ):
        parser = idea_actions.add_parser(name, help=help_text)
        parser.add_argument("project", nargs="?", default=None)
        parser.add_argument("--limit", type=int, default=50)

    show = idea_actions.add_parser("show", help="One idea in full.")
    show.add_argument("idea_id")
    show.add_argument("--json", action="store_true", dest="as_json")

    lineage = idea_actions.add_parser("lineage", help="Where an idea came from.")
    lineage.add_argument("idea_id")

    seed = subparsers.add_parser("seed", help="Give the portfolio a direction.")
    seed_actions = seed.add_subparsers(dest="seed_command")
    seed.set_defaults(seed_parser=seed)
    add = seed_actions.add_parser("add", help="Record a seed for the seeded explorer.")
    add.add_argument("project")
    add.add_argument("--text", required=True, help="The direction, in your own words.")
    add.add_argument("--note", default="")
    listing = seed_actions.add_parser("list", help="Seeds, consumed and pending.")
    listing.add_argument("project", nargs="?", default=None)


def dispatch_portfolio(args: argparse.Namespace) -> int:
    command = getattr(args, "portfolio_command", None)
    if command is None:
        args.portfolio_parser.print_help()
        return EXIT_OK
    return {
        "enable": _enable,
        "status": _status,
        "top": _top,
        "pause": _pause,
        "resume": _resume,
        "digest": _digest,
    }[command](args)


def dispatch_ideas(args: argparse.Namespace) -> int:
    command = getattr(args, "ideas_command", None)
    if command is None:
        args.ideas_parser.print_help()
        return EXIT_OK
    return {
        "list": lambda a: _list(a, None),
        "rejected": lambda a: _list(a, [IdeaStatus.REJECTED, IdeaStatus.PARKED]),
        "validated": lambda a: _list(a, [IdeaStatus.VALIDATED]),
        "human-ready": lambda a: _list(a, [IdeaStatus.HUMAN_READY]),
        "show": _show,
        "lineage": _lineage,
    }[command](args)


def dispatch_seed(args: argparse.Namespace) -> int:
    command = getattr(args, "seed_command", None)
    if command is None:
        args.seed_parser.print_help()
        return EXIT_OK
    return {"add": _seed_add, "list": _seed_list}[command](args)


# ------------------------------------------------------------- plumbing --
def _database() -> Database:
    config = load_runtime_config()
    return Database(config.require_dsn())


def _resolve_project(value: str | None, db: Database) -> tuple[str, Path]:
    """Resolve what a researcher typed to ``(project_id, repo_path)``.

    The one resolver every command here uses, and there were two. ``enable``
    read the capsule; every other command looked the argument up in the
    global registry and, when the registry did not list that path, carried on
    with the path itself as the project id. The first live qualification hit
    it on 2026-09-24: the data home's registry listed the project at an older
    clone, ``portfolio enable <repository>`` worked, ``portfolio status
    <repository>`` said there was no portfolio, and ``portfolio pause
    <repository>`` wrote the filesystem path into ``portfolio_state`` and was
    stopped only by the foreign key.

    The rule is :func:`~research_os.runtime.kernel.resolve_project_argument`,
    shared with `researchctl runtime`: a known id is that project, and
    anything else that exists is a path read through the kernel adapter --
    the only thing that knows what a capsule is -- so the registry is never
    asked what a path means. An id is known to the operational table first
    and the registry second: the registry is disposable (`ARCHITECTURE.md`),
    and it is consulted at all because a researcher who ran
    `register-project` reasonably expects the id it printed to be usable.
    Anything else is refused, so nothing a researcher mistyped can become
    somebody's project id.
    """

    from research_os.runtime.kernel import (
        ScientificKernelAdapter,
        resolve_project_argument,
    )

    if value is None:
        # A researcher standing in their project should not have to name it:
        # the capsule they are standing in is the project. It was the sole
        # registry entry instead, whatever the working directory -- so with
        # the live qualification's stale registry, a bare `portfolio enable`
        # typed inside the canonical checkout enabled the dogfood's older
        # clone. An independent review found it.
        try:
            git_root, here = ScientificKernelAdapter(Path.cwd()).identity()
        except ResearchOSError:
            pass  # not standing in a capsule; fall back to the registry
        else:
            return str(here.id), Path(git_root)
        # Standing nowhere in particular, one registered project is the
        # obvious default and several are not.
        entries = list_projects()
        if len(entries) != 1:
            raise ResearchOSError(
                "name a project: this machine has "
                f"{len(entries)} registered, and there is no sensible default."
            )
        value = entries[0].project_id

    def known(project_id: str) -> Path | None:
        stored = RuntimeStore(db).get_project(project_id)
        if stored is not None:
            return Path(stored.repo_path)
        for entry in list_projects():
            if entry.project_id == project_id:
                return Path(entry.path)
        return None

    resolved = resolve_project_argument(value, known=known)
    if resolved is None:
        raise ResearchOSError(
            f"{value!r} is neither a path nor a project this machine knows. "
            f"Give the path instead: `researchctl portfolio enable <path>`."
        )
    return resolved


def _has_operational_row(db: Database, project: str) -> bool:
    """Whether the ``projects`` row every portfolio row hangs from exists.

    Only `portfolio enable` and `runtime start` create it. A project the
    registry knows and the runtime does not has nothing to pause, resume or
    digest, and saying so is better than a foreign-key violation.
    """

    if RuntimeStore(db).get_project(project) is not None:
        return True
    _print(
        f"{project} has no portfolio yet. "
        f"`researchctl portfolio enable {project}` starts one."
    )
    return False


def _print(text: str) -> None:
    print(terminal_safe(text))


def _last_failure(store: PortfolioStore, idea_id: str) -> tuple[IdeaAction, str] | None:
    """The most recent unresolved failed attempt, and how often that stage failed.

    Not filtered to the stage ``next`` names: the two differ whenever the
    machine gave up before reaching the stage it would choose, and the
    attempt that failed is the one worth reading either way.

    It **is** filtered to failures the stage has not since overcome, which is
    a different question and was the defect. On 2026-09-22 an idea whose
    evidence stage had been refused five times for a missing capability got
    the capability, ran a contained measurement, and wrote an interpreted
    experiment -- and this view still opened with "last attempt at evidence
    failed (capability_denied); tried 5 times" followed by the whole stale
    refusal, while the line above it said the next stage was the review
    board. A record that asserts something untrue about the present is worse
    than no record: ``portfolio status`` already separates the two cases in
    so many words -- "work has succeeded since the last of them, so these are
    history rather than a diagnosis" -- and this surface did not.

    Per stage rather than in aggregate, because a stage that is still failing
    must keep reporting even when a cheaper one has succeeded since.
    """

    actions = store.list_actions(idea_id=idea_id)
    latest_success: dict[object, int] = {
        item.stage: index
        for index, item in enumerate(actions)
        if item.status is ActionStatus.SUCCEEDED
    }
    failed = [
        item
        for index, item in enumerate(actions)
        if item.status is ActionStatus.FAILED
        and index > latest_success.get(item.stage, -1)
    ]
    if not failed:
        return None
    latest = failed[-1]
    n = sum(1 for item in failed if item.stage == latest.stage)
    return latest, ("once" if n == 1 else f"{n} times")


def _provenance_line(store: PortfolioStore, idea: PortfolioIdea) -> str:
    # The current version's evidence only, as the bank's headers count it: a
    # revision's measurements are its own, never its predecessor's.
    evidence = store.list_evidence(
        idea_id=idea.idea_id, idea_version=idea.current_version
    )
    reviews = store.live_reviews(idea_id=idea.idea_id)
    objections = store.open_objections(idea_id=idea.idea_id)
    models = board_independence(reviews)
    sources = len(
        {
            item.literature_key
            for item in evidence
            if item.kind is EvidenceKind.LITERATURE and item.literature_key
        }
    )
    executions = sum(1 for item in evidence if item.job_id)
    # `executions N` alone is the failure the curator's own comment names:
    # it tells a reader how much was measured and not what the measurement
    # said. The bank pages carry the direction; this line is the other
    # surface a researcher reads, and it did not.
    refuting = sum(
        1
        for item in evidence
        if item.job_id and item.strength is EvidenceStrength.CONTRADICTS
    )
    measured = (
        f"executions {executions}"
        if not refuting
        else f"executions {executions} ({refuting} refuting)"
    )
    independence = (
        "one model reviewed this; that is not independent review"
        if models <= 1
        else f"{models} distinct reviewer models"
    )
    return (
        f"    {measured} | sources {sources} | {independence} | "
        f"objections {len(objections)} "
        f"({sum(1 for item in objections if item.blocking)} blocking)"
    )


# ------------------------------------------------------------- handlers --
def _enable(args: argparse.Namespace) -> int:
    """Put this project's tick on the runtime's schedule, and say so.

    Without this there is no production path that creates a
    ``PORTFOLIO_TICK_DUE`` schedule, and a seeded project sits with state and
    no cadence forever -- which is precisely the property this whole layer
    exists to provide. Separate from `seed add` because enabling a portfolio
    is a decision about how the machine spends money, and recording an idea is
    not; a researcher may reasonably want to leave seeds for later.

    Idempotent in both halves: `upsert_state` is, and `ensure_schedule`
    returns the existing schedule rather than creating a second one.
    """

    from research_os.runtime.kernel import ScientificKernelAdapter

    config = load_config()
    with _database() as db:
        project, repo_path = _resolve_project(args.project, db)
        # `enable` is the one command here that writes a repository path, and
        # everything the portfolio does afterwards -- the Curator's commits,
        # every contained experiment, every capsule read -- happens in the
        # repository it wrote. So the repository is read before it is written,
        # whichever source named it: a registry entry whose checkout has since
        # been replaced by another project's, or a stored path that now holds
        # something else, would otherwise attribute that project's work to
        # this id.
        git_root, found = ScientificKernelAdapter(repo_path).identity()
        if str(found.id) != project:
            raise ResearchOSError(
                f"{repo_path} holds the capsule of {found.id!s}, not {project}. "
                f"Give the path of {project}'s repository: "
                f"`researchctl portfolio enable <path>`."
            )
        repo_path = Path(git_root)
        runtime = RuntimeStore(db)
        previous = runtime.get_project(project)
        # The operational `projects` row, which `portfolio_state` has a foreign
        # key to, is created by `runtime start` and by nothing else -- so
        # enabling a portfolio on a project that has never had an R5 objective
        # failed on that constraint. Creating it here is the fix, and it is the
        # right one: the portfolio exists precisely to run when no objective
        # is running, so requiring one first inverts the dependency.
        runtime.upsert_project(project_id=project, repo_path=str(repo_path))
        store = PortfolioStore(db)
        store.upsert_state(project_id=project)
        state = store.get_state(project)
        if state is not None and state.status is PortfolioStatus.PAUSED_BY_RESEARCHER:
            # A pause is a researcher's decision and enabling is not the verb
            # that reverses it. Saying so beats silently un-pausing.
            _print(
                f"{project} is paused by you: {state.detail or 'no reason recorded'}. "
                f"`researchctl portfolio resume {project}` restarts allocation."
            )
        elif state is None or state.status is not PortfolioStatus.RUNNING:
            store.set_portfolio_status(
                project_id=project,
                status=PortfolioStatus.RUNNING,
                detail="enabled by the researcher",
            )
        schedule_id = ensure_schedule(db=db, project_id=project, config=config)
    minutes = config.cadence.tick_seconds / 60
    _print(f"portfolio enabled for {project} ({schedule_id})")
    _print(f"repository {repo_path}")
    if previous is not None and Path(previous.repo_path) != repo_path:
        # Moving a project to another checkout is legitimate -- a re-clone,
        # a moved directory, the canonical repository after a trial clone --
        # and it moves everything the portfolio does with it. It is not done
        # silently.
        _print(f"           re-pointed from {previous.repo_path}")
    _print(
        f"`researchd` will tick it every {minutes:.0f} min and spend up to "
        f"{config.bounds.max_active_tracks} tracks' worth of model calls "
        f"against your budgets. Ceilings are `researchctl runtime budget`; "
        f"`researchctl portfolio pause {project}` stops allocation."
    )
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        store = PortfolioStore(db)
        state = store.get_state(project)
        if state is None:
            _print(
                f"{project} has no portfolio yet. "
                f"`researchctl portfolio enable {project}` starts one."
            )
            return EXIT_OK
        scheduled = any(
            row.project_id == project and row.kind == "PORTFOLIO_TICK_DUE"
            for row in RuntimeStore(db).list_schedules()
        )
        counts = store.counts_by_status(project)
        blocked = store.blocked_counts(project)
        failures, failed_total, failures_are_current = store.failed_work(project)
        payload = {
            "project": project,
            "status": str(state.status),
            "detail": state.detail,
            "active_tracks": store.active_count(project),
            "uncurated": store.uncurated_count(project),
            "scheduled": scheduled,
            "bank_commit": state.bank_commit,
            "last_tick_at": state.last_tick_at.isoformat()
            if state.last_tick_at
            else None,
            "counts": {str(key): value for key, value in sorted(counts.items())},
            "blocked": dict(sorted(blocked.items())),
            "failed_work": failed_total,
            "failures_are_current": failures_are_current,
            "failures": [
                {"kind": kind, "failure_class": failure_class, "error": error}
                for kind, failure_class, error in failures
            ],
        }
        if getattr(args, "as_json", False):
            _print(json.dumps(payload, indent=2, sort_keys=True))
            return EXIT_OK
        _print(f"portfolio  {project}  {state.status}")
        if state.detail:
            _print(f"           {state.detail}")
        if not scheduled:
            # A RUNNING portfolio with no schedule advances only when somebody
            # types a command, which is the state this whole layer exists to
            # avoid. It is worth a line rather than a silence.
            _print(
                f"           not scheduled: nothing ticks it. "
                f"`researchctl portfolio enable {project}`"
            )
        _print(
            f"tracks     {payload['active_tracks']} in flight"
            f"   (a HUMAN_READY idea holds no slot)"
        )
        for key, value in sorted(counts.items()):
            _print(f"  {str(key).lower():<14} {value}")
        for key, value in sorted(blocked.items()):
            # A blocked idea is counted above under its scientific status, so
            # without this line a dead end reads as a healthy PROMISING idea.
            _print(f"blocked    {value} idea(s) {key}")
        _print(
            f"bank       {payload['uncurated']} idea(s) not yet written to Git"
            + (f"; last commit {state.bank_commit[:12]}" if state.bank_commit else "")
        )
        if payload["uncurated"]:
            _print(
                "           uncurated ideas are the only thing losing the "
                "operational database would lose"
            )
        if failed_total:
            # Not a silence, for the reason the "not scheduled" line above is
            # not one. A portfolio whose work is failing looks identical to a
            # healthy quiet one from every other line of this output.
            #
            # But a count with no recency is not a diagnosis either, and the
            # first version of this said so wrongly: the soak's first project
            # carried eight failures from a defect fixed an hour earlier, was
            # advancing ideas past them the whole time, and was told it was not
            # making progress.
            _print("")
            _print(
                f"failed     {failed_total} portfolio work item(s) have failed"
                + (
                    ", and nothing has succeeded since the last one. A portfolio "
                    "that cannot advance an idea is not making progress, whatever "
                    "the status line says."
                    if failures_are_current
                    else "; work has succeeded since the last of them, so these "
                    "are history rather than a diagnosis."
                )
            )
            for kind, failure_class, error in failures:
                _print(
                    f"  {kind:<24} {failure_class:<14} "
                    f"{terminal_safe(error.splitlines()[0] if error else '')[:90]}"
                )
            _print("           `researchctl runtime status` has the full list.")
    return EXIT_OK


def _top(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        # `produce` records a digest, which is a write.
        if not _has_operational_row(db, project):
            return EXIT_OK
        record = digest_module.produce(db=db, project_id=project, config=load_config())
        entries = record.payload.get("top_ideas", [])[: args.limit]
        _print(KIND_BANNER)
        _print("")
        if not entries:
            _print("Nothing has reached a tier worth surfacing yet.")
            return EXIT_OK
        for entry in entries:
            _print(
                f"{entry['idea_id']}  {entry['status']}  "
                f"(tier reached {entry['quality_tier']})"
            )
            _print(f"    {entry['research_question']}")
            _print(f"    independence: {entry['independence_note']}")
            _print(f"    strongest objection: {entry['strongest_objection']}")
            _print("")
    return EXIT_OK


def _pause(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        if not _has_operational_row(db, project):
            return EXIT_ERROR
        store = PortfolioStore(db)
        store.upsert_state(project_id=project)
        store.set_portfolio_status(
            project_id=project,
            status=PortfolioStatus.PAUSED_BY_RESEARCHER,
            detail=args.reason,
            paused_by="researcher",
        )
    _print(f"{project} paused. Nothing will be allocated until you resume it.")
    return EXIT_OK


def _resume(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        if not _has_operational_row(db, project):
            return EXIT_ERROR
        store = PortfolioStore(db)
        store.upsert_state(project_id=project)
        store.set_portfolio_status(
            project_id=project,
            status=PortfolioStatus.RUNNING,
            detail="resumed by the researcher",
        )
        # Resuming the portfolio and unblocking its ideas are one act,
        # because the tick cannot do the second by itself. It lifts
        # BLOCKED_PROVIDER against provider health it can observe and
        # deliberately guesses at nothing else -- so a portfolio whose
        # blocker a person has just fixed had no command that restarted it.
        unblocked = store.unblock_ideas(project_id=project)
        # And forgive the stage-failure ceiling, or unblocking is undone by
        # the next tick: the ceiling counts failed work items and never
        # decays, so an idea whose stage failed three times while a
        # capability was missing was blocked again the moment it arrived.
        # The count itself is untouched -- the dedup key is built from it.
        store.forgive_stage_failures(project_id=project)
    _print(f"{project} resumed.")
    if unblocked:
        _print(
            f"  {unblocked} blocked idea(s) returned to IDLE. If what blocked "
            f"them is still missing they will report it again."
        )
    return EXIT_OK


def _digest(args: argparse.Namespace) -> int:
    which = str(args.which)
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        store = PortfolioStore(db)
        if which == "list":
            records = store.list_digests(project_id=project)
            if not records:
                _print(f"no digest for {project} yet")
                return EXIT_OK
            for record in records:
                _print(
                    f"{record.digest_id}  {record.period_start:%Y-%m-%d %H:%M} -> "
                    f"{record.period_end:%Y-%m-%d %H:%M}"
                )
            return EXIT_OK
        record = (
            store.latest_digest(project)
            if which == "latest"
            else store.get_digest(which)
        )
        if record is None:
            _print(
                f"no digest for {project} yet; one is produced on the portfolio's "
                f"own schedule"
            )
            return EXIT_OK
        if getattr(args, "as_json", False):
            _print(json.dumps(record.payload, indent=2, sort_keys=True))
            return EXIT_OK
        _print(digest_module.render(record.payload))
    return EXIT_OK


def _list(args: argparse.Namespace, statuses: list[IdeaStatus] | None) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        store = PortfolioStore(db)
        ideas = store.list_ideas(
            project_id=project, statuses=statuses, limit=args.limit
        )
        _print(KIND_BANNER)
        _print("")
        if not ideas:
            _print("(none)")
            return EXIT_OK
        for idea in ideas:
            version = store.get_version(idea.idea_id)
            _print(
                f"{idea.idea_id}  {idea.status:<14} {idea.origin:<24} "
                f"{version.title if version else ''}"
            )
            if idea.retire_reason:
                _print(f"    why it stopped: {idea.retire_reason}")
            if idea.revisit_if:
                _print(f"    revisit if: {idea.revisit_if}")
            if idea.status in {IdeaStatus.VALIDATED, IdeaStatus.HUMAN_READY}:
                _print(_provenance_line(store, idea))
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    with _database() as db:
        store = PortfolioStore(db)
        idea = store.get_idea(args.idea_id)
        if idea is None:
            _print(f"{args.idea_id} is not an idea in any portfolio on this machine")
            return EXIT_ERROR
        version = store.require_version(idea.idea_id)
        config = load_config()
        snapshot = snapshot_for(
            store,
            idea,
            version=version,
            max_review_age_seconds=config.thresholds.review_max_age_seconds,
        )
        assert snapshot is not None  # `version` was resolved above
        # Read off the one snapshot rather than queried a second time, so the
        # reviews and objections printed are the ones `select_stage` decided
        # on. Two reads of a live database can legitimately disagree.
        reviews = snapshot.live_reviews
        objections = snapshot.open_objections
        stage, why = select_stage(snapshot, config)
        if getattr(args, "as_json", False):
            _print(
                json.dumps(
                    {
                        "idea": idea.model_dump(mode="json"),
                        "version": version.model_dump(mode="json"),
                        "next_stage": str(stage) if stage else None,
                        "next_reason": why,
                        "reviews": [item.model_dump(mode="json") for item in reviews],
                        "objections": [
                            item.model_dump(mode="json") for item in objections
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
            return EXIT_OK
        _print(KIND_BANNER)
        _print("")
        _print(f"{idea.idea_id}  {idea.status}  (tier reached {idea.quality_tier})")
        _print(_provenance_line(store, idea))
        _print("")
        _print(f"  {version.title}")
        _print(f"  question:  {version.research_question}")
        _print(f"  core:      {version.core_idea}")
        _print(f"  mechanism: {version.mechanism or '(none stated)'}")
        _print(f"  falsifier: {version.falsifier or '(none stated)'}")
        _print(
            "  settled by: "
            + (
                ", ".join(str(item) for item in version.adjudication_types)
                or "(not yet classified)"
            )
        )
        _print("")
        _print(f"  next: {stage or 'nothing'} -- {why}")
        # `next` is what `select_stage` would choose, which is not the same
        # thing as what happened. Five real ideas sat at BLOCKED_EXTERNAL
        # overnight, one after five failed attempts at the stage named on
        # that line, and this view mentioned neither -- so it read as work
        # about to start rather than work already refused. The refusal is
        # the most useful sentence the system has about such an idea.
        if idea.operational_state is not OperationalState.IDLE:
            _print(f"  state: {idea.operational_state}")
        failure = _last_failure(store, idea.idea_id)
        if failure is not None:
            last, tried = failure
            _print("")
            _print(
                f"  last attempt at {last.stage} failed "
                f"({last.failure_class or 'no class recorded'}); "
                f"tried {tried}:"
            )
            for line in (last.detail or "(nothing recorded)").splitlines():
                _print(f"    {line}")
        if reviews:
            _print("")
            _print("  live reviews")
            for item in reviews:
                _print(
                    f"    {item.reviewer_role:<22} {item.verdict:<22} "
                    f"{item.provider_family}/{item.model or 'unnamed'} "
                    f"({item.independence_vs_origin})"
                )
        if objections:
            _print("")
            _print("  standing objections")
            for item in objections:
                _print(
                    f"    [{item.severity}] v{item.raised_at_version} {item.summary}"
                )
    return EXIT_OK


def _lineage(args: argparse.Namespace) -> int:
    with _database() as db:
        store = PortfolioStore(db)
        idea = store.get_idea(args.idea_id)
        if idea is None:
            _print(f"{args.idea_id} is not an idea in any portfolio on this machine")
            return EXIT_ERROR
        _print(f"{idea.idea_id}  depth {idea.depth}  root {idea.lineage_root}")
        _print("")
        _print("  ancestors (nearest first)")
        for item in store.ancestors(idea.idea_id) or ("(none)",):
            _print(f"    {item}")
        _print("")
        _print("  descendants")
        for item in store.descendants(idea.idea_id) or ("(none)",):
            _print(f"    {item}")
        _print("")
        _print("  edges")
        for edge in store.edges_of(idea.idea_id):
            _print(
                f"    {edge.kind:<14} {edge.parent_idea_id} -> {edge.child_idea_id}"
                + (f"  {edge.detail}" if edge.detail else "")
            )
        survivor = store.duplicate_survivor(idea.idea_id)
        if survivor != idea.idea_id:
            _print("")
            _print(f"  this idea is a restatement of {survivor}")
    return EXIT_OK


def _seed_add(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        store = PortfolioStore(db)
        # `portfolio_state` has a foreign key to the operational `projects`
        # row, and `portfolio enable` is the only thing in this layer that
        # creates one. Without this check the first command a researcher types
        # after `register-project` answers with
        #
        #     insert or update on table "portfolio_state" violates foreign key
        #     constraint "portfolio_state_project_id_fkey"
        #
        # which is a true statement about PostgreSQL and no help at all. The
        # first dogfood hit it following this layer's own documented order.
        #
        # Deliberately *not* creating the row here. `_enable` says why the two
        # commands are separate: enabling commits the machine to spending
        # against a budget and recording a direction does not, so `seed add`
        # must not quietly do the thing that starts the spending.
        if RuntimeStore(db).get_project(project) is None:
            _print(
                f"{project} has no portfolio yet, so there is nowhere to put a "
                f"seed. `researchctl portfolio enable {project}` starts one -- "
                f"it is the command that commits this machine to spending "
                f"against your budgets, which is why recording a direction "
                f"does not do it for you."
            )
            return EXIT_ERROR
        store.upsert_state(project_id=project)
        seed = store.add_seed(project_id=project, text=args.text, note=args.note)
        scheduled = any(
            row.project_id == project and row.kind == "PORTFOLIO_TICK_DUE"
            for row in RuntimeStore(db).list_schedules()
        )
    _print(f"recorded {seed.seed_id}")
    _print(
        "The seeded explorer will take it on the portfolio's next pass. It is a "
        "direction to push, not a claim: the system may well conclude it is "
        "already known or not worth pursuing, and that is a result."
    )
    if not scheduled:
        _print(
            f"There is no next pass yet: `researchctl portfolio enable {project}` "
            f"puts this project on the tick."
        )
    return EXIT_OK


def _seed_list(args: argparse.Namespace) -> int:
    with _database() as db:
        project, _repo = _resolve_project(args.project, db)
        store = PortfolioStore(db)
        seeds = store.list_seeds(project_id=project)
        if not seeds:
            _print(
                f"{project} has no seeds. `researchctl seed add` gives the "
                f"portfolio a direction to push."
            )
            return EXIT_OK
        for seed in seeds:
            state = "pending" if seed.consumed_at is None else "taken"
            _print(f"{seed.seed_id}  {state:<8} {seed.text}")
    return EXIT_OK


def payload_for_tests(record: Any) -> dict[str, Any]:  # pragma: no cover - helper
    return dict(record.payload)


__all__ = [
    "KIND_BANNER",
    "add_parsers",
    "dispatch_ideas",
    "dispatch_portfolio",
    "dispatch_seed",
]

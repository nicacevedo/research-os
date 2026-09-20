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
    EvidenceKind,
    IdeaStatus,
    PortfolioIdea,
    PortfolioStatus,
)
from research_os.portfolio.stages import TrackSnapshot, select_stage
from research_os.portfolio.store import PortfolioStore
from research_os.registry import list_projects
from research_os.runtime.config import load_config as load_runtime_config
from research_os.runtime.db import Database
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


def _project(value: str | None) -> str:
    """Resolve a project id or a repository path to a project id.

    A path as well as an id, because a researcher standing in their project
    should not have to look up what the registry calls it.
    """

    if value is None:
        entries = list_projects()
        if len(entries) == 1:
            return entries[0].project_id
        raise ResearchOSError(
            "name a project: this machine has "
            f"{len(entries)} registered, and there is no sensible default."
        )
    candidate = Path(value).expanduser()
    if candidate.exists():
        for entry in list_projects():
            if Path(entry.path).resolve() == candidate.resolve():
                return entry.project_id
    return value


def _print(text: str) -> None:
    print(terminal_safe(text))


def _provenance_line(store: PortfolioStore, idea: PortfolioIdea) -> str:
    evidence = store.list_evidence(idea_id=idea.idea_id)
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
    independence = (
        "one model reviewed this; that is not independent review"
        if models <= 1
        else f"{models} distinct reviewer models"
    )
    return (
        f"    executions {executions} | sources {sources} | {independence} | "
        f"objections {len(objections)} "
        f"({sum(1 for item in objections if item.blocking)} blocking)"
    )


# ------------------------------------------------------------- handlers --
def _status(args: argparse.Namespace) -> int:
    project = _project(args.project)
    with _database() as db:
        store = PortfolioStore(db)
        state = store.get_state(project)
        if state is None:
            _print(
                f"{project} has no portfolio yet. Seed one with `researchctl seed add`."
            )
            return EXIT_OK
        counts = store.counts_by_status(project)
        payload = {
            "project": project,
            "status": str(state.status),
            "detail": state.detail,
            "active_tracks": store.active_count(project),
            "uncurated": store.uncurated_count(project),
            "bank_commit": state.bank_commit,
            "last_tick_at": state.last_tick_at.isoformat()
            if state.last_tick_at
            else None,
            "counts": {str(key): value for key, value in sorted(counts.items())},
        }
        if getattr(args, "as_json", False):
            _print(json.dumps(payload, indent=2, sort_keys=True))
            return EXIT_OK
        _print(f"portfolio  {project}  {state.status}")
        if state.detail:
            _print(f"           {state.detail}")
        _print(
            f"tracks     {payload['active_tracks']} in flight"
            f"   (a HUMAN_READY idea holds no slot)"
        )
        for key, value in sorted(counts.items()):
            _print(f"  {str(key).lower():<14} {value}")
        _print(
            f"bank       {payload['uncurated']} idea(s) not yet written to Git"
            + (f"; last commit {state.bank_commit[:12]}" if state.bank_commit else "")
        )
        if payload["uncurated"]:
            _print(
                "           uncurated ideas are the only thing losing the "
                "operational database would lose"
            )
    return EXIT_OK


def _top(args: argparse.Namespace) -> int:
    project = _project(args.project)
    with _database() as db:
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
    project = _project(args.project)
    with _database() as db:
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
    project = _project(args.project)
    with _database() as db:
        store = PortfolioStore(db)
        store.upsert_state(project_id=project)
        store.set_portfolio_status(
            project_id=project,
            status=PortfolioStatus.RUNNING,
            detail="resumed by the researcher",
        )
    _print(f"{project} resumed.")
    return EXIT_OK


def _digest(args: argparse.Namespace) -> int:
    project = _project(args.project)
    which = str(args.which)
    with _database() as db:
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
    project = _project(args.project)
    with _database() as db:
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
        reviews = store.live_reviews(idea_id=idea.idea_id)
        objections = store.open_objections(idea_id=idea.idea_id)
        evidence = store.list_evidence(
            idea_id=idea.idea_id, idea_version=version.version
        )
        snapshot = TrackSnapshot(
            status=idea.status,
            version=version,
            succeeded_stages=store.succeeded_stages_for_version(
                idea_id=idea.idea_id, idea_version=version.version
            ),
            evidence=evidence,
            live_reviews=reviews,
            open_objections=objections,
            revision_count=store.revision_count(idea.idea_id),
            review_count=store.review_count(idea.idea_id),
        )
        stage, why = select_stage(snapshot, load_config())
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
    project = _project(args.project)
    with _database() as db:
        store = PortfolioStore(db)
        store.upsert_state(project_id=project)
        seed = store.add_seed(project_id=project, text=args.text, note=args.note)
    _print(f"recorded {seed.seed_id}")
    _print(
        "The seeded explorer will take it on the portfolio's next pass. It is a "
        "direction to push, not a claim: the system may well conclude it is "
        "already known or not worth pursuing, and that is a result."
    )
    return EXIT_OK


def _seed_list(args: argparse.Namespace) -> int:
    project = _project(args.project)
    with _database() as db:
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

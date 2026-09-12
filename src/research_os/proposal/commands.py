"""``researchctl propose`` commands.

Read commands behave like every other read command here. The one write command
-- ``promote`` -- is different on purpose, and is the only place in Research OS
where something outside ``init-project`` and ``review`` puts a file into a
project's scientific record.

It is guarded the same way ``researchctl review`` is: an interactive terminal
and an explicit confirmation, with the exact file contents shown first. Both
guards are about the same thing. A non-human process must not be able to put
science into a project, and a human must not be able to do it without having
seen what they are creating.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from research_os.errors import EXIT_ERROR, EXIT_OK, PromotionRefusedError
from research_os.proposal.models import ResearchProposal
from research_os.proposal.promote import prepare_promotion, write_promotion
from research_os.proposal.report import (
    blocking_summary,
    render_promotion_preview,
    render_proposal,
    render_proposal_list,
)
from research_os.proposal.store import ProposalStore


def add_propose_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``propose`` command group."""

    propose = subparsers.add_parser(
        "propose",
        help="Inspect scientific proposals and promote one into a draft object.",
    )
    actions = propose.add_subparsers(dest="propose_command")

    start = actions.add_parser(
        "start",
        help="Produce a scientific proposal for a goal. Writes nothing to the project.",
    )
    start.add_argument("project", metavar="PROJECT", help="Project id or path.")
    start.add_argument("--goal", required=True, help="What you want to find out.")
    start.add_argument(
        "--literature",
        action="store_true",
        help="Read the local literature index for the goal first.",
    )
    start.add_argument(
        "--retrieve",
        action="store_true",
        help="Also ask the literature providers before reading. Implies --literature.",
    )
    start.add_argument(
        "--no-assess",
        action="store_true",
        help="Skip the independent assessment. Saves one model call.",
    )
    start.add_argument("--config", help="Path to an automation config file.")

    actions.add_parser("list", help="List proposals, newest last.")

    show = actions.add_parser("show", help="Show one proposal in full.")
    show.add_argument("proposal_id", metavar="PROPOSAL_ID")

    promote = actions.add_parser(
        "promote",
        help=(
            "Promote one proposed item into a DRAFT capsule object. "
            "Requires an interactive terminal."
        ),
    )
    promote.add_argument("proposal_id", metavar="PROPOSAL_ID")
    promote.add_argument(
        "--item", required=True, metavar="PR-001", help="Which proposed item."
    )
    promote.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project path. Defaults to the project the proposal was made for.",
    )

    events = actions.add_parser("events", help="Print one proposal's event ledger.")
    events.add_argument("proposal_id", metavar="PROPOSAL_ID")

    propose.set_defaults(propose_parser=propose)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "propose_command", None)
    if command is None:
        args.propose_parser.print_help()
        return EXIT_OK
    handlers = {
        "start": _start,
        "list": _list,
        "show": _show,
        "promote": _promote,
        "events": _events,
    }
    return handlers[command](args)


def _start(args: argparse.Namespace) -> int:
    from research_os.automation.commands import resolve_project
    from research_os.automation.config import load_config
    from research_os.automation.providers import default_registry
    from research_os.literature.config import load_config as load_literature_config
    from research_os.proposal.controller import ProposalController

    controller = ProposalController(
        providers=default_registry(),
        config=load_config(Path(args.config) if args.config else None),
        literature_config=load_literature_config(),
    )
    outcome = controller.propose(
        project_path=resolve_project(args.project),
        goal=args.goal,
        with_literature=bool(args.literature or args.retrieve),
        retrieve=bool(args.retrieve),
        assess=not args.no_assess,
    )
    print(
        render_proposal(
            outcome.proposal,
            assessment=outcome.assessment,
            promotions=outcome.store.promotions(),
        ),
        end="",
    )
    print(f"proposal directory  {outcome.store.directory}")
    print(f"model calls         {outcome.model_calls}")
    return EXIT_OK


def _list(_args: argparse.Namespace) -> int:
    entries: list[tuple[ResearchProposal, object, int]] = []
    for proposal_id in ProposalStore.list_proposal_ids():
        store = ProposalStore.open(proposal_id)
        entries.append((store.load(), store.load_assessment(), len(store.promotions())))
    print(render_proposal_list(entries), end="")  # type: ignore[arg-type]
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    store = ProposalStore.open(args.proposal_id)
    print(
        render_proposal(
            store.load(),
            assessment=store.load_assessment(),
            promotions=store.promotions(),
        ),
        end="",
    )
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    import json

    store = ProposalStore.open(args.proposal_id)
    for record in store.iter_events():
        print(json.dumps(record, ensure_ascii=True, sort_keys=True))
    return EXIT_OK


def _promote(args: argparse.Namespace) -> int:
    """Turn one proposed item into a draft capsule object, if a human says so."""

    from research_os.cli import _confirm, _is_interactive

    if not _is_interactive():
        raise PromotionRefusedError(
            "researchctl propose promote requires an interactive terminal. "
            "Promotion writes a file into your project's scientific record, and "
            "a non-human process must not do that. Nothing has been written."
        )

    store = ProposalStore.open(args.proposal_id)
    proposal = store.load()
    assessment = store.load_assessment()
    prepared = prepare_promotion(
        proposal,
        args.item,
        project_path=Path(args.path).expanduser() if args.path else None,
    )

    print(render_promotion_preview(prepared), end="")
    warning = blocking_summary(assessment)
    if warning:
        print(f"WARNING: {warning}\n")

    if not _confirm(
        f"Write {prepared.obj.id} as a DRAFT {prepared.obj.type} "
        f"in {prepared.relative_target}?"
    ):
        print("cancelled: nothing was written")
        return EXIT_ERROR

    record = write_promotion(prepared)
    store.record_promotion(record)
    print(f"Wrote {record.written_path}")
    print(
        f"\n{record.object_id} is a DRAFT. It has not been reviewed or accepted.\n"
        "Complete it, then validate the capsule:\n"
        f"     researchctl validate-project {record.project_path}"
    )
    if record.object_type == "claim":
        print(
            "\nA Claim becomes accepted only through a human Review:\n"
            f"     researchctl review {record.object_id} {record.project_path}"
        )
    return EXIT_OK

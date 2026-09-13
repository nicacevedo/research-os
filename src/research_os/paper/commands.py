"""``researchctl paper`` commands.

Every command here ends with a diff on a branch and a report. None of them
merges, pushes, or touches a project's scientific record, and ``sources`` exists
so a researcher can see exactly what a writer would be given -- including which
Claims are being withheld and why -- before spending anything.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_os.automation.config import load_config
from research_os.automation.worktree import release_worktree
from research_os.errors import EXIT_ERROR, EXIT_OK, PaperError
from research_os.paper.controller import PaperController
from research_os.paper.models import SectionKind
from research_os.paper.packet import build_source_packet, render_source_packet
from research_os.paper.report import (
    render_draft,
    render_draft_list,
    render_manifest,
)
from research_os.paper.store import DraftStore


def add_paper_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``paper`` command group."""

    group = subparsers.add_parser(
        "paper",
        help="Draft manuscript sections from accepted science, and check them.",
    )
    actions = group.add_subparsers(dest="paper_command")

    sources = actions.add_parser(
        "sources",
        help="Show exactly what a writer would be given. Spends nothing.",
    )
    sources.add_argument("project", metavar="PROJECT", help="Project id or path.")
    sources.add_argument("--claim", action="append", default=[], dest="claims")
    sources.add_argument("--work", action="append", default=[], dest="works")
    sources.add_argument("--limitation", action="append", default=[])

    write = actions.add_parser("write", help="Draft one manuscript section.")
    write.add_argument("project", metavar="PROJECT", help="Project id or path.")
    write.add_argument(
        "--section",
        required=True,
        choices=[item.value for item in SectionKind],
    )
    write.add_argument("--instruction", required=True)
    write.add_argument(
        "--path",
        action="append",
        required=True,
        dest="paths",
        help="A repository-relative path the writer may change. Repeatable.",
    )
    write.add_argument("--claim", action="append", default=[], dest="claims")
    write.add_argument("--work", action="append", default=[], dest="works")
    write.add_argument("--limitation", action="append", default=[])
    write.add_argument(
        "--no-repair",
        action="store_true",
        help="Do not spend the one bounded repair attempt.",
    )
    write.add_argument("--config", help="Path to an automation config file.")

    show = actions.add_parser("show", help="Show one draft in full.")
    show.add_argument("draft_id", metavar="DRAFT_ID")

    manifest = actions.add_parser("manifest", help="Print one draft's source manifest.")
    manifest.add_argument("draft_id", metavar="DRAFT_ID")
    manifest.add_argument("--json", action="store_true")

    actions.add_parser("drafts", help="List writing tasks.")

    events = actions.add_parser("events", help="Print one draft's event ledger.")
    events.add_argument("draft_id", metavar="DRAFT_ID")

    cleanup = actions.add_parser(
        "cleanup", help="Remove one draft's worktree. The branch is kept."
    )
    cleanup.add_argument("draft_id", metavar="DRAFT_ID")

    group.set_defaults(paper_parser=group)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "paper_command", None)
    if command is None:
        args.paper_parser.print_help()
        return EXIT_OK
    handlers = {
        "sources": _sources,
        "write": _write,
        "show": _show,
        "manifest": _manifest,
        "drafts": _drafts,
        "events": _events,
        "cleanup": _cleanup,
    }
    return handlers[command](args)


def _sources(args: argparse.Namespace) -> int:
    from research_os.automation.commands import (
        resolve_project,
    )

    packet = build_source_packet(
        resolve_project(args.project),
        claim_ids=args.claims or None,
        literature_keys=args.works,
        limitations=args.limitation,
    )
    from research_os.textsafe import terminal_safe

    print(terminal_safe(render_source_packet(packet)), end="")
    if packet.excluded_claims:
        print(
            "\nSome claims you asked for are not available to write from. That is"
            "\nnot a malfunction: a manuscript may only state what this project"
            "\nhas actually accepted, as it stands now."
        )
        return EXIT_ERROR
    return EXIT_OK


def _write(args: argparse.Namespace) -> int:
    from research_os.automation.commands import (
        provider_registry,
        resolve_project,
    )

    project = resolve_project(args.project)
    packet = build_source_packet(
        project,
        claim_ids=args.claims or None,
        literature_keys=args.works,
        limitations=args.limitation,
    )
    controller = PaperController(
        providers=provider_registry(),
        config=load_config(Path(args.config) if args.config else None),
        allow_repair=not args.no_repair,
    )
    outcome = controller.write(
        project_path=project,
        section=SectionKind(args.section),
        instruction=args.instruction,
        packet=packet,
        allowed_paths=list(args.paths),
    )
    print(render_draft(outcome.draft, outcome.packet), end="")
    print(f"draft directory   {outcome.store.directory}")
    print(f"model calls       {outcome.model_calls}")
    return EXIT_OK if outcome.ready_for_human else EXIT_ERROR


def _show(args: argparse.Namespace) -> int:
    store = DraftStore.open(args.draft_id)
    print(render_draft(store.load(), store.load_packet()), end="")
    return EXIT_OK


def _manifest(args: argparse.Namespace) -> int:
    store = DraftStore.open(args.draft_id)
    draft = store.load()
    if args.json:
        if draft.manifest is None:
            raise PaperError(f"{args.draft_id} has no source manifest")
        print(
            json.dumps(
                draft.manifest.model_dump(mode="json"),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_manifest(draft), end="")
    return EXIT_OK


def _drafts(_args: argparse.Namespace) -> int:
    drafts = [DraftStore.open(item).load() for item in DraftStore.list_draft_ids()]
    print(render_draft_list(drafts), end="")
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    store = DraftStore.open(args.draft_id)
    for record in store.iter_events():
        print(json.dumps(record, ensure_ascii=True, sort_keys=True))
    return EXIT_OK


def _cleanup(args: argparse.Namespace) -> int:
    """Remove the worktree, keep the branch.

    The branch holds the draft. Removing it would be deletion rather than
    cleanup, and a researcher who has not decided yet would lose the work.
    """

    from research_os.automation.models import WorktreeRecord
    from research_os.automation.worktree import lock_path as worktree_lock_path

    store = DraftStore.open(args.draft_id)
    draft = store.load()
    if not draft.worktree_path or not Path(draft.worktree_path).exists():
        print(f"{draft.draft_id}: no live worktree to remove")
        return EXIT_OK
    release_worktree(
        WorktreeRecord(
            task_id="T-001",
            path=draft.worktree_path,
            branch=draft.branch or "unknown",
            base_commit=draft.base_commit or "0" * 40,
            # The real lock, from the helper that creates it. Deriving it by
            # changing the worktree's suffix pointed at a path that never
            # existed, so every cleanup left its lock file behind.
            lock_path=str(worktree_lock_path(Path(draft.worktree_path))),
            created_at=draft.created_at,
        ),
        repository=Path(draft.project_path),
    )
    store.append_event("worktree_removed", path=draft.worktree_path)
    print(f"removed worktree {draft.worktree_path}")
    print(
        f"The branch {draft.branch} was kept. The draft is still there, and the"
        f"\nrecord remains at {store.directory}."
    )
    return EXIT_OK

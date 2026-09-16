"""``researchctl insight`` commands.

Three operations matter here, and the interesting one is the gap between two of
them.

``nominate`` writes a suggestion into runtime state. Anything may call it,
including an agent, because a suggestion reaches nobody.

``promote`` writes durable knowledge that other projects' workers will read. It
requires an interactive terminal and an explicit confirmation, shows the whole
file first, and refuses to proceed while scope, assumptions, or applicability
are missing -- because an insight without those is a sentence that will be
applied somewhere it does not hold.

``search`` and ``show`` read. They include retired insights when asked, because
an insight that turned out to be wrong is the one a researcher most needs to
find again.
"""

from __future__ import annotations

import argparse
import json

from research_os.automation.models import utc_now
from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    InsightError,
    InsightPromotionRefusedError,
)
from research_os.insights.models import (
    InsightNomination,
    PromotedInsight,
    PromotionType,
    SourceReference,
)
from research_os.insights.packet import build_insight_packet, render_insight_packet
from research_os.insights.report import (
    render_insight,
    render_insight_list,
    render_matches,
    render_nomination,
    render_nomination_list,
)
from research_os.insights.store import (
    InsightStore,
    NominationStore,
    make_insight_id,
    make_nomination_id,
)
from research_os.textsafe import terminal_safe


def add_insight_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``insight`` command group."""

    group = subparsers.add_parser(
        "insight",
        help="Move knowledge between projects, explicitly and with provenance.",
    )
    actions = group.add_subparsers(dest="insight_command")

    nominate = actions.add_parser(
        "nominate",
        help="Record a candidate insight. Reaches no other project by itself.",
    )
    nominate.add_argument("--title", required=True)
    nominate.add_argument("--statement", required=True)
    nominate.add_argument("--project", required=True, help="The source project id.")
    nominate.add_argument(
        "--type",
        dest="promotion_type",
        default="observation",
        choices=[item.value for item in PromotionType],
    )
    nominate.add_argument("--rationale", required=True, help="Why it may transfer.")
    nominate.add_argument(
        "--object", dest="object_id", help="Source capsule object id."
    )
    nominate.add_argument("--digest", dest="object_digest")
    nominate.add_argument("--commit")
    nominate.add_argument("--scope", default="")
    nominate.add_argument("--assumption", action="append", default=[])
    nominate.add_argument("--applicability", default="")
    nominate.add_argument("--by", dest="nominated_by", default="agent")

    promote = actions.add_parser(
        "promote",
        help=(
            "Promote a nomination into durable cross-project knowledge. "
            "Requires an interactive terminal."
        ),
    )
    promote.add_argument("nomination_id", metavar="NOMINATION_ID")
    promote.add_argument("--scope", help="Where this holds. Required if not nominated.")
    promote.add_argument("--assumption", action="append", default=[])
    promote.add_argument("--applicability", help="When to reach for it, and when not.")
    promote.add_argument(
        "--confidence", default=None, choices=["high", "medium", "low"]
    )
    promote.add_argument("--keyword", action="append", default=[])

    decline = actions.add_parser("decline", help="Decline a nomination, with a reason.")
    decline.add_argument("nomination_id", metavar="NOMINATION_ID")
    decline.add_argument("--reason", required=True)

    search = actions.add_parser("search", help="Find insights from other projects.")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument(
        "--for-project",
        dest="for_project",
        help="Exclude insights this project itself promoted.",
    )
    search.add_argument(
        "--include-retired",
        action="store_true",
        help="Include withdrawn insights. They are shown marked.",
    )
    search.add_argument(
        "--packet",
        action="store_true",
        help="Render the fenced block a worker would actually be given.",
    )

    show = actions.add_parser("show", help="Show one insight in full.")
    show.add_argument("insight_id", metavar="INSIGHT_ID")
    show.add_argument("--json", action="store_true")

    listing = actions.add_parser("list", help="List insights and nominations.")
    listing.add_argument(
        "--nominations", action="store_true", help="List nominations instead."
    )
    listing.add_argument("--include-retired", action="store_true")

    retire = actions.add_parser(
        "retire", help="Withdraw an insight. It stays readable, marked."
    )
    retire.add_argument("insight_id", metavar="INSIGHT_ID")
    retire.add_argument("--reason", required=True)

    supersede = actions.add_parser(
        "supersede", help="Point one insight at the insight that replaced it."
    )
    supersede.add_argument("insight_id", metavar="INSIGHT_ID")
    supersede.add_argument("--by", dest="successor_id", required=True)

    group.set_defaults(insight_parser=group)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "insight_command", None)
    if command is None:
        args.insight_parser.print_help()
        return EXIT_OK
    handlers = {
        "nominate": _nominate,
        "promote": _promote,
        "decline": _decline,
        "search": _search,
        "show": _show,
        "list": _list,
        "retire": _retire,
        "supersede": _supersede,
    }
    return handlers[command](args)


def _nominate(args: argparse.Namespace) -> int:
    created_at = utc_now()
    nomination = InsightNomination(
        nomination_id=make_nomination_id(
            title=args.title, project_id=args.project, created_at=created_at
        ),
        title=args.title,
        statement=args.statement,
        promotion_type=PromotionType(args.promotion_type),
        source=SourceReference(
            project_id=args.project,
            object_id=args.object_id,
            object_digest=args.object_digest,
            commit=args.commit,
        ),
        scope=args.scope,
        assumptions=list(args.assumption),
        applicability=args.applicability,
        rationale=args.rationale,
        nominated_by=args.nominated_by,
        created_at=created_at,
    )
    NominationStore().write(nomination)
    print(render_nomination(nomination), end="")
    return EXIT_OK


def _promote(args: argparse.Namespace) -> int:
    """Turn one nomination into durable cross-project knowledge, if a human says so."""

    from research_os.cli import _confirm, _is_interactive

    if not _is_interactive():
        raise InsightPromotionRefusedError(
            "researchctl insight promote requires an interactive terminal. "
            "Promotion puts knowledge where other projects' workers will read "
            "it, and only a human decides that a finding transfers. Nothing has "
            "been written."
        )

    nominations = NominationStore()
    nomination = nominations.load(args.nomination_id)
    if nomination.promoted_insight_id:
        raise InsightError(
            f"{nomination.nomination_id} was already promoted as "
            f"{nomination.promoted_insight_id}"
        )

    scope = args.scope or nomination.scope
    assumptions = [
        item for item in ([*nomination.assumptions, *args.assumption]) if item.strip()
    ]
    applicability = args.applicability or nomination.applicability
    missing = [
        name
        for name, value in (
            ("--scope", scope),
            ("--applicability", applicability),
        )
        if not value.strip()
    ]
    if not assumptions:
        missing.append("--assumption")
    if missing:
        raise InsightError(
            f"{nomination.nomination_id} cannot be promoted yet: supply "
            + ", ".join(missing)
            + ". An insight without a scope and its assumptions is a sentence "
            "that will be applied somewhere it does not hold."
        )

    created_at = utc_now()
    insight = PromotedInsight(
        insight_id=make_insight_id(
            title=nomination.title,
            project_id=nomination.source.project_id,
            created_at=created_at,
        ),
        title=nomination.title,
        statement=nomination.statement,
        promotion_type=nomination.promotion_type,
        source=nomination.source,
        scope=scope,
        assumptions=assumptions,
        applicability=applicability,
        confidence=args.confidence or nomination.confidence,
        keywords=list(args.keyword),
        nomination_id=nomination.nomination_id,
        created_at=created_at,
        updated_at=created_at,
    )

    print(render_insight(insight, preview=True), end="")
    if not _confirm(
        f"Promote {insight.insight_id} into cross-project knowledge, readable "
        "by every project on this machine?"
    ):
        print("cancelled: nothing was written")
        return EXIT_ERROR

    store = InsightStore()
    target = store.write(insight)
    nominations.write(
        nomination.model_copy(update={"promoted_insight_id": insight.insight_id})
    )
    print(f"Wrote {target}")
    print(
        "\nThis is not a Claim in any project. It arrives in other projects "
        "labelled\nwith its source, its scope, and its assumptions, and it is "
        "never presented\nas established there."
    )
    return EXIT_OK


def _decline(args: argparse.Namespace) -> int:
    store = NominationStore()
    nomination = store.load(args.nomination_id)
    store.write(nomination.model_copy(update={"declined_reason": args.reason}))
    print(f"{nomination.nomination_id} declined: {args.reason}")
    return EXIT_OK


def _search(args: argparse.Namespace) -> int:
    matches = InsightStore().search(
        args.query,
        limit=args.limit,
        include_retired=args.include_retired,
        exclude_project=args.for_project,
    )
    if args.packet:
        packet = build_insight_packet(
            args.query, matches, receiving_project=args.for_project
        )
        # `--packet` shows the prompt as a model receives it, which is the whole
        # point of the flag -- so the fold `prompt_safe` applies is correct here
        # and the reader has to be told it happened. A final adversarial review
        # noted that a bidi control and a zero-width space simply vanished from
        # this output with nothing saying a character had been removed, which
        # inverts the display boundary's stated principle of making the
        # untrusted byte visible rather than hiding it.
        print(
            "# This is the prompt block as a model receives it. Control "
            "and invisible\n# characters have already been replaced with "
            "spaces; the stored insight is unchanged.\n"
        )
        print(terminal_safe(render_insight_packet(packet)))
    else:
        print(render_matches(matches, query=args.query), end="")
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    insight = InsightStore().load(args.insight_id)
    if args.json:
        print(
            json.dumps(
                insight.model_dump(mode="json"),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_insight(insight), end="")
    return EXIT_OK


def _list(args: argparse.Namespace) -> int:
    if args.nominations:
        print(render_nomination_list(NominationStore().all()), end="")
        return EXIT_OK
    store = InsightStore()
    insights = store.all() if args.include_retired else store.live()
    print(render_insight_list(insights), end="")
    return EXIT_OK


def _retire(args: argparse.Namespace) -> int:
    insight = InsightStore().retire(args.insight_id, reason=args.reason)
    print(f"{insight.insight_id} is now {insight.status}: {insight.retire_reason}")
    print(
        "It remains readable and is still shown in searches that ask for "
        "retired\ninsights, because a finding that turned out to be wrong is "
        "worth seeing."
    )
    return EXIT_OK


def _supersede(args: argparse.Namespace) -> int:
    insight = InsightStore().supersede(args.insight_id, successor_id=args.successor_id)
    print(f"{insight.insight_id} is superseded by {insight.superseded_by}")
    return EXIT_OK


def insights_for(query: str, *, project_id: str | None, limit: int = 8) -> str:
    """Return the prompt section a controller should give a worker, or nothing.

    The one function the rest of Research OS calls. It excludes the receiving
    project's own insights and returns an empty string when nothing matched, so
    a prompt never carries a "transferred knowledge" heading over an empty block.
    """

    from research_os.insights.packet import render_insight_section

    matches = InsightStore().search(query, limit=limit, exclude_project=project_id)
    if not matches:
        return ""
    packet = build_insight_packet(
        query, matches, receiving_project=project_id, max_insights=limit
    )
    return render_insight_section(packet)

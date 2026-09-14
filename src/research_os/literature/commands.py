"""``researchctl lit`` commands.

The same shape as ``researchctl auto``: each command resolves configuration,
opens the store, calls one deterministic function, and renders the result. No
orchestration lives here, so what the tests exercise is what a person runs.

Every command in this group is read-or-retrieve. None of them touches a project
capsule, creates a worktree, dispatches a worker, or records anything a human
would have to approve. The literature index is shared infrastructure: the worst
outcome of any command here is a slow network call and a larger cache.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_os.errors import EXIT_ERROR, EXIT_OK, LiteratureError
from research_os.literature.config import (
    KNOWN_SOURCES,
    LiteratureConfig,
    load_config,
)
from research_os.literature.evaluation import DEFAULT_CUTOFF, evaluate
from research_os.literature.models import ExtractionStatus, KnownItem
from research_os.literature.report import (
    render_evaluation,
    render_results,
    render_retrieval,
    render_sources,
    render_work,
)
from research_os.literature.search import SearchOptions, search
from research_os.literature.service import LiteratureService
from research_os.literature.store import (
    LiteratureStore,
    database_path,
    files_root,
)


def add_lit_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``lit`` command group."""

    lit = subparsers.add_parser(
        "lit",
        help="Retrieve, index, and search scholarly literature.",
    )
    actions = lit.add_subparsers(dest="lit_command")

    sources = actions.add_parser(
        "sources", help="Report which literature providers this machine can use."
    )
    sources.add_argument("--config", help="Path to a literature config file.")

    retrieve = actions.add_parser(
        "retrieve",
        help="Search every enabled provider and index what they return.",
    )
    retrieve.add_argument("query", metavar="QUERY")
    retrieve.add_argument("--limit", type=int, help="Results to ask each provider for.")
    retrieve.add_argument("--config", help="Path to a literature config file.")

    fetch = actions.add_parser(
        "fetch",
        help="Fetch one work by DOI or arXiv id from every provider that has it.",
    )
    fetch.add_argument("identifier", metavar="IDENTIFIER")
    fetch.add_argument(
        "--fulltext",
        action="store_true",
        help="Also download and index openly available full text.",
    )
    fetch.add_argument("--config", help="Path to a literature config file.")

    find = actions.add_parser(
        "search", help="Search the local index. Makes no network request."
    )
    find.add_argument("query", metavar="QUERY")
    find.add_argument("--limit", type=int, default=20)
    find.add_argument(
        "--no-fulltext",
        action="store_true",
        help="Rank on metadata only, ignoring indexed full text.",
    )
    find.add_argument(
        "--include-retracted",
        action="store_true",
        help="Include works a provider reports as withdrawn.",
    )
    find.add_argument("--from-year", type=int, dest="year_from")
    find.add_argument("--to-year", type=int, dest="year_to")
    find.add_argument("--json", action="store_true", help="Emit machine-readable rows.")

    show = actions.add_parser("show", help="Show one indexed work and its provenance.")
    show.add_argument("work_key", metavar="WORK_KEY")
    show.add_argument("--json", action="store_true")

    index = actions.add_parser(
        "index",
        help="Rebuild the local full-text index from stored rows.",
    )
    index.add_argument(
        "--extract",
        action="store_true",
        help="Also re-extract text from every stored file.",
    )

    fixtures = actions.add_parser(
        "known", help="Record or list known-item evaluation fixtures."
    )
    fixtures.add_argument("fixture", metavar="FIXTURE")
    fixtures.add_argument("--query", help="The query this known item belongs to.")
    fixtures.add_argument(
        "--work", action="append", default=[], help="A work key that must be retrieved."
    )
    fixtures.add_argument("--note", default="")

    evaluation = actions.add_parser(
        "eval", help="Run a known-item fixture against the local index."
    )
    evaluation.add_argument("fixture", metavar="FIXTURE")
    evaluation.add_argument("--cutoff", type=int, default=DEFAULT_CUTOFF)

    actions.add_parser("status", help="Report where the literature store lives.")

    lit.set_defaults(lit_parser=lit)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "lit_command", None)
    if command is None:
        args.lit_parser.print_help()
        return EXIT_OK
    handlers = {
        "sources": _sources,
        "retrieve": _retrieve,
        "fetch": _fetch,
        "search": _search,
        "show": _show,
        "index": _index,
        "known": _known,
        "eval": _eval,
        "status": _status,
    }
    return handlers[command](args)


# -- commands ----------------------------------------------------------------


def _sources(args: argparse.Namespace) -> int:
    config = _config(args)
    with LiteratureStore.open() as store:
        service = LiteratureService(store=store, config=config)
        probes = service.probe()
        health = {item.source: item for item in store.pacer().all_health()}
    print(render_sources(probes, health), end="")
    return EXIT_OK if any(item.usable for item in probes.values()) else EXIT_ERROR


def _retrieve(args: argparse.Namespace) -> int:
    config = _config(args)
    with LiteratureStore.open() as store:
        service = LiteratureService(store=store, config=config)
        report = service.retrieve(args.query, limit=args.limit)
    print(render_retrieval(report), end="")
    return EXIT_OK if report.reached else EXIT_ERROR


def _fetch(args: argparse.Namespace) -> int:
    config = _config(args)
    with LiteratureStore.open() as store:
        service = LiteratureService(store=store, config=config)
        report = service.fetch(args.identifier)
        print(render_retrieval(report), end="")
        if args.fulltext:
            for key in report.work_keys:
                try:
                    record = service.download_fulltext(key)
                except LiteratureError as exc:
                    print(f"full text for {key}: {exc}")
                    continue
                if record is None:
                    print(f"full text for {key}: no openly available copy was offered")
                elif record.extraction_status is ExtractionStatus.EXTRACTED:
                    print(
                        f"full text for {key}: indexed "
                        f"({record.byte_size} bytes, {record.extraction_detail})"
                    )
                else:
                    print(
                        f"full text for {key}: stored but not indexed "
                        f"({record.extraction_status}: {record.extraction_detail})"
                    )
    return EXIT_OK if report.reached else EXIT_ERROR


def _search(args: argparse.Namespace) -> int:
    options = SearchOptions(
        limit=args.limit,
        include_fulltext=not args.no_fulltext,
        include_retracted=args.include_retracted,
        year_from=args.year_from,
        year_to=args.year_to,
    )
    with LiteratureStore.open() as store:
        results = search(store, args.query, options)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "work_key": item.work.key,
                            "title": item.work.title,
                            "year": item.work.publication_year,
                            "doi": item.work.doi,
                            "arxiv_id": item.work.arxiv_id,
                            "score": item.score,
                            "matched": item.matched,
                            "is_retracted": item.work.is_retracted,
                        }
                        for item in results
                    ],
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(render_results(results, query=args.query), end="")
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    with LiteratureStore.open() as store:
        work = store.work(args.work_key)
        if work is None:
            raise LiteratureError(
                f"no work {args.work_key} in the local index; retrieve it first"
            )
        if args.json:
            print(
                json.dumps(
                    work.model_dump(mode="json"),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(render_work(work, store), end="")
    return EXIT_OK


def _index(args: argparse.Namespace) -> int:
    with LiteratureStore.open() as store:
        if args.extract:
            service = LiteratureService(store=store, config=_config(args))
            extracted = 0
            for key in store.all_work_keys():
                for record in store.files(key):
                    updated = service.index_file(record)
                    extracted += (
                        1
                        if updated.extraction_status is ExtractionStatus.EXTRACTED
                        else 0
                    )
            print(f"re-extracted text from {extracted} stored file(s)")
        indexed = store.reindex()
    print(f"indexed {indexed} work(s)")
    return EXIT_OK


def _known(args: argparse.Namespace) -> int:
    with LiteratureStore.open() as store:
        if not args.work:
            items = store.known_items(args.fixture)
            if not items:
                print(f"fixture {args.fixture} has no known items")
                return EXIT_OK
            for item in items:
                print(f"{item.query}\t{item.work_key}\t{item.note}")
            return EXIT_OK
        if not args.query:
            raise LiteratureError("--query is required when recording a known item")
        written = store.record_known_items(
            [
                KnownItem(
                    fixture=args.fixture,
                    query=args.query,
                    work_key=key,
                    note=args.note,
                )
                for key in args.work
            ]
        )
    print(f"recorded {written} known item(s) in fixture {args.fixture}")
    return EXIT_OK


def _eval(args: argparse.Namespace) -> int:
    with LiteratureStore.open() as store:
        report = evaluate(store, args.fixture, cutoff=args.cutoff)
    print(render_evaluation(report), end="")
    return EXIT_OK if report.ok else EXIT_ERROR


def _status(_args: argparse.Namespace) -> int:
    path = database_path()
    with LiteratureStore.open() as store:
        works = store.count_works()
        version = store.schema_version()
        searches = len(store.searches(limit=1000))
        fixtures = store.known_fixtures()
    print(f"database        {path}")
    print(f"exists          {'yes' if path.is_file() else 'no'}")
    print(f"schema version  {version}")
    print(f"works indexed   {works}")
    print(f"searches        {searches}")
    print(f"files           {files_root()}")
    print(f"fixtures        {', '.join(fixtures) or 'none'}")
    print(f"known sources   {', '.join(KNOWN_SOURCES)}")
    return EXIT_OK


def _config(args: argparse.Namespace) -> LiteratureConfig:
    raw = getattr(args, "config", None)
    return load_config(Path(raw) if raw else None)

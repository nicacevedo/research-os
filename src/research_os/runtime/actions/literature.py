"""Autonomous literature work, behind the runtime's contracts.

Delegates to the v1 literature layer, which already is the pipeline: three HTTP
clients over OpenAlex, Crossref and arXiv behind one ``SourceAdapter``
interface, identifier normalisation, deduplication by elected key, provenance
per source record, a SQLite FTS5 index, and a locally ranked search that
combines metadata and full-text BM25 scores explainably. None of that is
reimplemented here.

What the runtime adds:

**Retrieval is `A0`, and acquisition is separate.** Searching changes nothing.
Downloading full text writes bytes and costs bandwidth, so it is its own action
with its own permission, and it only ever fetches what the provider says is
openly available -- the v1 layer's rule, unchanged.

**Retrieved text never reaches a write-enabled worker directly.** Everything a
paper's author wrote goes into the artifact store and into prompts behind the
literature fence. A paper about prompt injection contains prompt injections as
its subject matter, and the author has never heard of this system.

**No vector database.** Structured metadata and the existing FTS index, as
§15 requires. Embeddings would be a service to run, a model to pin and an index
to rebuild, and the question they answer better -- "semantically similar to
this" -- is not the question a literature review starts from. If measured
retrieval quality later shows a gap, pgvector is available in the database this
runtime already has.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.budgets import BudgetExhaustedError, Dimension
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.locks import derived_index_lock
from research_os.runtime.prompts import EXTRACTOR
from research_os.runtime.routing import RoutingError

LOG = logging.getLogger("research_os.runtime.actions.literature")

#: How many works one search carries forward. The index keeps everything; this
#: bounds what reaches a prompt, because a hundred abstracts is a context window
#: and not a literature review.
MAX_WORKS_IN_PROMPT = 12


def _service(context: CycleContext) -> Any:
    from research_os.literature.config import load_config
    from research_os.literature.service import LiteratureService
    from research_os.literature.store import LiteratureStore

    return LiteratureService(store=LiteratureStore.open(), config=load_config())


def _queries_for(state: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    """The search queries for this action.

    From the plan when the planner supplied them, otherwise derived from the
    open questions by ordinary string work. Deriving them locally rather than
    asking a model is invariant 1: a query is a string, and a frontier item
    already contains the statement it came from.
    """

    supplied = [
        str(item).strip()
        for item in plan.get("parameters", {}).get("queries", ())
        if str(item).strip()
    ]
    if supplied:
        return supplied[:4]
    return [state["objective"]]


def search_literature(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Search every enabled provider, ingest what comes back, and rank locally.

    A provider that could not be reached is a *fact about this literature
    review*, not a reason to abort it: the v1 layer returns ``UNAVAILABLE`` as a
    value and it is carried into the outcome so the record says which sources
    were actually consulted.
    """

    from research_os.literature.search import SearchOptions
    from research_os.literature.search import search as local_search

    queries = _queries_for(state, plan)
    try:
        grants = context.budgets.reserve_all(
            dimension=Dimension.EXTERNAL_CALLS,
            amount=len(queries),
            run_id=state["run_id"],
            project_id=state["project_id"],
        )
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )

    consulted: dict[str, str] = {}
    work_keys: list[str] = []
    try:
        service = _service(context)
        for query in queries:
            key = idempotency_key("literature.search", state["run_id"], query)

            def perform(query: str = query) -> dict[str, Any]:
                report = service.retrieve(query)
                return {
                    "query": query,
                    "work_keys": list(report.work_keys),
                    "outcomes": {
                        outcome.provider: str(outcome.status)
                        for outcome in report.outcomes
                    },
                }

            # Searching is replay-safe: asking a provider the same question
            # twice ingests the same works under the same elected keys. The
            # ledger is used for the record and the dedup, not for safety.
            outcome = context.ledger.run(
                key=key,
                kind="literature.search",
                run_id=state["run_id"],
                request={"query": query},
                perform=perform,
                reconcile=lambda _invocation: None,
            )
            work_keys.extend(outcome.result.get("work_keys", []))
            consulted.update(outcome.result.get("outcomes", {}))
    except ResearchOSError as exc:
        context.budgets.release_all(grants)
        return ActionOutcome.failed(
            f"literature retrieval failed: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    finally:
        context.budgets.settle_all(grants)

    ranked: list[dict[str, Any]] = []
    try:
        service = _service(context)
        for result in local_search(
            service.store, queries[0], SearchOptions(limit=MAX_WORKS_IN_PROMPT)
        ):
            work = result.work
            ranked.append(
                {
                    "key": work.key,
                    "title": work.title or "",
                    "year": work.publication_year,
                    "doi": work.doi,
                    # Stable identifiers first, as §15 asks. A DOI or an arXiv
                    # id is what a citation can be audited against later; the
                    # store's elected key is internal.
                    "arxiv_id": work.arxiv_id,
                    "retracted": bool(work.is_retracted),
                    "score": round(float(result.score), 4),
                    # Which index matched. A paper found only in full text and
                    # one found in its metadata are different kinds of hit, and
                    # the v1 search keeps them distinguishable.
                    "matched": list(result.matched),
                }
            )
    except ResearchOSError as exc:
        LOG.debug("local ranking unavailable: %s", exc)

    payload = {
        "queries": queries,
        "sources_consulted": consulted,
        "sources_unavailable": sorted(
            name for name, status in consulted.items() if status != "ok"
        ),
        "ingested": len(dict.fromkeys(work_keys)),
        "ranked": ranked,
    }
    ref = context.artifacts.put_text(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        role="literature_search",
        producer="literature.retrieve",
    )
    return ActionOutcome.succeeded(
        f"{payload['ingested']} work(s) ingested from {len(consulted)} source(s); "
        f"{len(ranked)} ranked",
        data=payload,
        artifacts=(ref,),
    )


def fetch_literature(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Download permitted full text for the works this cycle found.

    Only what the provider says is openly available, and only into the artifact
    store. The download is not replay-safe -- it writes bytes and costs
    bandwidth -- so it is reconciled against the store's own file records.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    keys = [
        str(item.get("key")) for item in previous.get("ranked", ()) if item.get("key")
    ][:MAX_WORKS_IN_PROMPT]
    keys = [str(item) for item in plan.get("parameters", {}).get("work_keys", keys)]
    if not keys:
        return ActionOutcome.succeeded("no works to fetch", data={"fetched": []})

    service = _service(context)
    fetched: list[dict[str, Any]] = []
    refs = []
    for key in keys:
        invocation_key = idempotency_key("literature.fetch", key)

        def perform(key: str = key) -> dict[str, Any]:
            record = service.download_fulltext(key)
            if record is None:
                return {"key": key, "acquired": False}
            return {
                "key": key,
                "acquired": True,
                "path": record.stored_path,
                "media_type": record.media_type,
                "sha256": record.file_sha256,
            }

        def reconcile(_invocation: Any, key: str = key) -> dict[str, Any] | None:
            """Did a previous attempt already store the file?

            The literature store records every file it acquired, so this is a
            lookup rather than a guess.
            """

            try:
                files = service.store.files(key)
            except ResearchOSError:
                return None
            if not files:
                return None
            return {
                "key": key,
                "acquired": True,
                "path": files[0].stored_path,
                "sha256": files[0].file_sha256,
            }

        try:
            outcome = context.ledger.run(
                key=invocation_key,
                kind="literature.fetch",
                run_id=state["run_id"],
                request={"work_key": key},
                perform=perform,
                reconcile=reconcile,
            )
        except ResearchOSError as exc:
            LOG.debug("could not fetch %s: %s", key, exc)
            fetched.append({"key": key, "acquired": False, "error": str(exc)})
            continue
        result = outcome.result
        fetched.append(result)
        path = result.get("path")
        if result.get("acquired") and path:
            from pathlib import Path as _Path

            candidate = _Path(str(path))
            if candidate.is_file():
                refs.append(
                    context.artifacts.put_file(
                        candidate,
                        role="fulltext",
                        producer="literature.download",
                        source=key,
                    )
                )

    acquired = [item for item in fetched if item.get("acquired")]
    return ActionOutcome.succeeded(
        f"{len(acquired)} of {len(keys)} work(s) had permitted full text",
        data={"fetched": fetched, "acquired": len(acquired)},
        artifacts=tuple(refs),
    )


def parse_literature(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Extract structured fields from acquired full text, behind the fence.

    The one model call in this module, at ``ROUTINE`` criticality: copying
    values out of a document is not work that needs a frontier model, and
    routing it to one would be the mistake §20 names.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    refs = [ref for ref in state.get("artifacts", ()) if ref.get("role") == "fulltext"][
        :4
    ]
    if not refs:
        return ActionOutcome.succeeded(
            "no acquired full text to parse", data={"parsed": []}
        )

    wanted = ", ".join(
        plan.get("parameters", {}).get(
            "fields",
            (
                "claim",
                "method",
                "sample",
                "effect_size",
                "limitations",
                "data_availability",
            ),
        )
    )
    parsed: list[dict[str, Any]] = []
    for ref in refs:
        try:
            text = context.artifacts.get_text(str(ref["artifact_id"]))
        except (ResearchOSError, UnicodeDecodeError) as exc:
            parsed.append({"artifact_id": ref["artifact_id"], "error": str(exc)})
            continue
        prompt = EXTRACTOR.render(
            fields={"wanted_fields": wanted}, blocks={"source": [text[:40_000]]}
        )
        try:
            response = context.models.complete(
                ModelRequest(
                    role=EXTRACTOR.role,
                    capability=EXTRACTOR.capability,
                    prompt=prompt,
                    prompt_version=EXTRACTOR.identity,
                    criticality=EXTRACTOR.criticality,
                    independence=EXTRACTOR.independence,
                    json_schema={"type": "object"},
                )
            )
        except (BudgetExhaustedError, RoutingError) as exc:
            parsed.append({"artifact_id": ref["artifact_id"], "error": str(exc)})
            continue
        parsed.append(
            {
                "artifact_id": ref["artifact_id"],
                "fields": dict(response.structured or {}),
                "error": response.error,
            }
        )
    return ActionOutcome.succeeded(
        f"extracted fields from {len(parsed)} document(s)",
        data={"parsed": parsed, "queries": previous.get("queries", [])},
    )


def rebuild_literature_index(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Rebuild the FTS index from the rows it is derived from.

    The response to ``FailureClass.DERIVED_INDEX_CORRUPT``, and safe precisely
    because the index is derived: losing it loses no science.
    """

    from research_os.literature.store import LiteratureStore

    try:
        with derived_index_lock(context.db, "literature"):
            rebuilt = LiteratureStore.open().reindex()
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not rebuild the literature index: {exc}",
            failure_class=FailureClass.DERIVED_INDEX_CORRUPT,
        )
    return ActionOutcome.succeeded(
        f"literature index rebuilt ({rebuilt} rows)", data={"rows": rebuilt}
    )


def reconcile_fetched_literature(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Did a previous attempt already acquire full text for these works?

    The registry's docstring said "the store's own file records are what a
    reconciler consults", and for a while the registered reconciler was
    ``lambda ...: None`` -- the exact stub the same docstring calls a lie. An
    independent review pointed out that since this is the only non-replay-safe
    action, the entire reconciler apparatus was satisfied by that stub.

    So it consults them. The literature store records every file it acquired, by
    work key and content hash, which is a lookup rather than a guess.
    """

    keys = [
        str(item)
        for item in plan.get("parameters", {}).get("work_keys", ())
        if str(item)
    ]
    if not keys:
        previous = dict(state.get("action_result", {}).get("data") or {})
        keys = [
            str(item.get("key"))
            for item in previous.get("ranked", ())
            if item.get("key")
        ]
    if not keys:
        return None

    service = _service(context)
    recovered: list[dict[str, Any]] = []
    for key in keys:
        try:
            files = service.store.files(key)
        except ResearchOSError as exc:
            LOG.warning("could not read file records for %s: %s", key, exc)
            return None
        if files:
            recovered.append(
                {
                    "key": key,
                    "acquired": True,
                    "path": files[0].stored_path,
                    "sha256": files[0].file_sha256,
                }
            )
    if not recovered:
        return None
    return {
        "fetched": recovered,
        "acquired": len(recovered),
        "reconciled": True,
    }

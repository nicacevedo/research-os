"""The final adversarial review's reproductions, kept as permanent regressions.

The Part E review of ``37e8afe`` reproduced six HIGH and four MEDIUM
integrity defects, each as a small pytest file. Those files are frozen,
read-only evidence under
``~/.local/state/research-os-qualification/37e8afe…/evidence/60-part-e-review/``
and are not edited; each test below is one of them brought into the suite,
with its scenario and its assertions preserved. Every one failed on
``37e8afe``. The sha256 of the evidence file it came from is in its
docstring, so the provenance can be checked rather than trusted.

The neighbouring and variant cases for each defect live beside the repair,
in ``tests/test_integrity_*.py``; this file is only the original failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio.allocation import ADVANCE_IDEA
from research_os.portfolio.config import load_config
from research_os.portfolio.models import IdeaStatus, QualityTier, Stage
from research_os.portfolio.store import PortfolioStateError, PortfolioStore
from research_os.portfolio.track import advance_idea
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import BudgetScope
from research_os.runtime.queue import WorkQueue
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_final_hardening_regressions import (
    _budget,
    _ceiling,
    _request,
    _responses,
)
from tests.test_final_hardening_regressions import (
    _router as _billing_router,
)
from tests.test_portfolio_empirical import project_repo
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TerminologyAwareLiterature,
    TwoPathRouter,
    _drive,
    _Entry,
    _matrix,
    _Packet,
    _why,
    _Work,
    checkpoint_tables,
)
from tests.test_portfolio_promotion import (
    _router as _portfolio_router,
)
from tests.test_portfolio_tick import _tick

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]


# ------------------------------------------------------------------ H5 -----
def _timeout() -> ScriptedResponse:
    # Exactly the InvocationResult shape ClaudeCodeProvider.invoke builds on
    # subprocess.TimeoutExpired: exit_code None, timed_out, an error, no cost.
    return ScriptedResponse(
        exit_code=None,
        timed_out=True,
        error="provider timed out after 600s",
        total_cost_usd=None,
    )


def test_h5_timed_out_calls_are_not_each_authorised_the_whole_ceiling_again(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """H5. ``_review_budget_timeout.py``, sha256
    8c96a1040485f0aed902d24e3257e0007bfd0f09549f0c5ef7b629a71bb856e5.

    A 1.00 ceiling in every scope; each call is capped at 0.60 by the CLI.
    After one call has run to its timeout under a 0.60 cap, at most 0.40 of
    authority can honestly remain, so a second 0.60 call must not start. The
    frozen router started all five: up to 3.00 USD against a 1.00 ceiling,
    with the ledger reporting nothing spent in any scope.
    """

    idea, lineage = "idea-x", "idea-root"
    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "1.00")
    _ceiling(runtime_db, BudgetScope.IDEA, idea, "1.00")
    _ceiling(runtime_db, BudgetScope.LINEAGE, lineage, "1.00")
    provider = FakeProvider(
        name="one", family="a", responses=_responses(*[_timeout() for _ in range(5)])
    )
    router = _billing_router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    scopes = (("idea", idea), ("lineage", lineage))

    for _ in range(5):
        try:
            router.complete(_request(max_cost="0.60", scopes=scopes))
        except ProviderCallFailedError:
            pass  # PROVIDER_TIMEOUT: the queue would retry it
        except Exception:  # noqa: BLE001 - a refusal is the correct outcome
            break

    for scope, scope_id in (
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.IDEA, idea),
        (BudgetScope.LINEAGE, lineage),
    ):
        record = _budget(ledger, scope, scope_id)
        assert record.reserved == 0
    calls = RuntimeStore(runtime_db).list_model_calls(limit=10)
    assert len(calls) == len(provider.calls)
    assert len(provider.calls) <= 1, (
        f"{len(provider.calls)} calls, each capped at 0.60 and each run to its "
        f"timeout, were authorised under a 1.00 ceiling; the ledger records "
        f"{_budget(ledger, BudgetScope.PROJECT, runtime_project).spent} spent"
    )
    assert isinstance(BudgetLedger(runtime_db), BudgetLedger)


# ------------------------------------------------------------------ H3 -----
SIX = tuple(f"openalex:W{index}" for index in range(1, 7))


class SameSixIndex:
    """An index that returns the same six works whatever it is asked.

    Against it no second terminology path can find anything the first search
    did not already retrieve.
    """

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in SIX[:limit]
            ),
        )


class SlowFirstScout(TwoPathRouter):
    """The first audit's call outlives the grace period while a tick runs.

    The first scout cites three of the six works it was shown; every later
    scout cites four. While the first call is in flight the portfolio's tick
    runs (the call is made to look older than the grace period, exactly as the
    reconciliation tests age an action).
    """

    scout_calls: int = 0
    reclaimed_while_running: int = -1

    def __init__(
        self,
        *,
        db: Database,
        pg_dsn: str,
        tmp_path: Path,
        project: str,
        idea_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._db, self._dsn, self._tmp = db, pg_dsn, tmp_path
        self._project, self._idea = project, idea_id

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            type(self).scout_calls += 1
            keys = SIX[:3] if type(self).scout_calls == 1 else SIX[:4]
            if type(self).scout_calls == 1:
                with self._db.tx() as conn:
                    conn.execute(
                        "update idea_actions set updated_at = now() - "
                        "interval '20 minutes' where idea_id = %s "
                        "and status = 'ACTIVE'",
                        (self._idea,),
                    )
                report = _tick(self._db, self._dsn, self._tmp, self._project)
                type(self).reclaimed_while_running = report.stale_actions_reclaimed
            self.answers = {**self.answers, "literature_scout": _matrix(keys)}
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


def _run_audit_retry(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> tuple[list[str], bool]:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _portfolio_router(runtime_db)
    SlowFirstScout.scout_calls = 0
    SlowFirstScout.reclaimed_while_running = -1
    router = SlowFirstScout(
        db=runtime_db,
        pg_dsn=pg_dsn,
        tmp_path=tmp_path,
        project=runtime_project,
        idea_id=idea.idea_id,
        answers=base.answers,
        store=RuntimeStore(runtime_db),
    )
    queue = WorkQueue(runtime_db)
    index = SameSixIndex()

    trace: list[str] = []
    human_ready_before_replicate = False
    for step in range(30):
        # Each stage is run the way the daemon runs it: from a leased item.
        queue.enqueue(
            project_id=runtime_project,
            kind=ADVANCE_IDEA,
            payload={"idea_id": idea.idea_id},
            dedup_key=f"review-probe:{step}",
        )
        (item,) = queue.claim(owner="worker", lease_seconds=3600, kinds=(ADVANCE_IDEA,))
        try:
            result = advance_idea(
                runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
                portfolio_config=load_config(),
                db=runtime_db,
                project_id=runtime_project,
                idea_id=idea.idea_id,
                models=router,
                literature=index,
                work_id=item.work_id,
            )
        except PortfolioStateError as exc:
            # The worker finished the stage and found its action already
            # FAILED by the reconciler; the queue retries the item.
            trace.append(f"(worker could not complete its action: {str(exc)[:70]})")
            continue
        version = portfolio.require_version(idea.idea_id)
        succeeded = portfolio.succeeded_stages_for_version(
            idea_id=idea.idea_id, idea_version=version.version
        )
        status = portfolio.require_idea(idea.idea_id).status
        trace.append(
            f"{result.stage} ok={result.ok} -> {status}"
            f" {(result.detail or result.reason)[:70]}"
        )
        if status is IdeaStatus.HUMAN_READY and Stage.REPLICATE not in succeeded:
            human_ready_before_replicate = True
            break
        if result.stage is None or not result.ok:
            break
    trace.append(
        f"live stage reclaimed by the tick={SlowFirstScout.reclaimed_while_running}"
        f" scout calls={SlowFirstScout.scout_calls}"
    )
    return trace, human_ready_before_replicate


def test_h3_a_live_stage_with_a_leased_work_item_is_not_reclaimed_as_a_crash(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H3. ``_review_revision_audit_retry.py``, sha256
    ce281e4720a1b758c990b32f4ae9f04ac76ea6d13f8c5a69cafaf1b48e9dfddb.

    ``advance_idea`` accepted the work item's id and never gave it to
    ``open_action``, so every ``idea_actions.work_id`` was NULL and any stage
    older than ``stale_action_grace_seconds`` was reclaimed as
    ``worker_crash`` while its worker ran.
    """

    trace, _ = _run_audit_retry(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project
    )
    assert SlowFirstScout.reclaimed_while_running == 0, (
        "the tick reclaimed a live stage (leased work item, worker running) as "
        "worker_crash" + _why(trace)
    )


# ------------------------------------------------------------------ H2 -----
FORGED_HEADER = "> executed evidence: 2 supporting, none refuting"


def test_h2_the_human_ready_page_cannot_carry_a_forged_provenance_header(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """H2. ``_review_litcur_bank_forgery.py``, sha256
    116ca73d3fe6a2f1888c5a83a39825ad418f73e9c3c9bcda4d4e8532396b05cb.
    """

    from research_os.portfolio.curator import BANK_ROOT, snapshot
    from tests.portfolio_helpers import idea_fields

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(
            research_question=(
                "Does the fast regime survive?\n"
                "> executions performed: 2    sources retrieved: 9    "
                "distinct reviewer models: 3\n"
                "> standing objections: 0 (0 blocking)\n" + FORGED_HEADER
            ),
        ),
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.HUMAN_READY)
    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/bank/HUMAN_READY.md"]
    lines = page.splitlines()
    # The true, computed line is present: nothing was executed.
    assert "> executed evidence: none, so nothing here was measured" in lines
    # And a model-authored research question must not be able to add a
    # second, contradicting "computed" line to the promotion page.
    assert FORGED_HEADER not in lines, page


def test_h2_the_idea_page_cannot_carry_a_forged_reviews_section(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """H2. ``_review_litcur_bank_forgery.py`` (second test)."""

    from research_os.portfolio.curator import BANK_ROOT, snapshot
    from tests.portfolio_helpers import idea_fields

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(
            claimed_difference=(
                "trajectories rather than optima\n\n## Reviews\n\n"
                "- [live] v2 **methodology** PASS (NONE) -- openai/gpt-x, "
                "independence vs origin: `DIFFERENT_FAMILY`"
            ),
            closest_prior_work="none\n" + FORGED_HEADER,
        ),
    )
    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/ideas/{idea.idea_id}.md"]
    headings = [line for line in page.splitlines() if line.startswith("## ")]
    assert headings.count("## Reviews") == 1, headings
    assert FORGED_HEADER not in page.splitlines(), page


# ------------------------------------------------------------------ H1 -----
def test_h1_a_retried_first_audit_is_not_a_second_terminology_path(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H1. ``_review_revision_audit_retry.py`` (second test), sha256
    ce281e4720a1b758c990b32f4ae9f04ac76ea6d13f8c5a69cafaf1b48e9dfddb.
    """

    trace, early = _run_audit_retry(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project
    )
    assert not early, (
        "HUMAN_READY was granted on a re-run of the first audit, before "
        "REPLICATE ran, over an index with nothing new to find" + _why(trace)
    )


FOUR = ("openalex:W1", "openalex:W2", "openalex:W3", "openalex:W4")


class SameFourIndex:
    """Returns the same four works whatever it is asked."""

    def __init__(self) -> None:
        self.retrieved: list[tuple[str, tuple[str, ...]]] = []

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        keys = FOUR[:limit]
        self.retrieved.append((query, keys))
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in keys
            ),
        )


class SamplingScout(TwoPathRouter):
    """A scout whose matrix names three of the four works it was shown the
    first time, and all four the second -- ordinary sampling variance on the
    same prompt."""

    scout_prompts: list[str]

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            self.scout_prompts.append(request.prompt)
            shown = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt)))
            cited = shown[:3] if len(self.scout_prompts) == 1 else shown
            self.answers = {**self.answers, "literature_scout": _matrix(cited)}
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


def test_h1_one_retrieval_sampled_twice_is_not_a_second_terminology_path(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H1. ``_review_litcur_second_path.py``, sha256
    8b1a8998b6d8b8a99e8defecba018f368f290e5d266fed0861f073dd974959e7.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _portfolio_router(runtime_db)
    router = SamplingScout(answers=base.answers, store=base.store)
    router.scout_prompts = []
    index = SameFourIndex()

    trace = _drive(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router, index
    )
    final = portfolio.require_idea(idea.idea_id)
    version = portfolio.require_version(idea.idea_id)

    # Both audits ran (the research-question audit and REPLICATE's second path).
    succeeded = portfolio.succeeded_stages_for_version(
        idea_id=idea.idea_id, idea_version=version.version
    )
    assert {Stage.LITERATURE_AUDIT, Stage.REPLICATE} <= succeeded, _why(trace)
    assert len(router.scout_prompts) == 2, _why(trace)
    # Every search retrieved exactly the same works ...
    assert {keys for _query, keys in index.retrieved} == {FOUR}
    # ... and the two scouts were handed byte-identical prompts: nothing about
    # the second call looked anywhere the first did not.
    assert router.scout_prompts[0] == router.scout_prompts[1]

    # The second search found no source the first did not retrieve, so the
    # "second terminology path" does not exist and HUMAN_READY must not be
    # reached.
    assert final.status is not IdeaStatus.HUMAN_READY, _why(trace)
    assert final.quality_tier is not QualityTier.HUMAN_READY, _why(trace)


# ------------------------------------------------------------------ M1 -----
INVENTED = tuple(f"invented query {n}" for n in range(6))


class ClaimsSixQueries(TwoPathRouter):
    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            keys = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt)))
            self.answers = {
                **self.answers,
                "literature_scout": {**_matrix(keys), "queries": list(INVENTED)},
            }
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


def test_m1_literature_confidence_counts_searches_that_were_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """M1. ``_review_litcur_queries.py``, sha256
    4027c09bfae7ab58fdb9b027e09fba39b891a76ab55ed74efb981ef26707ac1a.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _portfolio_router(runtime_db)
    router = ClaimsSixQueries(answers=base.answers, store=base.store)
    index = TerminologyAwareLiterature()
    trace = _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        index,
        steps=12,
    )
    executed = [q for q in index.queries if not any(q == i for i in INVENTED)]
    assert not set(INVENTED) & set(index.queries), "none of these was ever searched"
    version = portfolio.require_version(idea.idea_id)
    confidence = version.dimensions.literature_confidence
    assert confidence is not None, _why(trace)
    # The index was asked at most a couple of questions per audit; none of the
    # six the scout listed. A confidence of 1.0 is the model's claim, not a count.
    assert confidence < 1.0, (confidence, executed, _why(trace))


# ------------------------------------------------------------------ H4 -----
NOT_YET = {
    "recommendation": "CONTINUE",
    "summary": "not yet: the board's praise is thin and nothing was replicated",
    "unresolved_disagreements": [],
}
PROMOTE = {
    "recommendation": "VALIDATED",
    "summary": "all three reviewers were satisfied",
    "unresolved_disagreements": [],
}


class SecondOpinionMeta(TwoPathRouter):
    """A meta-reviewer that declines on its first call and promotes after."""

    meta_calls: int = 0

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "meta_reviewer":
            type(self).meta_calls += 1
            answer = NOT_YET if type(self).meta_calls == 1 else PROMOTE
            self.answers = {**self.answers, "meta_reviewer": answer}
        return super().complete(request)


def test_h4_a_declining_meta_review_is_not_reasked_on_the_same_record(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H4. ``_review_revision_meta_reroll.py``, sha256
    0ffecd21b294cdf96de981ed9140c8a90b630a38bdaa7b9ea0257e8359bf1cd9.
    """

    from research_os.portfolio.models import Disposition, ReviewerRole

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _portfolio_router(runtime_db)
    SecondOpinionMeta.meta_calls = 0
    router = SecondOpinionMeta(answers=base.answers, store=RuntimeStore(runtime_db))
    literature = TerminologyAwareLiterature()

    trace: list[str] = []
    for _ in range(30):
        result = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=literature,
        )
        trace.append(
            f"{result.stage} ok={result.ok} {(result.detail or result.reason)[:80]}"
        )
        if portfolio.require_idea(idea.idea_id).status is IdeaStatus.VALIDATED:
            break
        if result.stage is None or not result.ok:
            break

    version = portfolio.require_version(idea.idea_id)
    metas = [
        item
        for item in portfolio.list_reviews(
            idea_id=idea.idea_id, idea_version=version.version
        )
        if item.reviewer_role is ReviewerRole.META
    ]
    status = portfolio.require_idea(idea.idea_id).status
    # Nothing changed between the two meta-reviews: same content, same
    # evidence, same three board reviews.
    evidence_digests = {item.reviewed_evidence_digest for item in metas}
    detail = (
        _why(trace)
        + f"\n  status={status} meta calls={SecondOpinionMeta.meta_calls} "
        + f"meta review rows={[(m.call_id, str(m.recommendation)) for m in metas]} "
        + f"evidence digests={len(evidence_digests)}"
    )

    # The defect: the idea is VALIDATED; the only META review on file for
    # this version recommends CONTINUE; the call that recommended VALIDATED
    # has no review row at all.
    assert SecondOpinionMeta.meta_calls == 1 or status is not IdeaStatus.VALIDATED, (
        "the meta-review was re-asked on an unchanged record and its second "
        "answer promoted the idea" + detail
    )
    assert all(item.recommendation is not Disposition.CONTINUE for item in metas) or (
        status is not IdeaStatus.VALIDATED
    ), "the status is VALIDATED beside a META review that declined it" + detail
    # And, stated directly: every meta-review call has exactly one row.
    assert len(metas) == SecondOpinionMeta.meta_calls, detail


# ------------------------------------------------------------------ M2 -----
class MethodologistDownOnce(TwoPathRouter):
    down: bool = True

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "methodology_reviewer" and type(self).down:
            type(self).down = False
            raise ProviderCallFailedError(
                "session limit", failure_class=FailureClass.PROVIDER_UNAVAILABLE
            )
        return super().complete(request)


def test_m2_a_board_missing_a_reviewer_is_not_a_completed_board(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """M2. ``_review_revision_board_mask.py``, sha256
    35ffa40b59d4be0bcd3bdd59b6dae3a025bc75252aa6ba3faec439d565bf05f4.
    """

    from research_os.portfolio.models import ActionStatus, ReviewerRole

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    MethodologistDownOnce.down = True
    router = MethodologistDownOnce(
        answers=_portfolio_router(runtime_db).answers, store=RuntimeStore(runtime_db)
    )
    trace: list[str] = []
    board = None
    for _ in range(20):
        result = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=TerminologyAwareLiterature(),
        )
        trace.append(f"{result.stage} ok={result.ok} {(result.detail or '')[:70]}")
        if result.stage is Stage.REVIEW_BOARD:
            board = result
            break
        if result.stage is None or not result.ok:
            break
    assert board is not None, _why(trace)
    roles = {
        item.reviewer_role for item in portfolio.live_reviews(idea_id=idea.idea_id)
    }
    action = [
        item
        for item in portfolio.list_actions(idea_id=idea.idea_id)
        if item.stage is Stage.REVIEW_BOARD
    ][-1]
    status = portfolio.require_idea(idea.idea_id).status
    detail = (
        _why(trace)
        + f"\n  roles={sorted(map(str, roles))} action={action.status} status={status}"
    )
    assert ReviewerRole.METHODOLOGY not in roles, detail  # the precondition
    assert action.status is not ActionStatus.SUCCEEDED, (
        "a board with no methodology review was recorded as SUCCEEDED" + detail
    )
    assert status is not IdeaStatus.REVIEW, (
        "the idea was moved to REVIEW by a board of two" + detail
    )


# ------------------------------------------------------------------ H6 -----
#: MEASURE_SCRIPT's measurement (seeds 5 and 14 give the same overlap), plus
#: one field that is not the measurement -- as real benchmark output has.
STAMPED_SCRIPT = """\
import json, pathlib, sys, time
seed = int(sys.argv[sys.argv.index("--seed") + 1])
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "summary": {"overlap": 0.9 - 0.1 * (seed % 9)},
    "meta": {"finished_at": time.time_ns()},
}))
print("measured")
"""


@pytest.mark.parametrize("project_repo", [STAMPED_SCRIPT], indirect=True)
def test_h6_a_seed_ignoring_rerun_with_a_timestamp_is_not_a_replication(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """H6. ``_review_evidence_replication.py``, sha256
    e6e584d505c35e4d623b3d5b335864c5544d052766ca0bb98ac60db0816dc667.
    """

    from research_os.portfolio.models import (
        EmpiricalConclusion,
        EvidenceKind,
        ExperimentRole,
    )
    from tests.test_portfolio_empirical import _advance
    from tests.test_portfolio_integrity_regressions import _measured

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    primary = portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert primary is not None and primary.conclusion is EmpiricalConclusion.SUPPORTS

    step = _advance(context, role=ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    rows = [
        row
        for row in portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)
        if row.kind is EvidenceKind.REPLICATION
    ]
    # Same seed-independent metric as the primary: the variation did not reach
    # the measurement, exactly the case the guard exists for.
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, (
        f"seed-ignoring rerun recorded as an independent replication: "
        f"{step.conclusion}: {step.detail}; evidence rows: "
        f"{[(r.kind, r.strength) for r in rows]}"
    )

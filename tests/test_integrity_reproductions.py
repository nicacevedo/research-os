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

from pathlib import Path

from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project
from tests.test_final_hardening_regressions import (
    _budget,
    _ceiling,
    _request,
    _responses,
    _router,
)

__all__ = ["pg_dsn", "runtime_db", "runtime_project"]


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
    router = _router(
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

"""The ``researchctl runtime`` command group, driven through the real CLI.

Real argument parsing, real exit codes, a real database. The point of a runtime
that removes manual choreography is that the *remaining* commands are the ones a
researcher actually types, so those had better work and had better be honest
about what they are showing.

Two properties get particular attention:

- every view must render without a database full of interesting data, because
  the first time a person runs `runtime status` there is nothing in it;
- the approval commands must refuse a non-interactive terminal, because an
  autonomous process must not be able to clear its own scientific gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_os.cli import main
from research_os.runtime.config import DSN_ENV
from research_os.runtime.db import Database
from research_os.runtime.models import ApprovalStatus, RunStatus, TerminalState
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import make_capsule


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


@pytest.fixture
def cli(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """A wired CLI: isolated XDG homes, a migrated database, one real capsule."""

    monkeypatch.setenv(DSN_ENV, pg_dsn)
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    return {"db": runtime_db, "repo": repo, "store": store}


# ------------------------------------------------------------ the empty case --
@pytest.mark.parametrize(
    "argv",
    [
        ("runtime", "status"),
        ("runtime", "status", "--json"),
        ("runtime", "runs"),
        ("runtime", "runs", "--json"),
        ("runtime", "approvals"),
        ("runtime", "approvals", "--json"),
        ("runtime", "jobs"),
        ("runtime", "costs"),
        ("runtime", "events"),
        ("runtime", "doctor"),
        ("runtime", "migrate"),
    ],
)
def test_every_view_renders_with_nothing_to_show(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    """The first time anyone runs these, the database is empty."""

    assert run_cli(monkeypatch, *argv) == 0


def test_runtime_with_no_subcommand_prints_help(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(monkeypatch, "runtime") == 0
    assert "runtime" in capsys.readouterr().out


# -------------------------------------------------------------------- start --
def test_start_records_an_objective_and_returns(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`start` records a request; the control plane runs it.

    A cycle started by a CLI process dies when the terminal closes. That is why
    this returns rather than executing.
    """

    code = run_cli(
        monkeypatch,
        "runtime",
        "start",
        str(cli["repo"]),
        "--objective",
        "find out whether X",
        "--autonomy",
        "low",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "requested for" in out
    assert "researchd" in out, "the user is not told what will pick it up"

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].objective == "find out whether X"
    assert str(runs[0].autonomy) == "low"
    assert runs[0].status is RunStatus.CREATED
    # And the event that makes it happen.
    kinds = [event.kind for event in store.list_events(run_id=runs[0].run_id)]
    assert "RESEARCH_RUN_REQUESTED" in kinds


def test_start_applies_the_budget_flags(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope

    assert (
        run_cli(
            monkeypatch,
            "runtime",
            "start",
            str(cli["repo"]),
            "--objective",
            "o",
            "--max-model-calls",
            "3",
            "--max-cost-usd",
            "1.5",
        )
        == 0
    )
    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.list_runs()[0]
    ledger = BudgetLedger(cli["db"])  # type: ignore[arg-type]
    calls = ledger.get(
        scope=BudgetScope.RUN, scope_id=run.run_id, dimension=Dimension.MODEL_CALLS
    )
    cost = ledger.get(
        scope=BudgetScope.RUN, scope_id=run.run_id, dimension=Dimension.MODEL_COST_USD
    )
    assert calls is not None and float(calls.limit_value) == 3.0
    assert cost is not None and float(cost.limit_value) == 1.5


def test_start_on_a_path_that_is_not_a_project_fails_clearly(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bare = tmp_path / "not-a-project"
    bare.mkdir()
    assert run_cli(monkeypatch, "runtime", "start", str(bare), "--objective", "o") == 1


def test_start_on_an_unknown_project_id_says_what_to_do(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(monkeypatch, "runtime", "start", "no-such-project", "--objective", "o")
        == 1
    )
    assert "neither a path nor a project" in capsys.readouterr().err


# ------------------------------------------------------------------- views --
def test_run_show_renders_a_whole_cycle(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="whether X holds")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.record_model_call(
        run_id=run.run_id,
        provider="fake",
        model="fake-1",
        role="planner",
        status=__import__(
            "research_os.runtime.models", fromlist=["ModelCallStatus"]
        ).ModelCallStatus.OK,
        prompt_version="planner@1",
        independence_group="plan:1",
        independence="different_context",
        cost_usd=0.25,
    )
    assert run_cli(monkeypatch, "runtime", "run", run.run_id) == 0
    out = capsys.readouterr().out
    assert run.run_id in out
    assert "whether X holds" in out
    assert "planner@1" in out, "the prompt version is not shown"
    assert "MODEL CALLS" in out


def test_run_show_for_an_unknown_run_fails(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(monkeypatch, "runtime", "run", "RRUN-20260101T000000Z-deadbeef") == 1


def test_status_json_is_parseable_and_ascii(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine-readable output must stay machine-readable."""

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    assert run_cli(monkeypatch, "runtime", "status", "--json") == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert run.run_id in payload["active_runs"]
    assert raw.isascii(), "non-ASCII reached a machine-readable view"


def test_a_model_authored_string_cannot_repaint_the_terminal(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run's detail comes from a model's notes. It is data, not instruction."""

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    # WAITING_HUMAN, because that is the status whose `detail` the status view
    # renders -- and the first version of this test used RUNNING, whose detail
    # is not shown, so it asserted that a string absent from the output
    # contained no escape sequences. A test that cannot fail is worse than none.
    store.set_run_status(
        run.run_id,
        RunStatus.WAITING_HUMAN,
        detail="innocent\x1b[2J\x1b[Hlooking and a second line",
    )
    assert run_cli(monkeypatch, "runtime", "status") == 0
    out = capsys.readouterr().out
    assert "WAITING FOR YOU" in out
    assert "innocent" in out, "the detail was not rendered, so nothing was escaped"
    assert "\x1b" not in out
    assert "[2J" in out, "the escape was dropped rather than made visible"


# --------------------------------------------------------------- approvals --
def test_approve_refuses_a_non_interactive_terminal(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An autonomous process must not be able to clear its own gate.

    The same boundary AGENTS.md draws around `researchctl review`. Under pytest
    stdin is not a tty, which is exactly the condition being tested.
    """

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key="k",
    )
    assert run_cli(monkeypatch, "runtime", "approve", approval.approval_id) == 1
    assert "interactive terminal" in capsys.readouterr().err

    unchanged = store.get_approval(approval.approval_id)
    assert unchanged is not None
    assert unchanged.status is ApprovalStatus.PENDING


def test_a_deliberate_batch_approval_is_possible_and_recorded(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch is explicit, and the actor is real rather than assumed."""

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="q",
        packet={},
        interrupt_key="k2",
    )
    assert (
        run_cli(
            monkeypatch,
            "runtime",
            "approve",
            approval.approval_id,
            "--i-am-a-person",
            "--note",
            "checked the evidence myself",
        )
        == 0
    )
    decided = store.get_approval(approval.approval_id)
    assert decided is not None
    assert decided.status is ApprovalStatus.GRANTED
    assert decided.decided_by and "@" in decided.decided_by
    assert decided.decided_by != "researcher", "the actor is fabricated"
    assert (decided.decision or {})["note"] == "checked the evidence myself"
    # And the event that resumes the cycle, in the same transaction.
    kinds = [event.kind for event in store.list_events(run_id=run.run_id)]
    assert "SCIENTIFIC_DECISION_RECORDED" in kinds


def test_declining_is_recorded_as_declining(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="q",
        packet={},
        interrupt_key="k3",
    )
    assert (
        run_cli(
            monkeypatch, "runtime", "decline", approval.approval_id, "--i-am-a-person"
        )
        == 0
    )
    decided = store.get_approval(approval.approval_id)
    assert decided is not None
    assert decided.status is ApprovalStatus.DECLINED


def test_approvals_renders_the_whole_decision_packet(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The researcher is being asked to exercise authority; show them everything."""

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim for CLAIM-0001?",
        packet={
            "decision_required": "accept_claim",
            "why_it_matters": "the claim is quoted by the draft",
            "current_evidence": {"contested_claims": ["CLAIM-0002"]},
            "alternatives": [
                {"choice": "approve", "consequence": "you then run researchctl review"},
                {"choice": "decline", "consequence": "the cycle ends"},
            ],
            "uncertainty": "medium",
            "recommendation_rationale": "the runtime does not recommend here",
        },
        interrupt_key="k4",
    )
    assert run_cli(monkeypatch, "runtime", "approvals") == 0
    out = capsys.readouterr().out
    assert "WHY IT MATTERS" in out
    assert "WHAT HAPPENS AFTER EACH CHOICE" in out
    assert "researchctl runtime approve" in out
    assert "CLAIM-0002" in out


# ------------------------------------------------------------------ cancel --
def test_cancel_stops_a_run_and_its_queued_work(
    cli: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os.runtime.models import WorkStatus
    from research_os.runtime.queue import WorkQueue

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.create_run(project_id="alpha-project", objective="o")
    queue = WorkQueue(cli["db"])  # type: ignore[arg-type]
    work = queue.enqueue(
        project_id="alpha-project", kind="run_cycle", run_id=run.run_id
    ).item
    assert run_cli(monkeypatch, "runtime", "cancel", run.run_id) == 0
    assert store.require_run(run.run_id).terminal_state is TerminalState.CANCELLED
    assert queue.get(work.work_id).status is WorkStatus.CANCELLED  # type: ignore[union-attr]


# ------------------------------------------------------------------ doctor --
def test_doctor_reports_the_unimplemented_actions(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gap between "the policy knows this" and "this build can do it"."""

    assert run_cli(monkeypatch, "runtime", "doctor") == 0
    out = capsys.readouterr().out
    assert "schema" in out
    assert "have a policy but no handler" in out


def test_doctor_redacts_the_database_password(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        DSN_ENV, "postgresql://someone:hunter2@localhost:5432/research_os"
    )
    run_cli(monkeypatch, "runtime", "doctor")
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "someone:***@" in out


def test_doctor_without_a_database_warns_and_exits_zero(
    runtime_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An absent capability is the shape of a setup, not breakage."""

    monkeypatch.delenv(DSN_ENV, raising=False)
    assert run_cli(monkeypatch, "runtime", "doctor") == 0
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "dev-db start" in out


def test_dev_db_status_needs_no_database(
    runtime_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(monkeypatch, "runtime", "dev-db", "status") == 0
    assert "stopped" in capsys.readouterr().out


# ------------------------------------------------------------------ daemon --
def test_the_daemon_runs_one_pass_from_the_cli(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(monkeypatch, "runtime", "daemon", "--once") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["work_claimed"] == 0
    assert "leases_reclaimed" in report


def test_start_foreground_runs_a_cycle_inline(
    cli: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The path for someone who wants to watch one go.

    Uses the same provider-registry seam the automation CLI tests use, so a real
    router runs over a fake adapter -- which means this exercises routing,
    provenance and budget settlement rather than only the graph.
    """

    from research_os.automation import commands as auto_commands
    from research_os.runtime.checkpoints import ensure_tables
    from tests.fake_providers import FakeProvider, ScriptedResponse
    from tests.runtime_graph_helpers import plan_answer, review_answer

    ensure_tables(str(cli["db"].dsn))  # type: ignore[attr-defined]
    fake = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            "planner": [ScriptedResponse(structured=plan_answer("validate_capsule"))],
            "reviewer": [ScriptedResponse(structured=review_answer())],
        },
    )
    auto_commands.set_registry_factory(lambda: {"fake": fake})
    try:
        code = run_cli(
            monkeypatch,
            "runtime",
            "start",
            str(cli["repo"]),
            "--objective",
            "find out whether X",
            "--foreground",
        )
    finally:
        auto_commands.set_registry_factory(None)

    assert code == 0
    out = capsys.readouterr().out
    assert "SUCCEEDED" in out
    assert "DONE_FOR_NOW" in out

    store: RuntimeStore = cli["store"]  # type: ignore[assignment]
    run = store.list_runs()[0]
    assert run.status is RunStatus.SUCCEEDED
    # Provenance was recorded through the real router, not a stand-in.
    calls = store.list_model_calls(run_id=run.run_id)
    assert calls, "no model call was recorded"
    assert {call.provider for call in calls} == {"fake"}
    assert all(call.independence for call in calls), "independence was not persisted"
    # And the frontier digest, so continuation can tell progress from repetition.
    assert run.frontier_digest and len(run.frontier_digest) == 64, (
        f"notes were: {[n for n in store.list_events(run_id=run.run_id)]}; "
        f"detail={run.detail!r}"
    )

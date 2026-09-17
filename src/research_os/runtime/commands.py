"""``researchctl runtime``: the autonomous runtime behind a few verbs.

The interaction this is shaped around is one command and then nothing:

```bash
researchctl runtime start <project> --objective "..." --autonomy high
researchd                                    # in a terminal, or a systemd user unit
```

and then, occasionally:

```bash
researchctl runtime status
researchctl runtime approvals
researchctl runtime approve APRV-...
```

Everything else here is observability. There is deliberately no verb for "run
the next step", "check the cluster", or "resume after a crash" -- if a
researcher ever needs one of those, the control plane has failed at its job and
the fix belongs there rather than in a new command.

``start`` records a request and returns. It does not run the cycle itself, and
that is not laziness: a cycle started by a CLI process dies when the terminal
closes, and a cycle started by the control plane survives it. For the case where
someone genuinely wants to watch one go, ``--foreground`` runs a single cycle
inline and prints the result.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from decimal import Decimal
from pathlib import Path

from research_os.errors import EXIT_ERROR, EXIT_OK, ResearchOSError
from research_os.runtime import report as views
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.config import DSN_ENV, RuntimeConfig, load_config, redact_dsn
from research_os.runtime.db import Database
from research_os.runtime.migrations import current_version, migrate, pending
from research_os.runtime.models import Autonomy, BudgetScope, RunStatus
from research_os.runtime.store import RuntimeStateError, RuntimeStore


def add_runtime_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``runtime`` command group."""

    runtime = subparsers.add_parser(
        "runtime",
        help="Run and observe the autonomous research runtime.",
    )
    actions = runtime.add_subparsers(dest="runtime_command")

    start = actions.add_parser(
        "start", help="Record a research objective for the control plane to run."
    )
    start.add_argument(
        "project", metavar="PROJECT", help="Project id or repository path."
    )
    start.add_argument("--objective", required=True, help="What you want to find out.")
    start.add_argument(
        "--autonomy",
        choices=[str(level) for level in Autonomy],
        default=None,
        help="How much the runtime may do on its own. Default: from runtime.yaml.",
    )
    start.add_argument(
        "--foreground",
        action="store_true",
        help="Run one cycle inline instead of leaving it to researchd.",
    )
    start.add_argument(
        "--max-model-calls",
        type=int,
        default=None,
        help="Model calls one cycle of this objective may make.",
    )
    start.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help=(
            "Provider spend one CYCLE of this objective may authorise. An "
            "objective may run several cycles, so its exposure is this times "
            "the configured max_cycles_per_objective; `runtime run` prints "
            "both. The value is inherited by every successor cycle."
        ),
    )

    status = actions.add_parser("status", help="What is running, waiting, and failed.")
    status.add_argument("--project", default=None)
    status.add_argument("--json", action="store_true")

    runs = actions.add_parser("runs", help="List research cycles.")
    runs.add_argument("--project", default=None)
    runs.add_argument("--active", action="store_true", help="Only unfinished cycles.")
    runs.add_argument("--limit", type=int, default=25)
    runs.add_argument("--json", action="store_true")

    run_show = actions.add_parser("run", help="Show one cycle in full.")
    run_show.add_argument("run_id", metavar="RUN_ID")
    run_show.add_argument("--json", action="store_true")

    approvals = actions.add_parser("approvals", help="Decisions waiting for you.")
    approvals.add_argument("--all", action="store_true", help="Include decided ones.")
    approvals.add_argument("--json", action="store_true")

    approve = actions.add_parser("approve", help="Authorise a pending decision.")
    approve.add_argument("approval_id", metavar="APPROVAL_ID")
    approve.add_argument("--note", default="", help="Why, for the record.")
    approve.add_argument(
        "--i-am-a-person",
        dest="i_am_a_person",
        action="store_true",
        help=(
            "Answer without an interactive terminal. Deliberate, recorded, and "
            "not something an autonomous process should be passing."
        ),
    )

    decline = actions.add_parser("decline", help="Refuse a pending decision.")
    decline.add_argument("approval_id", metavar="APPROVAL_ID")
    decline.add_argument("--note", default="", help="Why, for the record.")
    decline.add_argument(
        "--i-am-a-person",
        dest="i_am_a_person",
        action="store_true",
        help=(
            "Answer without an interactive terminal. Deliberate, recorded, and "
            "not something an autonomous process should be passing."
        ),
    )

    jobs = actions.add_parser("jobs", help="External (cluster) jobs.")
    jobs.add_argument("--run", dest="run_id", default=None)
    jobs.add_argument("--json", action="store_true")

    costs = actions.add_parser("costs", help="What has been spent.")
    costs.add_argument("--run", dest="run_id", default=None)
    costs.add_argument("--json", action="store_true")

    budget = actions.add_parser(
        "budget",
        help=(
            "Show or set a project's standing cost ceiling. Without "
            "--max-cost-usd it only reports."
        ),
    )
    budget.add_argument("project", metavar="PROJECT", help="Project id or path.")
    budget.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help=(
            "The project's lifetime provider-spend ceiling. Raising it is how "
            "a project that has hit its ceiling is unblocked; lowering it "
            "below what has already been spent stops further work at once and "
            "is refused unless --force is given."
        ),
    )
    budget.add_argument(
        "--force",
        action="store_true",
        help="Allow setting a ceiling at or below what has already been spent.",
    )

    findings = actions.add_parser(
        "findings",
        help=(
            "Noncanonical runtime findings, and what each one rests on. "
            "Nothing here is accepted science."
        ),
    )
    findings.add_argument("--project", help="Only this project.")
    findings.add_argument("--run", dest="run", help="Only this cycle.")
    findings.add_argument("--limit", type=int, default=40)
    findings.add_argument("--json", action="store_true")

    events = actions.add_parser("events", help="The operational event log.")
    events.add_argument("--run", dest="run_id", default=None)
    events.add_argument("--project", default=None)
    events.add_argument("--limit", type=int, default=40)
    events.add_argument("--json", action="store_true")

    cancel = actions.add_parser("cancel", help="Stop a cycle and its queued work.")
    cancel.add_argument("run_id", metavar="RUN_ID")

    actions.add_parser("doctor", help="Report whether the runtime can run here.")
    actions.add_parser("migrate", help="Apply pending operational schema migrations.")

    daemon = actions.add_parser(
        "daemon", help="Run the control plane in the foreground (same as researchd)."
    )
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--max-ticks", type=int, default=None)

    devdb = actions.add_parser(
        "dev-db", help="Manage a disposable local PostgreSQL for development."
    )
    devdb.add_argument("action", choices=["start", "stop", "status"])

    runtime.set_defaults(runtime_parser=runtime)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "runtime_command", None)
    if command is None:
        args.runtime_parser.print_help()
        return EXIT_OK
    handlers = {
        "start": _start,
        "status": _status,
        "runs": _runs,
        "run": _run_show,
        "approvals": _approvals,
        "approve": _approve,
        "decline": _decline,
        "jobs": _jobs,
        "costs": _costs,
        "budget": _budget,
        "findings": _findings,
        "events": _events,
        "cancel": _cancel,
        "doctor": _doctor,
        "migrate": _migrate,
        "daemon": _daemon,
        "dev-db": _dev_db,
    }
    return handlers[command](args)


# ------------------------------------------------------------------ helpers --
def _emit(payload: object, rendered: str, *, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                payload, ensure_ascii=True, indent=2, sort_keys=True, default=str
            )
        )
    else:
        print(rendered, end="")
    return EXIT_OK


def _database(config: RuntimeConfig) -> Database:
    return Database(config.require_dsn())


def _resolve_project(value: str) -> tuple[str, Path]:
    """Resolve a project id or path to ``(project_id, repo_path)``.

    A path is read through the kernel adapter, which is the only thing that
    knows what a capsule is. An id is looked up in the operational projects
    table -- not the global registry, which the architecture calls disposable.
    """

    from research_os.runtime.kernel import ScientificKernelAdapter

    candidate = Path(value).expanduser()
    if candidate.exists():
        adapter = ScientificKernelAdapter(candidate)
        git_root, project = adapter.identity()
        return str(project.id), Path(git_root)
    config = load_config()
    with _database(config) as db:
        stored = RuntimeStore(db).get_project(value)
    if stored is None:
        raise ResearchOSError(
            f"{value!r} is neither a path nor a project this runtime knows. "
            f"Start it once with a path: `researchctl runtime start <path> --objective ...`"
        )
    return stored.project_id, Path(stored.repo_path)


# ------------------------------------------------------------------- verbs --
def _start(args: argparse.Namespace) -> int:
    from research_os.runtime.checkpoints import ensure_tables
    from research_os.runtime.cycles import apply_default_budgets

    config = load_config()
    if args.autonomy:
        config = RuntimeConfig(
            dsn=config.dsn,
            artifacts_root=config.artifacts_root,
            settings=config.settings,
            budget=config.budget,
            autonomy=str(args.autonomy),
            source=config.source,
        )
    project_id, repo_path = _resolve_project(args.project)

    with _database(config) as db:
        migrate(db)
        ensure_tables(config.require_dsn())
        store = RuntimeStore(db)
        store.upsert_project(project_id=project_id, repo_path=str(repo_path))
        run = store.create_run(
            project_id=project_id,
            objective=args.objective.strip(),
            autonomy=Autonomy(config.autonomy),
        )
        ledger = BudgetLedger(db)
        # The explicit caps are applied *before* the defaults, and the defaults
        # then refuse to raise a tighter limit. Applied the other way round,
        # `apply_default_budgets` would create the project ceiling from the
        # configuration's per-cycle cost rather than from the researcher's --
        # so `--max-cost-usd 6` would still buy a 300 USD objective ceiling.
        if args.max_model_calls is not None:
            ledger.set_limit(
                scope=BudgetScope.RUN,
                scope_id=run.run_id,
                dimension=Dimension.MODEL_CALLS,
                limit_value=args.max_model_calls,
            )
        if args.max_cost_usd is not None:
            ledger.set_limit(
                scope=BudgetScope.RUN,
                scope_id=run.run_id,
                dimension=Dimension.MODEL_COST_USD,
                limit_value=args.max_cost_usd,
            )
        # `project_id` was not passed here, so an objective started from the CLI
        # got no project ceiling at all until its *first successor* created one
        # -- which is to say, the ceiling that exists to bound an objective did
        # not exist for the first cycle of any objective.
        apply_default_budgets(
            ledger,
            config=config,
            run_id=run.run_id,
            project_id=project_id,
            inherit_from_run_id=run.run_id,
        )
        store.record_event(
            kind="RESEARCH_RUN_REQUESTED",
            project_id=project_id,
            run_id=run.run_id,
            payload={"objective": run.objective, "autonomy": str(run.autonomy)},
            dedup_key=f"requested:{run.run_id}",
        )

        if not args.foreground:
            print(f"{run.run_id} requested for {project_id} (autonomy {run.autonomy}).")
            print(
                "The control plane will pick it up. Start it with `researchd` if it is not running."
            )
            print(f"Watch it with: researchctl runtime run {run.run_id}")
            return EXIT_OK

        from research_os.runtime.cycles import resume_cycle
        from research_os.runtime.daemon import default_model_factory

        models = default_model_factory(config=config, db=db)
        result = resume_cycle(
            config=config,
            db=db,
            run_id=run.run_id,
            repo_path=repo_path,
            models=models(run.run_id, project_id, None),
        )
        print(f"{result.run.run_id}: {result.status} -> {result.terminal_state}")
        for note in result.notes:
            print(f"  {views._safe(note, limit=200)}")
        if result.pending_approval_id:
            print(f"\nA decision is required: {result.pending_approval_id}")
            print("  researchctl runtime approvals")
        return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        report = views.collect_status(db, project_id=args.project)
    return _emit(report.payload(), views.render_status(report), as_json=args.json)


def _runs(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        runs = RuntimeStore(db).list_runs(
            project_id=args.project, active_only=args.active, limit=args.limit
        )
    payload = [run.model_dump(mode="json") for run in runs]
    return _emit(payload, views.render_runs(runs), as_json=args.json)


def _run_show(args: argparse.Namespace) -> int:
    from research_os.runtime.queue import WorkQueue

    config = load_config()
    with _database(config) as db:
        store = RuntimeStore(db)
        run = store.require_run(args.run_id)
        work = WorkQueue(db).list_for_run(run.run_id)
        events = store.list_events(run_id=run.run_id, limit=40)
        approvals = store.list_approvals(run_id=run.run_id, pending_only=False)
        jobs = store.list_external_jobs(run_id=run.run_id)
        calls = store.list_model_calls(run_id=run.run_id)
        budgets = BudgetLedger(db).list_for(scope=BudgetScope.RUN, scope_id=run.run_id)
        findings = store.list_findings(run_id=run.run_id, limit=40)
        interpretations = store.list_interpretations(run_id=run.run_id, limit=40)
    payload = {
        "run": run.model_dump(mode="json"),
        "work": [item.model_dump(mode="json") for item in work],
        "events": [event.model_dump(mode="json") for event in events],
        "approvals": [approval.model_dump(mode="json") for approval in approvals],
        "jobs": [job.model_dump(mode="json") for job in jobs],
        "model_calls": [call.model_dump(mode="json") for call in calls],
        "budgets": [budget.model_dump(mode="json") for budget in budgets],
        "findings": [entry.model_dump(mode="json") for entry in findings],
        "interpretations": [entry.model_dump(mode="json") for entry in interpretations],
    }
    rendered = views.render_run_detail(
        run,
        work=work,
        events=events,
        approvals=approvals,
        jobs=jobs,
        calls=calls,
        budgets=budgets,
        findings=findings,
        interpretations=interpretations,
    )
    return _emit(payload, rendered, as_json=args.json)


def _approvals(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        approvals = RuntimeStore(db).list_approvals(pending_only=not args.all, limit=50)
    if args.json:
        return _emit([a.model_dump(mode="json") for a in approvals], "", as_json=True)
    if not approvals:
        print("No decisions are waiting for you.")
        return EXIT_OK
    for approval in approvals:
        print(views.render_approval(approval))
    return EXIT_OK


def _is_interactive() -> bool:
    """Whether a person is driving this invocation.

    The same seam ``researchctl review`` uses, for the same reason and with the
    same honest caveat: it is a usability and safety guard, not authentication.
    It makes unattended approval inconvenient and obvious.
    """

    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _actor() -> str:
    """Who is recording this decision, as accurately as this can be known.

    An earlier version wrote the literal string ``"researcher"`` for every
    decision. An independent review pointed out that this is a fabricated
    attribution: the field is provenance for a scientific-authority decision,
    and filling it with a guess makes the audit trail claim something it does
    not know.
    """

    import getpass

    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # pragma: no cover - no passwd entry
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def _decide(args: argparse.Namespace, *, granted: bool) -> int:
    # The same boundary AGENTS.md draws around `researchctl review`. Answering a
    # scientific-authority gate is a person's act; an autonomous process must
    # not be able to clear its own gate by running the CLI.
    if not _is_interactive() and not args.i_am_a_person:
        raise ResearchOSError(
            "Answering a scientific decision requires an interactive terminal. "
            "This gate exists so that an autonomous process cannot authorise "
            "its own work. If you are scripting a deliberate batch approval, "
            "pass --i-am-a-person and your shell history will say you did."
        )
    config = load_config()
    with _database(config) as db:
        store = RuntimeStore(db)
        # `record_decision` writes the decision and the resuming event in one
        # transaction. Emitting the event here, as an earlier version did, left
        # a window in which a Ctrl-C stalled the run in WAITING_HUMAN with no
        # way for the researcher to retry.
        approval = store.record_decision(
            args.approval_id,
            granted=granted,
            decision={"granted": granted, "note": args.note.strip()},
            decided_by=_actor(),
        )
    verb = "authorised" if granted else "declined"
    print(
        f"{approval.approval_id} {verb}. The control plane will resume {approval.run_id}."
    )
    return EXIT_OK


def _approve(args: argparse.Namespace) -> int:
    return _decide(args, granted=True)


def _decline(args: argparse.Namespace) -> int:
    return _decide(args, granted=False)


def _jobs(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        jobs = RuntimeStore(db).list_external_jobs(run_id=args.run_id)
    return _emit(
        [job.model_dump(mode="json") for job in jobs],
        views.render_jobs(jobs),
        as_json=args.json,
    )


def _costs(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        store = RuntimeStore(db)
        calls = store.list_model_calls(run_id=args.run_id, limit=2000)
        ledger = BudgetLedger(db)
        if args.run_id:
            budgets = ledger.list_for(scope=BudgetScope.RUN, scope_id=args.run_id)
        else:
            budgets = ledger.list_for(scope=BudgetScope.SYSTEM, scope_id="system")
    payload = {
        "model_calls": len(calls),
        "cost_usd": str(sum((call.cost_usd or 0) for call in calls)),
        "budgets": [budget.model_dump(mode="json") for budget in budgets],
    }
    return _emit(payload, views.render_costs(calls, budgets), as_json=args.json)


def _findings(args: argparse.Namespace) -> int:
    """What the runtime observed, and what each observation rests on.

    A separate view rather than a section of ``status`` because the question it
    answers is a tracing question -- "this proposal cites FIND-x; what is
    FIND-x" -- and that is asked from a proposal, not from a dashboard.
    """

    config = load_config()
    with _database(config) as db:
        store = RuntimeStore(db)
        found = store.list_findings(
            project_id=args.project, run_id=args.run, limit=args.limit
        )
        # Loaded one by one so the reference edges come with them. `list_findings`
        # deliberately does not join them -- it is used on paths where the edges
        # are not needed and a join per row would be a query per row.
        detailed = tuple(
            entry
            for entry in (store.get_finding(item.finding_id) for item in found)
            if entry is not None
        )
    payload = [entry.model_dump(mode="json") for entry in detailed]
    return _emit(payload, views.render_findings(detailed), as_json=args.json)


def _events(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        events = RuntimeStore(db).list_events(
            run_id=args.run_id, project_id=args.project, limit=args.limit
        )
    return _emit(
        [event.model_dump(mode="json") for event in events],
        views.render_events(events),
        as_json=args.json,
    )


def _cancel(args: argparse.Namespace) -> int:
    from research_os.runtime.models import TerminalState
    from research_os.runtime.queue import WorkQueue

    config = load_config()
    with _database(config) as db:
        store = RuntimeStore(db)
        cancelled_work = WorkQueue(db).cancel_run_work(args.run_id)
        try:
            run = store.set_run_status(
                args.run_id,
                RunStatus.CANCELLED,
                terminal_state=TerminalState.CANCELLED,
                detail="cancelled by the researcher",
            )
        except RuntimeStateError:
            # Already finished. The queued work was still worth cancelling and
            # was cancelled, so report that rather than erroring after the fact.
            existing = store.get_run(args.run_id)
            if existing is None:
                raise
            print(
                f"{args.run_id} was already {existing.status}; "
                f"{cancelled_work} queued or leased item(s) stopped."
            )
            return EXIT_OK
    print(f"{run.run_id} cancelled; {cancelled_work} queued or leased item(s) stopped.")
    return EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    """Report whether the runtime can run here, and exit non-zero only if it cannot.

    Follows ``researchctl doctor``'s contract: an absent capability is a WARN
    and exits zero, because "no cluster configured" is the shape of a setup
    rather than breakage.
    """

    from research_os.runtime import devdb
    from research_os.runtime.registry import unimplemented_actions

    lines: list[str] = ["runtime\n"]
    ok = True

    try:
        config = load_config()
    except ResearchOSError as exc:
        print(f"FAIL  configuration  {exc}")
        return EXIT_ERROR
    lines.append(f"  OK    config        {config.source or '(defaults)'}\n")
    lines.append(f"  OK    artifacts     {config.artifacts_root}\n")

    for module, label in (("psycopg", "psycopg"), ("langgraph", "langgraph")):
        try:
            __import__(module)
        except ModuleNotFoundError:
            ok = False
            lines.append(
                f"  FAIL  {label:13} not installed; run `uv sync --extra runtime`\n"
            )
        else:
            lines.append(f"  OK    {label:13} importable\n")

    if not config.dsn:
        _running, detail = devdb.status()
        lines.append(
            f"  WARN  database      not configured. Set {DSN_ENV}, or run "
            f"`researchctl runtime dev-db start`. Local cluster: {detail}\n"
        )
    else:
        lines.append(f"  OK    database      {redact_dsn(config.dsn)}\n")
        try:
            with _database(config) as db:
                version = db.ping()
                outstanding = pending(db)
                applied = current_version(db)
            lines.append(f"  OK    server        {version.split(' on ')[0]}\n")
            if outstanding:
                lines.append(
                    f"  WARN  schema        {len(outstanding)} migration(s) pending "
                    f"(at {applied or 'none'}); run `researchctl runtime migrate`\n"
                )
            else:
                lines.append(f"  OK    schema        at {applied or 'none'}\n")
        except ResearchOSError as exc:
            ok = False
            lines.append(f"  FAIL  server        {exc}\n")

    gaps = unimplemented_actions()
    if gaps:
        lines.append(
            f"  WARN  actions       {len(gaps)} action(s) have a policy but no handler "
            f"in this build: {', '.join(str(a) for a in gaps[:6])}"
            + ("...\n" if len(gaps) > 6 else "\n")
        )
    else:
        lines.append("  OK    actions       every policy action has a handler\n")

    lines.extend(_role_lines())
    lines.extend(_sandbox_lines())
    lines.extend(_coding_profile_lines())

    print("".join(lines), end="")
    return EXIT_OK if ok else EXIT_ERROR


def _role_lines() -> list[str]:
    """Which model actually answers each role, and whether it is the configured one.

    Reported because the run report records the model the *provider* named, not
    the alias that was asked for -- so a substitution cannot be spotted from the
    ledger alone. A researcher who configured a planner model on a provider this
    machine does not have should learn that here rather than by comparing a
    config file with a provenance row.
    """

    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config as load_automation_config
    from research_os.automation.config import resolve_roles
    from research_os.automation.providers import probe_registry

    try:
        registry = provider_registry()
        resolved = resolve_roles(load_automation_config(), probe_registry(registry))
    except ResearchOSError as exc:
        return [
            (
                f"  WARN  roles         cannot resolve configured model roles: "
                f"{exc}. Every call would use its provider's default model.\n"
            )
        ]
    lines = [
        (
            "  OK    tiers         provider tiers are CONFIGURED priority, not "
            "measured performance: every available provider is assumed tier 3 "
            "unless you set one. A report saying a call was answered at tier 3 "
            "means the configuration permitted it, not that anyone benchmarked "
            "it.\n"
        ),
        "  OK    roles         "
        + ", ".join(
            f"{name}={setting.provider}/{setting.model or 'provider default'}"
            for name, setting in sorted(resolved.roles.items())
        )
        + "\n",
    ]
    for note in resolved.substitutions:
        lines.append(f"  WARN  roles         {note}\n")
    if resolved.degraded:
        lines.append(
            f"  WARN  independence  {resolved.independence}: {resolved.note}\n"
        )
    else:
        lines.append(f"  OK    independence  {resolved.independence}\n")
    return lines


def _sandbox_lines() -> list[str]:
    """Whether model-written code can actually be contained on this host.

    A ``WARN`` rather than a ``FAIL``, following ``researchctl doctor``'s
    contract that an absent capability exits zero -- but the message says
    exactly what it means for this deployment, because "no sandbox" and "no
    cluster" are not equally consequential and a line that read the same for
    both would be misleading.
    """

    from research_os.automation.config import load_config as load_automation_config
    from research_os.sandbox import SandboxMode, available_backend, probe

    backend = available_backend()
    try:
        configured = load_automation_config().sandbox.mode
    except ResearchOSError:
        configured = SandboxMode.PREFERRED

    if backend is not None:
        return [
            (
                f"  OK    sandbox       {backend.technology}: {backend.detail}; "
                f"configured mode {configured}\n"
            )
        ]
    lines = [
        (
            "  WARN  sandbox       no containment technology works here, so "
            "high-autonomy execution of model-written code will be REFUSED "
            f"(configured mode {configured}). The canonical-state fingerprint "
            "still detects a capsule or Git-ref change afterwards; it prevents "
            "nothing and sees nothing outside the repository.\n"
        )
    ]
    for candidate in probe():
        detail = candidate.detail
        if candidate.remedy:
            detail += f" -- {candidate.remedy}"
        lines.append(f"  WARN  sandbox       {candidate.technology}: {detail}\n")
    return lines


def _coding_profile_lines() -> list[str]:
    """Report which commands a coding cycle would actually gate on.

    This row used to be a divergence warning. v1.1 moved validation-check
    resolution into the controller, ``researchctl research run`` honoured
    ``projects.<id>.check_profiles`` from ``automation.yaml``, and the runtime's
    coding action -- which dispatches through ``AutomationController`` with no
    plan -- ran whatever the automation planner had written instead. Same
    project, same goal, two gates.

    Closed in ``AutomationController._accept_plan``, which is the one place a
    plan becomes work orders however it arrived. This row names the resolved
    argv per project, because the argv a researcher can compare against their
    own configuration is worth more than the word "unified".

    An earlier version of this row said "every path resolves the same set",
    which an adversarial review pointed out was false at the time: the subset
    exemption applied to planner-authored plans too, so a planner emitting one
    of two declared commands skipped the other and no event recorded it. The
    exemption is now restricted to controller-supplied plans, and the wording
    says which is which rather than claiming they are identical.
    """

    from research_os.automation.config import load_config as load_automation_config

    try:
        config = load_automation_config()
    except ResearchOSError:
        return []
    configured = sorted(config.projects)
    if not configured:
        return [
            (
                "  OK    checks        no project declares check_profiles, so "
                "discovery decides everywhere\n"
            )
        ]
    lines = [
        (
            f"  OK    checks        {len(configured)} project(s) declare "
            f"check_profiles. A planner-authored plan is gated on the declared "
            f"set; a plan supplied by a controller keeps the subset it already "
            f"resolved from the same profiles\n"
        )
    ]
    for project_id in configured[:4]:
        names = sorted(config.for_project(project_id).check_profiles)
        lines.append(
            f"        {project_id}: {', '.join(names) if names else '(none)'}\n"
        )
    if len(configured) > 4:
        lines.append(f"        ... and {len(configured) - 4} more\n")
    return lines


def _budget(args: argparse.Namespace) -> int:
    """Show or set the project's standing cost ceiling.

    This command exists because the ceiling did not have one. It is created
    once per project by ``apply_default_budgets``, ``spent`` accumulates over
    the project's whole lifetime, and until now nothing could change it -- so a
    project that reached it was permanently unable to do autonomous work, with
    no recourse. An adversarial review found that, and found that an earlier
    version of this release made it reachable by typing a small
    ``--max-cost-usd`` on the first objective.
    """

    from research_os.runtime.models import BudgetScope

    config = load_config()
    with _database(config) as db:
        migrate(db)
        project_id, _repo = _resolve_project(args.project)
        ledger = BudgetLedger(db)
        current = ledger.get(
            scope=BudgetScope.PROJECT,
            scope_id=project_id,
            dimension=Dimension.MODEL_COST_USD,
        )
        if args.max_cost_usd is not None:
            wanted = Decimal(str(args.max_cost_usd))
            spent = Decimal(current.spent) if current else Decimal(0)
            if wanted <= spent and not args.force:
                print(
                    f"refusing: {project_id} has already spent {spent} USD, so a "
                    f"ceiling of {wanted} would stop all further work "
                    f"immediately. Use --force if that is what you want."
                )
                return EXIT_ERROR
            current = ledger.set_limit(
                scope=BudgetScope.PROJECT,
                scope_id=project_id,
                dimension=Dimension.MODEL_COST_USD,
                limit_value=wanted,
            )
        if current is None:
            print(
                f"{project_id} has no standing cost ceiling yet; one is created "
                f"when its first objective starts."
            )
            return EXIT_OK
        available = (
            Decimal(current.limit_value)
            - Decimal(current.reserved)
            - Decimal(current.spent)
        )
        print(f"project        {project_id}")
        print(f"ceiling        {current.limit_value} USD")
        print(f"spent          {current.spent} USD")
        print(f"reserved       {current.reserved} USD")
        print(f"available      {available} USD")
        if available <= 0:
            print()
            print(
                "This project is at its ceiling, so every autonomous cycle will "
                "end BUDGET_EXHAUSTED. Raise it with:"
            )
            print(
                f"     researchctl runtime budget {project_id} "
                f"--max-cost-usd {Decimal(current.limit_value) * 2}"
            )
    return EXIT_OK


def _migrate(args: argparse.Namespace) -> int:
    config = load_config()
    with _database(config) as db:
        applied = migrate(db)
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("the operational schema is up to date")
    return EXIT_OK


def _daemon(args: argparse.Namespace) -> int:
    from research_os.runtime.daemon import build_daemon

    config = load_config()
    daemon, database = build_daemon(config)
    try:
        if args.once:
            print(json.dumps(daemon.tick().payload(), indent=2, sort_keys=True))
        else:
            total = daemon.run_forever(max_ticks=args.max_ticks)
            print(json.dumps(total.payload(), indent=2, sort_keys=True))
    finally:
        database.close()
    return EXIT_OK


def _dev_db(args: argparse.Namespace) -> int:
    from research_os.runtime import devdb

    if args.action == "status":
        running, detail = devdb.status()
        print(f"{'running' if running else 'stopped'}: {detail}")
        return EXIT_OK
    if args.action == "stop":
        print(devdb.stop())
        return EXIT_OK
    dsn = devdb.start()
    print("A disposable local PostgreSQL is running.")
    print(f"  {redact_dsn(dsn)}")
    print("\nPoint the runtime at it for this shell:")
    print(f"  export {DSN_ENV}='{dsn}'")
    print(
        "\nIt is for development and tests. Production should use a PostgreSQL with "
        "a backup.",
        file=sys.stderr,
    )
    return EXIT_OK

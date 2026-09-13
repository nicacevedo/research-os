"""``researchctl research`` commands: the whole architecture behind a few verbs.

This is the surface a researcher actually uses. ``start`` plans, ``run``
executes, ``answer`` unblocks a checkpoint, and ``status``/``report`` say what
happened. Everything else in Research OS is reachable from here, and nothing here
does any orchestration of its own -- it parses arguments, calls the controller,
and prints.

Two flags are worth reading before using them.

``--execute-experiments`` is the authority to spend real compute. Without it an
experiment task still plans and still resolves its exact command, then stops and
prints what it would have run. Off by default because the failure mode of the
other default is a cluster bill.

``--yes`` on ``answer`` is absent on purpose: answering a checkpoint is a
person's decision and there is nothing to confirm, but *declining* one ends the
run, so ``--stop`` says that explicitly rather than being inferred from a blank
answer.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from research_os.errors import EXIT_OK
from research_os.research.models import ResearchBudget, ResearchState
from research_os.research.report import render_run, render_run_list, render_status
from research_os.research.store import ResearchStore


def add_research_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``research`` command group."""

    research = subparsers.add_parser(
        "research",
        help="Plan and run a complete research goal end to end.",
    )
    actions = research.add_subparsers(dest="research_command")

    start = actions.add_parser(
        "start",
        help="Plan a research run for a goal. Executes nothing.",
    )
    start.add_argument("project", metavar="PROJECT", help="Project id or path.")
    start.add_argument("--goal", required=True, help="What you want to find out.")
    start.add_argument(
        "--run",
        action="store_true",
        help="Execute the plan immediately instead of stopping after planning.",
    )
    _add_budget_arguments(start)
    start.add_argument("--config", help="Path to an automation config file.")

    run = actions.add_parser("run", help="Execute a planned or resumed research run.")
    run.add_argument("run_id", metavar="RUN_ID")
    run.add_argument("--config", help="Path to an automation config file.")

    answer = actions.add_parser(
        "answer", help="Answer the human checkpoint a run stopped at."
    )
    answer.add_argument("run_id", metavar="RUN_ID")
    answer.add_argument("--answer", required=True, help="Your answer, in your words.")
    answer.add_argument(
        "--stop",
        action="store_true",
        help="Decline to continue. The run ends here rather than going on.",
    )

    resume = actions.add_parser(
        "resume",
        help="Recover a run whose process stopped while it was working.",
    )
    resume.add_argument("run_id", metavar="RUN_ID")
    resume.add_argument(
        "--retry",
        action="store_true",
        help=(
            "Re-run the task that was in flight instead of marking it failed. "
            "It may already have spent something."
        ),
    )
    resume.add_argument(
        "--force",
        action="store_true",
        help=(
            "Allow --retry on an interrupted experiment. Check "
            "'researchctl experiment runs' first: the previous attempt may "
            "already have run or been submitted."
        ),
    )

    status = actions.add_parser("status", help="Show where one run is.")
    status.add_argument("run_id", metavar="RUN_ID")

    report = actions.add_parser("report", help="Show one run in full.")
    report.add_argument("run_id", metavar="RUN_ID")
    report.add_argument(
        "--events", action="store_true", help="Include the event ledger."
    )

    actions.add_parser("list", help="List research runs, newest last.")

    cancel = actions.add_parser("cancel", help="Stop a run that has not finished.")
    cancel.add_argument("run_id", metavar="RUN_ID")
    cancel.add_argument("--reason", default="cancelled by the researcher")

    cleanup = actions.add_parser(
        "cleanup",
        help="Remove the worktrees this run's delegated automation runs left behind.",
    )
    cleanup.add_argument("run_id", metavar="RUN_ID")

    events = actions.add_parser("events", help="Print one run's event ledger.")
    events.add_argument("run_id", metavar="RUN_ID")

    research.set_defaults(research_parser=research)


def _add_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-tasks", type=int, default=None, help="Most tasks the plan may have."
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=None,
        help="Most model calls this run may spend in total.",
    )
    parser.add_argument(
        "--max-write-tasks",
        type=int,
        default=None,
        help="Most tasks that may write to the repository.",
    )
    parser.add_argument(
        "--max-experiments",
        type=int,
        default=None,
        help="Most experiment tasks this run may run.",
    )
    parser.add_argument(
        "--execute-experiments",
        action="store_true",
        help=(
            "Authorise this run to actually execute declared experiments. "
            "Without it, an experiment resolves its command and stops."
        ),
    )


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "research_command", None)
    if command is None:
        args.research_parser.print_help()
        return EXIT_OK
    handlers = {
        "start": _start,
        "run": _run,
        "resume": _resume,
        "answer": _answer,
        "status": _status,
        "report": _report,
        "list": _list,
        "cancel": _cancel,
        "cleanup": _cleanup,
        "events": _events,
    }
    return handlers[command](args)


def _controller(args: argparse.Namespace):
    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config
    from research_os.experiment.config import load_config as load_experiment_config
    from research_os.literature.config import load_config as load_literature_config
    from research_os.research.controller import ResearchController

    return ResearchController(
        providers=provider_registry(),
        config=load_config(
            Path(args.config) if getattr(args, "config", None) else None
        ),
        literature_config=load_literature_config(),
        experiment_config=load_experiment_config(),
    )


def _recording_controller():
    """A controller for the verbs that only record a decision.

    ``answer`` and ``cancel`` write to the run record and invoke nothing, so
    they are given no providers at all. A command that cannot reach a model
    cannot spend one by accident.
    """

    from research_os.automation.config import load_config
    from research_os.research.controller import ResearchController

    return ResearchController(providers={}, config=load_config(None))


def _budget(args: argparse.Namespace) -> ResearchBudget:
    supplied = {
        "max_tasks": args.max_tasks,
        "max_model_calls": args.max_model_calls,
        "max_write_tasks": args.max_write_tasks,
        "max_experiments": args.max_experiments,
    }
    return ResearchBudget(
        **{key: value for key, value in supplied.items() if value is not None}
    )


def _start(args: argparse.Namespace) -> int:
    from research_os.automation.commands import resolve_project

    controller = _controller(args)
    store, run = controller.start(
        project_path=resolve_project(args.project),
        goal=args.goal,
        budget=_budget(args),
        execute_experiments=bool(args.execute_experiments),
    )
    if args.run:
        run = controller.execute(store)
    print(render_run(run), end="")
    print(f"run directory   {store.directory}")
    if not args.run and run.state is ResearchState.PLAN_READY:
        print(f"\nRun it with:\n     researchctl research run {run.run_id}")
    return EXIT_OK


def _run(args: argparse.Namespace) -> int:
    store = ResearchStore.open(args.run_id)
    run = _controller(args).execute(store)
    print(render_run(run), end="")
    print(f"run directory   {store.directory}")
    return EXIT_OK


def _resume(args: argparse.Namespace) -> int:
    store = ResearchStore.open(args.run_id)
    run = _recording_controller().resume(
        store, retry=bool(args.retry), force=bool(args.force)
    )
    print(render_run(run), end="")
    if run.state is ResearchState.INTERRUPTED:
        print(f"\nContinue it with:\n     researchctl research run {run.run_id}")
    return EXIT_OK


def _answer(args: argparse.Namespace) -> int:
    store = ResearchStore.open(args.run_id)
    run = _recording_controller().answer(
        store, answer=args.answer, proceed=not args.stop
    )
    print(render_status(run))
    if args.stop:
        print("\nThe run was stopped at your request. Nothing further will run.")
    else:
        print(f"\nContinue it with:\n     researchctl research run {run.run_id}")
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    print(render_status(ResearchStore.open(args.run_id).load()))
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    store = ResearchStore.open(args.run_id)
    events = list(store.iter_events()) if args.events else None
    print(render_run(store.load(), events=events), end="")
    print(f"run directory   {store.directory}")
    return EXIT_OK


def _list(_args: argparse.Namespace) -> int:
    runs = [ResearchStore.open(item).load() for item in ResearchStore.list_run_ids()]
    print(render_run_list(runs), end="")
    return EXIT_OK


def _cancel(args: argparse.Namespace) -> int:
    store = ResearchStore.open(args.run_id)
    run = _recording_controller().cancel(store, reason=args.reason)
    print(render_status(run))
    return EXIT_OK


def _cleanup(args: argparse.Namespace) -> int:
    """Release the worktrees this run's delegated automation runs are holding.

    Delegated rather than reimplemented: the automation run store knows which
    worktrees it created and refuses to remove anything else. This walks the
    research run's own event ledger for the automation runs it started, so it can
    only ever reach checkouts this run is responsible for.
    """

    from research_os.automation.config import load_config
    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore
    from research_os.errors import AutomationError

    store = ResearchStore.open(args.run_id)
    controller = AutomationController(providers={}, config=load_config(None))
    removed: list[str] = []
    failures: list[str] = []
    for record in store.iter_events():
        if record.get("event") != "automation_run_started":
            continue
        inner_id = str(record.get("automation_run_id", ""))
        try:
            _, paths = controller.cleanup(RunStore.open(inner_id))
        except (AutomationError, OSError) as exc:
            failures.append(f"{inner_id}: {exc}")
            continue
        removed.extend(paths)
    if removed:
        print(f"Removed {len(removed)} runtime path(s):")
        for path in removed:
            print(f"  {path}")
    else:
        print("Nothing to remove: this run holds no worktrees.")
    for failure in failures:
        print(f"  could not clean up {failure}")
    print("\nBranches and run records are kept. Nothing scientific was touched.")
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    import json

    store = ResearchStore.open(args.run_id)
    for record in store.iter_events():
        print(json.dumps(record, ensure_ascii=True, sort_keys=True))
    return EXIT_OK

"""``researchctl experiment`` commands.

The one thing to notice about this group is the shape of ``run``: it does
nothing unless you say ``--execute``. That is not caution for its own sake. An
experiment spends real time and sometimes real cluster money, and a system where
asking a literature question and starting a cluster job look the same from the
outside is a system that will eventually start a cluster job by accident.

Without ``--execute`` the command resolves the argv, checks it against the
declared parameters, prints exactly what would run, and stops.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_os.errors import EXIT_ERROR, EXIT_OK, ExperimentError
from research_os.experiment.config import (
    EXAMPLE_CONFIG,
    ExperimentConfig,
    config_path,
    load_config,
)
from research_os.experiment.controller import ExperimentController
from research_os.experiment.report import (
    render_command_list,
    render_run,
    render_run_list,
    render_scheduler_probe,
)
from research_os.experiment.slurm import probe
from research_os.experiment.store import ExperimentStore


def add_experiment_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``experiment`` command group."""

    group = subparsers.add_parser(
        "experiment",
        help="Run declared experiments and ingest their results.",
    )
    actions = group.add_subparsers(dest="experiment_command")

    commands = actions.add_parser(
        "commands", help="List the experiment commands declared for a project."
    )
    commands.add_argument("project", metavar="PROJECT", help="Project id or path.")
    commands.add_argument("--config", help="Path to an experiment config file.")

    scheduler = actions.add_parser(
        "scheduler", help="Report whether a job could be submitted from this machine."
    )
    scheduler.add_argument("project", nargs="?", default=None, metavar="PROJECT")
    scheduler.add_argument("--config", help="Path to an experiment config file.")

    run = actions.add_parser(
        "run",
        help="Run one declared experiment. Does nothing without --execute.",
    )
    run.add_argument("project", metavar="PROJECT", help="Project id or path.")
    run.add_argument("task", metavar="TASK", help="A declared command name.")
    run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A value for a declared parameter. Repeatable.",
    )
    run.add_argument(
        "--worktree",
        help=(
            "Run in this directory instead of a fresh isolated worktree. "
            "Deliberate: it is the only way an experiment touches your "
            "checkout, and the run records that it was not isolated."
        ),
    )
    run.add_argument("--partition", help="Slurm partition. Must be in your allowlist.")
    run.add_argument(
        "--execute",
        action="store_true",
        help="Actually run it. Without this, nothing is executed.",
    )
    run.add_argument("--config", help="Path to an experiment config file.")

    show = actions.add_parser("show", help="Show one experiment run in full.")
    show.add_argument("run_id", metavar="RUN_ID")

    poll = actions.add_parser("poll", help="Ask the scheduler about a submitted job.")
    poll.add_argument("run_id", metavar="RUN_ID")
    poll.add_argument("--config", help="Path to an experiment config file.")

    cancel = actions.add_parser("cancel", help="Cancel a submitted job.")
    cancel.add_argument("run_id", metavar="RUN_ID")
    cancel.add_argument("--config", help="Path to an experiment config file.")

    actions.add_parser("runs", help="List experiment runs.")

    events = actions.add_parser("events", help="Print one run's event ledger.")
    events.add_argument("run_id", metavar="RUN_ID")

    actions.add_parser(
        "example-config", help="Print a commented example experiments.yaml."
    )

    group.set_defaults(experiment_parser=group)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "experiment_command", None)
    if command is None:
        args.experiment_parser.print_help()
        return EXIT_OK
    handlers = {
        "commands": _commands,
        "scheduler": _scheduler,
        "run": _run,
        "show": _show,
        "poll": _poll,
        "cancel": _cancel,
        "runs": _runs,
        "events": _events,
        "example-config": _example_config,
    }
    return handlers[command](args)


def _commands(args: argparse.Namespace) -> int:
    config = _config(args)
    project_id, _ = _project(args.project)
    print(render_command_list(config, project_id), end="")
    return EXIT_OK


def _scheduler(args: argparse.Namespace) -> int:
    config = _config(args)
    project_id = _project(args.project)[0] if args.project else None
    settings = config.slurm_for(project_id)
    found = probe(settings)
    print(render_scheduler_probe(found, config), end="")
    return EXIT_OK if found.available else EXIT_ERROR


def _run(args: argparse.Namespace) -> int:
    config = _config(args)
    project_id, project_path = _project(args.project)
    # None means an isolated worktree, which is the default and the only safe
    # one. --worktree is a deliberate override and is recorded as such.
    worktree = Path(args.worktree).expanduser() if args.worktree else None
    controller = ExperimentController(config=config)
    parameters = _parameters(args.param)

    if not args.execute:
        command = controller.preview(
            project_id=project_id,
            task_name=args.task,
            parameters=parameters,
            worktree=worktree,
        )
        where = str(worktree) if worktree else "a fresh isolated worktree"
        print()
        print(f"Would run, in {where}:")
        print(f"    {command.display}")
        print()
        print(f"executor        {command.executor}")
        print(f"timeout         {command.timeout_seconds}s")
        print(f"declared output {', '.join(command.outputs) or 'none'}")
        print(f"checks          {', '.join(command.checks) or 'none'}")
        print()
        print("Nothing was executed. Add --execute to run it.")
        return EXIT_OK

    store, run, packet = controller.run(
        project_path=project_path,
        task_name=args.task,
        worktree=worktree,
        parameters=parameters,
        project_id=project_id,
        execute=True,
        partition=args.partition,
    )
    print(render_run(run, packet), end="")
    print(f"run directory   {store.directory}")
    return EXIT_OK if run.state.value in {"completed", "submitted"} else EXIT_ERROR


def _show(args: argparse.Namespace) -> int:
    store = ExperimentStore.open(args.run_id)
    print(render_run(store.load(), store.load_packet()), end="")
    return EXIT_OK


def _poll(args: argparse.Namespace) -> int:
    store = ExperimentStore.open(args.run_id)
    controller = ExperimentController(config=_config(args))
    run = controller.poll(store)
    print(render_run(run, store.load_packet()), end="")
    return EXIT_OK


def _cancel(args: argparse.Namespace) -> int:
    store = ExperimentStore.open(args.run_id)
    controller = ExperimentController(config=_config(args))
    run = controller.cancel(store)
    print(f"{run.run_id} is now {run.state}")
    return EXIT_OK


def _runs(_args: argparse.Namespace) -> int:
    runs = [
        ExperimentStore.open(item).load() for item in ExperimentStore.list_run_ids()
    ]
    print(render_run_list(runs), end="")
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    store = ExperimentStore.open(args.run_id)
    for record in store.iter_events():
        print(json.dumps(record, ensure_ascii=True, sort_keys=True))
    return EXIT_OK


def _example_config(_args: argparse.Namespace) -> int:
    print(EXAMPLE_CONFIG, end="")
    print(f"\n# Write this to {config_path()} and edit it.")
    return EXIT_OK


def _config(args: argparse.Namespace) -> ExperimentConfig:
    raw = getattr(args, "config", None)
    return load_config(Path(raw) if raw else None)


def _project(value: str) -> tuple[str | None, Path]:
    """Resolve a project id or path to both, since commands need each."""

    from research_os.automation.commands import resolve_project
    from research_os.registry import list_projects

    path = resolve_project(value)
    for entry in list_projects():
        if Path(entry.path) == path.resolve():
            return entry.project_id, path
    # An unregistered project may still have a capsule, and its id is what
    # addresses its declared commands. A project with neither is not a failure:
    # it simply has no declared commands, and the caller says so.
    from research_os.capsule import load_project_identity
    from research_os.errors import ResearchOSError

    try:
        _, project = load_project_identity(path)
    except ResearchOSError:
        return None, path
    return project.id, path


def _parameters(entries: list[str]) -> dict[str, object]:
    """Parse ``NAME=VALUE`` pairs. The value is checked against its declared type."""

    parsed: dict[str, object] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator or not name.strip():
            raise ExperimentError(
                f"--param {entry!r} is not NAME=VALUE. Every experiment parameter "
                "is named and typed by the researcher who declared the command."
            )
        parsed[name.strip()] = value
    return parsed

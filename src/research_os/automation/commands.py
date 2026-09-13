"""``researchctl auto`` commands.

Every command here is a thin shell over the deterministic controller: resolve a
project, build a controller, call one method, render the result. The CLI holds no
orchestration logic of its own so that what the tests exercise is what a person
runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from research_os.automation.config import (
    AutomationConfig,
    load_config,
    resolve_roles,
)
from research_os.automation.controller import AutomationController
from research_os.automation.models import Budget
from research_os.automation.providers import (
    ProviderAdapter,
    default_registry,
    probe_registry,
)
from research_os.automation.report import (
    render_plan,
    render_providers,
    render_report,
    render_status,
)
from research_os.automation.store import RunStore
from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    AutomationError,
    ProviderUnavailableError,
)
from research_os.registry import list_projects
from research_os.textsafe import terminal_safe

RegistryFactory = Callable[[], dict[str, ProviderAdapter]]

_REGISTRY_FACTORY: list[RegistryFactory] = []


def add_auto_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``auto`` command group."""

    auto = subparsers.add_parser(
        "auto",
        help="Run the deterministic automation control plane.",
    )
    actions = auto.add_subparsers(dest="auto_command")

    start = actions.add_parser(
        "start",
        help="Preflight a project, build context, and plan bounded work.",
    )
    start.add_argument("project", metavar="PROJECT", help="Project id or path.")
    start.add_argument("--goal", required=True, help="What this run should achieve.")
    start.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only. Never invokes a write worker or creates a worktree.",
    )
    start.add_argument(
        "--skip-planner",
        action="store_true",
        help="Preflight and build context without any model invocation.",
    )
    start.add_argument("--config", help="Path to an automation config file.")
    start.add_argument(
        "--max-model-calls",
        type=int,
        help="Override the model-call budget for this run.",
    )
    start.add_argument(
        "--max-write-orders",
        type=int,
        help="Override how many write work orders this run may dispatch.",
    )

    execute = actions.add_parser(
        "run",
        help="Execute a PLAN_READY run through checks and independent review.",
    )
    execute.add_argument("run_id", metavar="RUN_ID")
    execute.add_argument("--config", help="Path to an automation config file.")

    status = actions.add_parser("status", help="Show one run's state.")
    status.add_argument("run_id", metavar="RUN_ID")
    status.add_argument("--json", action="store_true", help="Emit the raw run record.")

    report = actions.add_parser("report", help="Print one run's full human report.")
    report.add_argument("run_id", metavar="RUN_ID")

    events = actions.add_parser("events", help="Print one run's event ledger.")
    events.add_argument("run_id", metavar="RUN_ID")
    events.add_argument("--limit", type=int, default=0, help="Show only the last N.")

    actions.add_parser("runs", help="List automation runs.")

    cancel = actions.add_parser("cancel", help="Cancel a run that is not terminal.")
    cancel.add_argument("run_id", metavar="RUN_ID")
    cancel.add_argument("--reason", default="cancelled by the researcher")

    cleanup = actions.add_parser(
        "cleanup",
        help="Remove a run's isolated worktrees. Branches are kept.",
    )
    cleanup.add_argument("run_id", metavar="RUN_ID")

    providers = actions.add_parser(
        "providers",
        help="Report locally discovered providers and the role assignment.",
    )
    providers.add_argument("--config", help="Path to an automation config file.")

    auto.set_defaults(auto_parser=auto)


def dispatch(args: argparse.Namespace) -> int:
    """Run one ``auto`` subcommand."""

    command = getattr(args, "auto_command", None)
    if command is None:
        args.auto_parser.print_help()
        return EXIT_OK
    handlers = {
        "start": _start,
        "run": _run,
        "status": _status,
        "report": _report,
        "events": _events,
        "runs": _runs,
        "cancel": _cancel,
        "cleanup": _cleanup,
        "providers": _providers,
    }
    return handlers[command](args)


def _start(args: argparse.Namespace) -> int:
    controller = _controller(args)
    budget = _budget(controller.config, args)
    project = resolve_project(args.project)
    store, run = controller.start(
        project_path=project,
        goal=args.goal,
        dry_run=bool(args.dry_run),
        skip_planner=bool(args.skip_planner),
        budget=budget,
    )
    print(render_plan(run), end="")
    print(f"run directory  {store.directory}")
    if run.dry_run:
        print("\ndry run: no write worker was invoked and no worktree was created.")
    elif args.skip_planner:
        print("\nno plan was produced (--skip-planner).")
    else:
        print(f"\nExecute it with:\n    researchctl auto run {run.run_id}")
    return EXIT_OK


def _run(args: argparse.Namespace) -> int:
    controller = _controller(args)
    store = RunStore.open(args.run_id)
    try:
        run = controller.execute(store)
    except AutomationError as exc:
        print(render_report(store.load(), store), end="")
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    print(render_report(run, store), end="")
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    store = RunStore.open(args.run_id)
    run = store.load()
    if args.json:
        print(_machine_json(run.model_dump(mode="json"), indent=2))
    else:
        print(render_status(run, store), end="")
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    store = RunStore.open(args.run_id)
    print(render_report(store.load(), store), end="")
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    store = RunStore.open(args.run_id)
    records = list(store.iter_events())
    if args.limit and args.limit > 0:
        records = records[-args.limit :]
    for record in records:
        print(_machine_json(record))
    return EXIT_OK


def _runs(_args: argparse.Namespace) -> int:
    for run_id in RunStore.list_run_ids():
        store = RunStore.open(run_id)
        run = store.load()
        marker = "DRY " if run.dry_run else ""
        print(
            terminal_safe(
                f"{run.state:16} {marker}{run_id}  {run.project_path}  {run.goal}"
            )
        )
    return EXIT_OK


def _cancel(args: argparse.Namespace) -> int:
    controller = _controller(args)
    store = RunStore.open(args.run_id)
    run = controller.cancel(store, reason=args.reason)
    print(terminal_safe(f"{run.run_id} is now {run.state}: {run.failure_reason}"))
    return EXIT_OK


def _cleanup(args: argparse.Namespace) -> int:
    controller = _controller(args)
    store = RunStore.open(args.run_id)
    run, removed = controller.cleanup(store)
    if not removed:
        print(f"{run.run_id}: no live worktrees to remove")
        return EXIT_OK
    for path in removed:
        # A worktree sits under the worktrees root; a check environment sits
        # inside the run directory. Naming them apart keeps the line honest
        # about what was actually deleted.
        inside_run = Path(path).is_relative_to(store.directory)
        print(f"removed {'check environment' if inside_run else 'worktree'} {path}")
    print(
        "Branches were kept. The run record and its ledger remain at "
        f"{store.directory}."
    )
    return EXIT_OK


def _providers(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    registry = _registry()
    probes = probe_registry(registry)
    try:
        resolved = resolve_roles(config, probes)
    except ProviderUnavailableError:
        resolved = None
    print(render_providers(probes, config=config, resolved=resolved), end="")
    return EXIT_OK if resolved is not None else EXIT_ERROR


def _machine_json(payload: object, *, indent: int | None = None) -> str:
    """Return a JSON view that is both parseable and safe to print.

    These two commands emit the record itself, for a script or for a person
    piping it into one, so the answer here is not the display sanitizer used for
    the human renderers: rewriting a control character inside a JSON string
    would change the data a reader parses, and doing it after serialisation
    would produce a ``\\x`` that is not a legal JSON escape at all.

    ``ensure_ascii`` is the right tool instead. It escapes every non-ASCII
    character, including DEL and the C1 range a terminal in an 8-bit mode reads
    as control introducers, into ``\\uXXXX`` - so nothing in the output is a
    control character, and ``json.loads`` still returns the original strings.
    """

    return json.dumps(payload, ensure_ascii=True, indent=indent, sort_keys=True)


def resolve_project(value: str) -> Path:
    """Resolve a project id or filesystem path to a repository path.

    A registered project id wins over a same-named directory: the registry is
    how a researcher refers to their projects, and silently preferring a local
    directory of the same name would run against the wrong repository.
    """

    for entry in list_projects():
        if entry.project_id == value:
            return Path(entry.path)
    path = Path(value).expanduser()
    if not path.exists():
        raise AutomationError(
            f"{value!r} is neither a registered project id nor an existing path"
        )
    return path


def set_registry_factory(factory: RegistryFactory | None) -> None:
    """Install a provider registry factory. The seam tests use to inject fakes."""

    _REGISTRY_FACTORY.clear()
    if factory is not None:
        _REGISTRY_FACTORY.append(factory)


def provider_registry() -> dict[str, ProviderAdapter]:
    """Return the provider registry every command group should use.

    One accessor rather than each group calling ``default_registry`` itself, so
    the test seam above covers the whole CLI instead of the part that happened to
    be written first.
    """

    if _REGISTRY_FACTORY:
        return _REGISTRY_FACTORY[0]()
    return default_registry()


def _registry() -> dict[str, ProviderAdapter]:
    return provider_registry()


def _controller(args: argparse.Namespace) -> AutomationController:
    config = load_config(Path(args.config) if getattr(args, "config", None) else None)
    return AutomationController(providers=_registry(), config=config)


def _budget(config: AutomationConfig, args: argparse.Namespace) -> Budget:
    """Apply any CLI budget overrides, re-validating the result.

    Built by validation rather than ``model_copy`` so that an out-of-range
    override such as ``--max-model-calls 0`` is refused instead of silently
    disabling the guard it is meant to tighten.
    """

    updates: dict[str, int] = {}
    if getattr(args, "max_model_calls", None) is not None:
        updates["max_model_calls"] = args.max_model_calls
    if getattr(args, "max_write_orders", None) is not None:
        updates["max_write_work_orders"] = args.max_write_orders
    if not updates:
        return config.budget
    try:
        return Budget.model_validate({**config.budget.model_dump(), **updates})
    except ValidationError as exc:
        raise AutomationError(f"invalid budget override: {exc}") from exc

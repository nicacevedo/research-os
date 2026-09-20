"""How a layer above the runtime gets work run, without the runtime knowing it.

The direction that has to hold: the portfolio wraps the runtime, and the
runtime must not import the portfolio. ``tests/test_runtime_layering.py``
asserts it by parsing the package, and the reason is not tidiness --
``research_os/portfolio`` must be deletable, leaving a runtime that still
migrates and still runs an objective cycle for a researcher who does not want
autonomous discovery.

Every temptation runs the other way. The daemon needs a handler for
``portfolio_tick``; the obvious way to write one is an import. So instead the
daemon consults a registry, and whoever composes the process -- ``researchctl``
and ``research_os.service``, the two composition roots -- imports the layer
that fills it.

**What a handler is given** is a :class:`WorkContext` rather than the daemon,
because handing it the daemon would make every private method of the control
plane part of an extension contract nobody wrote down.

**What is not extensible.** The failure taxonomy, the retry policy, the lease,
the budget check and the idempotency ledger. An extension's handler runs inside
all of them exactly as a built-in handler does: it is claimed under a lease,
its exceptions are classified by the same table, and a kind whose handler is
missing is failed as ``POLICY_REFUSED`` rather than left claimable forever.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.db import Database
from research_os.runtime.interfaces import ModelProvider
from research_os.runtime.models import WorkItem
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from research_os.runtime.workkinds import EVENT_WORK, WORK_HANDLERS

LOG = logging.getLogger("research_os.runtime.extensions")


class ExtensionError(ResearchOSError):
    """Raised when a work extension is registered or resolved incorrectly."""


@dataclass(frozen=True, slots=True)
class WorkContext:
    """What an extension handler is given, and nothing more.

    ``models`` is a *factory* rather than a provider, because the router records
    provenance against a specific run and work item and a handler that opens
    several runs needs one per run.
    """

    config: RuntimeConfig
    db: Database
    store: RuntimeStore
    queue: WorkQueue
    item: WorkItem
    #: ``(run_id, project_id, work_id) -> ModelProvider``.
    models: Callable[[str, str, str | None], ModelProvider]
    #: Where this project's repository is, when the daemon could resolve one.
    repo_path: Path | None = None


WorkExtension = Callable[[WorkContext], dict[str, Any]]

#: Work kinds a layer above the runtime handles, by kind.
WORK_EXTENSIONS: dict[str, WorkExtension] = {}

#: Event kinds a layer above the runtime turns into work, event kind -> work
#: kind. Consulted after the runtime's own ``EVENT_WORK``, never instead of it,
#: so an extension cannot capture an event the runtime already routes.
EVENT_EXTENSIONS: dict[str, str] = {}


def register_work(
    kind: str, handler: WorkExtension, *, from_event: str | None = None
) -> None:
    """Register one work kind, and optionally the event that produces it.

    Refuses to replace an existing registration. Two layers claiming one kind
    is a composition error, and the version of this that silently took the
    last registration would make which handler runs depend on import order.
    """

    if kind in WORK_HANDLERS:
        raise ExtensionError(f"{kind!r} is a runtime work kind and cannot be replaced")
    existing = WORK_EXTENSIONS.get(kind)
    if existing is not None and existing is not handler:
        raise ExtensionError(f"{kind!r} is already registered by another extension")
    WORK_EXTENSIONS[kind] = handler
    if from_event is not None:
        if from_event in EVENT_WORK:
            raise ExtensionError(
                f"{from_event!r} is already routed by the runtime to "
                f"{EVENT_WORK[from_event]!r}"
            )
        EVENT_EXTENSIONS[from_event] = kind
    LOG.debug("registered work extension %s", kind)


def work_kind_for_event(event_kind: str) -> str | None:
    return EVENT_EXTENSIONS.get(event_kind)


def handler_for(kind: str) -> WorkExtension | None:
    return WORK_EXTENSIONS.get(kind)


def registered() -> tuple[str, ...]:
    """Every extension work kind in this process, for ``runtime doctor``."""

    return tuple(sorted(WORK_EXTENSIONS))

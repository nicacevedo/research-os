"""Telling a person that their attention is genuinely required.

Deliberately not a logging sink. This is called when a cycle has stopped for a
scientific decision or hit a fatal condition -- for one researcher and a handful
of projects, a few times per project rather than continuously. If this ever
fires often, the `A2` list has grown too long and the fix is there, not here.

Two implementations, and no third by default. :class:`LogNotifier` writes to the
log. :class:`FileNotifier` appends to a file under the state home, which is what
makes a notification survive the daemon restarting and is enough for a person
who is going to read it in the morning. Anything that reaches out to a network
service is a credential to manage and an outage to debug, and neither is
justified until a researcher asks for it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from research_os.paths import state_home
from research_os.textsafe import terminal_safe

LOG = logging.getLogger("research_os.runtime.notify")

INBOX_FILENAME = "notifications.jsonl"


def inbox_path() -> Path:
    return state_home() / INBOX_FILENAME


class LogNotifier:
    """Writes notifications to the log and nowhere else."""

    __slots__ = ()

    def notify(
        self,
        *,
        subject: str,
        body: str,
        run_id: str | None = None,
        urgent: bool = False,
    ) -> None:
        level = logging.WARNING if urgent else logging.INFO
        # Escaped, because this reaches a terminal. A notification body carries
        # cycle notes, which quote provider-reported error strings, and
        # `researchd` in the foreground is the documented way to run the control
        # plane -- so this is a display boundary even though it is a log call.
        # `runtime/commands.py` already escaped the same strings when printing
        # them; a final adversarial review found this path did not.
        LOG.log(
            level,
            "%s%s: %s",
            terminal_safe(subject),
            f" [{run_id}]" if run_id else "",
            terminal_safe(body),
        )


class FileNotifier:
    """Appends notifications to a JSONL file under the state home.

    Append-only and fsynced, so a notification written just before the machine
    lost power is still there. The file is runtime state: deleting it loses
    notices, not science.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or inbox_path()

    @property
    def path(self) -> Path:
        return self._path

    def notify(
        self,
        *,
        subject: str,
        body: str,
        run_id: str | None = None,
        urgent: bool = False,
    ) -> None:
        record = {
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "subject": subject,
            "body": body,
            "run_id": run_id,
            "urgent": urgent,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
        LogNotifier().notify(subject=subject, body=body, run_id=run_id, urgent=urgent)


class CollectingNotifier:
    """Keeps notifications in memory, for tests."""

    __slots__ = ("sent",)

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def notify(
        self,
        *,
        subject: str,
        body: str,
        run_id: str | None = None,
        urgent: bool = False,
    ) -> None:
        self.sent.append(
            {"subject": subject, "body": body, "run_id": run_id, "urgent": urgent}
        )


def read_inbox(
    path: Path | None = None, *, limit: int = 50
) -> tuple[dict[str, object], ...]:
    target = path or inbox_path()
    if not target.is_file():
        return ()
    lines = target.read_text(encoding="utf-8").splitlines()
    found: list[dict[str, object]] = []
    for line in lines[-limit:]:
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return tuple(found)

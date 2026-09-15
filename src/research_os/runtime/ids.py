"""Runtime identifiers.

Same shape as every other id in this system -- ``PREFIX-<UTC timestamp>-<8
hex>`` -- so a person reading a log can tell what kind of thing an id names and
roughly when it was made without looking anything up, and so ids sort
chronologically as text.

The prefixes are distinct from the kernel's (``Q-``, ``CLAIM-``, ``EVI-``...)
and from the v1 file-backed layers' (``RUN-``, ``RR-``, ``XRUN-``). That is not
tidiness: a runtime id and a scientific id must never be mistakable for one
another, because the entire authority model rests on them being different kinds
of thing.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

RUN_ID_RE = re.compile(r"^RRUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
WORK_ID_RE = re.compile(r"^WORK-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
EVENT_ID_RE = re.compile(r"^EVT-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
APPROVAL_ID_RE = re.compile(r"^APRV-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
INVOCATION_ID_RE = re.compile(r"^IVK-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
MODEL_CALL_ID_RE = re.compile(r"^MCALL-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
EXTERNAL_JOB_ID_RE = re.compile(r"^XJOB-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
BUDGET_ID_RE = re.compile(r"^BDGT-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
RESERVATION_ID_RE = re.compile(r"^RSV-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SCHEDULE_ID_RE = re.compile(r"^SCHED-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
INTERPRETATION_ID_RE = re.compile(r"^XINT-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
FINDING_ID_RE = re.compile(r"^FIND-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def new_id(prefix: str, *, moment: datetime | None = None) -> str:
    """Mint a fresh runtime id.

    Randomness rather than a counter, because ids are minted by several
    processes that share nothing but the database, and a counter would need a
    round trip to be unique. Eight hex digits inside one second is ample for a
    single-researcher deployment, and every table's primary key would catch a
    collision anyway.
    """

    return f"{prefix}-{_stamp(moment)}-{secrets.token_hex(4)}"


def new_run_id(*, moment: datetime | None = None) -> str:
    return new_id("RRUN", moment=moment)


def new_work_id(*, moment: datetime | None = None) -> str:
    return new_id("WORK", moment=moment)


def new_event_id(*, moment: datetime | None = None) -> str:
    return new_id("EVT", moment=moment)


def new_approval_id(*, moment: datetime | None = None) -> str:
    return new_id("APRV", moment=moment)


def new_invocation_id(*, moment: datetime | None = None) -> str:
    return new_id("IVK", moment=moment)


def new_model_call_id(*, moment: datetime | None = None) -> str:
    return new_id("MCALL", moment=moment)


def new_external_job_id(*, moment: datetime | None = None) -> str:
    return new_id("XJOB", moment=moment)


def new_budget_id(*, moment: datetime | None = None) -> str:
    return new_id("BDGT", moment=moment)


def new_reservation_id(*, moment: datetime | None = None) -> str:
    return new_id("RSV", moment=moment)


def new_schedule_id(*, moment: datetime | None = None) -> str:
    return new_id("SCHED", moment=moment)


def new_interpretation_id(*, moment: datetime | None = None) -> str:
    return new_id("XINT", moment=moment)


def new_finding_id(*, moment: datetime | None = None) -> str:
    return new_id("FIND", moment=moment)


def thread_id_for(run_id: str) -> str:
    """The LangGraph thread that belongs to one bounded cycle.

    Derived rather than stored-and-looked-up, and one per run rather than one
    per project: a thread is the unit of checkpoint retention, and a thread that
    lives as long as a project is a checkpoint table that grows forever.
    """

    return f"cycle:{run_id}"

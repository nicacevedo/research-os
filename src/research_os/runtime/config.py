"""Runtime configuration: one file, one env override per thing that varies.

The runtime needs to know four kinds of thing, and they have different
lifetimes, so they are kept apart.

**Where the operational database is.** A DSN, and nothing else. The runtime
never provisions PostgreSQL, never guesses a socket path and never writes a
password anywhere. Whoever runs ``researchd`` supplies a DSN, or asks
``researchd dev-db`` for a disposable local one. A DSN may embed a password, so
:func:`redact_dsn` exists and every log line and every rendered report goes
through it.

**Where large bytes go.** One content-addressed directory under the data home.
Artifacts outlive runs, so they are durable data, not runtime state.

**How patient the runtime is.** Lease length, poll interval, attempt ceilings,
checkpoint retention. These are the numbers a person tunes after watching it
run, so they live in a file rather than in code.

**What it is allowed to spend and to do.** Default budgets and the default
autonomy level for a new run. Both are also settable per run; the config only
supplies the default, and the *lower* of the two always wins, so editing this
file can never widen the authority of a run that is already going.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_os.errors import ResearchOSError
from research_os.paths import config_home, data_home, state_home

CONFIG_FILENAME = "runtime.yaml"

#: The DSN of the operational database. The one value with no sane default:
#: guessing would connect the runtime to whatever PostgreSQL happens to be
#: listening, which is the kind of help nobody wants.
DSN_ENV = "RESEARCH_OS_RUNTIME_DSN"

#: Set by ``researchd dev-db`` (and by the test fixture) to point at a
#: throwaway server. Separate from :data:`DSN_ENV` so a developer cannot leave a
#: test database configured and quietly run real work against it.
DEV_DSN_ENV = "RESEARCH_OS_RUNTIME_DEV_DSN"


class RuntimeConfigError(ResearchOSError):
    """Raised when runtime configuration is missing or unusable."""


def config_path() -> Path:
    return config_home() / CONFIG_FILENAME


def artifacts_root() -> Path:
    """Where artifact bytes live.

    Under the *data* home, not the state home: deleting runtime state must lose
    no evidence, and an artifact is evidence.
    """
    return data_home() / "artifacts"


def dev_db_root() -> Path:
    """Where ``researchd dev-db`` keeps its disposable PostgreSQL cluster."""
    return state_home() / "devdb"


class BudgetDefaults(BaseModel):
    """What one research cycle may spend before it must stop and say so."""

    model_config = ConfigDict(extra="forbid")

    max_model_calls: int = Field(default=60, ge=1, le=10_000)
    max_model_cost_usd: float = Field(default=25.0, ge=0.0)
    max_wall_clock_seconds: int = Field(default=86_400, ge=60)
    max_external_jobs: int = Field(default=8, ge=0, le=1_000)
    max_work_items: int = Field(default=400, ge=1, le=100_000)


class RuntimeSettings(BaseModel):
    """The tunable operational numbers."""

    model_config = ConfigDict(extra="forbid")

    #: How long a worker owns a claimed work item before the lease may be
    #: reclaimed. Must comfortably exceed the longest *checkpoint interval*, not
    #: the longest task: a worker renews while it works.
    lease_seconds: int = Field(default=120, ge=10, le=3_600)
    #: How often a working worker renews its lease.
    lease_renew_seconds: int = Field(default=30, ge=5, le=1_800)
    #: How long the daemon sleeps when it finds nothing due.
    poll_interval_seconds: float = Field(default=2.0, ge=0.05, le=300.0)
    #: How often external jobs are reconciled with the scheduler.
    external_poll_seconds: float = Field(default=60.0, ge=1.0, le=3_600.0)
    #: Default attempt ceiling for a work item that does not set its own.
    max_attempts: int = Field(default=3, ge=1, le=20)
    #: How many bounded cycles one objective may chain before it must stop and
    #: report DONE_FOR_NOW. The outer bound on autonomous continuation.
    max_cycles_per_objective: int = Field(default=12, ge=1, le=1_000)
    #: Checkpoints for threads of finished runs older than this are deleted.
    checkpoint_retention_days: int = Field(default=30, ge=1, le=3_650)
    #: A provider that fails this many times in a row is marked unhealthy.
    provider_failure_threshold: int = Field(default=3, ge=1, le=100)
    #: How long an unhealthy provider is left alone before being tried again.
    provider_cooldown_seconds: int = Field(default=300, ge=1, le=86_400)


class ConfigDocument(BaseModel):
    """The on-disk shape of ``runtime.yaml``. Every key optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    dsn: str | None = None
    artifacts_root: str | None = None
    settings: RuntimeSettings | None = None
    budget: BudgetDefaults | None = None
    autonomy: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Resolved runtime configuration.

    ``dsn`` may be empty. That is not an error here -- ``researchctl runtime
    doctor`` must be able to report "no database configured" rather than crash
    -- but every call that actually needs the database goes through
    :meth:`require_dsn`.
    """

    dsn: str
    artifacts_root: Path
    settings: RuntimeSettings
    budget: BudgetDefaults
    autonomy: str
    source: Path | None = None

    def require_dsn(self) -> str:
        if not self.dsn:
            raise RuntimeConfigError(
                "No operational database configured. Set "
                f"{DSN_ENV}, put `dsn:` in {config_path()}, or start a "
                "disposable local one with `researchctl runtime dev-db start`."
            )
        return self.dsn

    def with_dsn(self, dsn: str) -> RuntimeConfig:
        return replace(self, dsn=dsn)


def _resolve_dsn(document: ConfigDocument) -> str:
    for env in (DSN_ENV, DEV_DSN_ENV):
        raw = os.environ.get(env, "").strip()
        if raw:
            return raw
    return (document.dsn or "").strip()


def load_config(path: Path | None = None) -> RuntimeConfig:
    """Load runtime configuration, falling back to documented defaults.

    A missing default config file is normal and yields defaults. A config file
    that was *asked for* by path and is missing is an error, because the caller
    said where it was.
    """

    target = path or config_path()
    document = ConfigDocument()
    if target.exists():
        try:
            raw: Any = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeConfigError(f"{target}: not valid YAML: {exc}") from None
        if not isinstance(raw, dict):
            raise RuntimeConfigError(f"{target}: expected a mapping at the top level")
        try:
            document = ConfigDocument.model_validate(raw)
        except ValidationError as exc:
            raise RuntimeConfigError(f"{target}: {exc}") from None
    elif path is not None:
        raise RuntimeConfigError(f"{target}: no such runtime configuration file")

    root = (
        Path(document.artifacts_root).expanduser()
        if document.artifacts_root
        else artifacts_root()
    )
    autonomy = (document.autonomy or "high").strip().lower()
    if autonomy not in {"low", "medium", "high"}:
        raise RuntimeConfigError(
            f"{target}: autonomy must be one of low, medium, high (got {autonomy!r})"
        )
    return RuntimeConfig(
        dsn=_resolve_dsn(document),
        artifacts_root=root,
        settings=document.settings or RuntimeSettings(),
        budget=document.budget or BudgetDefaults(),
        autonomy=autonomy,
        source=target if target.exists() else None,
    )


def redact_dsn(dsn: str) -> str:
    """Return ``dsn`` with any password replaced, for logs and reports.

    Deliberately crude and deliberately fail-safe: anything between ``://`` and
    the last ``@`` of the authority is replaced wholesale. A DSN this does not
    recognise is returned with its authority removed entirely rather than
    printed hopefully.
    """

    if not dsn:
        return ""
    if "://" not in dsn:
        # keyword/value form: postgresql "host=... password=..."
        parts = []
        for token in dsn.split():
            key, sep, _ = token.partition("=")
            parts.append(
                f"{key}=***" if sep and key.strip().lower() == "password" else token
            )
        return " ".join(parts)
    scheme, _, rest = dsn.partition("://")
    authority, slash, tail = rest.partition("/")
    if "@" not in authority:
        return dsn
    userinfo, _, hostport = authority.rpartition("@")
    user, sep, _password = userinfo.partition(":")
    shown = f"{user}:***" if sep else user
    return f"{scheme}://{shown}@{hostport}{slash}{tail}"

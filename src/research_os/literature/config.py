"""Literature configuration: contact address, enabled sources, credentials by reference.

One small YAML file beside ``automation.yaml``, with the same rule: a secret is
never a value here. A provider that needs a credential names the environment
variable it is read from, and configuration says which variable, never what is
in it. That keeps a key out of the config file, out of a backup of the config
file, and out of any report that prints configuration.

Defaults are chosen so the subsystem works on a machine with no configuration at
all: every source that needs no credential is enabled, and the only thing a
researcher gains by writing the file is a contact address, which providers ask
for and Crossref rewards with its polite pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from research_os.errors import LiteratureError
from research_os.paths import config_home

CONFIG_FILENAME = "literature.yaml"

#: Where a contact address may come from when the config file does not say.
#:
#: Providers ask for one so they can get in touch about a misbehaving client;
#: Crossref grants its polite pool on the strength of it. It is an address, not
#: a secret, so an environment variable is a convenience rather than a control.
CONTACT_ENV = "RESEARCH_OS_CONTACT_EMAIL"

#: Every source Research OS has an adapter for.
KNOWN_SOURCES: tuple[str, ...] = ("openalex", "crossref", "arxiv")

#: How many provider results one retrieval asks for by default.
DEFAULT_SEARCH_LIMIT = 20

#: The ceiling on one retrieval, whatever is asked for.
#:
#: A bound on provider politeness and on how much untrusted text one command can
#: pull into the store in a single step.
MAX_SEARCH_LIMIT = 100


class LiteratureDocument(BaseModel):
    """The on-disk shape of ``literature.yaml``. Every key is optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    contact_email: str | None = None
    enabled_sources: list[str] | None = None
    offline: bool | None = None
    default_search_limit: int | None = None
    fetch_fulltext: bool | None = None


@dataclass(frozen=True, slots=True)
class LiteratureConfig:
    """Resolved literature settings and where they came from."""

    contact_email: str | None
    enabled_sources: tuple[str, ...]
    offline: bool
    default_search_limit: int
    fetch_fulltext: bool
    source: Path | None

    def enabled(self, name: str) -> bool:
        return name in self.enabled_sources

    def bounded_limit(self, requested: int | None = None) -> int:
        value = requested if requested is not None else self.default_search_limit
        return max(1, min(value, MAX_SEARCH_LIMIT))


def config_path() -> Path:
    return config_home() / CONFIG_FILENAME


def default_config() -> LiteratureConfig:
    return LiteratureConfig(
        contact_email=(os.environ.get(CONTACT_ENV) or "").strip() or None,
        enabled_sources=KNOWN_SOURCES,
        offline=False,
        default_search_limit=DEFAULT_SEARCH_LIMIT,
        fetch_fulltext=True,
        source=None,
    )


def load_config(path: Path | None = None) -> LiteratureConfig:
    """Load literature configuration, falling back to the shipped defaults."""

    target = path if path is not None else config_path()
    if not target.is_file():
        if path is not None:
            raise LiteratureError(f"no literature config at {target}")
        return default_config()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise LiteratureError(f"cannot read {target}: {exc}") from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise LiteratureError(f"invalid YAML in {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise LiteratureError(f"{target} must contain a mapping")
    try:
        document = LiteratureDocument.model_validate(data)
    except ValidationError as exc:
        raise LiteratureError(f"invalid literature config at {target}: {exc}") from exc

    sources = (
        tuple(document.enabled_sources)
        if document.enabled_sources is not None
        else KNOWN_SOURCES
    )
    unknown = [item for item in sources if item not in KNOWN_SOURCES]
    if unknown:
        raise LiteratureError(
            f"invalid literature config at {target}: enabled_sources names "
            f"{', '.join(sorted(unknown))}, which Research OS has no adapter for. "
            f"Known sources: {', '.join(KNOWN_SOURCES)}"
        )
    defaults = default_config()
    return LiteratureConfig(
        contact_email=(document.contact_email or "").strip() or defaults.contact_email,
        enabled_sources=sources,
        offline=(
            document.offline if document.offline is not None else defaults.offline
        ),
        default_search_limit=(
            document.default_search_limit
            if document.default_search_limit is not None
            else defaults.default_search_limit
        ),
        fetch_fulltext=(
            document.fetch_fulltext
            if document.fetch_fulltext is not None
            else defaults.fetch_fulltext
        ),
        source=target,
    )

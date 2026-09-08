"""Deterministic XDG path resolution for Research OS.

R0 has no configuration parser. Environment overrides relocate the four
directories; ``~/.config/research-os/config.toml`` is a reserved future path
only.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_CONFIG = "RESEARCH_OS_CONFIG_HOME"
_ENV_DATA = "RESEARCH_OS_DATA_HOME"
_ENV_CACHE = "RESEARCH_OS_CACHE_HOME"
_ENV_STATE = "RESEARCH_OS_STATE_HOME"

CONFIG_HOME_ENV = _ENV_CONFIG
DATA_HOME_ENV = _ENV_DATA
CACHE_HOME_ENV = _ENV_CACHE
STATE_HOME_ENV = _ENV_STATE


def _override_or_default(env_name: str, *default_parts: str) -> Path:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home().joinpath(*default_parts)


def config_home() -> Path:
    """Return the Research OS config directory."""
    return _override_or_default(_ENV_CONFIG, ".config", "research-os")


def data_home() -> Path:
    """Return the Research OS durable-data directory."""
    return _override_or_default(_ENV_DATA, ".local", "share", "research-os")


def cache_home() -> Path:
    """Return the Research OS cache directory."""
    return _override_or_default(_ENV_CACHE, ".cache", "research-os")


def state_home() -> Path:
    """Return the Research OS runtime-state directory."""
    return _override_or_default(_ENV_STATE, ".local", "state", "research-os")


def xdg_dirs() -> dict[str, Path]:
    """Return the four XDG directories keyed by doctor check name."""
    return {
        "config": config_home(),
        "data": data_home(),
        "cache": cache_home(),
        "state": state_home(),
    }


def xdg_dir_issue(path: Path) -> str | None:
    """Return a failure reason, or ``None`` if ``path`` is a writable directory.

    Does not create, chmod, or chown anything.
    """
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not a directory"
    if not os.access(path, os.W_OK):
        return "not writable"
    return None

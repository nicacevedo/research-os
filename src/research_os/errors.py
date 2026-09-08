"""Shared error and result primitives for Research OS."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class ResearchOSError(Exception):
    """Base exception for Research OS kernel failures."""

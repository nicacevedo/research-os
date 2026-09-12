"""Recovering structured output from a model response.

The providers used here validate output against a JSON Schema and return a
parsed object, so this is the fallback path only. It is deliberately
conservative: it locates the outermost JSON object and parses it, and never
repairs malformed JSON. A response that cannot be parsed is a rejected
invocation, not something to guess at.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """Return the outermost JSON object in ``text``, tolerating a code fence."""

    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if len(lines) > 2 else lines[1:]).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None

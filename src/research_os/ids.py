"""Scientific-object and project identifier grammar."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from research_os.errors import InvalidIdError

OBJECT_ID_PATTERN = r"^(Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|REV|EVI)-[0-9]{4,}$"
OBJECT_ID_RE = re.compile(OBJECT_ID_PATTERN)
PROJECT_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"
PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)

PREFIXES = frozenset(
    {"Q", "IDEA", "HYP", "ASM", "CLAIM", "DEC", "EXP", "REV", "EVI"}
)

PREFIX_TO_TYPE = {
    "Q": "question",
    "IDEA": "idea",
    "HYP": "hypothesis",
    "ASM": "assumption",
    "CLAIM": "claim",
    "DEC": "decision",
    "EXP": "experiment",
    "REV": "review",
    "EVI": "evidence",
}

TYPE_TO_PREFIX = {value: key for key, value in PREFIX_TO_TYPE.items()}


@dataclass(frozen=True, slots=True)
class ParsedId:
    """Immutable parsed scientific-object identifier."""

    value: str
    prefix: str
    digits: str
    number: int


def validate_id(value: str) -> str:
    """Return ``value`` if it is a valid scientific-object ID."""

    if not isinstance(value, str) or OBJECT_ID_RE.fullmatch(value) is None:
        raise InvalidIdError(f"invalid object id: {value!r}")
    return value


def parse_id(value: str) -> ParsedId:
    """Parse a scientific-object ID into prefix and numeric parts."""

    validated = validate_id(value)
    prefix, digits = validated.split("-", 1)
    return ParsedId(
        value=validated,
        prefix=prefix,
        digits=digits,
        number=int(digits),
    )


def object_type_from_id(value: str) -> str:
    """Return the canonical object type name encoded by ``value``."""

    return PREFIX_TO_TYPE[parse_id(value).prefix]


def validate_project_id(value: str) -> str:
    """Return ``value`` if it is a valid project slug."""

    if not isinstance(value, str) or PROJECT_ID_RE.fullmatch(value) is None:
        raise InvalidIdError(f"invalid project id: {value!r}")
    return value


def next_id(prefix: str, existing: Iterable[str]) -> str:
    """Return the next ID for ``prefix`` given an explicit ID collection.

    Never scans projects, filesystems, or SQLite. IDs below 1000 are
    zero-padded to four digits; larger values keep all digits.
    """

    if prefix not in PREFIXES:
        raise InvalidIdError(f"unknown object prefix: {prefix!r}")
    maximum = 0
    for raw in existing:
        parsed = parse_id(raw)
        if parsed.prefix == prefix:
            maximum = max(maximum, parsed.number)
    return f"{prefix}-{maximum + 1:04d}"

"""Tests for scientific-object and project identifier grammar."""

from __future__ import annotations

import pytest

from research_os.errors import InvalidIdError
from research_os.ids import (
    OBJECT_ID_PATTERN,
    PREFIXES,
    next_id,
    object_type_from_id,
    parse_id,
    validate_id,
    validate_project_id,
)

VALID_IDS = (
    "Q-0001",
    "IDEA-0001",
    "HYP-0042",
    "ASM-0001",
    "CLAIM-10000",
    "DEC-0001",
    "EXP-0001",
    "REV-0001",
    "EVI-0001",
)


@pytest.mark.parametrize("value", VALID_IDS)
def test_valid_prefixes_and_padding(value: str) -> None:
    assert validate_id(value) == value
    parsed = parse_id(value)
    assert parsed.value == value
    assert object_type_from_id(value)


def test_all_approved_prefixes_are_accepted() -> None:
    for prefix in PREFIXES:
        validate_id(f"{prefix}-0001")


@pytest.mark.parametrize(
    "value",
    ["q-0001", "Hyp-0001", "claim-0001", "EVI-0001a", "Q-0001-1", "Q_0001"],
)
def test_malformed_casing_and_shape_rejected(value: str) -> None:
    with pytest.raises(InvalidIdError):
        validate_id(value)


@pytest.mark.parametrize("value", ["Q-1", "Q-01", "Q-001", "HYP-42", "CLAIM-999"])
def test_fewer_than_four_digits_rejected(value: str) -> None:
    with pytest.raises(InvalidIdError):
        validate_id(value)


def test_more_than_four_digits_allowed() -> None:
    parsed = parse_id("CLAIM-10000")
    assert parsed.prefix == "CLAIM"
    assert parsed.number == 10000
    assert parsed.digits == "10000"


def test_object_type_from_id() -> None:
    assert object_type_from_id("Q-0001") == "question"
    assert object_type_from_id("IDEA-0001") == "idea"
    assert object_type_from_id("HYP-0001") == "hypothesis"
    assert object_type_from_id("ASM-0001") == "assumption"
    assert object_type_from_id("CLAIM-0001") == "claim"
    assert object_type_from_id("DEC-0001") == "decision"
    assert object_type_from_id("EXP-0001") == "experiment"
    assert object_type_from_id("REV-0001") == "review"
    assert object_type_from_id("EVI-0001") == "evidence"


def test_next_id_from_explicit_collection_only() -> None:
    assert next_id("Q", []) == "Q-0001"
    assert next_id("Q", ["Q-0001", "IDEA-0009"]) == "Q-0002"
    assert next_id("CLAIM", ["CLAIM-10000", "CLAIM-0002"]) == "CLAIM-10001"


def test_next_id_rejects_unknown_prefix() -> None:
    with pytest.raises(InvalidIdError):
        next_id("PAPER", [])


def test_project_slug_grammar() -> None:
    assert validate_project_id("ab") == "ab"
    assert validate_project_id("alpha-project-1") == "alpha-project-1"
    for value in ("A", "1abc", "-abc", "a", "a" * 64, "Alpha", "has_underscore"):
        with pytest.raises(InvalidIdError):
            validate_project_id(value)


def test_object_id_pattern_is_the_approved_grammar() -> None:
    assert OBJECT_ID_PATTERN == r"^(Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|REV|EVI)-[0-9]{4,}$"

"""The integrity invariants are machine-checked, not only written down.

``docs/architecture_invariants.yaml`` maps INV-01 to INV-10 to the code that
enforces each, the tests that demonstrate it, the mutants that must be killed
and the reproduced findings that broke it. This test fails when any of that
stops being true: an enforcement point renamed away, a test deleted, a mutant
whose snippet no longer applies, a finding mapped to nothing -- so the
mapping cannot quietly rot into prose.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integrity_mutations import MUTANTS

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "docs" / "architecture_invariants.yaml"
SPEC = ROOT / "docs" / "ARCHITECTURE_INVARIANTS.md"
INVARIANTS = tuple(f"INV-{n:02d}" for n in range(1, 11))
FINDINGS = ("H1", "H2", "H3", "H4", "H5", "H6", "M1", "M2", "M3", "M4")


def _load() -> dict[str, Any]:
    return yaml.safe_load(MAPPING.read_text(encoding="utf-8"))


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def _resolve(point: str) -> Any:
    module_name, _, qualname = point.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def test_every_invariant_is_mapped_once_and_completely() -> None:
    mapping = _load()
    ids = [item["id"] for item in mapping["invariants"]]
    assert tuple(ids) == INVARIANTS
    for item in mapping["invariants"]:
        for key in ("title", "threat", "enforced_by", "tests", "pass_requires"):
            assert item.get(key), f"{item['id']} has no {key}"


@pytest.mark.parametrize("invariant", INVARIANTS)
def test_every_enforcement_point_exists(invariant: str) -> None:
    (item,) = [row for row in _load()["invariants"] if row["id"] == invariant]
    for point in item["enforced_by"]:
        assert callable(_resolve(point)), point


@pytest.mark.parametrize("invariant", INVARIANTS)
def test_every_named_test_exists(invariant: str) -> None:
    (item,) = [row for row in _load()["invariants"] if row["id"] == invariant]
    for node in item["tests"]:
        path, _, name = node.partition("::")
        assert (ROOT / path).is_file(), node
        assert name in _test_names(ROOT / path), node


def test_every_mutant_is_mapped_defined_and_still_applies() -> None:
    defined = {mutant.id: mutant for mutant in MUTANTS}
    mapped = {
        mutant_id for item in _load()["invariants"] for mutant_id in item["mutants"]
    }
    assert mapped <= set(defined), mapped - set(defined)
    assert set(defined) <= mapped, f"unmapped mutants: {set(defined) - mapped}"
    for mutant in MUTANTS:
        source = (ROOT / mutant.file).read_text(encoding="utf-8")
        assert source.count(mutant.find) == 1, f"{mutant.id} no longer applies"
        for node in mutant.tests:
            path, _, name = node.partition("::")
            assert name in _test_names(ROOT / path), f"{mutant.id}: {node}"


def test_every_finding_maps_to_an_invariant_and_a_reproduction() -> None:
    mapping = _load()
    findings = {row["id"]: row for row in mapping["findings"]}
    assert tuple(sorted(findings)) == tuple(sorted(FINDINGS))
    by_invariant = {row["id"]: set(row["findings"]) for row in mapping["invariants"]}
    for finding, row in findings.items():
        assert row["invariants"], finding
        assert any(finding in by_invariant[inv] for inv in row["invariants"]), finding
        path, _, name = row["reproduction"].partition("::")
        assert name in _test_names(ROOT / path), row["reproduction"]


def test_the_prose_and_the_mapping_name_the_same_things() -> None:
    text = SPEC.read_text(encoding="utf-8")
    headings = re.findall(r"^## (INV-\d\d) ", text, re.MULTILINE)
    assert tuple(headings) == INVARIANTS
    for finding in FINDINGS:
        assert f"| {finding} " in text, f"{finding} missing from the defect map"


def test_each_reproduction_came_from_the_frozen_evidence_it_names() -> None:
    """Where the frozen evidence is on this machine, its bytes are what is cited."""

    mapping = _load()
    evidence = Path(str(mapping["evidence"])).expanduser()
    if not evidence.is_dir():
        pytest.skip(f"the frozen evidence is not on this machine ({evidence})")
    for row in mapping["findings"]:
        if not row["evidence_file"]:
            continue
        digest = hashlib.sha256(
            (evidence / row["evidence_file"]).read_bytes()
        ).hexdigest()
        assert digest == row["evidence_sha256"], row["id"]

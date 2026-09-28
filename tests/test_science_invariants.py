"""The science-execution invariants are machine-checked, not only written down.

``docs/science_invariants.yaml`` maps SCI-01 to SCI-05 to the code that
enforces each, the tests that demonstrate it and the mutants that must be
killed (``tests/science_mutations.py``). This fails when an enforcement point
is renamed away, a test is deleted, or a mutant stops applying.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.science_mutations import MUTANTS

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "docs" / "science_invariants.yaml"
SPEC = ROOT / "docs" / "SCIENCE_EXECUTION.md"
INVARIANTS = tuple(f"SCI-{n:02d}" for n in range(1, 6))


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
    items = _load()["invariants"]
    assert tuple(item["id"] for item in items) == INVARIANTS
    for item in items:
        for key in (
            "title",
            "threat",
            "enforced_by",
            "tests",
            "mutants",
            "pass_requires",
        ):
            assert item.get(key), f"{item['id']} has no {key}"


@pytest.mark.parametrize("invariant", INVARIANTS)
def test_every_enforcement_point_and_test_exists(invariant: str) -> None:
    (item,) = [row for row in _load()["invariants"] if row["id"] == invariant]
    for point in item["enforced_by"]:
        assert callable(_resolve(point)), point
    for node in item["tests"]:
        path, _, name = node.partition("::")
        assert name in _test_names(ROOT / path), node


def test_every_mutant_is_mapped_defined_and_still_applies() -> None:
    defined = {mutant.id: mutant for mutant in MUTANTS}
    mapped = {m for item in _load()["invariants"] for m in item["mutants"]}
    assert mapped == set(defined), mapped ^ set(defined)
    for mutant in MUTANTS:
        source = (ROOT / mutant.file).read_text(encoding="utf-8")
        assert source.count(mutant.find) == 1, f"{mutant.id} no longer applies"
        for node in mutant.tests:
            path, _, name = node.partition("::")
            assert name in _test_names(ROOT / path), f"{mutant.id}: {node}"
        (item,) = [row for row in _load()["invariants"] if mutant.id in row["mutants"]]
        assert item["id"] == mutant.invariant, mutant.id


def test_the_prose_names_every_invariant() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert tuple(re.findall(r"^- \*\*(SCI-\d\d) ", text, re.MULTILINE)) == INVARIANTS

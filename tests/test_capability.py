"""The generic typed-capability schema and its mechanical resolution.

``docs/SCIENCE_EXECUTION.md``. No database and no model: a capability is
parsed from bytes, cross-checked against the host's declared command, and
resolved against requirements derived from a frozen analysis -- all by
ordinary code, deterministically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from research_os import capability as caps
from research_os.experiment.spec import CommandSpec
from research_os.portfolio.contracts import AnalysisSpec
from research_os.portfolio.sciencechain import requirements_from_analysis

BASE: dict[str, Any] = {
    "schema": "research-os-capabilities-v1",
    "capabilities": [
        {
            "id": "demo.cells",
            "version": 2,
            "title": "Cells of a factorial",
            "command": "cells",
            "parameters": [{"name": "plan", "description": "the plan"}],
            "inputs": [{"path": "data/table.csv"}],
            "result": {
                "artifact": "results/cells.json",
                "schema": {
                    "type": "object",
                    "required": ["cells", "summary"],
                    "properties": {
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["size", "label", "seconds"],
                                "properties": {
                                    "size": {"type": "integer"},
                                    "label": {"type": "string"},
                                    "seconds": {"type": "number"},
                                },
                            },
                        },
                        "summary": {
                            "type": "object",
                            "properties": {"total": {"type": "number"}},
                        },
                    },
                },
            },
            "observables": [
                {
                    "name": "cells",
                    "kind": "records",
                    "path": "cells",
                    "fields": [
                        {"name": "size", "type": "integer"},
                        {"name": "label", "type": "string"},
                        {
                            "name": "seconds",
                            "type": "number",
                            "unit": "s",
                            "deterministic": False,
                        },
                    ],
                },
                {
                    "name": "total",
                    "kind": "scalar",
                    "path": "summary.total",
                    "type": "number",
                },
            ],
            "determinism": "seeded",
            "replication": {
                "perturbations": [
                    {"kind": "seeds", "description": "the seed draws the instances"},
                    {
                        "kind": "parameter",
                        "name": "plan",
                        "description": "every cell is computed from the plan",
                    },
                ]
            },
            "resources": {"timeout_seconds": 600},
        }
    ],
}

COMMAND = CommandSpec.model_validate(
    {
        "name": "cells",
        "argv": ["python3", "cells.py", "--plan", "{plan}"],
        "parameters": [
            {
                "name": "plan",
                "type": "generated",
                "required": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                },
            }
        ],
        "outputs": ["results/cells.json"],
        "timeout_seconds": 900,
    }
)


def _raw(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def _loaded(document: dict[str, Any] = BASE) -> caps.LoadedManifest:
    raw = _raw(document)
    return caps.LoadedManifest(
        manifest=caps.parse_manifest(raw), raw=raw, sha256="s" * 64, commit="c" * 40
    )


def _edit(**changes: Any) -> dict[str, Any]:
    import copy

    document = copy.deepcopy(BASE)
    document["capabilities"][0].update(changes)
    return document


def _analysis(**overrides: Any) -> AnalysisSpec:
    spec = {
        "analysable": True,
        "estimand": "how time grows with size",
        "observables": [
            {
                "name": "cells",
                "source": "results/cells.json",
                "kind": "records",
                "path": "cells",
                "fields": ["size"],
                "include": [{"field": "label", "comparator": "==", "value": "a"}],
            }
        ],
        "reductions": [
            {
                "name": "slope",
                "op": "ols_coefficient",
                "observable": "cells",
                "response": "seconds",
                "terms": ["size"],
                "coefficient": "size",
            }
        ],
        "primary_statistic": "slope",
        "success": {"comparator": ">", "threshold": 2.0},
        "failure": {"comparator": "<", "threshold": 0.5},
    }
    spec.update(overrides)
    return AnalysisSpec.model_validate(spec)


def _needs(spec: AnalysisSpec | None = None) -> tuple[caps.ObservableNeed, ...]:
    return requirements_from_analysis(spec or _analysis())


# ------------------------------------------------------------ the schema --
def test_a_valid_manifest_parses_and_hashes_deterministically() -> None:
    manifest = caps.parse_manifest(_raw(BASE))
    (capability,) = manifest.capabilities
    assert capability.ref == "demo.cells@2"
    assert caps.capability_digest(capability) == caps.capability_digest(
        caps.parse_manifest(_raw(BASE)).capabilities[0]
    )
    assert caps.capability_digest(capability).startswith("rcap-v1:")
    changed = caps.parse_manifest(_raw(_edit(title="another title"))).capabilities[0]
    assert caps.capability_digest(changed) != caps.capability_digest(capability)


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        (
            {"observables": [{"name": "x", "kind": "scalar", "path": "summary.total"}]},
            "declares a `type`",
        ),
        (
            {"observables": [{"name": "x", "kind": "records", "path": "cells"}]},
            "declares `fields`",
        ),
        (
            {
                "observables": [
                    {
                        "name": "x",
                        "kind": "scalar",
                        "path": "summary.absent",
                        "type": "number",
                    }
                ]
            },
            "has nothing at",
        ),
        (
            {
                "observables": [
                    {
                        "name": "x",
                        "kind": "scalar",
                        "path": "summary.total",
                        "type": "string",
                    }
                ]
            },
            "result schema says",
        ),
        (
            {
                "observables": [
                    {
                        "name": "x",
                        "kind": "records",
                        "path": "cells",
                        "fields": [{"name": "undeclared", "type": "number"}],
                    }
                ]
            },
            "not in the result schema",
        ),
        ({"determinism": "sometimes"}, "determinism"),
        ({"id": "Bad Id"}, "id"),
        (
            {"replication": {"perturbations": [{"kind": "seeds", "description": " "}]}},
            "attestation",
        ),
        (
            {
                "replication": {
                    "perturbations": [{"kind": "parameter", "description": "x"}]
                }
            },
            "names the parameter",
        ),
        ({"extra_key": 1}, "Extra inputs"),
    ],
)
def test_an_incoherent_declaration_is_refused_when_it_is_read(
    changes: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(caps.CapabilityError, match=fragment):
        caps.parse_manifest(_raw(_edit(**changes)))


def test_a_result_schema_keyword_nothing_honours_is_refused() -> None:
    document = _edit()
    document["capabilities"][0]["result"]["schema"]["properties"]["cells"][
        "pattern"
    ] = "x"
    with pytest.raises(caps.CapabilityError, match="pattern"):
        caps.parse_manifest(_raw(document))


def test_a_result_needs_exactly_one_location_and_a_schema() -> None:
    both = _edit()
    both["capabilities"][0]["result"]["artifact_parameter"] = "plan"
    with pytest.raises(caps.CapabilityError, match="exactly one"):
        caps.parse_manifest(_raw(both))
    none = _edit()
    none["capabilities"][0]["result"]["schema"] = {}
    with pytest.raises(caps.CapabilityError, match="schema"):
        caps.parse_manifest(_raw(none))


def test_two_capabilities_may_not_share_an_id_or_a_command() -> None:
    import copy

    document = copy.deepcopy(BASE)
    document["capabilities"].append(copy.deepcopy(BASE["capabilities"][0]))
    with pytest.raises(caps.CapabilityError, match="declared twice"):
        caps.parse_manifest(_raw(document))
    document["capabilities"][1]["id"] = "demo.other"
    with pytest.raises(caps.CapabilityError, match="one command"):
        caps.parse_manifest(_raw(document))


def test_bytes_that_are_not_one_strict_mapping_are_refused() -> None:
    with pytest.raises(caps.CapabilityError, match="UTF-8"):
        caps.parse_manifest(b"\xff\xfe")
    with pytest.raises(caps.CapabilityError, match="duplicate key"):
        caps.parse_manifest(b"schema: a\nschema: b\n")
    with pytest.raises(caps.CapabilityError, match="one mapping"):
        caps.parse_manifest(b"- a\n- b\n")
    with pytest.raises(caps.CapabilityError, match="at most"):
        caps.parse_manifest(b"#" * (caps.MAX_MANIFEST_BYTES + 1))


# ---------------------------------------------------- against the host --
def test_a_capability_must_describe_the_host_command_it_names() -> None:
    (capability,) = _loaded().manifest.capabilities
    assert caps.check_against_command(capability, {"cells": COMMAND}) == []
    (missing,) = caps.check_against_command(capability, {})
    assert missing.requirement == "host command"
    wrong_output = COMMAND.model_copy(update={"outputs": ["elsewhere.json"]})
    (unmet,) = caps.check_against_command(capability, {"cells": wrong_output})
    assert unmet.requirement == "result artifact"
    short = COMMAND.model_copy(update={"timeout_seconds": 60})
    (unmet,) = caps.check_against_command(capability, {"cells": short})
    assert unmet.requirement == "resources"
    other = _loaded(_edit(parameters=[{"name": "other"}])).manifest.capabilities[0]
    assert caps.check_against_command(other, {"cells": COMMAND})[0].requirement == (
        "parameters"
    )


# ------------------------------------------------------------ resolution --
def test_a_satisfiable_analysis_resolves_to_an_exact_binding() -> None:
    resolution = caps.resolve(
        caps.Requirements(observables=_needs()),
        loaded=_loaded(),
        commands={"cells": COMMAND},
    )
    assert resolution.executable, resolution.summary()
    binding = resolution.binding
    assert binding is not None
    assert binding.capability.ref == "demo.cells@2"
    assert dict(binding.observables) == {"cells": "cells"}
    assert binding.result_path == "results/cells.json"
    assert binding.commit == "c" * 40
    # The analysis regresses on a wall-clock field: said, not hidden.
    assert binding.nondeterministic == ("cells.seconds",)
    record = binding.record()
    assert record["perturbations"] == ["seeds", "plan"]
    assert record["comparison"] == "same_outcome"


def test_the_requirements_name_every_field_read_and_which_are_numbers() -> None:
    (need,) = _needs()
    assert set(need.fields) == {"size", "label", "seconds"}
    assert set(need.numeric) == {"size", "seconds"}
    assert "label" not in need.numeric, "an equality selection is not arithmetic"


@pytest.mark.parametrize(
    ("analysis", "fragment"),
    [
        (
            {
                "observables": [
                    {
                        "name": "cells",
                        "source": "results/cells.json",
                        "kind": "records",
                        "path": "cells",
                        "fields": ["size", "gap"],
                    }
                ]
            },
            "reads field 'gap'",
        ),
        (
            {
                "reductions": [
                    {
                        "name": "slope",
                        "op": "ols_coefficient",
                        "observable": "cells",
                        "response": "label",
                        "terms": ["size"],
                        "coefficient": "size",
                    }
                ]
            },
            "reads field 'label' as a number",
        ),
        (
            {
                "observables": [
                    {
                        "name": "cells",
                        "source": "results/other.json",
                        "kind": "records",
                        "path": "cells",
                        "fields": ["size"],
                    }
                ]
            },
            "result artifact is",
        ),
        (
            {
                "observables": [
                    {
                        "name": "cells",
                        "source": "results/cells.json",
                        "kind": "records",
                        "path": "rows",
                        "fields": ["size"],
                    }
                ]
            },
            "no declared observable is that",
        ),
    ],
)
def test_an_unmet_requirement_is_capability_limited_and_named(
    analysis: dict[str, Any], fragment: str
) -> None:
    resolution = caps.resolve(
        caps.Requirements(observables=_needs(_analysis(**analysis))),
        loaded=_loaded(),
        commands={"cells": COMMAND},
    )
    assert resolution.status is caps.CapabilityStatus.CAPABILITY_LIMITED
    assert resolution.binding is None
    assert fragment in resolution.summary(), resolution.summary()


def test_a_label_read_by_the_value_operation_must_be_a_number() -> None:
    document = _edit()
    document["capabilities"][0]["observables"][1]["type"] = "string"
    document["capabilities"][0]["result"]["schema"]["properties"]["summary"][
        "properties"
    ]["total"]["type"] = "string"
    spec = _analysis(
        observables=[
            {
                "name": "total",
                "source": "results/cells.json",
                "kind": "scalar",
                "path": "summary.total",
            }
        ],
        reductions=[{"name": "total_value", "op": "value", "observable": "total"}],
        primary_statistic="total_value",
    )
    resolution = caps.resolve(
        caps.Requirements(observables=_needs(spec)),
        loaded=_loaded(document),
        commands={"cells": COMMAND},
    )
    assert "read as a number" in resolution.summary()


def test_a_chosen_command_perturbation_and_host_limit_are_all_checked() -> None:
    loaded, commands = _loaded(), {"cells": COMMAND}
    unbacked = caps.resolve(
        caps.Requirements(observables=_needs(), command="plot"),
        loaded=loaded,
        commands=commands,
    )
    assert "backs no declared capability" in unbacked.summary()
    varied = caps.resolve(
        caps.Requirements(observables=_needs(), perturbations=("implementation",)),
        loaded=loaded,
        commands=commands,
    )
    assert "replication perturbation" in varied.summary()
    slow = caps.resolve(
        caps.Requirements(observables=_needs(), max_seconds=60),
        loaded=loaded,
        commands=commands,
    )
    assert "this host permits 60s" in slow.summary()
    fine = caps.resolve(
        caps.Requirements(
            observables=_needs(), perturbations=("seeds", "plan"), max_seconds=600
        ),
        loaded=loaded,
        commands=commands,
    )
    assert fine.executable


def test_no_manifest_is_capability_limited() -> None:
    resolution = caps.resolve(
        caps.Requirements(observables=_needs()), loaded=None, commands={}
    )
    assert not resolution.executable
    assert "declares no research-capabilities.yaml" in resolution.summary()


def test_a_result_named_by_a_path_parameter_is_located_by_its_value() -> None:
    document = _edit()
    document["capabilities"][0]["result"] = {
        **document["capabilities"][0]["result"],
        "artifact": "",
        "artifact_parameter": "out",
    }
    document["capabilities"][0]["parameters"] = []
    document["capabilities"][0]["replication"] = {
        "perturbations": [{"kind": "seeds", "description": "the seed draws them"}]
    }
    command = CommandSpec.model_validate(
        {
            "name": "cells",
            "argv": ["python3", "cells.py", "--out", "{out}"],
            "parameters": [{"name": "out", "type": "path", "required": True}],
            "timeout_seconds": 900,
        }
    )
    loaded = _loaded(document)
    # Before a design exists the path is unknown, and any source is possible.
    early = caps.resolve(
        caps.Requirements(observables=_needs()),
        loaded=loaded,
        commands={"cells": command},
    )
    assert early.executable
    wrong = caps.resolve(
        caps.Requirements(observables=_needs(), parameters={"out": "results/x.json"}),
        loaded=loaded,
        commands={"cells": command},
    )
    assert "result artifact is 'results/x.json'" in wrong.summary()
    right = caps.resolve(
        caps.Requirements(
            observables=_needs(), parameters={"out": "results/cells.json"}
        ),
        loaded=loaded,
        commands={"cells": command},
    )
    assert right.executable and right.binding.result_path == "results/cells.json"


# ------------------------------------------------------------ catalogue --
def test_the_catalogue_shows_types_units_variability_and_perturbations() -> None:
    lines = "\n".join(caps.catalogue_lines(_loaded(), {"cells": COMMAND}))
    assert "capability demo.cells@2" in lines
    assert "seconds: number [s] NONDETERMINISTIC" in lines
    assert "observable total: scalar number" in lines
    assert "a replication may vary: seeds, plan" in lines
    assert "only observables that exist" in lines
    assert caps.catalogue_lines(None) == []
    broken = "\n".join(caps.catalogue_lines(_loaded(), {}))
    assert "NOT usable" in broken


# ------------------------------------------------------------- git pins --
def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "science"
    repo.mkdir()
    for args in (
        ["init", "-q", "--initial-branch=main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, name: str, data: bytes) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_bytes(data)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_the_manifest_is_read_from_a_commit_and_pinned_to_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert caps.load_committed(repo) is None, "no commits: nothing declared"
    first = _commit(repo, caps.MANIFEST_PATH, _raw(BASE))
    loaded = caps.load_committed(repo)
    assert loaded is not None and loaded.commit == first
    # A working-tree edit declares nothing; the next commit does.
    (repo / caps.MANIFEST_PATH).write_bytes(_raw(_edit(title="uncommitted")))
    assert (
        caps.load_committed(repo).manifest.capabilities[0].title
        == BASE["capabilities"][0]["title"]
    )
    second = _commit(repo, caps.MANIFEST_PATH, _raw(_edit(title="committed")))
    assert caps.load_committed(repo).manifest.capabilities[0].title == "committed"
    assert caps.load_committed(repo, commit=first).commit == first
    # An invalid committed manifest is an error, not an absence.
    _commit(repo, caps.MANIFEST_PATH, b"schema: nonsense\n")
    with pytest.raises(caps.CapabilityError):
        caps.load_committed(repo)
    assert second != first


def test_input_artifacts_are_hashed_at_the_pinned_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit = _commit(repo, "data/table.csv", b"a,b\n1,2\n")
    (capability,) = _loaded().manifest.capabilities
    found, unmet = caps.input_digests(repo, commit=commit, capability=capability)
    import hashlib

    assert found == [("data/table.csv", hashlib.sha256(b"a,b\n1,2\n").hexdigest())]
    assert unmet == []
    (repo / "data" / "table.csv").write_bytes(b"changed\n")
    again, _ = caps.input_digests(repo, commit=commit, capability=capability)
    assert again == found, "the working tree does not move a pinned input"
    other = _loaded(_edit(inputs=[{"path": "data/absent.csv"}])).manifest.capabilities[
        0
    ]
    found, unmet = caps.input_digests(repo, commit=commit, capability=other)
    assert found == [] and unmet[0].requirement == "input artifact"

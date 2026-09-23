"""Documents a plan may compose: what is bounded, and what stays human-owned.

The mechanism exists to remove one measured bottleneck -- a new experimental
design inside an already-approved capability required a person to author and
commit a plan file -- without moving the boundary that makes the capability
approved in the first place. So most of this file is refusals.

The organising question for every test here is: *what could a model cause to
happen that the researcher did not authorise?* A composed document reaches a
program the researcher trusts, gets written into a checkout, and is hashed
into a preregistration, so the answers worth checking are about structure,
size, location, and immutability after the fact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_os.errors import ExperimentSpecError
from research_os.experiment.generated import (
    GENERATED_INPUT_DIR,
    assert_schema_supported,
    canonical_bytes,
    freeze,
)
from research_os.experiment.models import ParameterSpec, ParameterType
from research_os.experiment.spec import CommandSpec, resolve_command
from research_os.portfolio.empirical import (
    _spec_record,
    command_catalogue,
    spec_from_record,
)
from research_os.runtime.executors import spec_digest
from research_os.runtime.interfaces import ExecutionSpec

#: A plan schema shaped like the real one: a solver enum, an instance-family
#: enum, bounded repetitions and a bounded timeout.
PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["solver", "instances", "repetitions", "timeout_seconds"],
    "properties": {
        "solver": {"type": "string", "enum": ["cg_hist", "socp"]},
        "repetitions": {"type": "integer", "minimum": 1, "maximum": 5},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "threads": {"type": "integer", "minimum": 1, "maximum": 4},
        "instances": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["family", "n", "p"],
                "properties": {
                    "family": {
                        "type": "string",
                        "enum": ["sparse", "toeplitz", "block"],
                    },
                    "n": {"type": "integer", "minimum": 10, "maximum": 5000},
                    "p": {"type": "integer", "minimum": 10, "maximum": 2000},
                    "rho": {"type": "number", "minimum": 0.0, "maximum": 0.99},
                },
            },
        },
    },
}

GOOD_PLAN: dict[str, object] = {
    "solver": "cg_hist",
    "repetitions": 3,
    "timeout_seconds": 300,
    "instances": [{"family": "toeplitz", "n": 2000, "p": 500, "rho": 0.6}],
}


def plan_parameter(**overrides: object) -> ParameterSpec:
    fields: dict[str, object] = {
        "name": "plan",
        "type": ParameterType.GENERATED,
        "required": True,
        "input_schema": PLAN_SCHEMA,
    }
    fields.update(overrides)
    return ParameterSpec(**fields)  # type: ignore[arg-type]


def sweep_command(parameter: ParameterSpec | None = None) -> CommandSpec:
    return CommandSpec(
        name="sweep",
        description="run one frozen sweep plan",
        argv=["uv", "run", "python", "scripts/run_sweep.py", "--plan", "{plan}"],
        parameters=[parameter or plan_parameter()],
        outputs=["results/sweep.json"],
    )


def frozen(document: object = GOOD_PLAN) -> object:
    return freeze(
        parameter="plan",
        document=document,
        schema=PLAN_SCHEMA,
        max_bytes=65_536,
        command="sweep",
    )


# ------------------------------------------------ the capability boundary --
def test_a_declaration_is_what_permits_a_composed_document() -> None:
    """The type is in `experiments.yaml`, which no worktree can reach.

    This is the whole authority argument in one assertion: composing is not
    a thing a plan may decide to do, it is a thing a parameter *is*. A design
    that supplies a document for an ordinary parameter has supplied a value
    that will be refused by that parameter's own type.
    """

    ordinary = ParameterSpec(name="plan", type=ParameterType.PATH, required=True)
    with pytest.raises(ExperimentSpecError, match="not a usable path|must be relative"):
        resolve_command(
            sweep_command(ordinary), {"plan": "/etc/passwd"}, worktree=Path("/tmp")
        )


def test_a_generated_parameter_without_a_schema_is_not_a_declaration() -> None:
    with pytest.raises(ValidationError, match="input_schema"):
        plan_parameter(input_schema={})


def test_a_schema_keyword_this_checker_cannot_honour_is_refused() -> None:
    """Refused at configuration time, which is the lesson from the other direction.

    This codebase has now paid five times for a limit that was enforced and
    never stated. Accepting `pattern:` and not applying it is the same defect
    with the arrow reversed: a researcher would believe a constraint holds
    because nothing complained.
    """

    with pytest.raises(ValidationError, match="does not honour"):
        plan_parameter(input_schema={"type": "string", "pattern": "^x"})


def test_additional_properties_may_only_be_closed() -> None:
    with pytest.raises(ValidationError, match="additionalProperties must be false"):
        plan_parameter(input_schema={"type": "object", "additionalProperties": True})


def test_a_schema_on_a_parameter_that_is_not_generated_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a generated parameter"):
        ParameterSpec(
            name="out", type=ParameterType.PATH, input_schema={"type": "object"}
        )


# ------------------------------------------------------- what is accepted --
def test_a_plan_inside_the_declaration_is_accepted_and_placed() -> None:
    item = frozen()
    assert item.path.startswith(f"{GENERATED_INPUT_DIR}/plan-")
    assert item.sha256 == hashlib.sha256(item.canonical).hexdigest()
    assert json.loads(item.canonical) == GOOD_PLAN

    resolved = resolve_command(
        sweep_command(), {}, worktree=Path("/tmp"), generated={"plan": item.path}
    )
    assert list(resolved.argv)[-1] == item.path


def test_the_document_is_canonical_so_key_order_is_not_identity() -> None:
    """Two spellings of one plan are one plan, and hash alike."""

    reordered = dict(reversed(list(GOOD_PLAN.items())))
    assert frozen(GOOD_PLAN).sha256 == frozen(reordered).sha256


def test_a_changed_plan_is_a_different_input_and_a_different_spec() -> None:
    other = dict(GOOD_PLAN) | {"repetitions": 4}
    first, second = frozen(), frozen(other)
    assert first.sha256 != second.sha256
    assert first.path != second.path

    def spec_for(item: object) -> ExecutionSpec:
        return ExecutionSpec(
            name="idea-experiment-sweep",
            argv=("uv", "run", "python", "s.py", "--plan", item.path),  # type: ignore[attr-defined]
            cwd="/w",
            inputs=((item.path, item.sha256),),  # type: ignore[attr-defined]
        )

    assert spec_digest(spec_for(first)) != spec_digest(spec_for(second))


# ------------------------------------------------------- what is refused --
@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({**GOOD_PLAN, "solver": "gurobi"}, "must be one of"),
        (
            {
                **GOOD_PLAN,
                "instances": [{"family": "banded", "n": 100, "p": 50}],
            },
            "must be one of",
        ),
        ({**GOOD_PLAN, "repetitions": 99}, "must be at most 5"),
        ({**GOOD_PLAN, "timeout_seconds": 99_999}, "must be at most 600"),
        ({**GOOD_PLAN, "threads": 64}, "must be at most 4"),
        ({**GOOD_PLAN, "instances": []}, "at least 1 item"),
        ({k: v for k, v in GOOD_PLAN.items() if k != "solver"}, "missing required"),
        ({**GOOD_PLAN, "exec": "rm -rf /"}, "undeclared key"),
        ({**GOOD_PLAN, "repetitions": float("nan")}, "must be a integer"),
        ({**GOOD_PLAN, "repetitions": "3"}, "must be a integer"),
    ],
    ids=[
        "unsupported-solver",
        "unsupported-instance-family",
        "repetitions-over-ceiling",
        "timeout-over-ceiling",
        "threads-over-ceiling",
        "empty-instance-list",
        "missing-required-field",
        "undeclared-key",
        "not-a-number",
        "wrong-scalar-type",
    ],
)
def test_a_plan_outside_the_declaration_is_refused_before_anything_runs(
    document: dict[str, object], expected: str
) -> None:
    with pytest.raises(ExperimentSpecError, match=expected):
        frozen(document)


def test_an_oversize_document_is_refused_against_the_declared_ceiling() -> None:
    big = {
        **GOOD_PLAN,
        "instances": [
            {"family": "sparse", "n": 1000 + index, "p": 500} for index in range(8)
        ],
    }
    with pytest.raises(ExperimentSpecError, match="allows 64"):
        freeze(
            parameter="plan",
            document=big,
            schema=PLAN_SCHEMA,
            max_bytes=64,
            command="sweep",
        )


def test_shell_shaped_text_stays_inside_the_document() -> None:
    """A composed string is data in a file, never argv structure.

    The declaration decides argv; a document decides nothing about it. So a
    string full of shell syntax is only refused if the *schema* refuses it,
    and either way it cannot become a second argument or a second command.
    """

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["note"],
        "properties": {"note": {"type": "string", "maxLength": 200}},
    }
    item = freeze(
        parameter="plan",
        document={"note": "; rm -rf / #$(whoami) `id` --out=/etc/shadow"},
        schema=schema,
        max_bytes=65_536,
        command="sweep",
    )
    resolved = resolve_command(
        sweep_command(plan_parameter(input_schema=schema)),
        {},
        worktree=Path("/tmp"),
        generated={"plan": item.path},
    )
    assert list(resolved.argv) == [
        "uv",
        "run",
        "python",
        "scripts/run_sweep.py",
        "--plan",
        item.path,
    ]
    assert "rm -rf" not in " ".join(resolved.argv)


def test_a_caller_may_not_name_where_its_document_lands() -> None:
    """Research OS chooses the path. This is the escape that is not available."""

    item = frozen()
    for attempt in ("/etc/cron.d/x", "../../etc/passwd", "results/mine.json"):
        with pytest.raises(ExperimentSpecError, match="not a path"):
            resolve_command(
                sweep_command(),
                {"plan": attempt},
                worktree=Path("/tmp"),
                generated={"plan": item.path},
            )


def test_a_caller_that_cannot_compose_is_told_so_rather_than_blamed() -> None:
    """Two situations, and one message for both cost a human their CLI.

    Only the portfolio's empirical route freezes documents. The objective
    cycle and `researchctl experiment run` call `resolve_command` with no
    `generated=` at all, so converting a declared parameter to `generated`
    silently removes the command from both -- and they reported it as if a
    design had forgotten to supply something. An architecture review found
    it by reading the call sites; it was then reproduced on the real
    `sweep-lambda-support` declaration.
    """

    with pytest.raises(ExperimentSpecError, match="this caller cannot compose"):
        resolve_command(sweep_command(), {}, worktree=Path("/tmp"))

    # A caller that *can* compose and simply did not is a different fault,
    # and still says so.
    with pytest.raises(ExperimentSpecError, match="none was frozen"):
        resolve_command(sweep_command(), {}, worktree=Path("/tmp"), generated={})


def test_nothing_may_place_a_file_for_an_ordinary_parameter() -> None:
    ordinary = ParameterSpec(name="plan", type=ParameterType.PATH, required=True)
    with pytest.raises(ExperimentSpecError, match="not generated parameters"):
        resolve_command(
            sweep_command(ordinary),
            {"plan": "experiments/p.json"},
            worktree=Path("/tmp"),
            generated={"plan": "anywhere.json"},
        )


def test_a_placed_path_is_checked_against_the_worktree_like_any_other(
    tmp_path: Path,
) -> None:
    """The containment rule is applied to a path this system chose.

    Not because it is suspected. A rule that is only ever applied to
    untrusted input is a rule nobody is exercising, and this one is the
    difference between a workspace and a filesystem.
    """

    with pytest.raises(ExperimentSpecError, match="must be relative"):
        resolve_command(
            sweep_command(), {}, worktree=tmp_path, generated={"plan": "/etc/passwd"}
        )
    with pytest.raises(ExperimentSpecError, match=r"'\.\.'"):
        resolve_command(
            sweep_command(),
            {},
            worktree=tmp_path,
            generated={"plan": "../outside.json"},
        )


def test_a_symlink_out_of_the_worktree_is_refused(tmp_path: Path) -> None:
    worktree = tmp_path / "tree"
    (worktree / GENERATED_INPUT_DIR).mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / GENERATED_INPUT_DIR / "escape").symlink_to(outside)
    with pytest.raises(ExperimentSpecError, match="outside the experiment worktree"):
        resolve_command(
            sweep_command(),
            {},
            worktree=worktree,
            generated={"plan": f"{GENERATED_INPUT_DIR}/escape/plan.json"},
        )


# ------------------------------------------------------------ visibility --
def test_the_designer_is_shown_the_exact_schema_it_must_satisfy() -> None:
    """Phase 5, and the reason it is the schema rather than a summary.

    The checker refuses an undeclared key and an unlisted enum value, so a
    paraphrase that dropped one would be a constraint the model is graded on
    and never shown -- the defect this codebase has already paid for five
    times.
    """

    rendered = "\n".join(command_catalogue({"sweep": sweep_command()}))
    assert "type=generated" in rendered
    assert "at most 65536 canonical bytes" in rendered
    assert "never a path" in rendered
    for token in ("cg_hist", "socp", "toeplitz", "block", "repetitions"):
        assert token in rendered, token
    # The schema as JSON, not prose about it.
    assert '"additionalProperties": false' in rendered


# ---------------------------------------------------------- compatibility --
def test_a_specification_with_no_composed_input_hashes_exactly_as_before() -> None:
    """The field was added without moving a single digest already written.

    Pinned against the real preregistration of `PEXP-20260922T195752Z-4331c06a`
    -- the first experiment this layer ever carried to a conclusion -- whose
    stored specification predates generated inputs entirely. An unconditional
    `"inputs": []` in the digest payload would have re-hashed every stored
    preregistration and every idempotency key in the ledger, which is a
    migration wearing the clothes of a field addition.
    """

    stored = {
        "argv": [
            "uv",
            "run",
            "--frozen",
            "--extra",
            "conic",
            "--extra",
            "baselines",
            "python",
            "scripts/run_sweep.py",
            "--plan",
            "experiments/EXP-0002-lambda-support-sweep-plan.json",
            "--out",
            "results/2026/sweep.json",
        ],
        "cwd": (
            "/home/nicacevedo/.local/state/research-os-dogfood/xdg/state/"
            "worktrees/RUN-20260922T195752Z-c855445e/T-001"
        ),
        "env": {"RESEARCH_OS_SEED_0": "20260915"},
        "environment": {"kind": "uv", "workspace": "disposable-worktree"},
        "name": "idea-experiment-sweep-lambda-support",
        "outputs": ["results/2026/sweep.json"],
        "resources": {"cpus": "1", "time_limit": "01:30:00"},
        "seeds": [20260915],
        "timeout_seconds": 3600,
    }
    spec = spec_from_record(stored)
    assert spec.inputs == ()
    assert (
        spec_digest(spec)
        == "6b0e069ed57e9bf9ab1a92f4f736a5818f867459e033fec25e17443744ca84f5"
    )
    assert _spec_record(spec) == stored


def test_the_variation_digest_also_did_not_move(mock_free: None = None) -> None:
    """The commit claimed two pins and shipped one.

    `spec_digest`'s compatibility is pinned against the real stored
    preregistration; `variation_digest`'s was only asserted in prose, so
    making its `inputs` key unconditional would silently re-hash every
    stored variation digest with nothing going red. A test audit found the
    missing half.
    """

    from research_os.portfolio.empirical import variation_digest

    spec = ExecutionSpec(
        name="idea-experiment-sweep-lambda-support",
        argv=(
            "uv",
            "run",
            "--frozen",
            "--extra",
            "conic",
            "--extra",
            "baselines",
            "python",
            "scripts/run_sweep.py",
            "--plan",
            "experiments/EXP-0002-lambda-support-sweep-plan.json",
            "--out",
            "results/2026/sweep.json",
        ),
        cwd="/anything: the variation digest removes the workspace",
        environment={"kind": "uv", "workspace": "disposable-worktree"},
        resources={"cpus": "1", "time_limit": "01:30:00"},
        env={"RESEARCH_OS_SEED_0": "20260915"},
        timeout_seconds=3600,
        outputs=("results/2026/sweep.json",),
        seeds=(20260915,),
    )
    assert spec.inputs == ()
    assert (
        variation_digest(spec)
        == "e3aaca6a1f3985c6e4684b630c723b141b7487b188eb980bd062cea330d6d118"
    )


def test_canonical_bytes_refuse_what_json_cannot_represent() -> None:
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ExperimentSpecError, match="not a JSON document"):
            canonical_bytes({"x": bad}, where="where")


def test_the_schema_checker_refuses_a_schema_that_is_not_an_object() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        assert_schema_supported(
            {"type": "object", "properties": {"a": "nope"}},  # type: ignore[dict-item]
            where="w",
        )


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "object"}, "must declare `properties`"),
        (
            {"type": "object", "additionalProperties": False},
            "must declare `properties`",
        ),
        ({"properties": {"a": {"type": "integer"}}}, "must declare `type`"),
        ({"type": "array"}, "must declare `items`"),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"o": {"type": "object"}},
            },
            "must declare `properties`",
        ),
    ],
    ids=[
        "open-object",
        "object-closed-but-empty",
        "no-type",
        "array-without-items",
        "open-nested-object",
    ],
)
def test_a_schema_with_an_unclosable_node_is_refused_at_configuration_time(
    schema: dict[str, object], expected: str
) -> None:
    """Closure has to be real, and it was not.

    `_check_object` implied closure with `if properties:`, so any node with
    none was wholly open -- including one a researcher had closed explicitly
    with `additionalProperties: false`, which was accepted at configuration
    time and then never read. An architecture review and a security review on
    2026-09-23 found it independently and both were right that it made the
    module's own promise false, and `SECURITY.md`'s "refuses any key the
    schema does not list" false with it.

    Refused where the mistake is -- in the declaration -- rather than
    silently admitting everything underneath it.
    """

    with pytest.raises(ValueError, match=expected):
        assert_schema_supported(schema, where="w")


def test_an_explicitly_closed_object_really_is_closed() -> None:
    """The reviewers' own reproduction, kept as the behavioural half."""

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["solver"],
        "properties": {
            "solver": {"type": "string", "enum": ["cg_hist"]},
            "options": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"tolerance": {"type": "number"}},
            },
        },
    }
    assert_schema_supported(schema, where="w")
    with pytest.raises(ExperimentSpecError, match="undeclared key"):
        freeze(
            parameter="plan",
            document={"solver": "cg_hist", "options": {"--config": "/etc/passwd"}},
            schema=schema,
            max_bytes=65_536,
            command="sweep",
        )


@pytest.mark.parametrize(
    ("document", "schema"),
    [
        (True, {"type": "integer", "enum": [1, 2]}),
        (1.0, {"type": "number", "const": 1}),
        (
            "not an object",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "integer"}},
                "required": ["a"],
            },
        ),
    ],
    ids=[
        "bool-satisfying-an-int-enum",
        "float-satisfying-an-int-const",
        "scalar-for-object",
    ],
)
def test_a_value_of_the_wrong_kind_does_not_satisfy_a_declaration(
    document: object, schema: dict[str, object]
) -> None:
    """`True == 1` and `1.0 == 1` in Python, and neither is true of JSON.

    Without this the written file carries `true` where the declaration listed
    `1`, and the researcher's program parses it. Type confusion reaching a
    trusted program.
    """

    with pytest.raises(ExperimentSpecError):
        freeze(
            parameter="plan",
            document=document,
            schema=schema,
            max_bytes=65_536,
            command="sweep",
        )


def test_a_lone_surrogate_is_refused_as_a_contract_failure_not_a_crash() -> None:
    """It is a `ValueError`, and it was escaping unclassified.

    `json.loads` accepts a lone surrogate and `str.encode` rejects it. The
    encode sat outside `canonical_bytes`' handler, so the exception escaped
    as `CODE_EXCEPTION` -- "the code is broken, retry" -- letting a model
    turn its own invalid document into a paid retry loop instead of a
    refusal.
    """

    document = json.loads('{"a": "\ud800"}')
    with pytest.raises(ExperimentSpecError, match="not a JSON document"):
        canonical_bytes(document, where="w")

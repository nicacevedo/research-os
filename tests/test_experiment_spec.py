"""What may become an experiment command, and what may fill in its blanks.

This is where "models do not invent shell strings" is either true or it is not.
A command is something the researcher declared, by name, in a file outside every
worktree. A plan chooses one and supplies values for declared parameters, and
every value is checked against a declared type before it becomes an argument.

The tests are mostly refusals, because the interesting property is what cannot
get through: a value with shell syntax in it, a path that leaves the worktree, a
parameter nobody declared, a placeholder glued into a larger token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_os.errors import ExperimentSpecError
from research_os.experiment.models import ExecutorKind, ParameterSpec, ParameterType
from research_os.experiment.spec import CommandSpec, resolve_command


def spec(**overrides: object) -> CommandSpec:
    payload: dict[str, object] = {
        "name": "fit-model",
        "argv": ["uv", "run", "python", "-m", "widget.fit", "--seed", "{seed}"],
        "parameters": [
            {
                "name": "seed",
                "type": "integer",
                "required": True,
                "minimum": 0,
                "maximum": 65535,
            }
        ],
        "outputs": ["results/fit.json"],
        "checks": ["outputs_exist"],
    }
    payload.update(overrides)
    return CommandSpec.model_validate(payload)


# -- what may be declared -----------------------------------------------------


def test_a_declared_command_becomes_an_argument_vector() -> None:
    resolved = resolve_command(spec(), {"seed": 42})

    assert resolved.argv == (
        "uv",
        "run",
        "python",
        "-m",
        "widget.fit",
        "--seed",
        "42",
    )
    assert resolved.parameters == {"seed": "42"}
    assert resolved.executor is ExecutorKind.LOCAL


def test_a_command_that_is_a_path_rather_than_a_program_is_refused() -> None:
    """What runs is resolved on PATH, not by a path in a configuration file."""

    with pytest.raises(ValidationError, match="bare program name"):
        spec(argv=["/usr/local/bin/thing", "{seed}"])


def test_shell_syntax_in_a_declared_argument_is_refused() -> None:
    for hostile in (
        ["sh", "-c", "rm -rf /; {seed}"],
        ["python", "a && b", "{seed}"],
        ["python", "$(whoami)", "{seed}"],
        ["python", "a|b", "{seed}"],
    ):
        with pytest.raises(ValidationError, match="plain argument token"):
            spec(argv=hostile)


def test_a_placeholder_glued_into_a_larger_token_is_refused() -> None:
    """Building a token out of a supplied value is string-building by another name."""

    with pytest.raises(ValidationError, match="whole argument"):
        spec(argv=["python", "--seed={seed}"])


def test_a_placeholder_with_no_parameter_is_refused() -> None:
    with pytest.raises(ValidationError, match="no declared parameter"):
        spec(argv=["python", "{seed}", "{missing}"])


def test_a_parameter_that_appears_nowhere_is_refused() -> None:
    """Otherwise a run would look parameterised when the value changed nothing."""

    with pytest.raises(ValidationError, match="change nothing"):
        spec(
            parameters=[
                {"name": "seed", "type": "integer"},
                {"name": "unused", "type": "integer"},
            ]
        )


def test_an_absolute_output_path_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be relative"):
        spec(outputs=["/etc/passwd"])


def test_an_output_that_traverses_upward_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"'\.' or '\.\.'"):
        spec(outputs=["../outside/result.json"])


def test_a_choice_parameter_with_no_choices_is_refused() -> None:
    with pytest.raises(ValidationError, match="no value could ever be valid"):
        ParameterSpec(name="mode", type=ParameterType.CHOICE)


def test_choices_on_a_non_choice_parameter_are_refused() -> None:
    with pytest.raises(ValidationError, match="the type decides"):
        ParameterSpec(name="seed", type=ParameterType.INTEGER, choices=["a"])


# -- what may be supplied -----------------------------------------------------


def test_a_value_outside_its_declared_range_is_refused() -> None:
    with pytest.raises(ExperimentSpecError, match="at most 65535"):
        resolve_command(spec(), {"seed": 999999})
    with pytest.raises(ExperimentSpecError, match="at least 0"):
        resolve_command(spec(), {"seed": -1})


def test_a_value_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ExperimentSpecError, match="takes a integer"):
        resolve_command(spec(), {"seed": "not-a-number"})


def test_a_parameter_nobody_declared_is_refused_rather_than_ignored() -> None:
    """A plan that thinks it controls something it does not would misdescribe the run."""

    with pytest.raises(ExperimentSpecError, match="cannot add one"):
        resolve_command(spec(), {"seed": 1, "gpu_count": 8})


def test_a_required_parameter_with_no_value_is_refused() -> None:
    with pytest.raises(ExperimentSpecError, match="requires a value"):
        resolve_command(spec(), {})


def test_a_default_is_used_when_no_value_is_supplied() -> None:
    declared = spec(
        parameters=[
            {"name": "seed", "type": "integer", "required": False, "default": 7}
        ]
    )

    assert resolve_command(declared, {}).parameters == {"seed": "7"}


def test_a_token_value_carrying_shell_syntax_is_refused_not_escaped() -> None:
    """Escaping would mean guessing what the caller meant, and it may be a model."""

    declared = spec(
        argv=["python", "-m", "widget.fit", "--tag", "{tag}"],
        parameters=[{"name": "tag", "type": "token", "required": True}],
    )

    for hostile in ("a b", "a;rm -rf /", "$(whoami)", "a`b`", "a|b", "a\nb"):
        with pytest.raises(ExperimentSpecError, match="not a plain token"):
            resolve_command(declared, {"tag": hostile})


def test_a_choice_value_outside_its_choices_is_refused() -> None:
    declared = spec(
        argv=["python", "-m", "widget.fit", "--mode", "{mode}"],
        parameters=[
            {
                "name": "mode",
                "type": "choice",
                "required": True,
                "choices": ["fast", "thorough"],
            }
        ],
    )

    assert resolve_command(declared, {"mode": "fast"}).parameters == {"mode": "fast"}
    with pytest.raises(ExperimentSpecError, match="must be one of"):
        resolve_command(declared, {"mode": "arbitrary"})


def test_a_path_value_that_leaves_the_worktree_is_refused(tmp_path: Path) -> None:
    declared = spec(
        argv=["python", "-m", "widget.fit", "--data", "{data}"],
        parameters=[{"name": "data", "type": "path", "required": True}],
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    for hostile in ("/etc/passwd", "../outside/data.csv", "~/secrets"):
        with pytest.raises(ExperimentSpecError):
            resolve_command(declared, {"data": hostile}, worktree=worktree)


def test_a_path_value_that_follows_a_symlink_out_is_refused(tmp_path: Path) -> None:
    """The structural rule catches '..'; this catches a link that goes further."""

    declared = spec(
        argv=["python", "-m", "widget.fit", "--data", "{data}"],
        parameters=[{"name": "data", "type": "path", "required": True}],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("x\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "link.csv").symlink_to(outside / "secret.csv")

    with pytest.raises(ExperimentSpecError, match="outside the experiment worktree"):
        resolve_command(declared, {"data": "link.csv"}, worktree=worktree)


def test_a_path_value_inside_the_worktree_is_accepted(tmp_path: Path) -> None:
    declared = spec(
        argv=["python", "-m", "widget.fit", "--data", "{data}"],
        parameters=[{"name": "data", "type": "path", "required": True}],
    )
    worktree = tmp_path / "worktree"
    (worktree / "data").mkdir(parents=True)
    (worktree / "data" / "input.csv").write_text("x\n", encoding="utf-8")

    resolved = resolve_command(declared, {"data": "data/input.csv"}, worktree=worktree)

    assert resolved.parameters == {"data": "data/input.csv"}


def test_a_flag_parameter_takes_a_boolean() -> None:
    declared = spec(
        argv=["python", "-m", "widget.fit", "--verbose", "{verbose}"],
        parameters=[{"name": "verbose", "type": "flag", "default": False}],
    )

    assert resolve_command(declared, {"verbose": True}).parameters == {
        "verbose": "true"
    }
    with pytest.raises(ExperimentSpecError, match="takes true or false"):
        resolve_command(declared, {"verbose": "yes"})


def test_nothing_a_plan_supplies_can_change_the_program_that_runs() -> None:
    """The whole point: the argv shape is the researcher's, the values are not."""

    declared = spec()

    resolved = resolve_command(declared, {"seed": 1})

    assert resolved.argv[0] == "uv"
    assert list(resolved.argv[:5]) == ["uv", "run", "python", "-m", "widget.fit"]


# -- what a value may not become ----------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "--config=/home/someone/.ssh/id_rsa",
        "--output=/etc/passwd",
        "-rf",
        "-",
    ],
)
def test_a_path_value_that_is_really_a_flag_is_refused(value: str) -> None:
    """It stayed in its slot and escaped its type.

    A security review of this branch found that the containment check
    compares `worktree / value`, and for a value with no leading slash that
    is a *relative* join -- so `--config=/home/u/.ssh/id_rsa` resolves to
    `<worktree>/--config=/home/u/.ssh/id_rsa`, inside the worktree, and was
    accepted. The string then reaches the declared program as one whole
    argv token, where it is an option and not a path.

    Whether that buys anything depends on the declared program's argument
    parser, which is exactly the reasoning this layer exists to make
    unnecessary.
    """

    command = spec(
        argv=["uv", "run", "python", "-m", "widget.fit", "--plan", "{plan}"],
        parameters=[{"name": "plan", "type": "path", "required": True}],
    )
    with pytest.raises(ExperimentSpecError, match="must not begin with"):
        resolve_command(command, {"plan": value})


def test_a_token_value_that_is_really_a_flag_is_refused() -> None:
    command = spec(
        argv=["uv", "run", "python", "-m", "widget.fit", "--mode", "{mode}"],
        parameters=[{"name": "mode", "type": "token", "required": True}],
    )
    with pytest.raises(ExperimentSpecError, match="must not begin with"):
        resolve_command(command, {"mode": "--trace"})


def test_an_ordinary_relative_path_is_still_accepted(tmp_path: Path) -> None:
    """Positive control: the rule rejects flags, not paths."""

    command = spec(
        argv=["uv", "run", "python", "-m", "widget.fit", "--plan", "{plan}"],
        parameters=[{"name": "plan", "type": "path", "required": True}],
    )
    resolved = resolve_command(command, {"plan": "experiments/EXP-0001-plan.json"})
    assert "experiments/EXP-0001-plan.json" in resolved.argv


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_non_finite_number_does_not_satisfy_a_declared_bound(value: str) -> None:
    """`nan < minimum` and `nan > maximum` are both False.

    So a NaN satisfies every bound a researcher wrote by failing to compare
    with any of them, and this function's contract is that a value is
    checked against the bounds that were written down. The decision-rule
    side of the same hazard is handled in `contracts.DecisionPredicate`;
    this is the parameter side, and it was open.
    """

    command = spec(
        argv=["uv", "run", "python", "-m", "widget.fit", "--rate", "{rate}"],
        parameters=[
            {
                "name": "rate",
                "type": "number",
                "required": True,
                "minimum": 0.0,
                "maximum": 1.0,
            }
        ],
    )
    with pytest.raises(ExperimentSpecError):
        resolve_command(command, {"rate": value})


def test_a_finite_number_inside_its_bounds_is_accepted() -> None:
    """Positive control, including a negative value, which has no leading-dash rule."""

    command = spec(
        argv=["uv", "run", "python", "-m", "widget.fit", "--shift", "{shift}"],
        parameters=[
            {
                "name": "shift",
                "type": "number",
                "required": True,
                "minimum": -1.0,
                "maximum": 1.0,
            }
        ],
    )
    assert "-0.5" in resolve_command(command, {"shift": -0.5}).argv

"""Experiment commands the researcher declares, and the values a plan may fill in.

This module is where "models do not invent shell strings" stops being a wish.

An experiment command is not something a plan writes. It is something the
researcher declared beforehand, by name, in their own configuration file --
which lives under their config home, **outside every worktree**. A write-enabled
worker running in an isolated worktree therefore cannot add a command, widen an
existing one, or change what it runs, because the file that says so is not
reachable from where the worker is confined.

What a plan may do is choose a declared command by name and supply values for
its declared parameters. Every value is checked against a type the researcher
declared: an integer inside a range, one of a fixed set of choices, a path that
stays inside the worktree, a bare token with no shell syntax in it. A value that
does not fit is refused; it is never quoted, escaped, or repaired.

Substitution is by whole token. ``{seed}`` as an entire argument becomes one
argument; a placeholder embedded in a longer string is refused outright, because
that is the construction that turns a validated value back into string-building.
Nothing ever reaches a shell: the result is an argv list for ``subprocess.run``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.errors import ExperimentSpecError
from research_os.experiment.models import (
    TASK_NAME_RE,
    ExecutorKind,
    ParameterSpec,
    ParameterType,
)
from research_os.models import NonBlankStr

#: A whole-argument placeholder, and the only form substitution accepts.
PLACEHOLDER_RE = re.compile(r"^\{([a-z][a-z0-9_]{0,31})\}$")

#: Anything that looks like a placeholder anywhere in a token.
ANY_PLACEHOLDER_RE = re.compile(r"\{[a-z][a-z0-9_]{0,31}\}")

#: One plain argument token: no whitespace, no quoting, no shell syntax.
#:
#: The same idea as the acceptance-command policy's token rule, and for the same
#: reason: nothing here runs through a shell, so a token carrying shell syntax
#: has no legitimate purpose and exists only to be misread later.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.,:=+@/%{}\[\]-]+$")

#: A value a plan supplied for a ``token`` parameter. Narrower than an argv
#: token, because this one came from outside.
VALUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.,:=+@-]{1,256}$")

MAX_ARGV = 64
MAX_OUTPUTS = 32
DEFAULT_TIMEOUT_SECONDS = 3600


class CommandSpec(BaseModel):
    """One experiment command a researcher declared for one project."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    argv: list[NonBlankStr] = Field(min_length=1, max_length=MAX_ARGV)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list, max_length=MAX_OUTPUTS)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=86_400)
    executor: ExecutorKind = ExecutorKind.LOCAL
    working_directory: str | None = None
    checks: list[str] = Field(default_factory=list)
    """Deterministic postprocessing checks to run on the outputs, by name.

    Named rather than expressed, so a check is something the controller knows
    how to do rather than something a configuration file can describe in code.
    """

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if TASK_NAME_RE.fullmatch(value) is None:
            raise ValueError(
                "a command name must be 1-64 lowercase letters, digits, or "
                "hyphens, starting with a letter"
            )
        return value

    @field_validator("argv")
    @classmethod
    def _argv_is_plain(cls, value: list[str]) -> list[str]:
        program = value[0]
        if "/" in program or program.startswith("-"):
            raise ValueError(
                "an experiment command must start with a bare program name, so "
                "what runs is resolved on PATH rather than by a path in a "
                "configuration file"
            )
        if PLACEHOLDER_RE.fullmatch(program) is not None:
            # A placeholder contains no `/` and does not start with `-`, so
            # the rule above admits `argv: ["{tool}", ...]`. With `tool`
            # declared as a token, a model could then name `bash`, `env` or
            # `curl` and this layer would resolve it on PATH and bind its
            # execution closure into the sandbox. A security review of this
            # branch found it. The researcher chooses the program; a
            # parameter chooses its arguments.
            raise ValueError(
                f"the program to run may not be a parameter: {program!r}. A "
                f"declared command names its own program, because choosing "
                f"what executes is the researcher's decision and not a value "
                f"supplied per run"
            )
        for token in value:
            if TOKEN_RE.fullmatch(token) is None:
                raise ValueError(
                    f"argument {token!r} is not a plain argument token; "
                    "experiment commands are argument vectors with no shell "
                    "syntax, because nothing here runs through a shell"
                )
        return value

    @field_validator("outputs", "checks")
    @classmethod
    def _entries_are_relative(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("entries must be non-empty")
        return value

    @model_validator(mode="after")
    def _placeholders_and_parameters_agree(self) -> Self:
        """Refuse a command whose placeholders and parameters do not match.

        Both directions. A placeholder with no parameter could never be filled;
        a parameter with no placeholder would be accepted from a plan and then
        silently ignored, which is worse -- it makes a run look parameterised
        when the value changed nothing.
        """

        declared = {item.name for item in self.parameters}
        if len(declared) != len(self.parameters):
            raise ValueError("parameter names must not repeat")

        used: set[str] = set()
        for token in self.argv:
            whole = PLACEHOLDER_RE.fullmatch(token)
            if whole is not None:
                used.add(whole.group(1))
                continue
            if ANY_PLACEHOLDER_RE.search(token):
                raise ValueError(
                    f"argument {token!r} embeds a placeholder in a larger token. "
                    "A parameter must be a whole argument, because building a "
                    "token out of a supplied value is string-building by another "
                    "name"
                )
        unfillable = sorted(used - declared)
        if unfillable:
            raise ValueError(
                "these placeholders name no declared parameter: "
                + ", ".join(unfillable)
            )
        unused = sorted(declared - used)
        if unused:
            raise ValueError(
                "these parameters appear in no argument, so supplying them would "
                "change nothing: " + ", ".join(unused)
            )
        for output in self.outputs:
            _assert_relative(output, "an output path")
        if self.working_directory is not None:
            _assert_relative(self.working_directory, "a working directory")
        return self

    def parameter(self, name: str) -> ParameterSpec:
        for item in self.parameters:
            if item.name == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class ResolvedCommand:
    """One command with its parameters filled in, ready to hand to an executor."""

    name: str
    argv: tuple[str, ...]
    parameters: dict[str, str]
    outputs: tuple[str, ...]
    timeout_seconds: int
    executor: ExecutorKind
    working_directory: str | None
    checks: tuple[str, ...]

    @property
    def display(self) -> str:
        return " ".join(self.argv)


def resolve_command(
    spec: CommandSpec,
    values: dict[str, Any] | None = None,
    *,
    worktree: Path | None = None,
    generated: Mapping[str, str] | None = None,
) -> ResolvedCommand:
    """Fill one declared command's parameters and return an argv vector.

    Every supplied value is validated against the type the researcher declared
    before it is placed, and a value for a parameter that was not declared is
    refused rather than ignored: a plan that thinks it is controlling something
    it is not would produce a run whose record does not describe what happened.

    ``generated`` carries the in-tree paths Research OS *chose* for
    :class:`~research_os.experiment.models.ParameterType.GENERATED`
    parameters, after freezing each composed document. They arrive separately
    from ``values`` and that separation is the authority boundary in one
    argument list: the caller composes content, this function is told where
    that content landed, and a caller that tries to supply the path itself is
    refused below.
    """

    supplied = dict(values or {})
    placed = dict(generated or {})
    declared = {item.name for item in spec.parameters}
    unknown = sorted((set(supplied) | set(placed)) - declared)
    if unknown:
        raise ExperimentSpecError(
            f"command {spec.name!r} has no parameter(s) {', '.join(unknown)}. "
            "A plan may fill in the parameters the researcher declared and no "
            "others; it cannot add one."
        )
    generated_names = {
        item.name for item in spec.parameters if item.type is ParameterType.GENERATED
    }
    misplaced = sorted(set(placed) - generated_names)
    if misplaced:
        raise ExperimentSpecError(
            f"command {spec.name!r} parameter(s) {', '.join(misplaced)} are not "
            "generated parameters, so nothing may place a file for them"
        )
    named_directly = sorted(generated_names & set(supplied))
    if named_directly:
        raise ExperimentSpecError(
            f"command {spec.name!r} parameter(s) {', '.join(named_directly)} "
            "take a composed document, not a path. Research OS decides where a "
            "generated document lands; a caller that names the location has "
            "named a path it was not given."
        )

    resolved: dict[str, str] = {}
    for parameter in spec.parameters:
        if parameter.type is ParameterType.GENERATED:
            if parameter.name not in placed:
                # Two different situations, and saying "none was frozen" to
                # both of them cost a human their command line. Only the
                # portfolio's empirical route freezes documents; the
                # objective cycle and `researchctl experiment run` call this
                # with no `generated=` at all. Converting a declared
                # parameter to `generated` therefore takes the command away
                # from those two routes, and before this they reported it as
                # if a design had forgotten something.
                raise ExperimentSpecError(
                    (
                        f"command {spec.name!r} takes a composed document for "
                        f"{parameter.name!r}, and this caller cannot compose "
                        f"one. Composed documents are supported on the "
                        f"autonomous portfolio's experiment route; declare the "
                        f"parameter as a `path` if it must also be runnable "
                        f"from `researchctl experiment run` or from an "
                        f"objective cycle."
                    )
                    if generated is None
                    else (
                        f"command {spec.name!r} requires a composed document "
                        f"for {parameter.name!r} and none was frozen"
                    )
                )
            # The same containment rule a `path` value faces, applied to a
            # path this system chose. Not because it is suspected -- because
            # a rule that is only applied to untrusted input is a rule nobody
            # is checking.
            resolved[parameter.name] = _validate_path(
                placed[parameter.name],
                where=f"command {spec.name!r} parameter {parameter.name!r}",
                worktree=worktree,
            )
            continue
        if parameter.name in supplied:
            raw = supplied[parameter.name]
        elif parameter.default is not None:
            raw = parameter.default
        elif parameter.required:
            raise ExperimentSpecError(
                f"command {spec.name!r} requires a value for {parameter.name!r}"
            )
        else:
            raise ExperimentSpecError(
                f"command {spec.name!r} parameter {parameter.name!r} has no value "
                "and no default, so the command cannot be built"
            )
        resolved[parameter.name] = _validate_value(
            parameter, raw, command=spec.name, worktree=worktree
        )

    argv: list[str] = []
    for token in spec.argv:
        whole = PLACEHOLDER_RE.fullmatch(token)
        argv.append(resolved[whole.group(1)] if whole is not None else token)

    # The program rule again, on the *substituted* argv.
    #
    # `_argv_is_plain` checks `spec.argv[0]`, which for a command declared as
    # `argv: ["{tool}", ...]` is the placeholder -- it contains no `/` and
    # passes. Substitution then puts a supplied value there, and a final
    # adversarial review pointed out that this defeats the rule the validator
    # exists to state: "what runs is resolved on PATH rather than by a path in
    # a configuration file". A parameter value cannot be allowed to name the
    # program by path, whoever supplied it.
    program = argv[0] if argv else ""
    if "/" in program or program.startswith("-") or not program:
        raise ExperimentSpecError(
            f"command {spec.name!r} resolved to {program!r} as its program. A "
            f"parameter may not decide what binary runs: declare the program "
            f"literally in experiments.yaml and parameterise its arguments"
        )

    return ResolvedCommand(
        name=spec.name,
        argv=tuple(argv),
        parameters=resolved,
        outputs=tuple(spec.outputs),
        timeout_seconds=spec.timeout_seconds,
        executor=spec.executor,
        working_directory=spec.working_directory,
        checks=tuple(spec.checks),
    )


def _validate_value(
    parameter: ParameterSpec,
    raw: Any,
    *,
    command: str,
    worktree: Path | None,
) -> str:
    """Return one supplied value as a safe argv token, or refuse it.

    Refusal rather than repair, throughout. Quoting or escaping a value that
    does not fit its declared type would mean guessing what the caller meant,
    and the caller here may be a model.
    """

    where = f"command {command!r} parameter {parameter.name!r}"
    if isinstance(raw, bool) or parameter.type is ParameterType.FLAG:
        if not isinstance(raw, bool):
            raise ExperimentSpecError(f"{where} is a flag and takes true or false")
        return "true" if raw else "false"

    if parameter.type in {ParameterType.INTEGER, ParameterType.NUMBER}:
        try:
            number = int(raw) if parameter.type is ParameterType.INTEGER else float(raw)
        except (TypeError, ValueError) as exc:
            raise ExperimentSpecError(
                f"{where} takes a {parameter.type}, not {raw!r}"
            ) from exc
        if isinstance(number, float) and not math.isfinite(number):
            # `nan < minimum` and `nan > maximum` are both False, so a NaN
            # satisfies every bound a researcher declared by failing to
            # compare with any of them. An infinity clears one side. This
            # function's contract is that a value is checked against the
            # bounds that were written down, and a value that cannot be
            # compared has not been.
            raise ExperimentSpecError(
                f"{where} takes a finite {parameter.type}, not {raw!r}"
            )
        if parameter.minimum is not None and number < parameter.minimum:
            raise ExperimentSpecError(
                f"{where} must be at least {parameter.minimum}, not {number}"
            )
        if parameter.maximum is not None and number > parameter.maximum:
            raise ExperimentSpecError(
                f"{where} must be at most {parameter.maximum}, not {number}"
            )
        return str(number)

    if parameter.type is ParameterType.GENERATED:  # pragma: no cover - unreachable
        raise ExperimentSpecError(
            f"{where} is a generated parameter and is placed rather than "
            "validated as a value; `resolve_command` handles it before here"
        )

    text = str(raw)
    if parameter.type is ParameterType.CHOICE:
        if text not in parameter.choices:
            raise ExperimentSpecError(
                f"{where} must be one of {', '.join(parameter.choices)}, not {text!r}"
            )
        return text

    if parameter.type is ParameterType.PATH:
        return _validate_path(text, where=where, worktree=worktree)

    if VALUE_TOKEN_RE.fullmatch(text) is None:
        raise ExperimentSpecError(
            f"{where} is not a plain token: {text!r}. Experiment parameters are "
            "placed into an argument vector as whole arguments, so a value "
            "carrying whitespace, quoting, or shell syntax is refused rather "
            "than escaped"
        )
    _assert_not_a_flag(text, where)
    return text


def _assert_not_a_flag(value: str, where: str) -> None:
    """A parameter value may not start with ``-``.

    Without this a value stays inside its argv slot and escapes its *type*:
    the declared program sees an option where the researcher declared data.
    A security review of this branch showed `--config=/home/u/.ssh/id_rsa`
    passing as a ``path`` -- the containment test compares
    ``worktree / value``, which for a value with no leading slash is a
    relative join and therefore always inside the worktree, whatever the
    value means to the program that receives it.

    Numbers do not come through here: `_validate_value` returns
    ``str(number)`` before either check, so a negative number is unaffected.
    """

    if value.startswith("-"):
        raise ExperimentSpecError(
            f"{where} must not begin with '-': {value!r} would reach the "
            f"declared command as an option rather than as the value the "
            f"researcher declared"
        )


def _validate_path(value: str, *, where: str, worktree: Path | None) -> str:
    """Return a path value that stays inside the worktree, or refuse it.

    Checked structurally and then, when a worktree is known, checked again
    against where it actually resolves. Both are needed: the structural rule
    catches ``..`` and absolute paths, and the resolution catches a path that
    follows a symlink out of the tree.
    """

    try:
        _assert_relative(value, where)
    except ValueError as exc:
        raise ExperimentSpecError(str(exc)) from exc
    _assert_not_a_flag(value, where)
    if worktree is None:
        return value
    root = worktree.resolve()
    try:
        resolved = (root / value).resolve()
    except (OSError, RuntimeError) as exc:
        raise ExperimentSpecError(f"{where} is not a usable path: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise ExperimentSpecError(
            f"{where} resolves to {resolved}, outside the experiment worktree "
            f"{root}. An experiment reads and writes inside its own checkout."
        )
    return value


def _assert_relative(value: str, where: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a plain in-tree relative path.

    ``ValueError`` rather than a Research OS error because this runs inside
    pydantic validators as well as at resolution time. A validator that raised a
    domain exception would make ``model_validate`` raise two different things
    depending on which rule failed, and a caller cannot reasonably handle that.
    The resolution path translates it.
    """

    if not value.strip():
        raise ValueError(f"{where} must not be empty")
    if value.startswith(("/", "~")):
        raise ValueError(f"{where} must be relative, not {value!r}")
    if "\\" in value:
        raise ValueError(f"{where} must use POSIX '/' separators")
    segments = [item for item in value.split("/") if item]
    if not segments or any(item in {".", ".."} for item in segments):
        raise ValueError(f"{where} must not contain '.' or '..' segments: {value!r}")
    if any(character in value for character in "\n\r\t\x00"):
        raise ValueError(f"{where} must not contain control characters")

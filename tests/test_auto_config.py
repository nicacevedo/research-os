"""Tests for automation role/budget configuration and role resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_os.automation.config import (
    DEFAULT_ALLOWED_CHECK_PROGRAMS,
    config_path,
    default_config,
    load_config,
    resolve_roles,
)
from research_os.automation.models import (
    Independence,
    ProviderProbe,
    RoleSetting,
)
from research_os.errors import AutomationError, ProviderUnavailableError
from research_os.paths import config_home


def probe(name: str, family: str, *, available: bool = True) -> ProviderProbe:
    return ProviderProbe(name=name, family=family, available=available)


def test_the_config_lives_under_the_config_home(automation_home: Path) -> None:
    assert config_path() == config_home() / "automation.yaml"


def test_defaults_apply_when_no_file_exists(automation_home: Path) -> None:
    config = load_config()

    assert config.source is None
    assert config.explicit_roles == frozenset()
    assert config.allowed_check_programs == DEFAULT_ALLOWED_CHECK_PROGRAMS
    assert config.role("planner").read_only is True
    assert config.role("reviewer").read_only is True
    assert config.role("coder").read_only is False
    assert config.role("coder").tools == ["Read", "Write", "Edit", "Glob", "Grep"]
    assert "Bash" not in config.role("coder").tools


def test_the_shipped_defaults_do_not_review_with_the_implementing_model() -> None:
    config = default_config()
    assert config.role("coder").model != config.role("reviewer").model


def test_a_config_file_overrides_only_what_it_names(automation_home: Path) -> None:
    config_path().write_text(
        "reviewer:\n"
        "  provider: codex\n"
        "  model: some-model\n"
        "  read_only: true\n"
        "budget:\n"
        "  max_model_calls: 3\n",
        encoding="utf-8",
    )
    config = load_config()

    assert config.source == config_path()
    assert config.explicit_roles == frozenset({"reviewer"})
    assert config.role("reviewer").provider == "codex"
    assert config.role("planner").provider == "claude"
    assert config.budget.max_model_calls == 3
    assert config.budget.max_write_work_orders == 2


def test_an_empty_config_file_is_the_defaults(automation_home: Path) -> None:
    config_path().write_text("\n", encoding="utf-8")
    assert load_config().role("planner").provider == "claude"


def test_invalid_yaml_is_a_clean_error(automation_home: Path) -> None:
    config_path().write_text("planner: [unclosed\n", encoding="utf-8")
    with pytest.raises(AutomationError, match="invalid YAML"):
        load_config()


def test_a_non_mapping_config_is_refused(automation_home: Path) -> None:
    config_path().write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(AutomationError, match="must contain a mapping"):
        load_config()


def test_an_unknown_config_key_is_refused(automation_home: Path) -> None:
    config_path().write_text("supervisor:\n  provider: claude\n", encoding="utf-8")
    with pytest.raises(AutomationError, match="invalid automation config"):
        load_config()


def test_a_named_config_file_must_exist(automation_home: Path, tmp_path: Path) -> None:
    with pytest.raises(AutomationError, match="no automation config"):
        load_config(tmp_path / "nowhere.yaml")


def test_no_available_provider_is_refused(automation_home: Path) -> None:
    probes = {"claude": probe("claude", "anthropic", available=False)}
    with pytest.raises(ProviderUnavailableError, match="no locally verified"):
        resolve_roles(default_config(), probes)


def test_one_family_is_degraded_independence(automation_home: Path) -> None:
    probes = {
        "claude": probe("claude", "anthropic"),
        "codex": probe("codex", "openai", available=False),
    }
    resolved = resolve_roles(default_config(), probes)

    assert resolved.independence is Independence.DEGRADED_SAME_PROVIDER_FAMILY
    assert resolved.degraded is True
    assert "NOT an independent review" in resolved.note
    assert resolved.roles["coder"].provider == "claude"
    assert resolved.roles["reviewer"].provider == "claude"


def test_a_second_family_makes_the_review_independent(automation_home: Path) -> None:
    probes = {
        "claude": probe("claude", "anthropic"),
        "codex": probe("codex", "openai"),
    }
    resolved = resolve_roles(default_config(), probes)

    assert resolved.roles["coder"].provider == "claude"
    assert resolved.roles["reviewer"].provider == "codex"
    assert resolved.independence is Independence.INDEPENDENT_PROVIDER_FAMILY
    assert resolved.degraded is False
    assert any(
        "implementer's own model family" in item for item in resolved.substitutions
    )


def test_an_unavailable_provider_is_substituted_and_reported(
    automation_home: Path,
) -> None:
    config_path().write_text(
        "coder:\n  provider: codex\n  model: pinned\n  read_only: false\n",
        encoding="utf-8",
    )
    probes = {
        "claude": probe("claude", "anthropic"),
        "codex": probe("codex", "openai", available=False),
    }
    resolved = resolve_roles(load_config(), probes)

    assert resolved.roles["coder"].provider == "claude"
    assert resolved.roles["coder"].model is None
    assert any("codex is unavailable" in item for item in resolved.substitutions)


def test_an_explicit_reviewer_is_not_moved(automation_home: Path) -> None:
    config_path().write_text(
        "reviewer:\n  provider: claude\n  model: sonnet\n  read_only: true\n",
        encoding="utf-8",
    )
    probes = {
        "claude": probe("claude", "anthropic"),
        "codex": probe("codex", "openai"),
    }
    resolved = resolve_roles(load_config(), probes)

    assert resolved.roles["reviewer"].provider == "claude"
    assert resolved.independence is Independence.DEGRADED_SAME_PROVIDER_FAMILY


# -- audit regression: a read-only role may not carry tools ------------------


@pytest.mark.parametrize("tools", [["Write"], ["Bash"], ["Write", "Bash"], ["Read"]])
def test_a_read_only_role_with_tools_is_refused(
    automation_home: Path, tools: list[str]
) -> None:
    """Fail closed rather than silently correcting a contradictory config.

    A read-only role that declares ``Write`` or ``Bash`` does not express a
    preference the controller can quietly tighten; it expresses two
    incompatible intentions, and the researcher should be told which one the
    file got wrong.
    """

    target = config_home() / "automation.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"planner:\n  provider: claude\n  read_only: true\n  tools: {tools}\n",
        encoding="utf-8",
    )

    with pytest.raises(AutomationError, match="read_only role must declare no tools"):
        load_config(target)


def test_a_read_only_role_setting_cannot_be_constructed_with_tools() -> None:
    with pytest.raises(ValidationError, match="read_only role must declare no tools"):
        RoleSetting(provider="claude", read_only=True, tools=["Write"])

    RoleSetting(provider="claude", read_only=True, tools=[])
    RoleSetting(provider="claude", read_only=False, tools=["Write"])


def test_the_reviewer_may_not_be_given_tools_either(automation_home: Path) -> None:
    target = config_home() / "automation.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "reviewer:\n  provider: claude\n  read_only: true\n  tools: [Bash]\n",
        encoding="utf-8",
    )

    with pytest.raises(AutomationError, match="read_only role must declare no tools"):
        load_config(target)


# -- audit regression: configuration may only narrow the command grammar -----


def test_the_default_check_programs_exclude_interpreters_and_git() -> None:
    assert DEFAULT_ALLOWED_CHECK_PROGRAMS == ("uv", "pytest", "ruff")
    for program in ("python", "python3", "git", "bash", "sh"):
        assert program not in DEFAULT_ALLOWED_CHECK_PROGRAMS


@pytest.mark.parametrize("program", ["python", "python3", "git", "bash", "curl"])
def test_a_config_cannot_widen_the_command_grammar(
    automation_home: Path, program: str
) -> None:
    """Adding a program to the config does not create a grammar for it."""

    target = config_home() / "automation.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"allowed_check_programs: [pytest, {program}]\n", encoding="utf-8"
    )

    with pytest.raises(AutomationError, match="no grammar for"):
        load_config(target)


def test_a_config_may_narrow_the_command_grammar(automation_home: Path) -> None:
    target = config_home() / "automation.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("allowed_check_programs: [pytest]\n", encoding="utf-8")

    assert load_config(target).allowed_check_programs == ("pytest",)

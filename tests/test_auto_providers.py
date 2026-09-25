"""Tests for provider discovery and the Claude Code adapter contract.

These never invoke a real CLI. The adapter's argument vector and its parsing of
the provider result envelope are both contracts worth pinning, so the subprocess
boundary is replaced and everything either side of it is exercised.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Access, Role
from research_os.automation.providers import (
    ClaudeCodeProvider,
    InvocationRequest,
    MissingProvider,
    default_registry,
    probe_registry,
    provider_family,
)


def request(**updates: Any) -> InvocationRequest:
    data: dict[str, Any] = {
        "role": Role.PLANNER,
        "prompt": "plan this",
        "cwd": Path("/tmp"),
        "read_only": True,
        "timeout_seconds": 60,
        "model": "sonnet",
    }
    data.update(updates)
    return InvocationRequest(**data)


def envelope(**updates: Any) -> str:
    payload: dict[str, Any] = {
        "result": "done",
        "is_error": False,
        "session_id": "abc",
        "total_cost_usd": 0.25,
        # Shaped like a real envelope, which is the whole point of the
        # numbers. The first version of this fixture had `input_tokens` and
        # `output_tokens` and nothing else, so it agreed with a parser that
        # read only `input_tokens` -- and the two shared the assumption that a
        # usage block has one input field. A real one splits input across
        # three, and the first dogfood recorded `tokens_in = 4` for a call that
        # returned nearly fourteen thousand output tokens.
        "usage": {
            "input_tokens": 11,
            "cache_creation_input_tokens": 120,
            "cache_read_input_tokens": 4_300,
            "output_tokens": 7,
            "output_tokens_details": {"thinking_tokens": 0},
            "service_tier": "standard",
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"outputTokens": 2},
            "claude-sonnet-5": {"outputTokens": 7},
        },
        "permission_denials": [],
    }
    payload.update(updates)
    return json.dumps(payload)


class Recorder:
    """Stands in for ``subprocess.run`` and records what it was asked to do."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.argv = argv
        self.kwargs = kwargs
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, kwargs.get("stderr_text", "")
        )


def test_families_are_named_per_provider() -> None:
    assert provider_family("claude") == "anthropic"
    assert provider_family("codex") == "openai"
    assert provider_family("gemini") == "google"
    assert provider_family("something-else") == "something-else"


def test_a_read_only_invocation_is_given_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(request())

    argv = recorder.argv
    assert argv[0] == "claude"
    assert "--print" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--restricted" in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert recorder.kwargs["input"] == "plan this"
    assert recorder.kwargs["cwd"] == "/tmp"
    assert recorder.kwargs["timeout"] == 60


def test_a_write_invocation_accepts_edits_with_a_named_tool_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(
        request(
            role=Role.CODER,
            read_only=False,
            model="opus",
            tools=("Read", "Write", "Edit"),
            cwd=Path("/tmp/worktree"),
        )
    )

    argv = recorder.argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--tools") + 1] == "Read,Write,Edit"
    assert "Bash" not in argv[argv.index("--tools") + 1]
    assert recorder.kwargs["cwd"] == "/tmp/worktree"


def test_a_schema_is_passed_through_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    ClaudeCodeProvider().invoke(request(json_schema=schema))

    argv = recorder.argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_effort_is_only_sent_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(request())
    assert "--effort" not in recorder.argv

    ClaudeCodeProvider().invoke(request(effort="high"))
    assert recorder.argv[recorder.argv.index("--effort") + 1] == "high"


def test_the_result_envelope_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Recorder(stdout=envelope(structured_output={"tasks": []})),
    )
    result = ClaudeCodeProvider().invoke(request())

    assert result.ok is True
    assert result.text == "done"
    assert result.structured == {"tasks": []}
    assert result.session_id == "abc"
    assert result.input_tokens == 11 + 120 + 4_300
    assert result.output_tokens == 7
    assert result.total_cost_usd == pytest.approx(0.25)
    assert result.permission_denials == 0
    assert result.resolved_model == "claude-sonnet-5"


def test_input_tokens_served_from_cache_are_still_input_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm prefix must not read as a call that consumed almost nothing.

    The dogfood case exactly: a large prompt whose prefix was cached, so
    ``input_tokens`` is a handful and the other two fields hold the rest.
    Recording the handful makes ``model_calls.tokens_in`` describe the cache
    rather than the call.
    """

    monkeypatch.setattr(
        subprocess,
        "run",
        Recorder(
            stdout=envelope(
                usage={
                    "input_tokens": 4,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 31_902,
                    "output_tokens": 13_854,
                }
            )
        ),
    )
    result = ClaudeCodeProvider().invoke(request())

    assert result.input_tokens == 31_906
    assert result.output_tokens == 13_854


def test_a_usage_block_naming_no_input_field_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero and "the provider did not say" must stay different answers."""

    monkeypatch.setattr(
        subprocess, "run", Recorder(stdout=envelope(usage={"output_tokens": 7}))
    )
    result = ClaudeCodeProvider().invoke(request())

    assert result.input_tokens is None
    assert result.output_tokens == 7


def test_an_unreported_cost_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", Recorder(stdout=json.dumps({"result": "hi"}))
    )
    result = ClaudeCodeProvider().invoke(request())

    assert result.total_cost_usd is None
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_non_json_output_is_an_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout="I cannot do that"))
    result = ClaudeCodeProvider().invoke(request())

    assert result.ok is False
    assert result.error is not None
    assert "JSON result envelope" in result.error
    assert result.stdout == "I cannot do that"


def test_a_provider_reported_error_is_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Recorder(stdout=envelope(is_error=True, result="rate limited")),
    )
    result = ClaudeCodeProvider().invoke(request())

    assert result.ok is False
    assert result.error == "rate limited"


def test_a_nonzero_exit_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=envelope(), returncode=2))
    result = ClaudeCodeProvider().invoke(request())

    assert result.ok is False
    assert result.error is not None
    assert "status 2" in result.error


def test_a_timeout_is_reported_without_an_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial")

    monkeypatch.setattr(subprocess, "run", explode)
    result = ClaudeCodeProvider().invoke(request())

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.ok is False
    assert result.stdout == "partial"


def test_a_missing_executable_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", explode)
    result = ClaudeCodeProvider().invoke(request())

    assert result.ok is False
    assert result.error is not None
    assert "not found" in result.error


def test_probing_an_absent_executable_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("research_os.automation.providers.shutil.which", lambda _: None)
    probe = ClaudeCodeProvider().probe()

    assert probe.available is False
    assert probe.noninteractive_verified is False
    assert probe.executable is None
    assert "not on PATH" in probe.detail


def test_probing_verifies_the_flags_the_adapter_depends_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "research_os.automation.providers.shutil.which",
        lambda _: "/fake/bin/claude",
    )

    def capture(argv: list[str], **kwargs: Any) -> Any:
        if argv[1] == "--version":
            out = "9.9.9 (Claude Code)\n"
        elif argv[1] == "--help":
            out = (
                "--print --output-format --json-schema --tools "
                "--restricted --strict-mcp-config --max-budget-usd"
            )
        else:
            out = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", capture)
    probe = ClaudeCodeProvider().probe()

    assert probe.available is True
    assert probe.noninteractive_verified is True
    assert probe.version == "9.9.9 (Claude Code)"
    assert probe.auth_status == "authenticated (claude.ai)"


def test_a_cli_missing_a_required_flag_is_not_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "research_os.automation.providers.shutil.which",
        lambda _: "/fake/bin/claude",
    )

    def capture(argv: list[str], **kwargs: Any) -> Any:
        out = "--print --output-format" if argv[1] == "--help" else "1.0"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", capture)
    probe = ClaudeCodeProvider().probe()

    assert probe.available is False
    assert "--json-schema" in probe.detail


def test_an_unimplemented_provider_is_listed_but_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("research_os.automation.providers.shutil.which", lambda _: None)
    provider = MissingProvider(name="codex")
    probe = provider.probe()

    assert provider.family == "openai"
    assert probe.available is False
    assert "not installed" in probe.detail
    with pytest.raises(NotImplementedError):
        provider.invoke(request())


def test_the_registry_lists_every_known_provider() -> None:
    registry = default_registry()

    assert set(registry) == {"claude", "codex", "gemini"}
    assert isinstance(registry["claude"], ClaudeCodeProvider)


def test_probing_the_registry_returns_one_probe_per_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("research_os.automation.providers.shutil.which", lambda _: None)
    probes = probe_registry()

    assert set(probes) == {"claude", "codex", "gemini"}
    assert all(probe.available is False for probe in probes.values())


# -- audit regression: read-only invocations have no tools, whatever is asked -


def test_a_read_only_request_drops_tools_it_was_constructed_with() -> None:
    """The second, independent enforcement of the read-only invariant.

    Configuration already refuses a read-only role with tools. This is what
    holds if some future caller assembles a request directly.
    """

    built = InvocationRequest(
        role=Role.PLANNER,
        prompt="plan",
        cwd=Path("/tmp"),
        read_only=True,
        timeout_seconds=60,
        tools=("Write", "Bash"),
    )

    assert built.tools == ()


def test_a_write_request_keeps_the_tools_it_was_given() -> None:
    built = InvocationRequest(
        role=Role.CODER,
        prompt="code",
        cwd=Path("/tmp/worktree"),
        read_only=False,
        timeout_seconds=60,
        tools=("Read", "Write", "Edit"),
    )

    assert built.tools == ("Read", "Write", "Edit")


def test_a_read_only_invocation_names_no_tool_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(request(tools=("Write", "Bash")))

    argv = recorder.argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "Write" not in argv
    assert "Bash" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"


# -- adjacent hardening: no ambient MCP tooling -----------------------------


def test_the_invocation_does_not_inherit_ambient_mcp_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--restricted`` alone still loads the machine's MCP configuration.

    The local ``--help`` for ``--restricted`` says to add ``--strict-mcp-config``
    to skip MCP servers too, so both flags are passed and no ``--mcp-config`` is
    supplied, leaving the worker with exactly the tools named by ``--tools``.
    """

    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(request())

    argv = recorder.argv
    assert "--restricted" in argv
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_a_write_invocation_is_equally_strict_about_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(stdout=envelope())
    monkeypatch.setattr(subprocess, "run", recorder)
    ClaudeCodeProvider().invoke(
        request(role=Role.CODER, read_only=False, tools=("Read", "Write"))
    )

    argv = recorder.argv
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Write"


def test_a_cli_without_strict_mcp_config_is_not_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is part of the capability the adapter depends on, not a bonus."""

    monkeypatch.setattr(
        "research_os.automation.providers.shutil.which",
        lambda _: "/fake/bin/claude",
    )

    def capture(argv: list[str], **kwargs: Any) -> Any:
        out = (
            "--print --output-format --json-schema --tools --restricted"
            if argv[1] == "--help"
            else "1.0"
        )
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", capture)
    probe = ClaudeCodeProvider().probe()

    assert probe.available is False
    assert "--strict-mcp-config" in probe.detail


def test_an_isolated_write_worker_cannot_be_given_a_command_tool() -> None:
    """Found by an independent reviewer: this position had no allowlist.

    The other two access positions enforced one; isolated_write did not, so a
    configuration naming ``Bash`` would have been honoured. A worker that can
    run a command is not confined by worktree isolation in any useful sense --
    it can reach every path the user can.
    """

    with pytest.raises(ValueError, match="not confined by worktree isolation"):
        InvocationRequest(
            role=Role.CODER,
            prompt="do it",
            cwd=Path("/tmp"),
            read_only=False,
            timeout_seconds=60,
            access=Access.ISOLATED_WRITE,
            tools=("Read", "Write", "Bash"),
        )


def test_an_isolated_write_worker_keeps_its_file_tools() -> None:
    request = InvocationRequest(
        role=Role.CODER,
        prompt="do it",
        cwd=Path("/tmp"),
        read_only=False,
        timeout_seconds=60,
        access=Access.ISOLATED_WRITE,
        tools=("Read", "Write", "Edit", "Glob", "Grep"),
    )
    assert "Write" in request.tools

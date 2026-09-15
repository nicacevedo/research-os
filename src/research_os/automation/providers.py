"""Provider adapters for locally installed, locally authenticated agent CLIs.

The adapter contract is two methods: ``probe`` establishes what this machine can
actually do, and ``invoke`` runs one bounded worker. Everything about a provider
here was verified by inspecting the installed executable, never recalled from
memory: an adapter exists only for an interface that was read out of the local
``--help``.

Read-only roles are given no tools at all. That is a stronger guarantee than
asking a model not to write: with an empty tool set there is nothing for it to
write with, so a planner or reviewer cannot touch the repository it is reasoning
about even if its prompt is subverted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from research_os.automation.models import (
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    Access,
    ProviderProbe,
    Role,
)

PROVIDER_FAMILIES: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}

KNOWN_PROVIDERS: tuple[str, ...] = ("claude", "codex", "gemini")

PROBE_TIMEOUT_SECONDS = 30


def provider_family(name: str) -> str:
    """Return the model family a provider belongs to."""

    return PROVIDER_FAMILIES.get(name, name)


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """One bounded worker invocation the controller is about to make.

    The effective tool set is decided here, from ``access``, and not taken on
    trust from the caller. The configuration layer already refuses a role whose
    declared reach and tools disagree; this is the second, independent
    enforcement, placed where every adapter must pass through it, so no future
    caller can assemble an invocation that hands a model more than its position
    allows.

    A context-only request is silently emptied, because "no tools" is the whole
    of that position and there is nothing to report. A snapshot-read request
    carrying a tool that could write or run a command is refused outright: that
    is not an over-specified preference, it is an attempt to give a read-only
    worker authority, and it must fail loudly rather than be trimmed.
    """

    role: Role
    prompt: str
    cwd: Path
    read_only: bool
    timeout_seconds: int
    model: str | None = None
    effort: str | None = None
    access: Access | None = None
    tools: tuple[str, ...] = ()
    json_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.access is None:
            derived = Access.CONTEXT_ONLY if self.read_only else Access.ISOLATED_WRITE
            object.__setattr__(self, "access", derived)
        if self.access is Access.CONTEXT_ONLY:
            if not self.read_only:
                raise ValueError("a context_only invocation must be read-only")
            object.__setattr__(self, "tools", ())
            return
        if self.access is Access.SNAPSHOT_READ:
            if not self.read_only:
                raise ValueError("a snapshot_read invocation must be read-only")
            forbidden = [item for item in self.tools if item not in READ_ONLY_TOOLS]
            if forbidden:
                raise ValueError(
                    "a snapshot_read invocation may only carry the read-only "
                    f"tools {', '.join(sorted(READ_ONLY_TOOLS))}, but this one "
                    f"carries {', '.join(forbidden)}"
                )
            if not self.tools:
                raise ValueError(
                    "a snapshot_read invocation needs at least one read-only "
                    "tool to read the snapshot with"
                )
            return
        if self.read_only:
            raise ValueError("an isolated_write invocation must not be read-only")
        forbidden = [item for item in self.tools if item not in WRITE_TOOLS]
        if forbidden:
            raise ValueError(
                "an isolated_write invocation may only carry the file tools "
                f"{', '.join(sorted(WRITE_TOOLS))}, but this one carries "
                f"{', '.join(forbidden)}. A worker that can run a command is "
                "not confined by worktree isolation: the controller runs "
                "acceptance commands itself, through a closed grammar."
            )


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """What the controller observed from one worker invocation."""

    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    text: str | None = None
    structured: dict[str, Any] | None = None
    resolved_model: str | None = None
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    permission_denials: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0


class ProviderAdapter(Protocol):
    """The whole provider contract: say what you can do, then do one thing."""

    name: str
    family: str

    def probe(self) -> ProviderProbe: ...

    def invoke(self, request: InvocationRequest) -> InvocationResult: ...


@dataclass
class ClaudeCodeProvider:
    """Adapter for the locally installed ``claude`` CLI.

    Every flag used here was read from ``claude --help`` on this machine:
    ``-p`` for non-interactive output, ``--output-format json`` for a parseable
    result envelope, ``--json-schema`` for validated structured output,
    ``--tools`` to bound the tool set, ``--restricted`` to confine file tools to
    the working directory and remove command-running tools,
    ``--strict-mcp-config`` so the run uses only MCP servers named on this
    command line -- and none are -- instead of whatever this machine happens to
    have configured, and ``--permission-prompts none`` so anything that would
    ask a human is denied instead of hanging.

    The local ``--help`` for ``--restricted`` says in as many words to add
    ``--strict-mcp-config`` to skip MCP servers too, so restricted mode on its
    own would still have inherited them.
    """

    name: str = "claude"
    family: str = "anthropic"
    executable: str = "claude"

    def probe(self) -> ProviderProbe:
        path = shutil.which(self.executable)
        if path is None:
            return ProviderProbe(
                name=self.name,
                family=self.family,
                available=False,
                detail=f"{self.executable} is not on PATH",
            )
        version = self._first_line(self._capture([path, "--version"]))
        help_text = self._capture([path, "--help"]) or ""
        required = (
            "--print",
            "--output-format",
            "--json-schema",
            "--tools",
            "--restricted",
            "--strict-mcp-config",
        )
        missing = [flag for flag in required if flag not in help_text]
        auth = self._auth_status(path)
        detail = "non-interactive print mode verified from local --help"
        if missing:
            detail = f"local --help does not document {', '.join(missing)}"
        return ProviderProbe(
            name=self.name,
            family=self.family,
            executable=path,
            available=not missing,
            noninteractive_verified=not missing,
            version=version,
            auth_status=auth,
            detail=detail,
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        argv = self._build_argv(request)
        try:
            completed = subprocess.run(
                argv,
                cwd=str(request.cwd),
                check=False,
                capture_output=True,
                text=True,
                input=request.prompt,
                timeout=request.timeout_seconds,
            )
        except FileNotFoundError as exc:
            return InvocationResult(
                argv=tuple(argv),
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error=f"provider executable not found: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            return InvocationResult(
                argv=tuple(argv),
                exit_code=None,
                timed_out=True,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                error=f"provider timed out after {request.timeout_seconds}s",
            )
        return self._parse(argv, completed, request)

    def _build_argv(self, request: InvocationRequest) -> list[str]:
        argv = [
            self.executable,
            "--print",
            "--output-format",
            "json",
            "--restricted",
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--permission-mode",
            "plan" if request.read_only else "acceptEdits",
            "--tools",
            ",".join(request.tools),
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if request.effort:
            argv.extend(["--effort", request.effort])
        if request.json_schema is not None:
            argv.extend(
                ["--json-schema", json.dumps(request.json_schema, sort_keys=True)]
            )
        return argv

    def _parse(
        self,
        argv: list[str],
        completed: subprocess.CompletedProcess[str],
        request: InvocationRequest,
    ) -> InvocationResult:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        try:
            payload = json.loads(stdout)
        except ValueError:
            return InvocationResult(
                argv=tuple(argv),
                exit_code=completed.returncode,
                timed_out=False,
                stdout=stdout,
                stderr=stderr,
                error="provider did not return a JSON result envelope",
            )
        if not isinstance(payload, dict):
            return InvocationResult(
                argv=tuple(argv),
                exit_code=completed.returncode,
                timed_out=False,
                stdout=stdout,
                stderr=stderr,
                error="provider JSON result was not an object",
            )
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        denials = payload.get("permission_denials")
        error = None
        if payload.get("is_error"):
            error = str(payload.get("result") or payload.get("subtype") or "is_error")
        elif completed.returncode != 0:
            error = f"provider exited with status {completed.returncode}"
        return InvocationResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            timed_out=False,
            stdout=stdout,
            stderr=stderr,
            text=payload.get("result")
            if isinstance(payload.get("result"), str)
            else None,
            structured=(
                payload.get("structured_output")
                if isinstance(payload.get("structured_output"), dict)
                else None
            ),
            resolved_model=self._resolved_model(payload, request.model),
            session_id=(
                payload.get("session_id")
                if isinstance(payload.get("session_id"), str)
                else None
            ),
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            total_cost_usd=_as_float(payload.get("total_cost_usd")),
            permission_denials=len(denials) if isinstance(denials, list) else None,
            error=error,
        )

    @staticmethod
    def _resolved_model(payload: dict[str, Any], requested: str | None) -> str | None:
        """Return the concrete model id the provider reported, if it named one.

        The CLI takes an alias such as ``opus`` and reports the model it
        resolved to. Recording the resolution is the difference between a run
        report that says which model reviewed the diff and one that repeats the
        alias back.

        The one rule that matters: **when the provider said which models ran,
        this never answers with the alias.** Echoing ``opus`` back over a report
        that names only some other model is not a missing detail, it is a run
        record attributing a plan to a model that did not make it -- and a
        default naming one specific model is exactly the configuration where
        that goes wrong quietly. Found by an independent review of the planner
        default, which supplied both shapes: a reply billed only to the
        auxiliary model, and a reply billed to two substantive models neither of
        which was the one asked for.

        So silence is the only thing that falls back to the alias. If usage was
        reported at all, the answer comes from it: the matching id where there
        is one, the single substantive id where there is one, and otherwise
        every id the provider named, joined -- which reads oddly in a report
        precisely because something odd happened, and is checkable against the
        archived envelope beside it.
        """

        usage = payload.get("modelUsage")
        if not isinstance(usage, dict) or not usage:
            return requested
        if requested:
            for key in sorted(usage):
                if key.startswith(f"claude-{requested}"):
                    return key
        candidates = [
            key for key in sorted(usage) if not key.startswith("claude-haiku")
        ]
        reported = candidates or sorted(usage)
        if len(reported) == 1:
            return reported[0]
        return "+".join(reported)

    def _auth_status(self, path: str) -> str:
        """Report whether the CLI is signed in, without revealing credentials."""

        raw = self._capture([path, "auth", "status"])
        if raw is None:
            return "unknown"
        try:
            payload = json.loads(raw)
        except ValueError:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        if payload.get("loggedIn") is True:
            method = payload.get("authMethod")
            return f"authenticated ({method})" if method else "authenticated"
        return "not authenticated"

    @staticmethod
    def _capture(argv: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0 and not completed.stdout.strip():
            return None
        return completed.stdout

    @staticmethod
    def _first_line(text: str | None) -> str | None:
        if not text:
            return None
        line = text.strip().splitlines()[0] if text.strip() else ""
        return line or None


@dataclass
class MissingProvider:
    """Placeholder for a provider CLI that is not installed on this machine.

    Kept in the registry rather than omitted so ``auto providers`` can report
    every provider Research OS knows how to use, including the absent ones.
    """

    name: str
    family: str = field(default="")

    def __post_init__(self) -> None:
        if not self.family:
            self.family = provider_family(self.name)

    def probe(self) -> ProviderProbe:
        path = shutil.which(self.name)
        return ProviderProbe(
            name=self.name,
            family=self.family,
            executable=path,
            available=False,
            detail=(
                f"{self.name} is on PATH but Research OS has no locally verified "
                "adapter for it"
                if path
                else f"{self.name} is not installed on this machine"
            ),
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        raise NotImplementedError(f"no adapter is implemented for {self.name}")


def default_registry() -> dict[str, ProviderAdapter]:
    """Return every provider adapter Research OS knows about."""

    registry: dict[str, ProviderAdapter] = {"claude": ClaudeCodeProvider()}
    for name in KNOWN_PROVIDERS:
        if name not in registry:
            registry[name] = MissingProvider(name=name)
    return registry


def probe_registry(
    registry: dict[str, ProviderAdapter] | None = None,
) -> dict[str, ProviderProbe]:
    """Probe every known provider and return the results by name."""

    adapters = registry if registry is not None else default_registry()
    return {name: adapter.probe() for name, adapter in sorted(adapters.items())}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""

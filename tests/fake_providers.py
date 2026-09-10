"""Deterministic fake provider adapters.

The automation tests must never require a real Claude or Codex installation, so
every test drives the controller through these. They implement the same two-method
adapter contract as the real ones and additionally assert the boundary the
controller is supposed to enforce: a fake asked to write during a read-only
invocation fails the test rather than quietly writing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.models import ProviderProbe, Role
from research_os.automation.providers import InvocationRequest, InvocationResult


@dataclass
class ScriptedResponse:
    """One canned worker response, optionally with side effects on disk."""

    structured: dict | None = None
    text: str | None = None
    exit_code: int | None = 0
    timed_out: bool = False
    error: str | None = None
    stderr: str = ""
    write_files: dict[str, str] = field(default_factory=dict)
    delete_files: tuple[str, ...] = ()
    total_cost_usd: float | None = 0.01
    input_tokens: int | None = 100
    output_tokens: int | None = 20


@dataclass
class FakeProvider:
    """A scripted provider. Records every request it was given."""

    name: str = "fake"
    family: str = "fake-family"
    available: bool = True
    responses: dict[str, list[ScriptedResponse]] = field(default_factory=dict)
    calls: list[InvocationRequest] = field(default_factory=list)

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            name=self.name,
            family=self.family,
            executable=f"/fake/bin/{self.name}" if self.available else None,
            available=self.available,
            noninteractive_verified=self.available,
            version="fake-1.0",
            auth_status="authenticated (fake)" if self.available else "unknown",
            detail="scripted test double",
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.calls.append(request)
        response = self._next(request.role)
        if response.write_files or response.delete_files:
            if request.read_only:
                raise AssertionError(
                    f"a read-only {request.role} invocation attempted to write"
                )
            self._apply(request.cwd, response)
        stdout = json.dumps(
            {
                "result": response.text,
                "structured_output": response.structured,
                "is_error": bool(response.error),
            }
        )
        return InvocationResult(
            argv=(self.name, "--fake"),
            exit_code=response.exit_code,
            timed_out=response.timed_out,
            stdout=stdout,
            stderr=response.stderr,
            text=response.text,
            structured=response.structured,
            resolved_model=request.model,
            session_id="fake-session",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_cost_usd=response.total_cost_usd,
            permission_denials=0,
            error=response.error,
        )

    def requests_for(self, role: Role) -> list[InvocationRequest]:
        return [item for item in self.calls if item.role is role]

    def _next(self, role: Role) -> ScriptedResponse:
        queue = self.responses.get(str(role))
        if not queue:
            raise AssertionError(f"{self.name} has no scripted response for {role}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    @staticmethod
    def _apply(cwd: Path, response: ScriptedResponse) -> None:
        for relative, content in response.write_files.items():
            target = cwd / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for relative in response.delete_files:
            (cwd / relative).unlink(missing_ok=True)


@dataclass
class UnavailableProvider:
    """A provider CLI that is not installed."""

    name: str = "absent"
    family: str = "absent-family"

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            name=self.name,
            family=self.family,
            available=False,
            detail="not installed in this test",
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        raise AssertionError("an unavailable provider must never be invoked")

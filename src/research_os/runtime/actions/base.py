"""What every action handler returns, and the one distinction it must get right.

:class:`ActionOutcome` has both ``ok`` and ``failure_class``, and the
relationship between them is the thing to understand:

- ``ok=True`` means the action did what it was asked to do. That includes an
  experiment that ran correctly and **refuted** its hypothesis. The code worked,
  the data were valid, the answer was no. ``failure_class`` is ``None``.
- ``ok=False`` with a ``failure_class`` means something malfunctioned, and the
  class decides whether it is retried, repaired, rescheduled or escalated.

There is no third option, and that is deliberate: a handler cannot express "the
science came out negative so this failed", because
:class:`~research_os.runtime.failures.FailureClass` has no member for it. A
system that retried refutations until they stopped refuting would be a machine
for manufacturing positive results, and this is where that would have to start.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ArtifactRef


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """The result of one action.

    ``data`` is small and structured -- the thing the next node reasons about.
    Anything large is in ``artifacts`` as a reference, because ``data`` ends up
    in a checkpoint.
    """

    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    failure_class: FailureClass | None = None

    def __post_init__(self) -> None:
        if self.ok and self.failure_class is not None:
            raise ValueError(
                "an outcome that succeeded has no failure class. A scientifically "
                "negative result is a success; see actions/base.py."
            )
        if not self.ok and self.failure_class is None:
            raise ValueError(
                "an outcome that failed must name its failure class, so the retry "
                "policy has something to decide from"
            )

    @classmethod
    def succeeded(
        cls,
        detail: str,
        *,
        data: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ActionOutcome:
        return cls(ok=True, detail=detail, data=data or {}, artifacts=artifacts)

    @classmethod
    def failed(
        cls,
        detail: str,
        *,
        failure_class: FailureClass,
        data: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ActionOutcome:
        return cls(
            ok=False,
            detail=detail,
            data=data or {},
            artifacts=artifacts,
            failure_class=failure_class,
        )


class ActionHandler(Protocol):
    """One action, performed once.

    Called from inside the idempotency ledger, so a handler does **not** need to
    be idempotent itself -- it needs to be *correct when called once*. The
    ledger is what guarantees it is.
    """

    def __call__(
        self,
        state: Mapping[str, Any],
        context: CycleContext,
        plan: Mapping[str, Any],
    ) -> ActionOutcome: ...


Handler = Callable[[Mapping[str, Any], CycleContext, Mapping[str, Any]], ActionOutcome]

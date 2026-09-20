"""``researchd``'s composition root.

One job: import the layers this process should be able to run, then hand over
to the control plane. It exists because ``research_os.runtime.daemon`` must not
import ``research_os.portfolio`` -- the portfolio wraps the runtime, and the
direction is asserted by ``tests/test_runtime_layering.py`` so that deleting
the discovery layer leaves a runtime that still migrates and still runs an
objective cycle.

Somebody has to import it, though, or the daemon's extension registry is empty
and a ``portfolio_tick`` work item has no handler. That somebody is a
composition root, and there are exactly two: ``research_os.cli`` for
``researchctl``, and this for ``researchd``.

The alternative was a lazy import inside the daemon, which would have passed
nothing: the layering test parses the whole module with ``ast.walk``, and a
function-local import is still an import.
"""

from __future__ import annotations

# Imported for the side effect, which is the point of this module. The
# portfolio's ``extensions`` module registers its work kinds with
# ``research_os.runtime.extensions`` at import time.
from research_os.portfolio import extensions as _portfolio_extensions  # noqa: F401
from research_os.runtime.daemon import main as _daemon_main


def main(argv: list[str] | None = None) -> None:
    """Run the control plane with every layer this build ships registered."""

    _daemon_main(argv)


if __name__ == "__main__":  # pragma: no cover - console-script path
    main()

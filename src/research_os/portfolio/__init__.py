"""The autonomous discovery portfolio: the layer above the R5 runtime.

`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` is its live specification.

Nothing in this package writes a capsule file, authors a Review, accepts a
Claim, promotes a proposal or an insight, merges, or pushes.
`tests/test_portfolio_authority.py` asserts that by parsing the package rather
than by trusting this docstring.
"""

from __future__ import annotations

__all__ = ["PORTFOLIO_LAYER_VERSION"]

#: Bumped when a change alters what a stored idea, review or digest *means*
#: rather than how it is computed. Recorded on digests so a bank rendered by an
#: older build is identifiable rather than silently mixed with a newer one.
PORTFOLIO_LAYER_VERSION = 1

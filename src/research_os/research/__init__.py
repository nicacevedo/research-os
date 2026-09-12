"""Unified research orchestration: one goal, one bounded plan, one human handoff.

This is the layer that turns "find out whether X" into the things Research OS
already knows how to do -- retrieve literature, analyse a repository, propose
science, implement code, run a declared experiment, draft a section -- in an
order a deterministic controller chose and a budget it cannot exceed.

It owns no worker. Every task kind maps onto a controller that was built and
tested on its own, so the guarantees those layers make are the guarantees a
research run makes: worktree isolation, scope enforcement from the observed diff,
one bounded repair, and nothing scientific accepted without a person.

What is new here is the stopping. A research run halts at every checkpoint its
plan names, and it halts before anything it was not authorised to spend.
"""

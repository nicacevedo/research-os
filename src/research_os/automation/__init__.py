"""Deterministic automation control plane for Research OS.

The controller in this package is ordinary Python. Models are bounded workers it
invokes; they never drive control flow, never set run state, and never approve
science. Everything this package writes is runtime state under the Research OS
state home, not scientific truth: deleting a run directory loses a provenance
ledger, never a Claim, Review, Evidence, or Experiment.
"""

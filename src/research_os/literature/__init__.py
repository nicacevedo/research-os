"""Reproducible literature and source intelligence.

Deterministic work happens before any model call: retrieval, caching,
normalisation, deduplication, provenance, local text extraction, and BM25
ranking are all ordinary Python and SQLite. A model is used for the one thing
none of that can do -- reading a paper and saying what it argues -- and it is
used read-only, on content that has already been fenced as untrusted data.

Nothing in this package is scientific truth. It is a shared index of what other
people published, held under the Research OS data home, and a researcher can
delete the whole database without losing a single Claim, Review, or Evidence.
"""

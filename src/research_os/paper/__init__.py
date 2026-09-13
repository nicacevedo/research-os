"""Evidence-grounded manuscript drafting.

A paper writer is the worker with the most opportunity to do quiet damage:
everything else produces a diff or a structured proposal, and a manuscript is
prose, where a claim can be strengthened, a contrary result dropped, or a
citation invented without any of it showing up as a change to anything.

So the writer is bounded on both sides. It receives only accepted Claims whose
human approval currently binds their evidence, the Evidence and Experiments
those Claims reach, literature with generated citation keys, and the unresolved
limitations -- never raw paper text. It must return a manifest of what it used,
which is checked against what it was given, and the prose is then checked
against the manifest. Bookkeeping is settled deterministically before any
reviewer is asked for judgement.
"""

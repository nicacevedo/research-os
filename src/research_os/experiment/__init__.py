"""Deterministic experiment execution, locally and on a scheduler.

Nothing in this package involves a model. Which command runs is a name the
researcher declared in configuration that lives outside every worktree; what its
parameters may be is a type they declared; whether it may run at all is an
authorisation they gave. A plan selects a command by name and fills in values,
and every value is checked before it becomes an argument.

What comes out is a *candidate* evidence packet: the command, the commit, the
digest of every file produced, which declared outputs are missing, and which
deterministic checks failed. It carries no verdict about any hypothesis, because
that judgement belongs to a person.
"""

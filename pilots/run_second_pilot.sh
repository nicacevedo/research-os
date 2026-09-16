#!/usr/bin/env bash
# A second pilot, on a structurally different project.
#
#   pilots/run_second_pilot.sh <project-path> "<objective>" [cycles] [autonomy]
#
# Identical machinery to `run_closed_loop.sh` -- it calls it -- and exists as a
# separate entry point so that "we ran the loop twice, on two differently shaped
# frontiers" is a command rather than a claim.
#
# ## Why a second project at all
#
# CCAO's frontier is one contested claim and one open question: nothing
# actionable, nothing pending, no hypothesis to design against. So the only
# useful action there is to propose, which is a narrow exercise of the loop even
# though it is the right answer for that capsule.
#
# A project with a *different* frontier shape -- an actionable hypothesis, code
# to read, a runnable experiment -- reaches different handlers: hypothesis
# critique, experiment design, interpretation selection. That is what makes it
# structurally different in the way that matters here.
#
# ## What it cannot exercise on this host, and why
#
# `edit_in_worktree` and `run_local_experiment` execute code a model wrote or
# chose. At `high` autonomy the runtime *requires* OS-level containment for
# both, and this host cannot provide any (`researchctl runtime doctor` says
# why). So those two paths are refused rather than run, and this pilot uses
# `low` autonomy deliberately rather than lowering the policy to reach them.
#
# Running them uncontained would demonstrate the wrong thing: that the release
# will execute model-written code with the researcher's credentials when the
# containment it declares as required is absent.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    sed -n '2,20p' "$0" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO_ROOT/pilots/run_closed_loop.sh" "$@"

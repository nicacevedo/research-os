#!/usr/bin/env bash
# Run the closed autonomous loop against a real research project, end to end,
# and prove each of its links.
#
#   pilots/run_closed_loop.sh <project-path> "<objective>" [cycles]
#
# What this adds to `run_pilot.sh`. That one demonstrates "launch once, then no
# manual choreography" and *fails if the project moved*, because a read-only
# pilot must not touch anything. This one has to exercise the human scientific
# gate, and a promotion writes a capsule file -- so it works on a **copy** of
# the project's capsule inside the pilot sandbox. The researcher's real
# repository is read once, hashed before and after, and never written.
#
#   phase 1   autonomous cycles -> findings -> a grounded proposal
#             -> WAITING_FOR_SCIENTIFIC_DECISION, with no successor
#   phase 2   the human scientific act, stood in for (see below)
#   phase 3   researchd observes -> CAPSULE_CHANGED, once -> successor cycle
#   phase 4   failure injection: restart, duplicate observation, and a worker
#             killed after the proposal persisted
#
# ## The one thing here that is not the real act
#
# Phase 2 writes a capsule object through the capsule layout, exactly as
# `researchctl propose promote` would. It does **not** invoke that command.
# `AGENTS.md` forbids an automated agent from invoking it or answering its
# confirmation prompt, and the command requires an interactive terminal for
# precisely this reason. So the promotion is a *stand-in for the human act*,
# labelled as one everywhere it appears.
#
# That is honest about what is being demonstrated. The promotion path itself is
# v1 code with its own tests (`tests/test_proposal_promotion.py`); what has
# never been shown is that the runtime *notices* a promotion nobody told it
# about and continues on its own. Phase 3 is the claim, and phase 2 is its
# premise.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    sed -n '2,20p' "$0" >&2
    exit 2
fi

SOURCE_PROJECT="$(cd "$1" && pwd)"
OBJECTIVE="$2"
CYCLES="${3:-2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PILOT="$REPO_ROOT/pilots/runs/closed-loop-$STAMP"

mkdir -p "$PILOT"/{config,data,cache,state}
export RESEARCH_OS_CONFIG_HOME="$PILOT/config"
export RESEARCH_OS_DATA_HOME="$PILOT/data"
export RESEARCH_OS_CACHE_HOME="$PILOT/cache"
export RESEARCH_OS_STATE_HOME="$PILOT/state"
cd "$REPO_ROOT"

ros() { uv run --extra runtime "$@"; }

echo "== closed-loop pilot $STAMP"
echo "   source project $SOURCE_PROJECT   (read only, hashed before and after)"
echo "   objective      $OBJECTIVE"
echo "   sandbox        $PILOT"

# --- the source project is read once, and proven untouched afterwards -------
fingerprint_source() {
    (
        cd "$SOURCE_PROJECT"
        git rev-parse HEAD 2>/dev/null || echo "no-head"
        git status --porcelain
        find .research -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum
    )
}
fingerprint_source > "$PILOT/source-before.txt"

# --- a working copy, because phase 2 writes science -------------------------
WORK="$PILOT/project"
echo "== copying the capsule into the pilot sandbox"
mkdir -p "$WORK"
# `git archive HEAD` only when the source is the *root* of its own work tree.
# From a subdirectory it archives the repository root, so a project vendored
# inside another repository -- which `pilots/fixtures/` is -- would copy the
# whole outer tree into the sandbox, or, when the vendored files are staged and
# not committed, nothing at all. The second is what happened.
SOURCE_ROOT="$(git -C "$SOURCE_PROJECT" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "$SOURCE_ROOT" == "$SOURCE_PROJECT" ]]; then
    git -C "$SOURCE_PROJECT" archive HEAD | tar -x -C "$WORK"
else
    # Ignored build artefacts would otherwise become part of the capsule copy.
    tar -c -C "$SOURCE_PROJECT" \
        --exclude=.git --exclude=__pycache__ --exclude=.venv \
        --exclude=.pytest_cache --exclude=.ruff_cache . | tar -x -C "$WORK"
fi
if [[ ! -d "$WORK/.research" ]]; then
    echo "!! FAIL: the capsule copy is empty; nothing to run a pilot against" >&2
    exit 1
fi
rm -rf "$WORK/.git"
git -C "$WORK" init -q --initial-branch=main
git -C "$WORK" config user.email "pilot@example.invalid"
git -C "$WORK" config user.name "Research OS closed-loop pilot"
git -C "$WORK" add -A
git -C "$WORK" commit -q -m "the project's capsule, as of the source HEAD"
ros researchctl validate-project "$WORK" | tee "$PILOT/validate.txt" | tail -3

# --- the researcher's experiment declarations, if the project ships an example
#
# `experiments.yaml` lives in the config home and never in the repository: a
# worker confined to a worktree must not be able to add a command or widen one.
# A project that wants its declared experiments reachable in a pilot therefore
# ships `experiments.yaml.example`, and this installs it into the *pilot's*
# config home -- which is disposable, and is not the researcher's.
#
# Without this, `design_experiment` has nothing to select and refuses, which is
# the correct behaviour and makes that whole branch of the loop unreachable.
if [[ -f "$SOURCE_PROJECT/experiments.yaml.example" ]]; then
    echo "== installing the project's declared experiments into the pilot config"
    install -m 600 "$SOURCE_PROJECT/experiments.yaml.example" \
        "$PILOT/config/experiments.yaml"
    ros researchctl experiment commands "$WORK" \
        2>&1 | tee "$PILOT/declared-experiments.txt" | head -20 || true
fi

# --- the database -----------------------------------------------------------
if [[ -z "${RESEARCH_OS_RUNTIME_DSN:-}" ]]; then
    echo "== starting a disposable PostgreSQL"
    RESEARCH_OS_RUNTIME_DSN="$(
        ros researchctl runtime dev-db start 2>/dev/null \
            | sed -n "s/^  export RESEARCH_OS_RUNTIME_DSN='\(.*\)'$/\1/p"
    )"
    export RESEARCH_OS_RUNTIME_DSN
fi
ros researchctl runtime migrate
ros researchctl runtime doctor | tee "$PILOT/doctor.txt"

# --- phase 1: autonomous work, up to a proposal -----------------------------
echo
echo "== phase 1: autonomous cycles"
ros researchctl runtime start "$WORK" \
    --objective "$OBJECTIVE" --autonomy low \
    --max-model-calls 12 --max-cost-usd 6 | tee "$PILOT/start.txt"

PASSES=$(( CYCLES * 4 + 4 ))
ros researchd --max-ticks "$PASSES" --log-level INFO \
    2>&1 | tee "$PILOT/phase1.log" | tail -8

ros researchctl runtime runs --json > "$PILOT/runs-phase1.json"
ros researchctl runtime findings --json > "$PILOT/findings.json"
ros researchctl propose list | tee "$PILOT/proposals-phase1.txt" || true

# --- phase 2: the human scientific act, stood in for ------------------------
echo
echo "== phase 2: the human scientific act (STAND-IN -- see this script's header)"
# `|| true` on the pipeline, and the exit code read from PIPESTATUS below.
#
# `set -o pipefail` makes a pipeline return its rightmost non-zero status, so
# the "phase 1 produced no proposal" exit of 3 aborted the whole script under
# `set -e` -- and that is a *legitimate planner decision*, not a harness error.
# It happened: the planner chose `propose_capsule_change`, the worker's output
# failed validation, the action failed correctly, and the pilot died at phase 2
# with no verdict instead of reporting what had occurred. A harness that a
# legal outcome kills is a harness that only reports the outcome it expected.
set +e
ros python - "$WORK" "$PILOT" <<'PY' | tee "$PILOT/phase2.txt"
"""Promote one proposed item the way a person would, without being one.

Writes through the capsule layout, exactly as `researchctl propose promote`
does, and prints what it wrote. It does not call that command: an automated
agent must not, and the command requires an interactive terminal.

If the cycle produced no proposal there is nothing to promote, and that is
reported rather than worked around -- a pilot that invented a capsule object to
keep going would be demonstrating nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

work = Path(sys.argv[1])
pilot = Path(sys.argv[2])

from research_os.proposal.basis import basis_status
from research_os.proposal.promote import prepare_promotion, write_promotion
from research_os.proposal.store import ProposalStore

ids = ProposalStore.list_proposal_ids()
if not ids:
    print("NO PROPOSAL: phase 1 produced none, so there is nothing to promote")
    raise SystemExit(3)

store = ProposalStore.open(ids[-1])
proposal = store.load()
print(f"proposal      {proposal.proposal_id}")
print(f"items         {[item.item_id for item in proposal.items]}")
print(f"grounded in   {proposal.grounding.finding_ids}")
print(f"quoted        {[f.finding_id for f in proposal.supplied_findings]}")

status = basis_status(proposal, project_path=work)
print(f"basis         checkable={status.checkable} fresh={status.fresh}")
print(f"              {status.reason}")

promotable = [
    item
    for item in proposal.items
    if str(item.kind) in {"question", "hypothesis", "experiment", "claim"}
]
if not promotable:
    print("NOTHING PROMOTABLE: every item is an evidence interpretation")
    raise SystemExit(4)

item = promotable[0]
prepared = prepare_promotion(proposal, item.item_id, project_path=work)
record = write_promotion(prepared)
store.record_promotion(record)
print(f"WROTE         {record.object_id} ({record.object_type}/{record.object_status})")
print(f"              {record.written_path}")
(pilot / "promoted.txt").write_text(
    f"{record.object_id} {record.object_type} {record.written_path}\n", encoding="utf-8"
)
PY
PROMOTED=${PIPESTATUS[0]}
set -e

if [[ "$PROMOTED" -ne 0 ]]; then
    echo
    echo "== phases 3 and 4 are unreachable, and this is the pilot's result"
    echo "   phase 2 exited $PROMOTED: no proposed item was promoted, so there is"
    echo "   no canonical scientific change for the runtime to observe. Phase 3"
    echo "   asserts that it observes one; with nothing to observe there is"
    echo "   nothing to assert, and reporting a pass here would be reporting a"
    echo "   property nothing was tested."
    echo
    echo "   Read $PILOT/phase2.txt and the run reports. If phase 1 produced no"
    echo "   proposal at all, the planner made a decision -- check its rationale"
    echo "   in \`researchctl runtime run <id>\` before assuming a defect."
    fingerprint_source > "$PILOT/source-after.txt"
    if ! diff -u "$PILOT/source-before.txt" "$PILOT/source-after.txt" \
            > "$PILOT/source-diff.txt"; then
        echo "!! FAIL: the pilot modified the researcher's real project:" >&2
        cat "$PILOT/source-diff.txt" >&2
        exit 1
    fi
    echo "   the researcher's project is unchanged (verified)"
    exit "$PROMOTED"
fi

git -C "$WORK" add -A
git -C "$WORK" commit -q -m "promote one proposed item (human act, stood in for)" || true

# --- phase 3: the runtime notices, and continues ----------------------------
echo
echo "== phase 3: researchd observes the change and continues"
ros researchd --max-ticks "$PASSES" --log-level INFO \
    2>&1 | tee "$PILOT/phase3.log" | tail -8

# --- phase 4: failure injection ---------------------------------------------
echo
echo "== phase 4: a second daemon, ticking again over the same state"
echo "   (a duplicate observation must not produce a duplicate cycle)"
ros researchd --max-ticks 6 --log-level INFO \
    2>&1 | tee "$PILOT/phase4.log" | tail -5

# --- what happened ----------------------------------------------------------
echo
fingerprint_source > "$PILOT/source-after.txt"
if ! diff -u "$PILOT/source-before.txt" "$PILOT/source-after.txt" \
        > "$PILOT/source-diff.txt"; then
    echo "!! FAIL: the pilot modified the researcher's real project:" >&2
    cat "$PILOT/source-diff.txt" >&2
    exit 1
fi
echo "== the source project is byte-for-byte unchanged"

ros researchctl runtime status    | tee "$PILOT/status.txt"
ros researchctl runtime runs      | tee "$PILOT/runs.txt"
ros researchctl runtime findings  | tee "$PILOT/findings.txt"
ros researchctl runtime costs     | tee "$PILOT/costs.txt"
ros researchctl runtime events    | tee "$PILOT/events.txt"
ros researchctl runtime runs --json     > "$PILOT/runs.json"
ros researchctl runtime events --json   > "$PILOT/events.json"
ros researchctl runtime status --json   > "$PILOT/status.json"

echo
echo "== verifying the loop's claims"
ros python - "$PILOT" <<'PY' | tee "$PILOT/verdict.txt"
"""Assert what a closed loop must be true of, from the durable record alone."""

from __future__ import annotations

import json
import sys
from pathlib import Path

pilot = Path(sys.argv[1])
runs = json.loads((pilot / "runs.json").read_text(encoding="utf-8"))
events = json.loads((pilot / "events.json").read_text(encoding="utf-8"))

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


changed = [e for e in events if e["kind"] == "CAPSULE_CHANGED"]
check(
    "CAPSULE_CHANGED exactly once",
    len(changed) == 1,
    f"{len(changed)} event(s)",
)

parents = [r["parent_run_id"] for r in runs if r.get("parent_run_id")]
check(
    "no run has two successors",
    len(parents) == len(set(parents)),
    f"parents {parents}",
)

objectives = {r["objective"] for r in runs}
check("one objective", len(objectives) == 1, f"{objectives}")

check(
    "a successor cycle exists",
    any(r.get("parent_run_id") for r in runs),
    f"{len(runs)} run(s)",
)

digests = [r.get("frontier_digest") for r in runs if r.get("frontier_digest")]
check(
    "the frontier changed between cycles",
    len(set(digests)) > 1,
    f"{[d[:12] if d else None for d in digests]}",
)

terminal = [r["terminal_state"] for r in runs]
check(
    "every cycle reached a terminal state",
    all(t for t in terminal),
    f"{terminal}",
)

from research_os.proposal.store import ProposalStore

proposals = ProposalStore.list_proposal_ids()
check("exactly one logical proposal", len(proposals) == 1, f"{proposals}")

if proposals:
    proposal = ProposalStore.open(proposals[0])
    promotions = proposal.promotions()
    check(
        "exactly one canonical promotion",
        len(promotions) == 1,
        f"{[p.object_id for p in promotions]}",
    )
    loaded = proposal.load()
    check(
        "the proposal is grounded in runtime findings",
        bool(loaded.grounding.finding_ids),
        f"{loaded.grounding.finding_ids}",
    )
    check(
        "the findings it cites are quoted in it",
        {f.finding_id for f in loaded.supplied_findings}
        == set(loaded.grounding.finding_ids),
    )
    check(
        "it recorded a scientific basis",
        loaded.scientific_basis is not None,
    )

print()
print("VERDICT: " + ("closed loop demonstrated" if not failures else f"FAILED {failures}"))
raise SystemExit(1 if failures else 0)
PY

echo
echo "== pilot artifacts in $PILOT"

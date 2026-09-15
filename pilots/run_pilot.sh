#!/usr/bin/env bash
# Run one autonomous pilot against a real research project, and prove the
# project was not modified.
#
#   pilots/run_pilot.sh <project-path> "<objective>" [cycles] [autonomy]
#
# Everything the runtime writes goes into a pilot-local XDG root under
# pilots/runs/<stamp>/, so a pilot cannot touch the researcher's shared
# literature index, artifact store, or runtime state. The project repository is
# read; its `.research/` tree and its Git HEAD are hashed before and after and
# compared, and the script fails if either moved.
#
# Autonomy defaults to `low`, which grants READ_REPO and NETWORK_READ and
# nothing else. A pilot that is allowed to write a worktree or submit to a
# cluster is a decision for the researcher to make explicitly:
#
#   pilots/run_pilot.sh <path> "<objective>" 3 high
#
set -euo pipefail

if [[ $# -lt 2 ]]; then
    sed -n '2,20p' "$0" >&2
    exit 2
fi

PROJECT="$(cd "$1" && pwd)"
OBJECTIVE="$2"
CYCLES="${3:-2}"
AUTONOMY="${4:-low}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PILOT="$REPO_ROOT/pilots/runs/$STAMP"

mkdir -p "$PILOT"/{config,data,cache,state}
export RESEARCH_OS_CONFIG_HOME="$PILOT/config"
export RESEARCH_OS_DATA_HOME="$PILOT/data"
export RESEARCH_OS_CACHE_HOME="$PILOT/cache"
export RESEARCH_OS_STATE_HOME="$PILOT/state"

cd "$REPO_ROOT"

echo "== pilot $STAMP"
echo "   project   $PROJECT"
echo "   objective $OBJECTIVE"
echo "   autonomy  $AUTONOMY, up to $CYCLES cycle(s)"
echo "   sandbox   $PILOT"

# --- the database -----------------------------------------------------------
if [[ -z "${RESEARCH_OS_RUNTIME_DSN:-}" ]]; then
    echo "== starting a disposable PostgreSQL (no system service, no container)"
    RESEARCH_OS_RUNTIME_DSN="$(
        uv run --extra runtime researchctl runtime dev-db start 2>/dev/null \
            | sed -n "s/^  export RESEARCH_OS_RUNTIME_DSN='\(.*\)'$/\1/p"
    )"
    export RESEARCH_OS_RUNTIME_DSN
fi
uv run --extra runtime researchctl runtime migrate

# --- what the project looks like before -------------------------------------
fingerprint() {
    (
        cd "$PROJECT"
        git rev-parse HEAD 2>/dev/null || echo "no-head"
        git status --porcelain
        find .research -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum
    )
}
fingerprint > "$PILOT/project-before.txt"

# --- go ---------------------------------------------------------------------
uv run --extra runtime researchctl runtime doctor | tee "$PILOT/doctor.txt"
uv run --extra runtime researchctl runtime start "$PROJECT" \
    --objective "$OBJECTIVE" --autonomy "$AUTONOMY" | tee "$PILOT/start.txt"

# Each pass claims at most one work item, so a cycle plus its continuation
# needs a few. The daemon is the only thing driving this: no step is chosen
# here, and there is no "now run the reviewer" line anywhere in this script.
PASSES=$(( CYCLES * 4 + 4 ))
echo "== researchd, $PASSES passes"
uv run --extra runtime researchd --max-ticks "$PASSES" --log-level INFO \
    2>&1 | tee "$PILOT/researchd.log" | tail -5

# --- what the project looks like after --------------------------------------
fingerprint > "$PILOT/project-after.txt"
if ! diff -u "$PILOT/project-before.txt" "$PILOT/project-after.txt" > "$PILOT/project-diff.txt"; then
    echo "!! FAIL: the pilot modified the project repository:" >&2
    cat "$PILOT/project-diff.txt" >&2
    exit 1
fi
echo "== the project repository is byte-for-byte unchanged"

# --- what happened ----------------------------------------------------------
uv run --extra runtime researchctl runtime status      | tee "$PILOT/status.txt"
uv run --extra runtime researchctl runtime runs        | tee "$PILOT/runs.txt"
uv run --extra runtime researchctl runtime costs       | tee "$PILOT/costs.txt"
uv run --extra runtime researchctl runtime approvals   | tee "$PILOT/approvals.txt"
uv run --extra runtime researchctl runtime status --json > "$PILOT/status.json"

echo
echo "== pilot artifacts in $PILOT"

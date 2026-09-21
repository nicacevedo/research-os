# The dogfood this build has not had, and how to run it

`docs/AUTONOMOUS_DISCOVERY_REPORT.md` §O records that the sparse-regression
dogfood was not performed and calls it the largest gap between what was built
and what is proven. This document is the other half of that sentence: why it
was not performed in the session that built the layer, what has to be true
before it is, and the exact bounded procedure, so that running it is a
decision and twenty minutes rather than a project.

Nothing here is a recommendation to run it. It is a recommendation that if it
is run, it is run this way.

---

## 1. Why it was not run here

Not caution in the abstract. Three specific facts about this machine, checked
at the time rather than assumed.

**The credential is shared, and it is an OAuth token rather than an API key.**
There is no `ANTHROPIC_API_KEY` in the environment and no `runtime.yaml` on
this host. Model calls go through the `claude` CLI adapter, which authenticates
from `~/.claude/.credentials.json` — the same file every interactive session
uses, and the same file `researchd` uses. The failure of 2026-09-19 that
`ROADMAP.md` records was an OAuth *refresh collision* between exactly these
consumers.

**The researcher's control plane is live, and it is working on the dogfood
target.** `researchd` has been up since 15:27 against
`~/.local/state/research-os/devdb`, and the project it is advancing is
`cg-sparse-regression` — which is
`/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection`,
the project §39 of the brief designates for the dogfood. They are the same
project.

**Its retry budget is already partly spent.** At 15:27 three runs were
rescheduled after `provider_unavailable`, each at attempt 1 of 5:

```text
RRUN-20260919T183618Z-81822ca9
RRUN-20260919T183649Z-abf8b7e3
RRUN-20260919T183750Z-131ff22a
```

A portfolio dogfood would put a third consumer on that token. The consequence
is now bounded — `dc6cfe3` is precisely the commit that stopped a provider
outage from being able to impersonate a scientific conclusion, so a collision
today fails loudly and reschedules instead of concluding falsely — but bounded
is not free. Those three runs have four attempts left between them and a
stranded objective leaves the frontier permanently.

So the thing that makes the dogfood unsafe *right now* is not the dogfood. It
is running it concurrently with the researcher's live pilot on one credential.
That is avoidable, and §2 is how.

## 2. The precondition

One condition, and it is not negotiable, because everything else in this
document assumes it:

> **The researcher's `researchd` is not running, or is running against a
> project the dogfood does not touch and has no runs in provider backoff.**

Checked with:

```bash
systemctl --user is-active researchd
researchctl runtime status
```

Stopping `researchd` is an operational act on live research and is the
researcher's to take, not an agent's. If it is stopped for this, it is started
again afterwards.

Two further conditions worth checking rather than assuming:

```bash
researchctl runtime doctor      # schema 0024, every policy action has a handler,
                                # and what it says about review independence
git -C <project> status         # clean: the Curator refuses a dirty tree
```

`runtime doctor` will say that this host has one provider family. That is
expected and is not a blocker for a dogfood — it is a blocker for calling any
of the resulting reviews independent. See §5.

## 3. Isolation

Sections 3 and 4 below have been **run**, on 2026-09-20, against a clone of
the real project, up to but not including the first model call: the cluster,
the migrations, the registration, the budget, the seed, `portfolio enable`,
and six deterministic ticks. They work, and finding that out cost two real
defects (`AUTONOMOUS_DISCOVERY_REPORT.md` §N.7 and §N.8). What has never run
is everything from the first provider call onwards, which is §5.

The dogfood runs against a **clone**, not the researcher's working copy. §39
of the brief says the human branch must not be modified; a clone makes that
structural instead of policy, and costs nothing.

```bash
SRC=/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection
DOG=$HOME/.local/state/research-os-dogfood
rm -rf "$DOG" && mkdir -p "$DOG"
git clone --no-hardlinks "$SRC" "$DOG/project"
git -C "$DOG/project" checkout --detach HEAD
```

`--no-hardlinks` so nothing the clone does can reach the original's object
store, and a detached HEAD so no branch of the researcher's is checked out and
therefore none can be advanced.

The clone carries the real `.research/` capsule, which is the point: on
2026-09-20 that was `CHARTER.md` plus 27 objects — 11 questions, 7 hypotheses,
4 assumptions, 2 decisions, 2 evidence records and 1 experiment. Those are
what the explorers read, and a synthetic project would test the plumbing and
none of the science.

The operational database is separate too — the portfolio schema is not in the
researcher's cluster and must not be added to it for this:

```bash
export RESEARCH_OS_CONFIG_HOME=$DOG/xdg/config
export RESEARCH_OS_DATA_HOME=$DOG/xdg/data
export RESEARCH_OS_CACHE_HOME=$DOG/xdg/cache
export RESEARCH_OS_STATE_HOME=$DOG/xdg/state
unset RESEARCH_OS_RUNTIME_DSN

researchctl runtime dev-db start        # a cluster under the redirected state home
export RESEARCH_OS_RUNTIME_DSN='<the DSN it prints>'
researchctl runtime migrate             # 24 migrations, head 0024
```

The four `*_HOME` variables must be exported in *every* shell that touches
this, including the one `researchd` runs in. `devdb` puts its cluster under
the state home, so redirecting them is what makes this a second cluster
rather than the researcher's.

## 4. The bounded run

```bash
researchctl register-project "$DOG/project"      # prints the project id
PROJ='<the id it printed>'

researchctl runtime budget "$PROJ" --max-cost-usd 15.00
researchctl seed add "$PROJ" --text "<one direction, in the researcher's words>"
researchctl portfolio enable "$PROJ"
researchd --log-level INFO
```

Three ceilings are already in force without being set, and they are the ones
that matter more than the wall-clock:

```text
idea_spend_ceiling_usd      8.00   per idea, checked before each stage
lineage_spend_ceiling_usd  40.00   per lineage family
max_active_tracks              8   concurrent idea tracks
```

`--max-cost-usd 15.00` above is deliberately *below* one lineage ceiling, so
the project ceiling binds first and the run stops as
`PAUSED_BUDGET_EXHAUSTED` with everything it produced intact rather than at
whatever the per-idea arithmetic happens to allow. Raise it once, on purpose,
if the run is worth continuing.

A first dogfood wants roughly two to four hours and to be watched, not
twenty-four and unattended. The soak in §41 of the brief is a different
exercise and comes after this one has told you the prompts work at all.

Stop it with `Ctrl-C`, or:

```bash
researchctl portfolio pause "$PROJ" --reason "enough for a first look"
```

Pausing leaves the daemon up and allocates nothing further; in-flight stages
finish.

## 5. What to read, and what would count

```bash
researchctl portfolio status "$PROJ"
researchctl ideas list "$PROJ"
researchctl ideas rejected "$PROJ"     # the important one
researchctl portfolio top "$PROJ"
researchctl portfolio digest "$PROJ"
git -C "$DOG/project" show research-os/autonomous --stat
```

The questions this answers, in the order they matter:

**Does the falsifier kill weak ideas cheaply?** Read `ideas rejected` and the
reason on each. If the cheap stages are killing things for reasons a
researcher would agree with, the economics of the whole layer work. If
everything survives to the expensive stages, they do not, and no amount of
downstream quality makes up for it.

**Is `duplicate_similarity: 0.72` right?** It is a starting value and
`config.py` says so. Two genuinely different directions that collided means
it is too low; the same idea appearing three times means it is too high. This
is the one number the dogfood exists to calibrate.

**Do the fourteen prompts produce anything?** None of them has met a real
model. A prompt that returns plausible, well-formed, useless output passes
every test in this branch, and only reading the output finds it.

**Does `novelty_min_sources: 3` stop everything?** The only route that can
reach `VALIDATED` on this build is `novelty_or_literature`, and it needs three
distinct retrieved sources. If retrieval cannot reach three on this project,
every idea stops below `VALIDATED` for an infrastructure reason wearing a
scientific-sounding message.

**What is a pass?** Not "an idea reached `HUMAN_READY`". A dogfood that
produced ten ideas, killed eight for good reasons, and left two the researcher
finds worth an hour is a pass. A dogfood that produced one `HUMAN_READY` idea
that is subtly wrong is a *failure*, and a worse one than producing nothing.

**And what it cannot tell you.** Every review board on this host is one model
three times. The system labels that rather than hiding it — the gate note, the
digest, `researchctl ideas` and every page of the bank all carry it — but no
result from this dogfood may be described as independently reviewed. That
needs a second provider family, which is an install and not a code change.

## 6. Afterwards

Nothing in the clone is scientific state and none of it should be moved into
the researcher's capsule by copying. An idea worth keeping becomes a proposal
the researcher promotes, by hand, through `researchctl propose promote` —
which no automated agent may invoke, and which is the boundary this whole
layer is built around.

```bash
researchctl runtime dev-db stop
# and then check it actually stopped, because it may not have:
ps -eo pid,args | grep "[p]ostgres -D .*$DOG"
# if it is still there, shut it down properly before deleting its data:
#   <venv>/lib/python3.12/site-packages/pgserver/pginstall/bin/pg_ctl \
#       -D "$DOG/xdg/state/devdb/cluster" -m fast stop

systemctl --user start researchd        # if it was stopped for this
rm -rf "$DOG"                           # when the findings are written down
```

**That check is there because the stop did not stop it.** Running this
procedure on 2026-09-20, `researchctl runtime dev-db stop` printed *stopped
the cluster at ...* and the postmaster was still running half an hour later.
This machine currently has around 130 `postgres` processes from `pytest`
clusters two to five days old, which looks like the same thing. The likely
cause is that `pgserver`'s cleanup is reference-counted and a `dev-db start`
whose shell exited without cleaning up leaves a reference nothing can
decrement — but that is an inference, and the observation is the part worth
acting on. It is a pre-existing kernel behaviour, outside the work package
that produced this document, and it is recorded here rather than fixed.

Record what was found in `docs/AUTONOMOUS_DISCOVERY_REPORT.md` §O, replacing
"**Not performed.**" with what happened — including, especially, if it went
badly. The report's five-word vocabulary exists for this: a completed dogfood
moves the claims it touches from *integration-tested* to *dogfood-proven*, and
moves nothing else.

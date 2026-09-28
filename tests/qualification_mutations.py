"""Mutation testing for the v1 qualification's purchase record (preflight B1).

Q07 and Q26 read ``idea_actions.utility``: a stage the allocator bought, with
the utility it was bought at. Each mutant below breaks one link of the chain
that writes it -- the tick puts the utility in the work item, the stage path
reads it back from that item, and the action records it -- or invents a
purchase that did not happen. The runner is the integrity harness's: it
applies the mutant, runs the tests that must notice, restores the file
whatever happens, and reports KILLED, SURVIVED or INVALID.

Not collected by pytest and never run by the suite.

    uv run python -m tests.qualification_mutations            # every mutant
    uv run python -m tests.qualification_mutations QUAL-B1-1  # some of them
    uv run python -m tests.qualification_mutations --list

``tests/test_portfolio_purchase_utility.py`` checks that every mutant here
still applies and names tests that exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

TICK = "src/research_os/portfolio/tick.py"
TRACK = "src/research_os/portfolio/track.py"

U = "tests/test_portfolio_purchase_utility.py"
BOUGHT = f"{U}::test_a_stage_the_allocator_bought_records_the_utility_it_was_bought_at"
RESCORED = f"{U}::test_the_recorded_utility_is_the_purchase_and_not_a_later_rescoring"
RETRY = f"{U}::test_a_retry_that_takes_over_a_dead_attempt_records_the_same_purchase"
UNBOUGHT = f"{U}::test_work_the_allocator_did_not_buy_records_no_utility"

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "QUAL-B1-1",
        "Q07",
        "B1",
        TRACK,
        """                basis_digest=basis,
                utility=utility,
""",
        """                basis_digest=basis,
""",
        (BOUGHT, RETRY),
        "the original defect: the stage path opens the action without the utility",
    ),
    Mutant(
        "QUAL-B1-2",
        "Q07",
        "B1",
        TRACK,
        """    utility = Decimal(str(raw)) if raw is not None else None
""",
        """    utility = None
""",
        (BOUGHT, RESCORED),
        "the stage path ignores the purchase the work item carries",
    ),
    Mutant(
        "QUAL-B1-3",
        "Q07",
        "B1",
        TICK,
        """                "reason": item.reason,
                "utility": str(item.utility),
""",
        """                "reason": item.reason,
""",
        (BOUGHT,),
        "the tick enqueues the purchase without its utility",
    ),
    Mutant(
        "QUAL-B1-4",
        "Q07",
        "B1",
        TRACK,
        """    utility = Decimal(str(raw)) if raw is not None else None
""",
        """    utility = Decimal(str(raw)) if raw is not None else Decimal(0)
""",
        (UNBOUGHT,),
        "work the allocator did not buy is recorded as a purchase",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    chosen = [m for m in MUTANTS if not args.ids or m.id in args.ids]
    if args.list:
        for mutant in chosen:
            print(f"{mutant.id:10} {mutant.invariant:7} {mutant.what}")
        return 0
    results = []
    for mutant in chosen:
        outcome = run(mutant)
        results.append(outcome)
        print(
            f"{outcome['id']:10} {outcome['result']:10} {outcome['detail']}", flush=True
        )
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(item["result"] == "KILLED" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

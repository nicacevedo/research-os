"""Mutation testing for the integrity repairs (INV-01 to INV-10).

Each mutant below re-introduces one of the reproduced defects, or removes one
protection the repair added, as a one-snippet edit of the source. The runner
applies it to the working tree, runs the integrity tests that must notice,
restores the file whatever happens, and reports KILLED (the tests failed, as
they must), SURVIVED (they did not -- a gap), or INVALID (the snippet no
longer matches exactly once, so the mutant must be updated with the code).

Not collected by pytest (no ``test_`` prefix) and never run by the suite:
mutating the tree under a running suite would be the flakiest test there is.

    uv run python -m tests.integrity_mutations            # every mutant
    uv run python -m tests.integrity_mutations MUT-H5-1   # some of them
    uv run python -m tests.integrity_mutations --list

``tests/test_architecture_invariants.py`` checks that every mutant named in
``docs/architecture_invariants.yaml`` exists here and still applies.

Mutants known to be *equivalent* -- killed by no test because another
protection makes them unobservable -- are listed in ``EQUIVALENT`` with the
reason, rather than deleted or hidden.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Mutant:
    id: str
    invariant: str
    finding: str
    file: str
    find: str
    replace: str
    tests: tuple[str, ...]
    what: str


B = "tests/test_integrity_budget_authority.py"
O = "tests/test_integrity_action_ownership.py"
R = "tests/test_integrity_bank_rendering.py"
L = "tests/test_integrity_retrieval_truth.py"
V = "tests/test_integrity_review_events.py"
C = "tests/test_integrity_replication_causality.py"
P = "tests/test_integrity_budget_parks.py"
A = "tests/test_integrity_human_authority.py"
X = "tests/test_integrity_reproductions.py"
L2 = "tests/test_integrity_v2_retrieval_paths.py"
R2 = "tests/test_integrity_v2_replication_provenance.py"
M2 = "tests/test_integrity_v2_migrations.py"
B2 = "tests/test_integrity_v2_budget_caps.py"
V2 = "tests/test_integrity_v2_review_events.py"
P2 = "tests/test_integrity_v2_parks_and_races.py"

MUTANTS: tuple[Mutant, ...] = (
    # ------------------------------------------------------------ INV-01 --
    Mutant(
        "MUT-H5-1",
        "INV-01",
        "H5",
        "src/research_os/runtime/routing.py",
        """            else:
                self._budgets.settle_unknown(cost_grants)
                self._budgets.settle_all(grants)
                spend_note = (
                    f" (no cost was reported;""",
        """            else:
                self._budgets.release_all(cost_grants)
                self._budgets.settle_all(grants)
                spend_note = (
                    f" (no cost was reported;""",
        (
            f"{B}::test_timed_out_calls_do_not_hand_back_their_ceiling_in_any_scope",
            f"{X}::test_h5_timed_out_calls_are_not_each_authorised_the_whole_ceiling_again",
        ),
        "the original defect: a failure with no reported cost releases its reservation",
    ),
    Mutant(
        "MUT-H5-2",
        "INV-01",
        "H5",
        "src/research_os/runtime/routing.py",
        """            else:
                self._budgets.settle_unknown(cost_grants)
                self._budgets.settle_all(grants)
                spend_note = (
                    f"spend unknown;""",
        """            else:
                self._budgets.release_all(cost_grants)
                self._budgets.settle_all(grants)
                spend_note = (
                    f"spend unknown;""",
        (f"{B}::test_an_adapter_that_raises_after_it_was_handed_the_call_is_charged",),
        "an adapter that raised is treated as having spent nothing",
    ),
    Mutant(
        "MUT-H5-3",
        "INV-01",
        "H5",
        "src/research_os/runtime/budgets.py",
        "submitted_at is not null as submitted",
        "false as submitted",
        (f"{B}::test_death_after_submission_is_charged_in_full",),
        "the reconciler releases every stale reservation, submitted or not",
    ),
    Mutant(
        "MUT-H5-4",
        "INV-01",
        "H5",
        "src/research_os/runtime/routing.py",
        "            self._budgets.mark_submitted(\n"
        "                grants + cost_grants, provider_cap=Decimal(str(cap))\n"
        "            )\n",
        "            pass\n",
        (f"{B}::test_death_after_submission_is_charged_in_full",),
        "submission is never recorded, so a crash mid-call looks unsubmitted",
    ),
    Mutant(
        "MUT-H5-5",
        "INV-01",
        "H5",
        "src/research_os/automation/providers.py",
        "                invoked=False,\n",
        "                invoked=True,\n",
        (
            f"{B}::test_the_real_adapter_says_an_unexecutable_cli_was_not_invoked",
            f"{B}::test_a_cli_that_cannot_be_executed_is_released_every_time",
        ),
        "a provider that never started is reported as started",
    ),
    Mutant(
        "MUT-H5-6",
        "INV-01",
        "H5",
        "src/research_os/runtime/spend.py",
        """            self.budgets.settle_unknown(grant.cost)
            self.unknown_usd += grant.reserved_usd
            return""",
        """            self.budgets.release_all(grant.cost)
            return""",
        (f"{B}::test_a_delegated_call_that_timed_out_is_charged_and_the_next_refused",),
        "the delegated wrapper releases a timed-out call",
    ),
    # ------------------------------------------------------------ INV-02 --
    Mutant(
        "MUT-H2-1",
        "INV-02",
        "H2",
        "src/research_os/portfolio/curator.py",
        r"""    collapsed = NEWLINE_MARKER.join(part.strip() for part in text.splitlines()).strip()
    escaped = "".join(
        f"\\{character}" if character in _MARKDOWN_ACTIVE else character
        for character in collapsed
    )
    return terminal_safe(escaped, keep=frozenset())""",
        r"""    collapsed = text
    escaped = "".join(
        f"\\{character}" if character in _MARKDOWN_ACTIVE else character
        for character in collapsed
    )
    return escaped""",
        (
            f"{R}::test_an_untrusted_value_is_one_inert_line",
            f"{X}::test_h2_the_idea_page_cannot_carry_a_forged_reviews_section",
        ),
        "model text keeps its line breaks (both layers that remove them gone)",
    ),
    Mutant(
        "MUT-H2-2",
        "INV-02",
        "H2",
        "src/research_os/portfolio/curator.py",
        '_MARKDOWN_ACTIVE = frozenset("\\\\`*_[]<>|~&#!")',
        "_MARKDOWN_ACTIVE = frozenset()",
        (f"{R}::test_an_untrusted_value_is_one_inert_line",),
        "markup inside a value is not escaped",
    ),
    Mutant(
        "MUT-H2-3",
        "INV-02",
        "H2",
        "src/research_os/portfolio/curator.py",
        """            if line.lstrip(" ").startswith(_STRUCTURAL_STARTS):
                raise CuratorError(""",
        """            if False:
                raise CuratorError(""",
        (f"{R}::test_a_page_refuses_a_value_line_that_begins_like_structure",),
        "the page verifier no longer refuses structure it did not emit",
    ),
    Mutant(
        "MUT-H2-4",
        "INV-02",
        "H2",
        "src/research_os/portfolio/curator.py",
        '            page.field("research question", version.research_question)\n            page.field("why it matters", version.why_it_matters, empty="(not stated)")',
        '            page.text(f"- research question: {version.research_question}")\n            page.field("why it matters", version.why_it_matters, empty="(not stated)")',
        (f"{X}::test_h2_the_human_ready_page_cannot_carry_a_forged_provenance_header",),
        "the original defect: the bank's research question printed raw",
    ),
    # ------------------------------------------------------------ INV-03 --
    Mutant(
        "MUT-H3-1",
        "INV-03",
        "H3",
        "src/research_os/portfolio/track.py",
        "                work_id=work_id,\n                run_id=run.run_id,",
        "                work_id=None,\n                run_id=run.run_id,",
        (
            f"{O}::test_a_live_stage_far_past_the_grace_period_is_not_reclaimed",
            f"{O}::test_a_killed_worker_is_reclaimed_after_its_lease_truly_expires",
        ),
        "the original defect: the work item's id is not recorded on the action",
    ),
    Mutant(
        "MUT-H3-2",
        "INV-03",
        "H3",
        "src/research_os/portfolio/store.py",
        """            if not (probe and probe["free"]):
                return OWNER_LIVE""",
        """            if False:
                return OWNER_LIVE""",
        (f"{O}::test_the_lock_alone_keeps_an_owner_live_whatever_its_age",),
        "a held session lock no longer proves the owner alive",
    ),
    Mutant(
        "MUT-H3-3",
        "INV-03",
        "H3",
        "src/research_os/portfolio/store.py",
        "                   and lease_expires_at > now()\n                   and (%(attempt)s::int is null",
        "                   and (%(attempt)s::int is null",
        (f"{O}::test_a_renewed_lease_is_a_heartbeat_and_a_lapsed_one_is_not",),
        "an expired lease still counts as a live heartbeat",
    ),
    Mutant(
        "MUT-H3-4",
        "INV-03",
        "H3",
        "src/research_os/portfolio/store.py",
        "            if self._fence is not None:\n",
        "            if False:\n",
        (
            f"{O}::test_a_late_result_from_an_attempt_that_lost_ownership_writes_nothing",
            f"{O}::test_a_fenced_store_refuses_every_write_once_its_action_is_closed",
        ),
        "a stage's store is not fenced to its action",
    ),
    Mutant(
        "MUT-H3-5",
        "INV-03",
        "H3",
        "src/research_os/portfolio/store.py",
        "                if self._owner_state(conn, busy) != OWNER_DEAD:",
        "                if False:",
        (
            f"{O}::test_a_second_purchase_of_a_live_stage_starts_nothing",
            f"{O}::test_a_held_lock_refuses_a_takeover_even_with_the_lease_gone",
        ),
        "a live owner's action is taken over",
    ),
    # ------------------------------------------------------------ INV-04 --
    Mutant(
        "MUT-H4-1",
        "INV-04",
        "H4",
        "src/research_os/portfolio/stages.py",
        "    if stage is Stage.META_REVIEW:\n        return [",
        "    if False:\n        return [",
        (
            f"{V}::test_a_meta_review_of_an_unchanged_record_is_not_bought_again",
            f"{X}::test_h4_a_declining_meta_review_is_not_reasked_on_the_same_record",
        ),
        "the original defect: a meta-review's own output is part of its basis",
    ),
    Mutant(
        "MUT-H4-2",
        "INV-04",
        "M3",
        "src/research_os/portfolio/store.py",
        """            if row is not None:
                # The objections, in the same transaction.""",
        """            if prior is not None:
                return IdeaReview.model_validate(
                    conn.execute(
                        f"select {REVIEW_COLUMNS} from idea_reviews where review_id = %s",
                        (prior["review_id"],),
                    ).fetchone()
                ), False
            if row is not None:
                # The objections, in the same transaction.""",
        (f"{V}::test_a_rerun_reviewers_objections_attach_to_its_own_review",),
        "the original defect: a later call on a binding gets the first call's row",
    ),
    Mutant(
        "MUT-H4-3",
        "INV-04",
        "H4",
        "src/research_os/portfolio/runner.py",
        "    if response.call_id is not None and review.call_id != response.call_id:",
        "    if False:",
        (f"{V}::test_a_recommendation_is_not_applied_beside_another_calls_row",),
        "a response is applied beside another call's review row",
    ),
    # ------------------------------------------------------------ INV-05 --
    Mutant(
        "MUT-H1-1",
        "INV-05",
        "H1",
        "src/research_os/portfolio/gates.py",
        "        already |= set(item.result_keys)",
        "        already |= {e.literature_key for e in evidence if e.retrieval_id == item.retrieval_id}",
        (f"{L}::test_a_work_the_first_path_retrieved_but_did_not_cite_is_not_new",),
        "the original defect: 'found before' means cited, not retrieved",
    ),
    Mutant(
        "MUT-H1-2",
        "INV-05",
        "H1",
        "src/research_os/portfolio/gates.py",
        "        if not candidate.action_id or candidate.action_id in first_actions:\n            continue\n",
        "",
        (f"{L}::test_one_execution_cannot_be_both_paths",),
        "one execution may be both paths",
    ),
    Mutant(
        "MUT-H1-3",
        "INV-05",
        "H1",
        "src/research_os/portfolio/gates.py",
        "        if not fresh:\n            continue\n",
        "",
        (
            f"{L}::test_the_same_words_asked_again_are_not_a_different_terminology",
            f"{L2}::test_rev_reordered_identical_terms_are_not_a_distinct_retrieval_path",
        ),
        "a search with no new content term counts as a distinct path",
    ),
    Mutant(
        "MUT-L2-1",
        "INV-05",
        "REORDERED_QUERY",
        "src/research_os/portfolio/digests.py",
        "    return left.startswith(right) or right.startswith(left)\n",
        "    return False\n",
        (f"{L2}::test_a_superficial_variant_of_the_first_path_is_not_a_second_path",),
        "inflections sharing a stem count as new words",
    ),
    Mutant(
        "MUT-L2-2",
        "INV-05",
        "REORDERED_QUERY",
        "src/research_os/portfolio/digests.py",
        "        return token[:-1]\n",
        "        return token\n",
        (
            f"{L2}::test_a_superficial_variant_of_the_first_path_is_not_a_second_path",
            f"{L2}::test_the_term_fingerprint_is_deterministic_and_order_free",
        ),
        "a plural is a new word",
    ),
    Mutant(
        "MUT-L2-3",
        "INV-05",
        "REORDERED_QUERY",
        "src/research_os/portfolio/gates.py",
        "        term for item in first_all for term in pdigests.retrieval_terms(item.query)\n",
        "        term for item in first for term in pdigests.retrieval_terms(item.query)\n",
        (f"{L2}::test_a_failed_first_path_search_still_counts_as_words_already_used",),
        "a first-path search that failed did not use its words",
    ),
    Mutant(
        "MUT-H1-4",
        "INV-05",
        "H1",
        "src/research_os/portfolio/gates.py",
        "            and item.retrieval_id == candidate.retrieval_id\n",
        "",
        (
            f"{L}::test_a_new_work_cited_outside_the_second_search_is_not_its_assessment",
        ),
        "any row citing a new work counts as the second search's assessment",
    ),
    Mutant(
        "MUT-H1-5",
        "INV-05",
        "H1",
        "src/research_os/portfolio/gates.py",
        "        if len(assessed) >= minimum_new_keys:",
        "        if len(new) >= minimum_new_keys:",
        (f"{L}::test_new_works_retrieved_but_never_assessed_are_not_a_path",),
        "new works retrieved but never assessed count",
    ),
    Mutant(
        "MUT-M1-1",
        "INV-05",
        "M1",
        "src/research_os/portfolio/runner.py",
        "            literature_confidence=min(1.0, len(searches) / 6.0),",
        "            literature_confidence=min(1.0, len(audit.queries) / 6.0),",
        (
            f"{L}::test_confidence_and_the_search_count_are_the_executed_searches",
            f"{X}::test_m1_literature_confidence_counts_searches_that_were_run",
        ),
        "the original defect: confidence from the scout's claimed queries",
    ),
    Mutant(
        "MUT-H1-6",
        "INV-05",
        "H1",
        "src/research_os/portfolio/runner.py",
        "            retrieval_id=retrieval.retrieval_id,\n        )\n        recorded += 1",
        "            retrieval_id=None,\n        )\n        recorded += 1",
        (f"{L}::test_every_search_a_stage_runs_is_a_recorded_owned_retrieval",),
        "an audit's rows do not name the search that supplied them",
    ),
    # ------------------------------------------------------------ INV-07 --
    Mutant(
        "MUT-H6-1",
        "INV-07",
        "H6",
        "src/research_os/portfolio/empirical.py",
        "        if finding.independent:\n            analysis = replace(",
        "        if True:\n            analysis = replace(",
        (
            f"{C}::test_without_an_attestation_a_delivered_seed_is_not_a_valid_perturbation",
            f"{X}::test_h6_a_seed_ignoring_rerun_with_a_timestamp_is_not_a_replication",
        ),
        "a replication is accepted whatever its trusted chain established",
    ),
    Mutant(
        "MUT-H6-2",
        "INV-07",
        "FALSE_RECEIPT",
        "src/research_os/portfolio/provenance.py",
        "        perturbation_attested=configuration_independent and bool(attested),\n",
        "        perturbation_attested=configuration_independent,\n",
        (
            f"{C}::test_without_an_attestation_a_delivered_seed_is_not_a_valid_perturbation",
            f"{R2}::test_rev_a_program_echoing_its_ignored_seed_is_not_a_replication",
        ),
        "a delivered variation counts as a valid perturbation with nothing attesting it",
    ),
    Mutant(
        "MUT-H6-3",
        "INV-07",
        "INTEGRATED_MODEL_RECEIPT_FORGERY",
        "src/research_os/portfolio/provenance.py",
        "    configuration_independent = bool(varied) and not not_delivered\n",
        "    configuration_independent = bool(varied)\n",
        (
            f"{R2}::test_a_variation_the_runner_did_not_deliver_is_not_configuration_independence",
        ),
        "a variation the manifest claims counts though the runner never delivered it",
    ),
    Mutant(
        "MUT-H6-4",
        "INV-07",
        "H6",
        "src/research_os/portfolio/empirical.py",
        "        if experiment.role is ExperimentRole.REPLICATION:\n            # Frozen here",
        "        if False:\n            # Frozen here",
        (f"{C}::test_the_manifest_is_frozen_before_the_replication_runs",),
        "no execution manifest is frozen before a replication runs",
    ),
    Mutant(
        "MUT-R2-1",
        "INV-08",
        "LEGACY_REPLICATION",
        "src/research_os/portfolio/gates.py",
        "        candidates = [item for item in candidates if item.evidence_id in admissible]\n",
        "",
        (
            "tests/test_portfolio_gates.py::test_an_executed_replication_row_without_a_trusted_chain_is_not_verification",
            f"{M2}::test_upgraded_0036_replication_evidence_cannot_satisfy_the_current_gate",
        ),
        "the readiness gate counts a replication row without reading its trusted chain",
    ),
    Mutant(
        "MUT-R2-2",
        "INV-08",
        "REPOINTED_MANIFEST",
        "src/research_os/portfolio/models.py",
        "            and self.chain_intact\n",
        "",
        (
            "tests/test_portfolio_gates.py::test_an_executed_replication_row_without_a_trusted_chain_is_not_verification",
        ),
        "a broken chain is admissible",
    ),
    Mutant(
        "MUT-R2-3",
        "INV-06",
        "REPOINTED_MANIFEST",
        "src/research_os/portfolio/provenance.py",
        "    if experiment.execution_manifest_artifact_id != receipt.manifest_artifact_id:\n",
        "    if False:\n",
        (
            f"{R2}::test_rev_a_manifest_cannot_be_repointed_once_its_execution_has_a_result",
        ),
        "the gate's chain does not check the manifest pointer against the receipt",
    ),
    Mutant(
        "MUT-R2-4",
        "INV-06",
        "SWAPPED_WORKSPACE_OUTPUT",
        "src/research_os/portfolio/empirical.py",
        "    changed = provenance.workspace_outputs_match(receipt_document, analysis.outputs)\n    if changed:\n",
        "    changed = provenance.workspace_outputs_match(receipt_document, analysis.outputs)\n    if False:\n",
        (
            f"{R2}::test_an_output_swapped_after_the_execution_is_refused_before_it_is_read",
        ),
        "an output changed after the runner hashed it is read as the result",
    ),
    Mutant(
        "MUT-R2-5",
        "INV-06",
        "REPOINTED_MANIFEST",
        "src/research_os/runtime/sql/0046_trusted_execution_provenance.sql",
        "    if new.execution_manifest_artifact_id is distinct from old.execution_manifest_artifact_id\n",
        "    if false\n",
        (
            f"{R2}::test_rev_a_manifest_cannot_be_repointed_once_its_execution_has_a_result",
        ),
        "the database lets a manifest be repointed after its execution has a result",
    ),
    Mutant(
        "MUT-R2-6",
        "INV-02",
        "WRONG_PARENT_STALE_RECEIPT",
        "src/research_os/runtime/sql/0046_trusted_execution_provenance.sql",
        "    if new.parent_receipt_id is not null and not exists (\n",
        "    if false and not exists (\n",
        (
            f"{R2}::test_rev_a_receipt_of_another_run_or_parent_is_refused_by_the_database",
        ),
        "a receipt may name another idea's execution, or a replication, as its parent",
    ),
    Mutant(
        "MUT-B2-1",
        "INV-01",
        "OBJECTIVE_OVERRUN",
        "src/research_os/runtime/routing.py",
        "                    max_budget_usd=cap,\n",
        "                    max_budget_usd=None,\n",
        (
            f"{B2}::test_rev_objective_call_with_no_declared_ceiling_is_capped",
            f"{B2}::test_rev_lost_settlements_cannot_hide_spend_beyond_any_of_five_ceilings",
        ),
        "the router hands the provider no cap",
    ),
    Mutant(
        "MUT-B2-2",
        "INV-01",
        "DELEGATED_OVERRUN",
        "src/research_os/runtime/spend.py",
        "            result = self.inner.invoke(capped)\n",
        "            result = self.inner.invoke(request)\n",
        (
            f"{B2}::test_rev_delegated_call_reaches_the_provider_capped_at_its_reservation",
        ),
        "a delegated call reaches its provider with the controller's uncapped request",
    ),
    Mutant(
        "MUT-B2-3",
        "INV-01",
        "PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE",
        "src/research_os/runtime/routing.py",
        "                        or enforces_hard_budget_cap(self._adapters[profile.name])\n",
        "                        or True\n",
        (
            f"{B2}::test_the_router_refuses_an_adapter_that_cannot_be_capped",
            f"{B2}::test_a_capped_provider_is_chosen_over_an_uncapped_one_that_could_also_serve",
        ),
        "the router routes to an adapter that cannot be capped",
    ),
    Mutant(
        "MUT-B2-4",
        "INV-01",
        "PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE",
        "src/research_os/runtime/spend.py",
        "        if not enforces_hard_budget_cap(self.inner):\n",
        "        if False:\n",
        (f"{B2}::test_the_delegated_wrapper_refuses_an_adapter_that_cannot_be_capped",),
        "the delegated wrapper spends through an adapter that cannot be capped",
    ),
    Mutant(
        "MUT-B2-5",
        "INV-01",
        "UNCAPPED_LOST_SETTLEMENT",
        "src/research_os/runtime/sql/0043_provider_budget_caps.sql",
        "    if dim = 'model_cost_usd' and new.provider_cap_usd is null then\n",
        "    if false then\n",
        (
            f"{B2}::test_the_database_refuses_an_uncapped_submission_whatever_the_code_does",
        ),
        "the database lets a cost reservation be submitted with no provider cap",
    ),
    Mutant(
        "MUT-B2-6",
        "INV-01",
        "OBJECTIVE_OVERRUN",
        "src/research_os/runtime/routing.py",
        "            else DEFAULT_CALL_CEILING_USD\n",
        "            else Decimal(str(profile.estimated_cost_usd))\n",
        (f"{B2}::test_rev_objective_call_with_no_declared_ceiling_is_capped",),
        "an undeclared call reserves the profile's estimate again",
    ),
    Mutant(
        "MUT-V3-1",
        "INV-04",
        "LOST_REVIEW_OBJECTION",
        "src/research_os/portfolio/store.py",
        "                for ordinal, (draft_severity, target, draft_summary) in enumerate(\n                    drafts\n                ):\n",
        "                for ordinal, (draft_severity, target, draft_summary) in enumerate(\n                    drafts[:1]\n                ):\n",
        (f"{V2}::test_every_objection_of_one_review_is_recorded_in_order",),
        "a review is committed with only some of its objections",
    ),
    Mutant(
        "MUT-V3-2",
        "INV-04",
        "LOST_REVIEW_OBJECTION",
        "src/research_os/runtime/sql/0044_atomic_review_events.sql",
        "    if found_n <> wanted or worst <> portfolio_severity_rank(stated) then\n",
        "    if false then\n",
        (f"{V2}::test_a_review_committed_without_its_objections_is_refused_at_commit",),
        "the database commits a review without its objections",
    ),
    Mutant(
        "MUT-V3-3",
        "INV-08",
        "LOST_REVIEW_OBJECTION",
        "src/research_os/portfolio/store.py",
        "                   and objection_count is not null\n",
        "",
        (
            f"{V2}::test_a_legacy_review_whose_objections_may_be_incomplete_is_never_live",
        ),
        "a legacy review whose objections may be lost counts as live",
    ),
    Mutant(
        "MUT-V3-4",
        "INV-08",
        "FATAL_DEDUPED_AS_MINOR",
        "src/research_os/portfolio/gates.py",
        "            review.objection_count is None\n            or len(own) != review.objection_count\n",
        "            False\n            and len(own) != review.objection_count\n",
        (f"{V2}::test_the_gate_itself_refuses_a_live_review_missing_its_objection",),
        "the gate counts a live review whose objections are not on record",
    ),
    Mutant(
        "MUT-V3-5",
        "INV-04",
        "LOST_REVIEW_OBJECTION",
        "src/research_os/portfolio/store.py",
        "        if Severity(severity) is not worst:\n",
        "        if False:\n",
        (
            f"{V2}::test_a_severity_that_is_not_the_worst_objection_is_refused_before_anything_is_written",
        ),
        "a review whose severity is not its worst objection is written",
    ),
    Mutant(
        "MUT-M4-3",
        "INV-10",
        "LEGACY_BUDGET_PARK",
        "src/research_os/runtime/sql/0045_legacy_park_reason.sql",
        "   set park_reason = 'legacy_unknown'\n",
        "   set park_reason = null\n",
        (f"{M2}::test_legacy_state_fails_closed_after_upgrading_to_the_head",),
        "a legacy park keeps no reason, and says nothing about how it recovers",
    ),
    Mutant(
        "MUT-O2-1",
        "INV-03",
        "SIMULTANEOUS_OWNERS",
        "src/research_os/portfolio/store.py",
        '            if "idea_actions_active_idx" not in str(exc):\n                raise\n',
        "            raise\n",
        (f"{P2}::test_the_scheduler_that_loses_the_insert_race_is_told_it_lost",),
        "the losing scheduler sees a raw database error",
    ),
    # ------------------------------------------------------------ INV-08 --
    Mutant(
        "MUT-M2-1",
        "INV-08",
        "M2",
        "src/research_os/portfolio/track.py",
        '    if state.get("failure_class") and outcome.ok:',
        "    if False:",
        (f"{V}::test_a_board_missing_any_reviewer_is_not_complete_and_says_which",),
        "the original defect: a later reviewer's success overwrites a failure",
    ),
    Mutant(
        "MUT-M2-2",
        "INV-08",
        "M2",
        "src/research_os/portfolio/runner.py",
        "    missing = refreshed.missing_review_roles\n    if missing:\n        return missing\n",
        "    missing = refreshed.missing_review_roles\n",
        (
            f"{V}::test_a_board_missing_any_reviewer_is_not_complete_and_says_which",
            f"{X}::test_m2_a_board_missing_a_reviewer_is_not_a_completed_board",
        ),
        "an incomplete board still resolves objections and moves the idea",
    ),
    Mutant(
        "MUT-M2-3",
        "INV-08",
        "M2",
        "src/research_os/portfolio/gates.py",
        "        elif not all(item.verdict in NON_NEGATIVE_VERDICTS for item in found):",
        "        elif not any(item.verdict in NON_NEGATIVE_VERDICTS for item in found):",
        (f"{V}::test_one_endorsement_does_not_mask_another_reading_of_the_same_role",),
        "one endorsement masks another reading of the same role",
    ),
    # ------------------------------------------------------ INV-10 / M4 --
    Mutant(
        "MUT-M4-1",
        "INV-10",
        "M4",
        "src/research_os/portfolio/tick.py",
        "    report.budget_parks_revived = _revive_budget_parks(store, project_id, config)",
        "    report.budget_parks_revived = 0",
        (
            f"{P}::test_raising_the_idea_ceiling_resumes_the_idea_as_it_was",
            f"{X}::test_m4_raising_the_ceiling_the_record_names_revisits_the_idea",
        ),
        "the original defect: nothing reads a budget park after a ceiling rises",
    ),
    Mutant(
        "MUT-M4-2",
        "INV-10",
        "M4",
        "src/research_os/portfolio/tick.py",
        "        if still is not None:\n            continue\n        applied = store.set_status(",
        "        applied = store.set_status(",
        (f"{P}::test_an_idea_also_blocked_by_the_novelty_floor_stays_parked",),
        "a park blocked for another reason is revived by a ceiling",
    ),
    Mutant(
        "MUT-INV10-1",
        "INV-10",
        "M4",
        "src/research_os/portfolio/tick.py",
        "    parked = [\n        idea\n        for idea in store.list_ideas(",
        "    store.upsert_state(project_id=project_id, bounds={})\n    parked = [\n        idea\n        for idea in store.list_ideas(",
        (f"{A}::test_no_autonomous_module_writes_a_portfolio_bound",),
        "an autonomous module writes a portfolio bound",
    ),
)

#: Mutants that no test can kill because another protection makes them
#: unobservable -- defence in depth, not a gap. Kept, with the argument.
EQUIVALENT: dict[str, str] = {
    "MUT-H2-1 as first written (only the splitlines collapse removed)": (
        "Unobservable: `terminal_safe(..., keep=frozenset())` independently "
        "renders every control and line-separator character -- \\n, \\r, \\v, "
        "\\f, NEL, U+2028, U+2029 -- as a visible escape, so a value is still one "
        "line. The first mutation run reported it SURVIVED (22 passed); it was "
        "replaced by the mutant that removes both layers, which is killed."
    ),
    "second-path condition 3 (result digest)": (
        "Removing `candidate.result_digest in first_results` is unobservable: if "
        "R2's result set equals a first-path result set then every key R2 "
        "retrieved is in `already`, so `new` is empty and condition 4 refuses "
        "it anyway. Condition 3 is stated for the reader and for a future "
        "change to condition 4, not because it can decide a case on its own."
    ),
    "stale_actions without the owner check": (
        "`reclaim_dead_actions` re-checks the owner under the row lock before "
        "closing anything, so an age-only `stale_actions` still reclaims no live "
        "stage; the mutant is killed only in `owner_is_live`-level form "
        "(MUT-H3-2, MUT-H3-3)."
    ),
}


def run(mutant: Mutant) -> dict[str, str]:
    path = ROOT / mutant.file
    original = path.read_text(encoding="utf-8")
    count = original.count(mutant.find)
    if count != 1:
        return {
            "id": mutant.id,
            "result": "INVALID",
            "detail": f"matches {count} times",
        }
    try:
        path.write_text(
            original.replace(mutant.find, mutant.replace, 1), encoding="utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                "-p",
                "no:cacheprovider",
                *mutant.tests,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    finally:
        path.write_text(original, encoding="utf-8")
    tail = (completed.stdout.strip().splitlines() or [""])[-1]
    if completed.returncode == 1:
        result = "KILLED"
    elif completed.returncode == 0:
        result = "SURVIVED"
    else:
        result = f"ERROR({completed.returncode})"
    return {"id": mutant.id, "result": result, "detail": tail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    chosen = [m for m in MUTANTS if not args.ids or m.id in args.ids]
    if args.list:
        for mutant in chosen:
            print(
                f"{mutant.id:12} {mutant.invariant:7} {mutant.finding:4} {mutant.what}"
            )
        return 0
    results = []
    for mutant in chosen:
        outcome = run(mutant)
        results.append(outcome)
        print(
            f"{outcome['id']:12} {outcome['result']:10} {outcome['detail']}", flush=True
        )
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(item["result"] == "KILLED" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

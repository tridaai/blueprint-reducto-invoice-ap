"""The eval pack, run in-process: the gates must hold here too."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_gates_pass_on_the_golden_world():
    runner = _load_runner()
    results = runner.evaluate(runner.build_offline_service())
    assert results["gate_passed"], results["failures"]
    assert results["match_decision_accuracy"] == 1.0
    assert results["fraud_caught"] == results["fraud_cases"] == 1
    assert results["citation_presence"] == 1.0
    assert results["extraction_accuracy_by_field"]["invoice_number"] == 1.0
    assert results["extraction_accuracy_by_field"]["total"] == 1.0
    assert results["cases"] == 32  # 31 submissions, one bundle of two
    recon = results["statement_reconciliation"]
    assert recon["breaks_exact"]
    assert recon["clean_reconciles_fully"]
    assert recon["totals_tie"]
    assert recon["records_untouched"]
    runs = results["payment_runs"]
    assert runs["first_run_nets_and_ties"]
    assert runs["totals_tie"]
    assert runs["refusals_named"]
    assert runs["member_named_with_holding_run"]
    assert runs["return_frees_members"]
    assert runs["export_master_details_only"]
    assert runs["export_byte_stable_and_digested"]
    assert runs["release_locks_members"]
    assert runs["paid_proposed_refused_forever"]
    confirmation = results["remittance_confirmation"]
    assert confirmation["classes_exact"]
    assert confirmation["summary_ties"]
    assert confirmation["unmatched_held"]
    assert confirmation["run_and_export_untouched"]
    credits = results["credit_notes"]
    assert credits["application_nets_invoice"]
    assert credits["full_cover_excludes"]
    assert credits["over_credit_refused"]
    assert credits["references_held"]
    assert credits["late_refused"]
    assert credits["refusals_named"]
    assert credits["export_net"]
    position = results["ap_position"]
    assert position["buckets_exact"]
    assert position["undated_named"]
    assert position["tie_holds"]
    assert position["state_split_sums"]
    assert position["tie_break_named"]
    ledger = results["supplier_ledger"]
    assert ledger["totals_tie_to_position"]
    assert ledger["unmatched_listed_nowhere_else"]
    assert ledger["story_complete"]
    assert ledger["bank_warning_matches_master"]
    partial = results["partial_payments"]
    assert partial["two_run_paydown_ties"]
    assert partial["overcommit_refused_with_figures"]
    assert partial["credit_lands_on_remainder"]
    assert partial["over_credit_judged_on_remainder"]
    assert partial["position_remainder_ages"]
    assert partial["readers_agree_at_the_end"]
    assert partial["confirmation_answers_the_part_line"]
    assert partial["watch_holds_on_cumulative_evidence"]
    coverage = results["po_coverage"]
    assert coverage["cumulative_states_exact"]
    assert coverage["credits_reduce_payments_do_not"]
    assert coverage["unapproved_excluded_unmeasured_listed"]
    assert coverage["record_fact_counts_the_invoice"]
    assert coverage["ranking_worst_first_unmeasured_last"]
    assert coverage["crossing_informs_never_blocks"]
    worklist = results["advice_worklist"]
    assert worklist["candidates_ranked_and_rule_named"]
    assert worklist["suggestions_confirm_nothing"]
    assert worklist["link_settles_lines"]
    assert worklist["near_link_classifies_short"]
    assert worklist["not_ours_closes_the_item"]
    new_supplier = results["new_supplier_hold"]
    assert new_supplier["held_with_evidence"]
    assert new_supplier["gate_and_governance_refusals"]
    assert new_supplier["enrolment_releases_and_judges"]
    assert new_supplier["enrolled_supplier_ordinary"]
    assert new_supplier["refusal_terminal"]

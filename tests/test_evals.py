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

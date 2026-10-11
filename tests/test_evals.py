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
    assert results["cases"] == 22  # 21 submissions, one bundle of two
    recon = results["statement_reconciliation"]
    assert recon["breaks_exact"]
    assert recon["clean_reconciles_fully"]
    assert recon["totals_tie"]
    assert recon["records_untouched"]

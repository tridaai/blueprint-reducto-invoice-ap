"""The paid-duplicate watch: the duplicate that arrives second.

The pipeline's number check owns the co-pending copy. These
tests own the other arrival — the one that comes after the
original was approved, run, and released — and pin:

- the hold: same supplier, same number (normalised the repo's
  way), the arrival's amount equal to the cent to the paid
  invoice's FACE or to the NET figure its released run
  instructed, with the paid evidence on the record;
- the gate's refusal (the evidence named, 409 at the API) and
  the run assembly's named refusal;
- the boundary: an unpaid or merely in-run twin is the number
  rule's case, not the watch's; a different amount is surfaced
  by the number rule, not held; a different number at the same
  amount is a different bill; a fraud-flagged resend is a fraud
  case; the paid invoice's own credits behave as round 5 built
  them;
- the resolutions: confirm refuses permanently, release
  returns the record to its own match state — and a released
  record can then be decided even though its submission's gate
  closed when it was submitted; a second arrival re-holds, so
  a release cannot be laundered by resending;
- the originals: the paid record, its run, and its export are
  byte-identical across every hold and resolution.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import _doc, _inv_fields, build_world
from reducto_ap_agent.errors import GateError
from reducto_ap_agent.schemas import LineItem
from reducto_ap_agent.service import ApService

APPROVER = "Priya Nair (AP lead)"
TREASURER = "Jonas Weber (treasurer)"
CONTROLLER = "Marta Chen (controller)"


def pay_inv1001(service: ApService):
    """INV-1001's whole paid story: approved at its 1400.00
    face, CN-1001 applied against it, released in a run at the
    1200.00 net. Returns the released run."""
    service.decide("REC-SUB-001-0", "approve", APPROVER, "matched")
    service.decide("REC-SUB-007-0", "approve", APPROVER, "matched")
    service.apply_credit("REC-SUB-007-0", CONTROLLER)
    run = service.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0"])
    service.decide_payment_run(run.run_id, "release", TREASURER, "reviewed")
    return service.get_payment_run(run.run_id)


def submit_text(service: ApService, submission_id: str, text: str):
    (record,) = service.submit(
        submission_id, f"{submission_id.lower()}.pdf",
        text.encode("utf-8"), "text/plain",
    )
    return record


def exact_resend(service: ApService, submission_id: str = "SUB-DUP-EXACT"):
    text = next(
        s.text for s in build_world().submissions
        if s.submission_id == "SUB-001"
    )
    return submit_text(service, submission_id, text)


def net_resend(service: ApService, submission_id: str = "SUB-DUP-NET"):
    s02 = build_world().supplier("SUP-02")
    text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1001", "PO-6001", 1200.00,
    ), [LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00)])
    return submit_text(service, submission_id, text)


# -- the hold ----------------------------------------------------


def test_exact_resend_after_release_is_held_with_the_paid_evidence(seeded):
    run = pay_inv1001(seeded)
    resend = exact_resend(seeded)
    assert resend.status == "PAID_DUPLICATE_HOLD"
    hold = resend.duplicate_hold
    assert hold is not None
    assert hold.original_record_id == "REC-SUB-001-0"
    assert hold.invoice_number == "INV-1001"
    assert hold.supplier_name == "Brightpath Components Inc"
    assert hold.payment_run_id == run.run_id
    assert hold.released_by == TREASURER
    assert hold.released_at == run.decision.at
    assert hold.amount_paid == 1200.00  # the run line's instructed figure
    assert hold.paid_face_amount == 1400.00
    assert hold.arrived_amount == 1400.00
    assert hold.amount_basis == "FACE"
    assert hold.currency == "USD"
    assert hold.resolution is None
    events = [a["event"] for a in resend.audit]
    assert "paid_duplicate_hold" in events
    detail = next(a["detail"] for a in resend.audit
                  if a["event"] == "paid_duplicate_hold")
    assert run.run_id in detail and "1200.00" in detail


def test_gate_cannot_decide_a_held_record_and_names_the_evidence(seeded):
    run = pay_inv1001(seeded)
    resend = exact_resend(seeded)
    with pytest.raises(GateError) as excinfo:
        seeded.decide(resend.record_id, "approve", APPROVER, "pay it")
    message = str(excinfo.value)
    assert "paid-duplicate watch" in message
    assert run.run_id in message
    assert "1200.00" in message
    assert "REC-SUB-001-0" in message
    after = seeded.get_record(resend.record_id)
    assert after.status == "PAID_DUPLICATE_HOLD"
    assert after.decision is None


def test_net_resend_is_held_on_the_net_basis(seeded):
    pay_inv1001(seeded)
    resend = net_resend(seeded)
    assert resend.status == "PAID_DUPLICATE_HOLD"
    hold = resend.duplicate_hold
    assert hold.amount_basis == "NET"
    assert hold.arrived_amount == 1200.00
    assert hold.amount_paid == 1200.00
    assert hold.paid_face_amount == 1400.00
    # The re-bill's own match stands beside the hold: against
    # the PO it disagrees by exactly the credit.
    assert resend.match.status == "EXCEPTION"
    assert resend.match.exception_codes == ["TOTAL_MISMATCH"]


def test_number_normalisation_catches_a_case_variant_resend(seeded):
    pay_inv1001(seeded)
    s02 = build_world().supplier("SUP-02")
    fields = _inv_fields(s02, "inv-1001", "PO-6001", 1400.00)
    text = _doc("INVOICE", 1, fields, [
        LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00),
        LineItem(sku="SKU-A2", description="Gasket sets",
                 quantity=5, unit_price=40.00, amount=200.00),
    ])
    resend = submit_text(seeded, "SUB-DUP-CASE", text)
    # The pipeline's raw-string duplicate check cannot see this
    # number as INV-1001's; the watch normalises and holds it.
    assert resend.status == "PAID_DUPLICATE_HOLD"
    assert resend.duplicate_hold.invoice_number == "inv-1001"
    assert "DUPLICATE_INVOICE" not in resend.match.exception_codes


def test_watch_list_carries_evidence_age_and_is_a_pure_read(seeded):
    pay_inv1001(seeded)
    resend = exact_resend(seeded)
    net_resend(seeded)
    before = [r.model_dump_json() for r in seeded.list_records()]
    watch = seeded.list_duplicate_watch("2026-11-15")
    assert [w["record_id"] for w in watch] == [
        resend.record_id, "REC-SUB-DUP-NET-0",
    ]
    entry = watch[0]
    assert entry["duplicate_hold"]["payment_run_id"]
    held_date = resend.duplicate_hold.held_at[:10]
    from datetime import date
    expected_age = (
        date(2026, 11, 15) - date.fromisoformat(held_date)
    ).days
    assert entry["hold_age_days"] == expected_age
    assert [r.model_dump_json() for r in seeded.list_records()] == before


# -- the boundary --------------------------------------------------


def test_unpaid_twin_is_the_number_rules_case_not_the_watchs(seeded):
    # The twin is approved but no run has released it: the
    # co-pending discipline owns the arrival.
    seeded.decide("REC-SUB-001-0", "approve", APPROVER, "matched")
    resend = exact_resend(seeded)
    assert resend.status == "EXCEPTION"
    assert "DUPLICATE_INVOICE" in resend.match.exception_codes
    assert resend.duplicate_hold is None


def test_twin_in_an_open_run_is_not_paid_history_yet(seeded):
    seeded.decide("REC-SUB-001-0", "approve", APPROVER, "matched")
    seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    resend = exact_resend(seeded)
    assert resend.status == "EXCEPTION"
    assert resend.duplicate_hold is None


def test_different_amount_is_surfaced_by_the_number_rule_not_held(seeded):
    pay_inv1001(seeded)
    s02 = build_world().supplier("SUP-02")
    text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1001", "PO-6001", 999.00,
    ), [LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=99.90, amount=999.00)])
    arrival = submit_text(seeded, "SUB-DUP-DIFF", text)
    assert arrival.status == "EXCEPTION"
    assert "DUPLICATE_INVOICE" in arrival.match.exception_codes
    assert arrival.duplicate_hold is None


def test_different_number_at_the_paid_amount_is_a_different_bill(seeded):
    pay_inv1001(seeded)
    s02 = build_world().supplier("SUP-02")
    text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1099", "PO-6001", 1400.00,
    ), [
        LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00),
        LineItem(sku="SKU-A2", description="Gasket sets",
                 quantity=5, unit_price=40.00, amount=200.00),
    ])
    arrival = submit_text(seeded, "SUB-DUP-NEWNUM", text)
    assert arrival.status == "AWAITING_APPROVER"
    assert arrival.duplicate_hold is None


def test_fraud_flagged_resend_is_a_fraud_case_first(seeded):
    pay_inv1001(seeded)
    s02 = build_world().supplier("SUP-02")
    text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1001", "PO-6001", 1400.00,
        bank="US00 ATTK 9999 0000 1111",
    ), [
        LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00),
        LineItem(sku="SKU-A2", description="Gasket sets",
                 quantity=5, unit_price=40.00, amount=200.00),
    ])
    arrival = submit_text(seeded, "SUB-DUP-FRAUD", text)
    assert arrival.status == "FRAUD_REVIEW"
    assert arrival.fraud_flags
    assert arrival.duplicate_hold is None


def test_the_paid_invoices_own_credits_behave_as_built(seeded):
    pay_inv1001(seeded)
    # CN-1003 names INV-1001 and arrives at the gate normally —
    # credit notes are not invoice-class arrivals for the watch.
    seeded.decide("REC-SUB-029-0", "approve", APPROVER, "matched")
    credit = seeded.get_record("REC-SUB-029-0")
    assert credit.duplicate_hold is None
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-029-0", CONTROLLER)
    assert str(excinfo.value).startswith("TARGET_PAID_PROPOSED")


def test_run_assembly_refuses_a_held_record_by_name(seeded):
    run = pay_inv1001(seeded)
    resend = exact_resend(seeded)
    seeded.decide("REC-SUB-017-0", "approve", APPROVER, "matched")
    batch = seeded.create_payment_run(
        "2026-10-23", record_ids=[resend.record_id, "REC-SUB-017-0"])
    (refusal,) = batch.refusals
    assert refusal.record_id == resend.record_id
    assert refusal.code == "PAID_DUPLICATE_HOLD"
    assert run.run_id in refusal.reason
    assert [line.record_id for line in batch.lines] == ["REC-SUB-017-0"]


# -- the resolutions -----------------------------------------------


def test_confirm_refuses_permanently_and_touches_nothing_paid(seeded):
    run = pay_inv1001(seeded)
    original_before = seeded.get_record("REC-SUB-001-0").model_dump_json()
    run_before = seeded.get_payment_run(run.run_id).model_dump_json()
    export_before = seeded.payment_run_export(run.run_id)
    resend = exact_resend(seeded)
    resolved = seeded.resolve_duplicate_hold(
        resend.record_id, "CONFIRMED_DUPLICATE", CONTROLLER,
        "supplier confirmed the original was paid; same bill twice",
    )
    assert resolved.status == "REFUSED"
    assert run.run_id in resolved.refusal_reason
    assert CONTROLLER in resolved.refusal_reason
    resolution = resolved.duplicate_hold.resolution
    assert resolution.action == "CONFIRMED_DUPLICATE"
    assert resolution.actor == CONTROLLER
    assert "same bill twice" in resolution.reason
    assert seeded.list_duplicate_watch("2026-11-15") == []
    # The payment's own records are exactly as released.
    assert seeded.get_record("REC-SUB-001-0").model_dump_json() \
        == original_before
    assert seeded.get_payment_run(run.run_id).model_dump_json() \
        == run_before
    assert seeded.payment_run_export(run.run_id) == export_before


def test_release_returns_the_record_to_its_own_match_state(seeded):
    pay_inv1001(seeded)
    resend = net_resend(seeded)
    resolved = seeded.resolve_duplicate_hold(
        resend.record_id, "RELEASED_NOT_DUPLICATE", CONTROLLER,
        "the supplier's corrected re-bill, net of CN-1001",
    )
    # Not laundered into clean: the re-bill still disagrees
    # with the PO total by the credit, and that break stands.
    assert resolved.status == "EXCEPTION"
    assert resolved.match.exception_codes == ["TOTAL_MISMATCH"]
    assert resolved.duplicate_hold.resolution.action \
        == "RELEASED_NOT_DUPLICATE"


def test_released_clean_resend_is_decidable_after_its_gate_closed(seeded):
    pay_inv1001(seeded)
    resend = exact_resend(seeded)
    seeded.resolve_duplicate_hold(
        resend.record_id, "RELEASED_NOT_DUPLICATE", CONTROLLER,
        "second site billed under the same number in error; this "
        "copy is the plant's own bill, verified with the supplier",
    )
    released = seeded.get_record(resend.record_id)
    assert released.status == "AWAITING_APPROVER"
    # The submission's graph ran to completion when the resend
    # was held — there is no interrupt left to resume. The
    # approver's decision must still land, in the gate's words.
    decided = seeded.decide(
        resend.record_id, "approve", APPROVER, "adjudicated")
    assert decided.status == "APPROVED"
    assert decided.decision.actor == APPROVER
    assert decided.decision.decision == "approve"


def test_second_arrival_reholds_a_release_cannot_be_laundered(seeded):
    pay_inv1001(seeded)
    first = exact_resend(seeded, "SUB-DUP-FIRST")
    seeded.resolve_duplicate_hold(
        first.record_id, "RELEASED_NOT_DUPLICATE", CONTROLLER,
        "adjudicated: not the paid bill",
    )
    second = exact_resend(seeded, "SUB-DUP-SECOND")
    assert second.record_id != first.record_id
    assert second.status == "PAID_DUPLICATE_HOLD"
    assert second.duplicate_hold.resolution is None


def test_resolution_refusals_are_named(seeded):
    pay_inv1001(seeded)
    resend = exact_resend(seeded)
    with pytest.raises(GateError, match="must be 'CONFIRMED_DUPLICATE'"):
        seeded.resolve_duplicate_hold(
            resend.record_id, "dismiss", CONTROLLER, "reason")
    with pytest.raises(GateError, match="must name its controller"):
        seeded.resolve_duplicate_hold(
            resend.record_id, "CONFIRMED_DUPLICATE", " ", "reason")
    with pytest.raises(GateError, match="requires a reason"):
        seeded.resolve_duplicate_hold(
            resend.record_id, "CONFIRMED_DUPLICATE", CONTROLLER, "")
    with pytest.raises(GateError, match="carries no paid-duplicate hold"):
        seeded.resolve_duplicate_hold(
            "REC-SUB-002-0", "CONFIRMED_DUPLICATE", CONTROLLER, "reason")
    seeded.resolve_duplicate_hold(
        resend.record_id, "CONFIRMED_DUPLICATE", CONTROLLER, "paid twice")
    with pytest.raises(GateError) as excinfo:
        seeded.resolve_duplicate_hold(
            resend.record_id, "RELEASED_NOT_DUPLICATE", CONTROLLER,
            "changed my mind")
    message = str(excinfo.value)
    assert "CONFIRMED_DUPLICATE" in message
    assert CONTROLLER in message


# -- the surfaces ----------------------------------------------------


def test_ledger_marks_the_hold_on_the_row_and_the_page(seeded):
    pay_inv1001(seeded)
    resend = exact_resend(seeded)
    index = seeded.supplier_index("2026-11-15")
    brightpath = next(
        e for e in index.suppliers if e.supplier_id == "SUP-02")
    assert brightpath.duplicate_hold_count == 1
    assert all(
        e.duplicate_hold_count == 0 for e in index.suppliers
        if e.supplier_id != "SUP-02"
    )
    ledger = seeded.supplier_ledger("SUP-02", "2026-11-15")
    held = next(
        i for i in ledger.invoices if i.record_id == resend.record_id)
    assert held.status == "PAID_DUPLICATE_HOLD"
    assert held.duplicate_hold.amount_paid == 1200.00
    # Resolution does not erase the marker's evidence: the hold
    # stays on the page, with its ruling.
    seeded.resolve_duplicate_hold(
        resend.record_id, "CONFIRMED_DUPLICATE", CONTROLLER, "same bill")
    index_after = seeded.supplier_index("2026-11-15")
    assert next(
        e for e in index_after.suppliers
        if e.supplier_id == "SUP-02").duplicate_hold_count == 0
    ledger_after = seeded.supplier_ledger("SUP-02", "2026-11-15")
    ruled = next(
        i for i in ledger_after.invoices
        if i.record_id == resend.record_id)
    assert ruled.duplicate_hold.resolution.action == "CONFIRMED_DUPLICATE"


def test_api_watch_gate_refusal_and_resolution(seeded):
    pay_inv1001(seeded)
    resend = exact_resend(seeded)
    client = TestClient(create_app(seeded))
    watch = client.get("/duplicates/watch", params={"as_of": "2026-11-15"})
    assert watch.status_code == 200
    (entry,) = watch.json()
    assert entry["record_id"] == resend.record_id
    assert entry["duplicate_hold"]["amount_basis"] == "FACE"
    assert entry["hold_age_days"] is not None
    refused = client.post(
        f"/records/{resend.record_id}/decision",
        json={"decision": "approve", "actor": APPROVER, "reason": ""},
    )
    assert refused.status_code == 409
    assert "paid-duplicate watch" in refused.json()["detail"]
    no_reason = client.post(
        f"/records/{resend.record_id}/duplicate-resolution",
        json={"action": "CONFIRMED_DUPLICATE", "actor": CONTROLLER,
              "reason": ""},
    )
    assert no_reason.status_code == 409
    resolved = client.post(
        f"/records/{resend.record_id}/duplicate-resolution",
        json={"action": "CONFIRMED_DUPLICATE", "actor": CONTROLLER,
              "reason": "supplier confirmed: same bill, sent twice"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "REFUSED"
    assert client.get(
        "/duplicates/watch", params={"as_of": "2026-11-15"}
    ).json() == []

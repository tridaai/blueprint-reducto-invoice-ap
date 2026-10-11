"""The whole world through the pipeline, and the gate discipline."""

from __future__ import annotations

import pytest

from reducto_ap_agent.errors import GateError


def _record(service, record_id: str):
    return service.get_record(record_id)


def test_every_planted_case_lands_where_gold_says(seeded, world):
    for submission in world.submissions:
        records = [r for r in seeded.list_records() if r.submission_id == submission.submission_id]
        assert len(records) == len(submission.expected)
        for record, gold in zip(records, submission.expected):
            assert record.doc_class == gold.doc_class, record.record_id
            assert record.status == gold.status, record.record_id
            if gold.match_status is not None:
                assert record.match is not None
                assert record.match.status == gold.match_status, record.record_id
                assert sorted(record.match.exception_codes) == sorted(gold.exception_codes), record.record_id


def test_gate_approves_and_the_packet_proposes(seeded):
    record = _record(seeded, "REC-SUB-001-0")
    assert record.status == "AWAITING_APPROVER"
    decided = seeded.decide(record.record_id, "approve", "Priya Nair (AP lead)", "matched")
    assert decided.status == "APPROVED"
    assert decided.decision is not None and decided.decision.actor.startswith("Priya")

    packet = seeded.payment_packet()
    lines = {line.invoice_number: line for line in packet.lines}
    assert lines["INV-1001"].amount == 1400.00
    assert packet.totals_by_currency["USD"] == 1400.00


def test_credit_note_reduces_its_invoice_once_applied(seeded):
    seeded.decide("REC-SUB-001-0", "approve", "Priya Nair (AP lead)")
    seeded.decide("REC-SUB-007-0", "approve", "Priya Nair (AP lead)")

    # Approved but unapplied, the credit has reduced nothing:
    # the invoice still proposes its full face, the credit is
    # no packet line, and it waits on the unapplied list.
    packet = seeded.payment_packet()
    amounts = {line.invoice_number: line.amount for line in packet.lines}
    assert amounts["INV-1001"] == 1400.00
    assert "CN-1001" not in amounts
    (waiting,) = packet.unapplied_credits
    assert waiting.credit_number == "CN-1001"
    assert waiting.invoice_number == "INV-1001"
    assert waiting.amount == 200.00

    applied = seeded.apply_credit("REC-SUB-007-0", "Marta Chen (controller)")
    assert applied.credit_state == "APPLIED"
    assert applied.applied_invoice_record_id == "REC-SUB-001-0"

    packet = seeded.payment_packet()
    (line,) = [l for l in packet.lines if l.invoice_number == "INV-1001"]
    assert line.amount == 1200.00  # the open amount is what is proposed
    assert line.gross_amount == 1400.00  # the face rides along, unchanged
    assert [c.credit_number for c in line.credits_applied] == ["CN-1001"]
    assert packet.totals_by_currency["USD"] == 1200.00
    assert packet.unapplied_credits == []
    # The invoice's history is untouched: same approval, same face.
    invoice = seeded.get_record("REC-SUB-001-0")
    assert invoice.status == "APPROVED"
    assert float(invoice.extraction.value("total")) == 1400.00
    # A proposal only: the packet type carries no execution field.
    assert not hasattr(packet, "execute")


def test_gate_refuses_fraud_exceptions_and_anonymous_decisions(seeded):
    with pytest.raises(GateError):
        seeded.decide("REC-SUB-008-0", "approve", "Priya Nair (AP lead)")  # fraud hold
    with pytest.raises(GateError):
        seeded.decide("REC-SUB-003-0", "approve", "Priya Nair (AP lead)")  # exception
    with pytest.raises(GateError):
        seeded.decide("REC-SUB-001-0", "approve", "  ")  # no approver named
    with pytest.raises(GateError):
        seeded.decide("REC-SUB-001-0", "maybe", "Priya Nair (AP lead)")


def test_rejection_is_recorded_with_reason(seeded):
    decided = seeded.decide("REC-SUB-002-0", "reject", "Priya Nair (AP lead)", "price drift unexplained")
    assert decided.status == "REJECTED"
    assert decided.decision.reason == "price drift unexplained"
    packet = seeded.payment_packet()
    assert all(line.invoice_number != "INV-1002" for line in packet.lines)


def test_duplicate_is_flagged_and_neither_copy_is_paid(seeded):
    dup = _record(seeded, "REC-SUB-006-0")
    assert dup.status == "EXCEPTION"
    assert "DUPLICATE_INVOICE" in dup.match.exception_codes
    with pytest.raises(GateError):
        seeded.decide(dup.record_id, "approve", "Priya Nair (AP lead)")


def test_injection_payload_is_flagged_on_the_record(seeded):
    record = _record(seeded, "REC-SUB-014-0")
    assert record.injection_flags
    assert record.status == "AWAITING_APPROVER"  # flagged, fenced — not silently trusted, not blocked

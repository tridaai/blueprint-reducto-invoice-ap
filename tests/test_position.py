"""The AP position at close: ageing in code off due dates,
the state split, and the tie-out against the packet. Records
are planted directly in the store (the test_store idiom) where
a scenario needs exact due dates and ledgers; the seeded world
carries the end-to-end tie."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.position import bucket_for
from reducto_ap_agent.schemas import (
    CreditApplication,
    DocumentRecord,
    ExtractedDocument,
    ExtractedField,
)
from reducto_ap_agent.service import ApService

AS_OF = "2026-11-15"


def _invoice(
    record_id: str,
    supplier: str,
    number: str,
    total: float,
    *,
    due: str | None = "2026-11-15",
    status: str = "APPROVED",
    currency: str = "USD",
    payment_state: str | None = None,
    run_id: str | None = None,
    credits: tuple[float, ...] = (),
    fraud_flags: tuple[str, ...] = (),
) -> DocumentRecord:
    fields = {
        "invoice_number": ExtractedField(value=number),
        "invoice_date": ExtractedField(value="2026-09-01"),
        "total": ExtractedField(value=total),
    }
    if due is not None:
        fields["due_date"] = ExtractedField(value=due)
    ledger = [
        CreditApplication(
            credit_record_id=f"REC-CR-{record_id}-{i}",
            credit_number=f"CN-{record_id}-{i}",
            invoice_record_id=record_id,
            amount=amount,
            currency=currency,
            actor="Marta Chen (controller)",
        )
        for i, amount in enumerate(credits)
    ]
    return DocumentRecord(
        record_id=record_id,
        submission_id="SUB-TEST",
        supplier_id=f"SUP-{supplier[:4].upper()}",
        supplier_name=supplier,
        status=status,  # type: ignore[arg-type]
        doc_class="invoice",
        extraction=ExtractedDocument(
            doc_class="invoice", fields=fields, currency=currency),
        payment_state=payment_state,  # type: ignore[arg-type]
        payment_run_id=run_id,
        credits_applied=ledger,
        fraud_flags=list(fraud_flags),
    )


def _plant(service: ApService, *records: DocumentRecord) -> None:
    for record in records:
        service.store.save(record)


def _lines(report) -> dict[str, object]:
    return {
        line.record_id: line
        for supplier in report.suppliers for line in supplier.lines
    }


# -- the buckets ---------------------------------------------------


def test_bucket_edges_are_upper_inclusive():
    assert bucket_for(-10) == "CURRENT"
    assert bucket_for(0) == "CURRENT"  # due today is not past due
    assert bucket_for(1) == "1-30"
    assert bucket_for(30) == "1-30"  # exactly on the edge: earlier bucket
    assert bucket_for(31) == "31-60"
    assert bucket_for(60) == "31-60"
    assert bucket_for(61) == "61-90"
    assert bucket_for(90) == "61-90"
    assert bucket_for(91) == "90+"


def test_position_ages_off_due_dates_not_invoice_age(service):
    _plant(
        service,
        _invoice("REC-P1", "Acme", "INV-P1", 100.0, due="2026-11-15"),
        _invoice("REC-P2", "Acme", "INV-P2", 200.0, due="2026-12-01"),
        _invoice("REC-P3", "Acme", "INV-P3", 300.0, due="2026-10-16"),
        _invoice("REC-P4", "Acme", "INV-P4", 400.0, due="2026-10-15"),
        _invoice("REC-P5", "Acme", "INV-P5", 500.0, due="2026-08-17"),
        _invoice("REC-P6", "Acme", "INV-P6", 600.0, due="2026-08-15"),
    )
    report = service.ap_position(AS_OF)
    lines = _lines(report)
    assert lines["REC-P1"].bucket == "CURRENT"
    assert lines["REC-P1"].days_past_due == 0
    # Not yet due is CURRENT however old the invoice is — ageing
    # runs off the due date, never off age since issue.
    assert lines["REC-P2"].bucket == "CURRENT"
    assert lines["REC-P2"].days_past_due == -16
    assert lines["REC-P3"].bucket == "1-30"
    assert lines["REC-P3"].days_past_due == 30
    assert lines["REC-P4"].bucket == "31-60"
    assert lines["REC-P4"].days_past_due == 31
    assert lines["REC-P5"].bucket == "61-90"
    assert lines["REC-P5"].days_past_due == 90  # the edge belongs to 61-90
    assert lines["REC-P6"].bucket == "90+"
    assert lines["REC-P6"].days_past_due == 92
    totals = report.bucket_totals_by_currency["USD"]
    assert totals == {
        "CURRENT": 300.0, "1-30": 300.0, "31-60": 400.0,
        "61-90": 500.0, "90+": 600.0, "UNDATED": 0.0,
    }
    assert report.open_totals_by_currency == {"USD": 2100.0}
    assert report.splits_agree is True
    assert report.tie_status == "TIED"


def test_undated_invoice_is_named_never_silently_bucketed(service):
    _plant(
        service,
        _invoice("REC-U1", "Acme", "INV-U1", 250.0, due=None),
        _invoice("REC-U2", "Acme", "INV-U2", 100.0, due="2026-11-15"),
    )
    report = service.ap_position(AS_OF)
    lines = _lines(report)
    assert lines["REC-U1"].bucket == "UNDATED"
    assert lines["REC-U1"].days_past_due is None
    assert lines["REC-U1"].due_date == ""
    # The undated money is still owed: it stands in the totals
    # and the tie still holds.
    assert report.bucket_totals_by_currency["USD"]["UNDATED"] == 250.0
    assert report.open_totals_by_currency == {"USD": 350.0}
    assert report.tie_status == "TIED"


# -- the tie and the splits ----------------------------------------


def test_seeded_position_ties_to_the_packet(seeded):
    report = seeded.ap_position(AS_OF)
    packet = seeded.payment_packet()
    assert report.tie_status == "TIED"
    assert report.splits_agree is True
    ties = {tie.currency: tie for tie in report.ties}
    assert set(ties) == set(packet.totals_by_currency)
    for currency, total in packet.totals_by_currency.items():
        assert ties[currency].packet_total == total
        assert ties[currency].position_total == total
        assert ties[currency].difference == 0.0


def test_state_split_separates_the_money_and_foots(service):
    _plant(
        service,
        _invoice("REC-S1", "Acme", "INV-S1", 100.0),
        _invoice("REC-S2", "Acme", "INV-S2", 200.0,
                 payment_state="IN_RUN", run_id="RUN-2026-11-20-01"),
        _invoice("REC-S3", "Acme", "INV-S3", 300.0,
                 payment_state="PAID_PROPOSED",
                 run_id="RUN-2026-11-06-01"),
    )
    report = service.ap_position(AS_OF)
    states = report.state_totals_by_currency["USD"]
    assert states == {
        "OPEN_FREE": 100.0, "IN_RUN": 200.0, "PAID_PROPOSED": 300.0,
    }
    assert sum(states.values()) == report.open_totals_by_currency["USD"]
    assert report.splits_agree is True
    lines = _lines(report)
    assert lines["REC-S2"].state == "IN_RUN"
    assert lines["REC-S2"].payment_run_id == "RUN-2026-11-20-01"


def test_credits_reduce_the_aged_open_not_the_face(service):
    _plant(
        service,
        _invoice("REC-C1", "Acme", "INV-C1", 1000.0, credits=(250.0,)),
    )
    report = service.ap_position(AS_OF)
    (line,) = _lines(report).values()
    assert line.face == 1000.0
    assert line.credits == 250.0
    assert line.open == 750.0
    (supplier,) = report.suppliers
    assert supplier.face_total == 1000.0
    assert supplier.credits_total == 250.0
    assert supplier.open_total == 750.0


# -- the disclosed sections -----------------------------------------


def test_fraud_held_is_disclosed_outside_the_tie(service):
    _plant(
        service,
        _invoice("REC-F1", "Granite", "INV-F1", 900.0,
                 status="FRAUD_REVIEW",
                 fraud_flags=("bank-change pattern in notes",)),
        _invoice("REC-F2", "Acme", "INV-F2", 100.0),
    )
    report = service.ap_position(AS_OF)
    assert [h.invoice_number for h in report.fraud_held] == ["INV-F1"]
    assert report.fraud_held[0].fraud_flags == [
        "bank-change pattern in notes"]
    assert report.fraud_held_totals_by_currency == {"USD": 900.0}
    # Outside the tie: the packet reads APPROVED records only,
    # and the position's aged total agrees with it.
    assert report.open_totals_by_currency == {"USD": 100.0}
    assert report.tie_status == "TIED"


def test_fully_credited_invoice_is_listed_and_owes_nothing(service):
    _plant(
        service,
        _invoice("REC-K1", "Acme", "INV-K1", 500.0, credits=(500.0,)),
        _invoice("REC-K2", "Acme", "INV-K2", 125.0),
    )
    report = service.ap_position(AS_OF)
    assert [f.invoice_number for f in report.fully_credited] == ["INV-K1"]
    assert report.fully_credited[0].face == 500.0
    assert report.fully_credited[0].credits == 500.0
    assert "REC-K1" not in _lines(report)
    assert report.open_totals_by_currency == {"USD": 125.0}
    assert report.tie_status == "TIED"


def test_negative_open_breaks_the_tie_by_name(service):
    # Application refuses over-credit in code, so this ledger —
    # 450.00 of credits against a 400.00 face — can only be a
    # corrupted record. The position keeps it out of the
    # buckets, names it, and lets the difference break the tie.
    _plant(
        service,
        _invoice("REC-N1", "Acme", "INV-N1", 400.0, credits=(450.0,)),
        _invoice("REC-N2", "Acme", "INV-N2", 100.0),
    )
    report = service.ap_position(AS_OF)
    assert report.tie_status == "POSITION_TIE_BREAK"
    (tie,) = report.ties
    assert tie.packet_total == -50.0 + 100.0
    assert tie.position_total == 100.0
    assert tie.difference == -50.0
    assert tie.tied is False
    (anomaly,) = report.anomalies
    assert anomaly.code == "NEGATIVE_OPEN"
    assert anomaly.invoice_number == "INV-N1"
    assert anomaly.face == 400.0
    assert anomaly.credits == 450.0
    assert anomaly.open == -50.0
    assert "REC-N1" not in _lines(report)


# -- the export and the HTTP surface ---------------------------------


def test_position_export_is_digested_stable_and_stored(service):
    _plant(service, _invoice("REC-E1", "Acme", "INV-E1", 700.0))
    key, digest, data = service.position_export(AS_OF)
    assert key == f"positions/{AS_OF}/ap-position-v1.csv"
    assert digest == hashlib.sha256(data).hexdigest()
    assert service.pipeline.objects.get(key) == data
    text = data.decode("utf-8")
    assert "INV-E1" in text
    assert "TIED" in text
    key2, digest2, data2 = service.position_export(AS_OF)
    assert (key2, digest2, data2) == (key, digest, data)


def test_position_over_http(seeded):
    client = TestClient(create_app(seeded))
    response = client.get("/position", params={"as_of": AS_OF})
    assert response.status_code == 200
    body = response.json()
    assert body["as_of"] == AS_OF
    assert body["tie_status"] == "TIED"
    assert body["open_totals_by_currency"] == \
        seeded.payment_packet().totals_by_currency
    export = client.get("/position/export", params={"as_of": AS_OF})
    assert export.status_code == 200
    assert export.headers["x-export-sha256"] == \
        hashlib.sha256(export.content).hexdigest()
    assert export.headers["x-export-key"] == \
        f"positions/{AS_OF}/ap-position-v1.csv"

"""The master has a past: bank versions, the as-of screen,
and the recording discipline.

Cedarline (SUP-03) is the staged supplier: a legitimate bank
change recorded effective 2026-10-01, from the seeded account
…4444 (Cedar Bank) to a new account …7777. Every screen
outcome is then exercised against invoices on both sides of
that date.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import _doc
from reducto_ap_agent.errors import MasterError, RecordNotFound
from reducto_ap_agent.guardrails import screen_bank_details
from reducto_ap_agent.schemas import BankVersion, LineItem, Supplier

OLD = "US00 CEDR 2222 3333 4444"
NEW = "US00 CEDR 9999 8888 7777"
NEVER = "US00 ATTK 0000 1111 2222"
SOURCE = "supplier letter dated 2026-09-25, verified by callback to the number on file"


def _record_change(service, **overrides):
    call = dict(
        bank_account=NEW, bank_name="Cedar Bank",
        effective_from="2026-10-01", actor="Priya Nair (AP lead)",
        source=SOURCE,
    )
    call.update(overrides)
    return service.record_bank_version("SUP-03", **call)


def _invoice(number: str, date: str, bank: str) -> str:
    fields = [
        ("Supplier", "Cedarline Packaging Co"),
        ("Invoice Number", number),
        ("Invoice Date", date),
        ("Due Date", "2026-11-14"),
        ("PO Reference", "PO-6002"),
        ("Currency", "USD"),
        ("Bank Account", bank),
        ("Subtotal", "1000.00"),
        ("Tax", "0.00"),
        ("Total", "1000.00"),
    ]
    return _doc("INVOICE", 1, fields, [
        LineItem(sku="SKU-B1", description="Shipping boxes",
                 quantity=100, unit_price=10.00, amount=1000.00),
    ])


def _submit(service, sid: str, number: str, date: str, bank: str):
    (record,) = service.submit(
        sid, f"{sid.lower()}.pdf", _invoice(number, date, bank).encode(),
        "text/plain",
    )
    return record


# -- the screen, against a two-version master -------------------

SUPPLIER = Supplier(
    supplier_id="SUP-X", name="Example Co", bank_account=OLD,
    bank_name="Cedar Bank",
)
SERIES = [
    BankVersion(version=1, bank_account=OLD, bank_name="Cedar Bank"),
    BankVersion(version=2, bank_account=NEW, bank_name="Cedar Bank",
                effective_from="2026-10-01", recorded_by="AP lead",
                source=SOURCE),
]


def test_screen_judges_the_invoice_by_its_own_date():
    # Dated before the change, quoting the old bank: clean —
    # however the master has moved since.
    assert screen_bank_details(OLD, SUPPLIER, SERIES, "2026-09-20") == []
    # Dated after the change, quoting the new bank: clean.
    assert screen_bank_details(NEW, SUPPLIER, SERIES, "2026-10-05") == []
    # On the effective date itself, the new version stands.
    assert screen_bank_details(NEW, SUPPLIER, SERIES, "2026-10-01") == []


def test_screen_names_the_retired_bank_quoted_late():
    flags = screen_bank_details(OLD, SUPPLIER, SERIES, "2026-10-05")
    assert len(flags) == 1
    assert flags[0].startswith("BANK_DETAILS_SUPERSEDED")
    assert "effective 2026-10-01" in flags[0]
    assert "2026-10-05" in flags[0]


def test_screen_names_the_future_bank_quoted_early():
    flags = screen_bank_details(NEW, SUPPLIER, SERIES, "2026-09-20")
    assert len(flags) == 1
    assert flags[0].startswith("BANK_DETAILS_NOT_YET_EFFECTIVE")
    assert "2026-10-01" in flags[0] and "2026-09-20" in flags[0]


def test_screen_names_an_account_no_version_ever_held():
    flags = screen_bank_details(NEVER, SUPPLIER, SERIES, "2026-10-05")
    assert len(flags) == 1
    assert flags[0].startswith("BANK_DETAILS_CHANGED")
    # Judged against the version in force on its date: the new
    # account is the approved one it differs from.
    assert "…7777" in flags[0]


def test_screen_without_a_date_judges_against_the_latest():
    assert screen_bank_details(NEW, SUPPLIER, SERIES, "") == []
    flags = screen_bank_details(OLD, SUPPLIER, SERIES, "")
    assert len(flags) == 1
    assert flags[0].startswith("BANK_DETAILS_SUPERSEDED")


# -- the recording discipline ------------------------------------


def test_recording_appends_a_version_with_its_provenance(service):
    version = _record_change(service)
    assert version.version == 2
    assert version.bank_account == NEW
    assert version.effective_from == "2026-10-01"
    assert version.recorded_by == "Priya Nair (AP lead)"
    assert version.source == SOURCE
    assert version.recorded_at  # stamped at the recording
    series = service.store.reference.bank_versions_for("SUP-03")
    assert [v.version for v in series] == [1, 2]


def test_recording_refuses_missing_provenance_by_name(service):
    with pytest.raises(MasterError, match="must name who recorded"):
        _record_change(service, actor=" ")
    with pytest.raises(MasterError, match="must state what it was "
                                           "recorded from"):
        _record_change(service, source="")
    # Nothing was appended by the refused recordings.
    assert len(service.store.reference.bank_versions_for("SUP-03")) == 1


def test_recording_refuses_a_dateless_or_rewinding_change(service):
    with pytest.raises(MasterError, match="must state the date"):
        _record_change(service, effective_from="")
    with pytest.raises(MasterError, match="must state the date"):
        _record_change(service, effective_from="next Tuesday")
    _record_change(service)
    with pytest.raises(MasterError, match="does not move"):
        _record_change(service, bank_account="US00 CEDR 5555 6666 7777",
                       effective_from="2026-10-01")
    with pytest.raises(MasterError, match="does not move"):
        _record_change(service, bank_account="US00 CEDR 5555 6666 7777",
                       effective_from="2026-09-01")


def test_recording_refuses_a_noop_and_an_unknown_supplier(service):
    with pytest.raises(MasterError, match="identical to version 1"):
        _record_change(service, bank_account=OLD)
    with pytest.raises(RecordNotFound, match="no supplier SUP-99"):
        service.record_bank_version(
            "SUP-99", bank_account=NEW, bank_name="Cedar Bank",
            effective_from="2026-10-01", actor="Priya Nair",
            source=SOURCE)


def test_history_is_append_only_under_later_recordings(service):
    seed_before = service.store.reference.bank_versions_for(
        "SUP-03")[0].model_dump_json()
    v2 = _record_change(service)
    v2_before = v2.model_dump_json()
    _record_change(service, bank_account="US00 CEDR 5555 6666 7777",
                   effective_from="2027-01-01")
    series = service.store.reference.bank_versions_for("SUP-03")
    assert [v.version for v in series] == [1, 2, 3]
    assert series[0].model_dump_json() == seed_before
    assert series[1].model_dump_json() == v2_before


# -- the pipeline, on both sides of the change --------------------


def test_pipeline_outcomes_across_the_change(service):
    _record_change(service)
    before_old = _submit(service, "SUB-T1", "INV-T1", "2026-09-20", OLD)
    after_old = _submit(service, "SUB-T2", "INV-T2", "2026-10-05", OLD)
    before_new = _submit(service, "SUB-T3", "INV-T3", "2026-09-20", NEW)
    after_new = _submit(service, "SUB-T4", "INV-T4", "2026-10-05", NEW)
    never = _submit(service, "SUB-T5", "INV-T5", "2026-10-05", NEVER)

    assert before_old.status == "AWAITING_APPROVER"
    assert before_old.fraud_flags == []
    assert after_new.status == "AWAITING_APPROVER"
    assert after_new.fraud_flags == []

    assert after_old.status == "FRAUD_REVIEW"
    assert after_old.fraud_flags[0].startswith("BANK_DETAILS_SUPERSEDED")
    assert before_new.status == "FRAUD_REVIEW"
    assert before_new.fraud_flags[0].startswith(
        "BANK_DETAILS_NOT_YET_EFFECTIVE")
    assert never.status == "FRAUD_REVIEW"
    assert never.fraud_flags[0].startswith("BANK_DETAILS_CHANGED")


def test_a_new_version_rejudges_no_decided_record(service):
    clean = _submit(service, "SUB-T1", "INV-T1", "2026-09-20", OLD)
    decided = service.decide(clean.record_id, "approve", "Priya Nair")
    held = _submit(service, "SUB-T8", "INV-T8", "2026-09-20", NEVER)
    assert held.status == "FRAUD_REVIEW"
    decided_before = decided.model_dump_json()
    held_before = held.model_dump_json()
    _record_change(service)
    assert service.get_record(clean.record_id).model_dump_json() \
        == decided_before
    assert service.get_record(held.record_id).model_dump_json() \
        == held_before


# -- the readers: history, ledger, export -------------------------


def test_bank_history_names_the_version_in_force(service):
    _record_change(service)
    early = service.bank_history("SUP-03", "2026-09-15")
    late = service.bank_history("SUP-03", "2026-11-15")
    assert [v.version for v in early.versions] == [1, 2]
    assert early.current_version == 1
    assert late.current_version == 2
    assert late.versions[0].effective_from == ""  # the seed is undated
    assert late.versions[1].source == SOURCE
    with pytest.raises(RecordNotFound):
        service.bank_history("SUP-99", "2026-11-15")


def test_ledger_shows_the_version_in_force_at_its_as_of(service):
    _record_change(service)
    before = service.supplier_ledger("SUP-03", "2026-09-15")
    after = service.supplier_ledger("SUP-03", "2026-11-15")
    assert before.bank.bank_account == OLD
    assert (before.bank.bank_version, before.bank.bank_version_count) \
        == (1, 2)
    assert before.bank.effective_from == ""
    assert after.bank.bank_account == NEW
    assert (after.bank.bank_version, after.bank.bank_version_count) \
        == (2, 2)
    assert after.bank.effective_from == "2026-10-01"


def test_a_released_run_pays_the_version_in_force(service):
    _record_change(service)
    invoice = _submit(service, "SUB-T4", "INV-T4", "2026-10-05", NEW)
    service.decide(invoice.record_id, "approve", "Priya Nair")
    run = service.create_payment_run("2026-10-16",
                                     record_ids=[invoice.record_id])
    service.decide_payment_run(run.run_id, "release", "Tomas Reed")
    export = service.payment_run_export(run.run_id).decode()
    assert NEW in export and OLD not in export


def test_a_future_version_does_not_reach_a_release(service):
    _record_change(service, effective_from="2027-01-01")
    invoice = _submit(service, "SUB-T1", "INV-T1", "2026-09-20", OLD)
    service.decide(invoice.record_id, "approve", "Priya Nair")
    run = service.create_payment_run("2026-10-16",
                                     record_ids=[invoice.record_id])
    service.decide_payment_run(run.run_id, "release", "Tomas Reed")
    export = service.payment_run_export(run.run_id).decode()
    assert OLD in export and NEW not in export


# -- the API -------------------------------------------------------


@pytest.fixture()
def client(service):
    with TestClient(create_app(service)) as test_client:
        yield test_client


def test_api_records_and_serves_the_timeline(client):
    created = client.post("/suppliers/SUP-03/bank-versions", json={
        "bank_account": NEW, "bank_name": "Cedar Bank",
        "effective_from": "2026-10-01", "actor": "Priya Nair (AP lead)",
        "source": SOURCE,
    })
    assert created.status_code == 201
    assert created.json()["version"] == 2
    history = client.get(
        "/suppliers/SUP-03/bank-history?as_of=2026-11-15").json()
    assert history["current_version"] == 2
    assert [v["version"] for v in history["versions"]] == [1, 2]
    refused = client.post("/suppliers/SUP-03/bank-versions", json={
        "bank_account": "US00 CEDR 5555 6666 7777",
        "effective_from": "2027-01-01", "actor": "Priya Nair",
        "source": "",
    })
    assert refused.status_code == 422
    assert "recorded from" in refused.json()["detail"]
    assert client.get("/suppliers/SUP-99/bank-history").status_code == 404

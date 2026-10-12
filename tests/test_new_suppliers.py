"""Unknown suppliers stop at the door: the new-supplier hold.

Before this round an invoice from a supplier the master had
never seen washed up as a generic match exception: its name
failed the exact match, no PO resolved it, the fraud screen
silently compared against nothing, and nothing named what it
was — a first payee, the highest-risk arrival in AP. These
tests pin the governed fate that replaced it:

- the stop: an unresolvable payable document lands in
  NEW_SUPPLIER_REVIEW — unmatched, unscreened (and saying
  so), undecidable at the gate, never a payable — with the
  near-miss named as evidence and never attached as a join;
- the enrolment: a named controller, the bank details, and
  what they were verified from — or it does not happen;
  exactly one master record is written, standing suppliers
  and their bank histories byte-identical; every held record
  of the identity is released through the pipeline's own
  adjudication, and a released document whose own stated
  details differ from the enrolled ones is a fraud case by
  the ordinary rule;
- the refusal: per record, reasoned, terminal —
  NOT_A_KNOWN_SUPPLIER — and it enrols nothing;
- the boundaries: a held record never rides a run and never
  enters the packet; the position discloses the held money
  beside its tie; a supplier enrolled yesterday is an
  ordinary supplier today.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import _doc, _inv_fields, build_world
from reducto_ap_agent.errors import GateError, MasterError, PaymentRunError
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.schemas import (
    GoodsReceipt,
    LineItem,
    PurchaseOrder,
    Supplier,
)
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore

CONTROLLER = "Marta Chen (controller)"
APPROVER = "Priya Nair (AP lead)"
SOURCE = "onboarding pack verified by callback to the number on file"
ACCOUNT_X = "US00 NBEA 1212 3434 5656"
ACCOUNT_Y = "US00 EVIL 9999 8888 7777"

NORTHBEAM = Supplier(
    supplier_id="SUP-77", name="Northbeam Fasteners Ltd",
    bank_account=ACCOUNT_X, bank_name="Northbeam Bank",
)


def submit(service: ApService, submission_id: str, text: str):
    (record,) = service.submit(
        submission_id, f"{submission_id.lower()}.pdf",
        text.encode("utf-8"), "text/plain",
    )
    return record


def northbeam_invoice(
    number: str = "INV-7701", total: float = 250.00,
    bank: str = ACCOUNT_X, po: str | None = "PO-7701",
) -> str:
    stated = Supplier(
        supplier_id="SUP-77", name=NORTHBEAM.name,
        bank_account=bank, bank_name="Northbeam Bank",
    )
    return _doc("INVOICE", 1, _inv_fields(stated, number, po, total), [
        LineItem(sku="SKU-Z1", description="Hex bolts",
                 quantity=10, unit_price=25.00, amount=250.00),
    ])


@pytest.fixture()
def door_service(world) -> ApService:
    """The seeded world plus one purchase order the buyer's
    ERP raised for a supplier AP has never enrolled — PO-7701
    names SUP-77, and SUP-77 stands on no master. The order
    existing is exactly what does NOT resolve the supplier:
    the intake's PO path reads the master, and the master
    does not carry the id."""
    store = InMemoryRecordStore()
    po = PurchaseOrder(
        po_number="PO-7701", supplier_id="SUP-77",
        supplier_name="Northbeam Fasteners Ltd", currency="USD",
        lines=[LineItem(sku="SKU-Z1", description="Hex bolts",
                        quantity=10, unit_price=25.00, amount=250.00)],
        total=250.00,
    )
    receipt = GoodsReceipt(
        receipt_id="RCV-7701", po_number="PO-7701",
        supplier_id="SUP-77",
        lines=[LineItem(sku="SKU-Z1", description="Hex bolts",
                        quantity=10, unit_price=25.00, amount=250.00)],
    )
    store.reference.load(
        world.suppliers,
        world.purchase_orders + [po],
        world.receipts + [receipt],
    )
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(),
        store=store,
        objects=InMemoryObjectStore(),
        checkpointer=MemorySaver(),
    )
    return ApService(pipeline=pipeline, store=store)


# -- the stop ----------------------------------------------------


def test_unknown_supplier_invoice_stops_at_the_door(service):
    record = submit(service, "SUB-NS-1", northbeam_invoice(po=None))
    assert record.status == "NEW_SUPPLIER_REVIEW"
    assert record.supplier_id is None
    # No judgement it never had: no match, no screen.
    assert record.match is None
    assert record.fraud_flags == []
    assert record.bank_account_seen is None
    hold = record.supplier_hold
    assert hold is not None
    assert hold.name_as_written == "Northbeam Fasteners Ltd"
    assert hold.normalised_name == "northbeam fasteners ltd"
    assert hold.near_miss_supplier_id is None  # resembles nothing on the master
    assert hold.resolution is None
    detail = next(
        a["detail"] for a in record.audit
        if a["event"] == "new_supplier_hold"
    )
    assert "not fraud-screened" in detail
    assert "NEW_SUPPLIER_REVIEW" in detail


def test_near_miss_is_named_as_evidence_and_never_attached(seeded):
    text = _doc("INVOICE", 1, [
        ("Supplier", "Harbor Light Marine Supply"),
        ("Invoice Number", "INV-2099"),
        ("Invoice Date", "2026-09-14"),
        ("Due Date", "2026-10-14"),
        ("PO Reference", "PO-9998"),
        ("Currency", "USD"),
        ("Bank Account", "US00 HARB 6666 7777 8888"),
        ("Subtotal", "150.00"), ("Tax", "0.00"), ("Total", "150.00"),
    ], [LineItem(sku="SKU-N9", description="Mooring line", quantity=1,
                 unit_price=150.00, amount=150.00)])
    record = submit(seeded, "SUB-NS-STRAY", text)
    assert record.status == "NEW_SUPPLIER_REVIEW"
    assert record.supplier_id is None  # resemblance is not a join
    assert record.supplier_hold is not None
    assert record.supplier_hold.near_miss_supplier_id == "SUP-08"
    assert record.supplier_hold.near_miss_supplier_name \
        == "Harborlight Marine Supply"
    index = seeded.supplier_index("2026-11-15")
    assert index.new_supplier_hold_count == 1
    assert record.record_id in [d.record_id for d in index.unmatched]


def test_gate_cannot_decide_a_held_record_and_names_the_door(service):
    record = submit(service, "SUB-NS-1", northbeam_invoice(po=None))
    with pytest.raises(GateError) as excinfo:
        service.decide(record.record_id, "approve", APPROVER, "pay it")
    message = str(excinfo.value)
    assert "held at the door" in message
    assert "Northbeam Fasteners Ltd" in message
    assert excinfo.value.status_code == 409
    assert service.get_record(record.record_id).status \
        == "NEW_SUPPLIER_REVIEW"


def test_credit_note_from_an_unknown_supplier_stops_too(service):
    fields = [
        ("Supplier", "Northbeam Fasteners Ltd"),
        ("Credit Note Number", "CN-7701"),
        ("Issue Date", "2026-09-22"),
        ("PO Reference", "PO-9997"),
        ("Credits Invoice", "INV-7701"),
        ("Currency", "USD"),
        ("Bank Account", ACCOUNT_X),
        ("Subtotal", "-50.00"), ("Tax", "0.00"), ("Total", "-50.00"),
    ]
    record = submit(service, "SUB-NS-CN", _doc(
        "CREDIT NOTE", 1, fields,
        [LineItem(sku="SKU-Z1", description="Hex bolts — damaged",
                  quantity=2, unit_price=-25.00, amount=-50.00)],
    ))
    assert record.status == "NEW_SUPPLIER_REVIEW"
    # Not yet born UNAPPLIED — the credit machinery starts
    # when the pipeline adjudicates it, after enrolment.
    assert record.credit_state is None
    service.enrol_new_supplier(
        record.record_id, CONTROLLER, ACCOUNT_X,
        "Northbeam Bank", SOURCE,
    )
    released = service.get_record(record.record_id)
    assert released.credit_state == "UNAPPLIED"
    assert released.status == "EXCEPTION"  # PO-9997 is not on file


# -- the enrolment -------------------------------------------------


def test_enrolment_carries_the_bankmaster_governance_whole(service):
    record = submit(service, "SUB-NS-1", northbeam_invoice(po=None))
    with pytest.raises(MasterError) as no_actor:
        service.enrol_new_supplier(
            record.record_id, " ", ACCOUNT_X, "Northbeam Bank", SOURCE)
    assert "must name the controller" in str(no_actor.value)
    with pytest.raises(MasterError) as no_source:
        service.enrol_new_supplier(
            record.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank", "")
    assert "verified from" in str(no_source.value)
    with pytest.raises(MasterError) as no_account:
        service.enrol_new_supplier(
            record.record_id, CONTROLLER, " ", "Northbeam Bank", SOURCE)
    assert "must state the bank account" in str(no_account.value)
    # Every refusal happened before any write: the master is
    # untouched and the record still stands at the door.
    assert "SUP-13" not in service.store.reference.suppliers
    assert service.get_record(record.record_id).status \
        == "NEW_SUPPLIER_REVIEW"
    # And a record that is not held has no supplier to enrol.
    seeded_record = service.submit(
        "SUB-NS-KNOWN", "known.pdf",
        next(s.text for s in build_world().submissions
             if s.submission_id == "SUB-001").encode("utf-8"),
        "text/plain",
    )[0]
    with pytest.raises(GateError):
        service.enrol_new_supplier(
            seeded_record.record_id, CONTROLLER, ACCOUNT_X,
            "Northbeam Bank", SOURCE)


def test_enrolment_writes_one_master_record_and_releases(service):
    before_suppliers = {
        sid: s.model_dump()
        for sid, s in service.store.reference.suppliers.items()
    }
    before_versions = {
        sid: [v.model_dump() for v in versions]
        for sid, versions
        in service.store.reference.bank_versions.items()
    }
    record = submit(service, "SUB-NS-1", northbeam_invoice(po=None))
    enrolment = service.enrol_new_supplier(
        record.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank",
        SOURCE,
    )
    assert enrolment.supplier.supplier_id == "SUP-13"  # next free id
    assert enrolment.supplier.name == "Northbeam Fasteners Ltd"
    assert enrolment.released_record_ids == [record.record_id]
    version = enrolment.bank_version
    assert version.version == 1
    assert version.effective_from == ""  # version 1 is the standing word
    assert version.recorded_by == CONTROLLER
    assert version.source == SOURCE
    # The master's history serves the enrolment as version 1.
    history = service.bank_history("SUP-13", "2026-11-15")
    assert [v.version for v in history.versions] == [1]
    assert history.versions[0].source == SOURCE
    # Every standing supplier and series is byte-identical.
    after_suppliers = {
        sid: s.model_dump()
        for sid, s in service.store.reference.suppliers.items()
        if sid != "SUP-13"
    }
    after_versions = {
        sid: [v.model_dump() for v in versions]
        for sid, versions
        in service.store.reference.bank_versions.items()
        if sid != "SUP-13"
    }
    assert after_suppliers == before_suppliers
    assert after_versions == before_versions
    # The released record was judged on its own contents: its
    # stated account is the enrolled one, so the screen is
    # clean; it names no PO, so the match is the exception any
    # supplier's PO-less invoice earns.
    released = service.get_record(record.record_id)
    assert released.supplier_id == "SUP-13"
    assert released.fraud_flags == []
    assert released.status == "EXCEPTION"
    assert released.match is not None
    assert released.match.exception_codes == ["NO_PO_REFERENCE"]
    resolution = released.supplier_hold.resolution
    assert resolution is not None
    assert resolution.action == "ENROLLED"
    assert resolution.actor == CONTROLLER
    assert resolution.supplier_id == "SUP-13"
    assert resolution.detail == SOURCE
    events = [a["event"] for a in released.audit]
    assert events.index("new_supplier_hold") \
        < events.index("supplier_enrolled") < events.index("exception")


def test_one_enrolment_releases_the_whole_group(service):
    first = submit(service, "SUB-NS-1",
                   northbeam_invoice("INV-7701", po=None))
    second = submit(service, "SUB-NS-2",
                    northbeam_invoice("INV-7702", po="PO-9998"))
    queue = service.new_supplier_review()
    assert queue.held_count == 2
    (group,) = queue.groups
    assert group.name_as_written == "Northbeam Fasteners Ltd"
    assert group.record_ids == [first.record_id, second.record_id]
    assert group.document_numbers == ["INV-7701", "INV-7702"]
    assert group.totals_by_currency == {"USD": 500.00}
    enrolment = service.enrol_new_supplier(
        second.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank",
        SOURCE,
    )
    assert enrolment.released_record_ids == [
        first.record_id, second.record_id]
    assert service.new_supplier_review().held_count == 0
    for rid in (first.record_id, second.record_id):
        assert service.get_record(rid).supplier_id == "SUP-13"


def test_enrolment_does_not_launder_a_documents_own_bank_claim(service):
    honest = submit(service, "SUB-NS-1",
                    northbeam_invoice("INV-7701", bank=ACCOUNT_X, po=None))
    lying = submit(service, "SUB-NS-2",
                   northbeam_invoice("INV-7702", bank=ACCOUNT_Y, po=None))
    service.enrol_new_supplier(
        honest.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank",
        SOURCE,
    )
    assert service.get_record(honest.record_id).fraud_flags == []
    released = service.get_record(lying.record_id)
    assert released.status == "FRAUD_REVIEW"
    assert any(
        flag.startswith("BANK_DETAILS_CHANGED")
        for flag in released.fraud_flags
    )


def test_enrolment_under_the_orders_id_runs_clean_to_the_gate(door_service):
    record = submit(door_service, "SUB-NS-1", northbeam_invoice())
    # The order on file does not resolve the supplier — its
    # id stands on no master — so the document still stops.
    assert record.status == "NEW_SUPPLIER_REVIEW"
    enrolment = door_service.enrol_new_supplier(
        record.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank",
        SOURCE, supplier_id="SUP-77",
    )
    assert enrolment.supplier.supplier_id == "SUP-77"
    released = door_service.get_record(record.record_id)
    assert released.status == "AWAITING_APPROVER"
    assert released.match is not None
    assert released.match.status == "MATCHED"
    decided = door_service.decide(
        record.record_id, "approve", APPROVER, "matched, enrolled")
    assert decided.status == "APPROVED"
    # A stated id already on the master is refused by name.
    other = submit(door_service, "SUB-NS-2", _doc("INVOICE", 1, [
        ("Supplier", "Quarryline Aggregates LLC"),
        ("Invoice Number", "INV-8801"),
        ("Invoice Date", "2026-09-14"),
        ("Due Date", "2026-10-14"),
        ("Currency", "USD"),
        ("Bank Account", "US00 QUAR 1212 3434 5656"),
        ("Subtotal", "90.00"), ("Tax", "0.00"), ("Total", "90.00"),
    ], [LineItem(sku="SKU-Q1", description="Crushed stone", quantity=3,
                 unit_price=30.00, amount=90.00)]))
    assert other.status == "NEW_SUPPLIER_REVIEW"
    with pytest.raises(MasterError) as taken:
        door_service.enrol_new_supplier(
            other.record_id, CONTROLLER, "US00 QUAR 1212 3434 5656",
            "Quarry Bank", SOURCE, supplier_id="SUP-77",
        )
    assert "already stands on the master" in str(taken.value)


def test_enrolled_supplier_is_ordinary_the_next_day(door_service):
    record = submit(door_service, "SUB-NS-1", northbeam_invoice())
    door_service.enrol_new_supplier(
        record.record_id, CONTROLLER, ACCOUNT_X, "Northbeam Bank",
        SOURCE, supplier_id="SUP-77",
    )
    # A later arrival from the same name resolves by the
    # ordinary exact match — and the ordinary screen judges
    # its bank claim against the enrolled details.
    fraud = submit(door_service, "SUB-NS-2",
                   northbeam_invoice("INV-7702", bank=ACCOUNT_Y))
    assert fraud.status == "FRAUD_REVIEW"
    assert fraud.supplier_hold is None
    clean = submit(door_service, "SUB-NS-3",
                   northbeam_invoice("INV-7703"))
    assert clean.status == "AWAITING_APPROVER"
    assert clean.supplier_id == "SUP-77"


# -- the refusal ---------------------------------------------------


def test_refusal_is_terminal_reasoned_and_per_record(service):
    first = submit(service, "SUB-NS-1",
                   northbeam_invoice("INV-7701", po=None))
    second = submit(service, "SUB-NS-2",
                    northbeam_invoice("INV-7702", po=None))
    with pytest.raises(GateError):
        service.refuse_new_supplier(first.record_id, " ", "a reason")
    with pytest.raises(GateError):
        service.refuse_new_supplier(first.record_id, CONTROLLER, " ")
    refused = service.refuse_new_supplier(
        first.record_id, CONTROLLER,
        "no onboarding pack exists for this name; the sender "
        "was asked to go through vendor onboarding",
    )
    assert refused.status == "REFUSED"
    assert refused.refusal_reason is not None
    assert refused.refusal_reason.startswith("NOT_A_KNOWN_SUPPLIER")
    assert "Northbeam Fasteners Ltd" in refused.refusal_reason
    resolution = refused.supplier_hold.resolution
    assert resolution is not None and resolution.action == "REFUSED"
    # Per record: the identity's other document still stands
    # at the door, and can still be enrolled.
    assert service.get_record(second.record_id).status \
        == "NEW_SUPPLIER_REVIEW"
    assert len(service.store.reference.suppliers) == 12  # nothing enrolled
    with pytest.raises(GateError):
        service.refuse_new_supplier(
            first.record_id, CONTROLLER, "again")
    with pytest.raises(GateError):
        service.enrol_new_supplier(
            first.record_id, CONTROLLER, ACCOUNT_X,
            "Northbeam Bank", SOURCE)


# -- the boundaries --------------------------------------------------


def test_held_record_never_rides_a_run_nor_enters_the_packet(seeded):
    held = submit(seeded, "SUB-NS-1", northbeam_invoice(po=None))
    packet = seeded.payment_packet()
    assert held.record_id not in [ln.record_id for ln in packet.lines]
    seeded.decide("REC-SUB-001-0", "approve", APPROVER, "matched")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", held.record_id])
    (refusal,) = [r for r in run.refusals if r.record_id == held.record_id]
    assert refusal.code == "NEW_SUPPLIER_HOLD"
    assert "not a payable" in refusal.reason
    assert held.record_id not in [ln.record_id for ln in run.lines]
    with pytest.raises(PaymentRunError):
        seeded.create_payment_run("2026-10-16", record_ids=[held.record_id])


def test_position_discloses_held_money_outside_the_tie(seeded):
    held = submit(seeded, "SUB-NS-1", northbeam_invoice(po=None))
    position = seeded.ap_position("2026-11-15")
    (line,) = position.new_supplier_held
    assert line.record_id == held.record_id
    assert line.supplier_name == "Northbeam Fasteners Ltd"  # as written
    assert line.face == 250.00 and line.open == 250.00
    assert position.new_supplier_held_totals_by_currency == {"USD": 250.00}
    # Outside the tie: the tie still holds, and no supplier
    # row carries the held money as owed.
    assert position.tie_status == "TIED"
    assert all(
        row.supplier_name != "Northbeam Fasteners Ltd"
        for row in position.suppliers
    )
    from reducto_ap_agent.position import position_csv
    assert b"NEW-SUPPLIER HELD" in position_csv(position)


def test_queue_groups_by_the_stated_identity_under_its_own_key(service):
    submit(service, "SUB-NS-1", northbeam_invoice("INV-7701", po=None))
    shouting = northbeam_invoice("INV-7702", po=None).replace(
        "Northbeam Fasteners Ltd", "NORTHBEAM FASTENERS LTD")
    submit(service, "SUB-NS-2", shouting)
    submit(service, "SUB-NS-3", _doc("INVOICE", 1, [
        ("Supplier", "Quarryline Aggregates LLC"),
        ("Invoice Number", "INV-8801"),
        ("Invoice Date", "2026-09-14"),
        ("Due Date", "2026-10-14"),
        ("Currency", "USD"),
        ("Bank Account", "US00 QUAR 1212 3434 5656"),
        ("Subtotal", "90.00"), ("Tax", "0.00"), ("Total", "90.00"),
    ], [LineItem(sku="SKU-Q1", description="Crushed stone", quantity=3,
                 unit_price=30.00, amount=90.00)]))
    queue = service.new_supplier_review()
    assert queue.held_count == 3
    assert [g.name_as_written for g in queue.groups] == [
        "Northbeam Fasteners Ltd", "Quarryline Aggregates LLC"]
    assert len(queue.groups[0].record_ids) == 2


# -- the API ---------------------------------------------------------


def test_api_serves_the_queue_and_both_acts(service):
    record = submit(service, "SUB-NS-1", northbeam_invoice(po=None))
    with TestClient(create_app(service)) as client:
        queue = client.get("/new-suppliers")
        assert queue.status_code == 200
        (group,) = queue.json()["groups"]
        assert group["record_ids"] == [record.record_id]
        unprovenanced = client.post(
            f"/records/{record.record_id}/enrol",
            json={"actor": CONTROLLER, "bank_account": ACCOUNT_X,
                  "bank_name": "Northbeam Bank", "source": ""},
        )
        assert unprovenanced.status_code == 422
        enrolled = client.post(
            f"/records/{record.record_id}/enrol",
            json={"actor": CONTROLLER, "bank_account": ACCOUNT_X,
                  "bank_name": "Northbeam Bank", "source": SOURCE},
        )
        assert enrolled.status_code == 201
        assert enrolled.json()["supplier"]["supplier_id"] == "SUP-13"
        assert enrolled.json()["released_record_ids"] == [record.record_id]
        assert client.get("/new-suppliers").json()["held_count"] == 0
        held_again = submit(service, "SUB-NS-2", _doc("INVOICE", 1, [
            ("Supplier", "Quarryline Aggregates LLC"),
            ("Invoice Number", "INV-8801"),
            ("Invoice Date", "2026-09-14"),
            ("Due Date", "2026-10-14"),
            ("Currency", "USD"),
            ("Bank Account", "US00 QUAR 1212 3434 5656"),
            ("Subtotal", "90.00"), ("Tax", "0.00"), ("Total", "90.00"),
        ], [LineItem(sku="SKU-Q1", description="Crushed stone",
                     quantity=3, unit_price=30.00, amount=90.00)]))
        decision = client.post(
            f"/records/{held_again.record_id}/decision",
            json={"decision": "approve", "actor": APPROVER},
        )
        assert decision.status_code == 409
        assert "held at the door" in decision.json()["detail"]
        refusal = client.post(
            f"/records/{held_again.record_id}/refuse-new-supplier",
            json={"actor": CONTROLLER,
                  "reason": "no onboarding pack on file"},
        )
        assert refusal.status_code == 200
        assert refusal.json()["status"] == "REFUSED"

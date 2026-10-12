"""The unmatched-advice worklist (round 12).

Round 4 holds an advice UNMATCHED when it names no run this
system can use — and there the story ended: the pile had no
arithmetic and no acts. These tests pin the worklist: the
candidate rules computed in code over the unsettled lines of
released runs (a typo'd reference, a combined lump, a near
amount), the pin that suggestions confirm NOTHING until a
named controller links, the link act itself under round 4's
exact classification, and the not-ours mark that closes an
item with its reason on the record.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.errors import ConfirmationError, GateError
from reducto_ap_agent.schemas import (
    DocumentRecord,
    ExtractedDocument,
    RemittanceLine,
)

CONTROLLER = "Marta Chen (controller)"
TREASURER = "Jonas Weber (treasurer)"


def _invoice(record_id: str, number: str, supplier_id: str,
             total: float) -> DocumentRecord:
    return DocumentRecord(
        record_id=record_id, submission_id=record_id,
        filename=f"{record_id}.pdf", doc_class="invoice",
        status="APPROVED", match_outcome="MATCHED",
        decided_by="Priya Nair (AP lead)",
        extraction=ExtractedDocument(fields={
            "invoice_number": {"value": number, "confidence": 0.99},
            "total": {"value": total, "confidence": 0.99},
        }),
        supplier_id=supplier_id,
    )


def _advice(record_id: str, rows, run_id: str | None = None) -> DocumentRecord:
    fields = {}
    if run_id:
        fields["payment_run_id"] = {"value": run_id, "confidence": 0.99}
    return DocumentRecord(
        record_id=record_id, submission_id=record_id,
        filename=f"{record_id}.pdf", doc_class="remittance_advice",
        status="RECEIVED",
        extraction=ExtractedDocument(
            fields=fields,
            remittance_lines=[
                RemittanceLine(
                    invoice_number=number, amount_paid=amount,
                    currency="USD", value_date="2026-10-20",
                    bank_reference=f"BNK-{record_id}-{i}",
                )
                for i, (number, amount) in enumerate(rows)
            ],
        ),
    )


def _released_run(service, record_ids):
    run = service.create_payment_run("2026-10-20", record_ids=record_ids)
    return service.decide_payment_run(
        run.run_id, "release", TREASURER, "batch reviewed"
    )


def _held(service, advice: DocumentRecord):
    service.store.save(advice)
    confirmation = service.confirm_remittance(advice.record_id)
    assert confirmation.status == "UNMATCHED"
    return confirmation


def _stage_typo_world(service):
    """Run lines INV-7001 (500) and INV-7002 (250), both
    released and unsettled; one held advice whose first line
    typo'd INV-7001's reference at the exact amount, and whose
    second line matches INV-7002's amount with no reference
    relation at all."""
    service.store.save(_invoice("REC-W1", "INV-7001", "SUP-02", 500.00))
    service.store.save(_invoice("REC-W2", "INV-7002", "SUP-02", 250.00))
    run = _released_run(service, ["REC-W1", "REC-W2"])
    held = _held(service, _advice("REC-ADV-TYPO", [
        ("INV-700I", 500.00),      # one edit from INV-7001, exact money
        ("SETTLE-ACCT-9", 250.00),  # exact money, unrelated reference
    ]))
    return run, held


def test_typo_reference_is_the_top_candidate(service):
    run, held = _stage_typo_world(service)
    worklist = service.advice_worklist()
    assert len(worklist.entries) == 1
    entry = worklist.entries[0]
    assert entry.confirmation_id == held.confirmation_id

    typo = entry.advice_lines[0]
    assert typo.candidates, "the typo'd line must be suggested"
    top = typo.candidates[0]
    assert top.rule == "reference_near"
    assert top.strength == 90
    assert top.member_record_ids == ["REC-W1"]
    assert top.advice_amount == 500.00
    assert top.member_total == 500.00
    assert top.delta == 0.0
    # The typo'd reference is NOT also sold as an amount-only
    # match on the same line: one pair, one strongest rule.
    assert [c.rule for c in typo.candidates
            if c.run_id == run.run_id
            and c.member_record_ids == ["REC-W1"]] == ["reference_near"]

    money_only = entry.advice_lines[1]
    assert money_only.candidates
    assert money_only.candidates[0].rule == "exact_amount"
    assert money_only.candidates[0].strength == 70
    assert entry.best_strength == 90

    # The rules ride the report.
    assert worklist.near_amount_abs == 5.00
    assert worklist.reference_max_edits == 1
    assert "Suggestions confirm nothing" in worklist.ranking_rule


def test_suggestions_confirm_nothing(service):
    run, held = _stage_typo_world(service)
    before = service.store.get("REC-W1")
    assert before.payment_confirmation is None

    service.advice_worklist()
    service.advice_worklist()

    member = service.store.get("REC-W1")
    assert member.payment_confirmation is None
    assert not member.payment_confirmation_id
    assert service.store.get("REC-W2").payment_confirmation is None
    confirmations = service.store.list_confirmations()
    assert [c.confirmation_id for c in confirmations] == [
        held.confirmation_id
    ]
    still = service.advice_worklist()
    assert [e.confirmation_id for e in still.entries] == [
        held.confirmation_id
    ]


def test_link_under_the_reference_rule_settles_the_line(service):
    run, held = _stage_typo_world(service)
    linked = service.link_confirmation(
        held.confirmation_id, CONTROLLER,
        [{"advice_line": 0, "run_id": run.run_id,
          "record_ids": ["REC-W1"]}],
        rule="reference_near",
    )
    assert linked.status == "AWAITING_CONTROLLER"
    assert linked.run_id == run.run_id
    assert linked.linked_by == CONTROLLER
    assert linked.linked_rule == "reference_near"
    assert linked.linked_from == held.confirmation_id

    (line,) = [l for l in linked.lines
               if l.member_record_id == "REC-W1"]
    assert line.classification == "paid_as_proposed"
    assert line.difference == 0.0
    assert "INV-700I" in line.note  # the reference as written
    assert "reference_near" in line.note
    # The unlinked advice line is shown, never absorbed.
    stray = [l for l in linked.lines
             if l.classification == "unexpected_line"]
    assert len(stray) == 1
    assert stray[0].invoice_number == "SETTLE-ACCT-9"

    member = service.store.get("REC-W1")
    assert member.payment_confirmation == "CONFIRMED"
    assert member.payment_confirmation_id == linked.confirmation_id

    held_after = service.get_confirmation(held.confirmation_id)
    assert held_after.status == "UNMATCHED"  # the held record stands
    assert any(a["event"] == "linked" for a in held_after.audit)
    assert service.advice_worklist().entries == []

    decided = service.decide_confirmation(
        linked.confirmation_id, "accept", CONTROLLER, "typo confirmed"
    )
    assert decided.status == "ACCEPTED"


def test_combined_sum_candidate_and_link(service):
    service.store.save(_invoice("REC-W3", "INV-7003", "SUP-02", 300.00))
    service.store.save(_invoice("REC-W4", "INV-7004", "SUP-02", 200.00))
    run = _released_run(service, ["REC-W3", "REC-W4"])
    held = _held(service, _advice("REC-ADV-LUMP", [("BATCH-42", 500.00)]))

    entry = service.advice_worklist().entries[0]
    combined = [c for c in entry.advice_lines[0].candidates
                if c.rule == "combined_sum"]
    assert len(combined) == 1
    assert combined[0].member_record_ids == ["REC-W3", "REC-W4"]
    assert combined[0].member_total == 500.00
    assert combined[0].strength == 80

    linked = service.link_confirmation(
        held.confirmation_id, CONTROLLER,
        [{"advice_line": 0, "run_id": run.run_id,
          "record_ids": ["REC-W3", "REC-W4"]}],
        rule="combined_sum",
    )
    settled = [l for l in linked.lines
               if l.classification == "paid_as_proposed"]
    assert {l.member_record_id for l in settled} == {"REC-W3", "REC-W4"}
    assert all(l.difference == 0.0 for l in settled)
    assert linked.totals_tie is True
    for record_id in ("REC-W3", "REC-W4"):
        assert service.store.get(record_id).payment_confirmation == \
            "CONFIRMED"


def test_near_amount_candidate_links_short(service):
    service.store.save(_invoice("REC-W5", "INV-7005", "SUP-02", 750.00))
    run = _released_run(service, ["REC-W5"])
    held = _held(service, _advice("REC-ADV-NEAR", [("INV-7005", 747.00)]))

    entry = service.advice_worklist().entries[0]
    candidates = entry.advice_lines[0].candidates
    assert len(candidates) == 1
    near = candidates[0]
    assert near.rule == "near_amount"
    assert near.strength == 50
    assert near.delta == -3.00

    linked = service.link_confirmation(
        held.confirmation_id, CONTROLLER,
        [{"advice_line": 0, "run_id": run.run_id,
          "record_ids": ["REC-W5"]}],
        rule="near_amount",
    )
    (line,) = linked.lines
    assert line.classification == "short_paid"
    assert line.difference == -3.00
    assert service.store.get("REC-W5").payment_confirmation == "SHORT_PAID"


def test_not_ours_closes_the_item(service):
    service.store.save(_invoice("REC-W6", "INV-7006", "SUP-02", 100.00))
    _released_run(service, ["REC-W6"])
    held = _held(service, _advice(
        "REC-ADV-FOREIGN", [("ACME-INV-88", 4321.00)]
    ))
    assert service.advice_worklist().entries[0].best_strength == 0

    marked = service.mark_advice_not_ours(
        held.confirmation_id, CONTROLLER,
        "the bank attached another customer's advice to our batch",
    )
    assert marked.status == "NOT_OURS"
    assert marked.not_ours is not None
    assert marked.not_ours.actor == CONTROLLER
    assert "another customer's advice" in marked.not_ours.reason
    assert service.advice_worklist().entries == []

    with pytest.raises(GateError, match="NOT_OURS"):
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": "RUN-X",
              "record_ids": ["REC-W6"]}],
        )
    with pytest.raises(GateError, match="NOT_OURS"):
        service.decide_confirmation(
            held.confirmation_id, "accept", CONTROLLER
        )
    with pytest.raises(GateError, match="already NOT_OURS"):
        service.mark_advice_not_ours(
            held.confirmation_id, CONTROLLER, "again"
        )


def test_settled_lines_are_never_candidates(service):
    service.store.save(_invoice("REC-W7", "INV-7007", "SUP-02", 500.00))
    run = _released_run(service, ["REC-W7"])
    answering = _advice("REC-ADV-NAMED", [("INV-7007", 500.00)],
                        run_id=run.run_id)
    service.store.save(answering)
    named = service.confirm_remittance(answering.record_id)
    assert named.status == "AWAITING_CONTROLLER"
    assert service.store.get("REC-W7").payment_confirmation == "CONFIRMED"

    held = _held(service, _advice("REC-ADV-DUP", [("INV-7007", 500.00)]))
    entry = service.advice_worklist().entries[0]
    assert entry.advice_lines[0].candidates == []

    with pytest.raises(GateError, match="already confirmed paid"):
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": run.run_id,
              "record_ids": ["REC-W7"]}],
        )


def test_combined_mismatch_refused_by_name_single_link_classifies(service):
    service.store.save(_invoice("REC-W8", "INV-7008", "SUP-02", 300.00))
    service.store.save(_invoice("REC-W9", "INV-7009", "SUP-02", 200.00))
    run = _released_run(service, ["REC-W8", "REC-W9"])
    held = _held(service, _advice("REC-ADV-LUMP2", [("BATCH-77", 499.00)]))

    with pytest.raises(GateError) as excinfo:
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": run.run_id,
              "record_ids": ["REC-W8", "REC-W9"]}],
            rule="manual",
        )
    message = str(excinfo.value)
    assert "499.00" in message and "500.00" in message
    assert "-1.00" in message and "single member" in message

    # The refusal's own remedy works: against one member the
    # lump classifies in round 4's vocabulary.
    linked = service.link_confirmation(
        held.confirmation_id, CONTROLLER,
        [{"advice_line": 0, "run_id": run.run_id,
          "record_ids": ["REC-W8"]}],
        rule="manual",
    )
    (line,) = [l for l in linked.lines
               if l.member_record_id == "REC-W8"]
    assert line.classification == "over_paid"
    assert line.difference == 199.00
    assert linked.linked_rule == "manual"


def test_link_refusals_name_themselves(service):
    service.store.save(_invoice("REC-WA", "INV-7010", "SUP-02", 100.00))
    service.store.save(_invoice("REC-WB", "INV-7011", "SUP-02", 100.00))
    run_a = _released_run(service, ["REC-WA"])
    run_b = _released_run(service, ["REC-WB"])
    held = _held(service, _advice("REC-ADV-X", [("INV-7010", 100.00)]))

    with pytest.raises(GateError, match="must name its controller"):
        service.link_confirmation(
            held.confirmation_id, " ",
            [{"advice_line": 0, "run_id": run_a.run_id,
              "record_ids": ["REC-WA"]}],
        )
    with pytest.raises(GateError, match="ONE run"):
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": run_a.run_id,
              "record_ids": ["REC-WA"]},
             {"advice_line": 0, "run_id": run_b.run_id,
              "record_ids": ["REC-WB"]}],
        )
    with pytest.raises(GateError, match="not a member"):
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": run_a.run_id,
              "record_ids": ["REC-WB"]}],
        )
    with pytest.raises(ConfirmationError, match="no lines"):
        service.link_confirmation(held.confirmation_id, CONTROLLER, [])

    linked = service.link_confirmation(
        held.confirmation_id, CONTROLLER,
        [{"advice_line": 0, "run_id": run_a.run_id,
          "record_ids": ["REC-WA"]}],
        rule="manual",
    )
    assert linked.run_id == run_a.run_id
    with pytest.raises(GateError, match="already linked"):
        service.link_confirmation(
            held.confirmation_id, CONTROLLER,
            [{"advice_line": 0, "run_id": run_a.run_id,
              "record_ids": ["REC-WA"]}],
        )


def test_entries_rank_by_best_strength(service):
    service.store.save(_invoice("REC-WC", "INV-7012", "SUP-02", 500.00))
    service.store.save(_invoice("REC-WD", "INV-7013", "SUP-02", 750.00))
    run = _released_run(service, ["REC-WC", "REC-WD"])
    none_held = _held(service, _advice(
        "REC-ADV-NONE", [("ZZZ-1", 9999.00)]))
    near_held = _held(service, _advice(
        "REC-ADV-NEAR2", [("INV-7013", 748.00)]))
    exact_held = _held(service, _advice(
        "REC-ADV-EXACT", [("INV-7012", 500.00)]))

    entries = service.advice_worklist().entries
    assert [e.confirmation_id for e in entries] == [
        exact_held.confirmation_id,
        near_held.confirmation_id,
        none_held.confirmation_id,
    ]
    assert [e.best_strength for e in entries] == [100, 50, 0]


def test_worklist_over_http(seeded):
    with TestClient(create_app(seeded)) as client:
        held = client.post("/remittances/REC-SUB-023-0/confirm")
        assert held.status_code == 201
        assert held.json()["status"] == "UNMATCHED"

        worklist = client.get("/confirmations/worklist")
        assert worklist.status_code == 200
        body = worklist.json()
        assert body["entries"][0]["advice_record_id"] == "REC-SUB-023-0"

        refused = client.post(
            "/confirmations/CONF-REC-SUB-023-0/not-ours",
            json={"actor": CONTROLLER, "reason": " "},
        )
        assert refused.status_code == 409

        marked = client.post(
            "/confirmations/CONF-REC-SUB-023-0/not-ours",
            json={"actor": CONTROLLER,
                  "reason": "names a run from another bank entirely"},
        )
        assert marked.status_code == 200
        assert marked.json()["status"] == "NOT_OURS"
        assert client.get("/confirmations/worklist").json()["entries"] == []

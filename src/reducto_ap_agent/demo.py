"""The traced demo: the whole synthetic AP world, end to end.

Runs every bundled submission through the pipeline on the
offline adapter, has a named approver decide the records that
reach the gate, and prints what an operator would see: the
matches, the exceptions with their codes, the fraud hold, the
refusal — then the two supplier statements reconciled line by
line with their breaks itemised and a controller's decision on
each — then the credit notes: a controller applies them
against the invoices they name (one nets, one covers its
invoice in full, two are refused by name — an over-credit and
one against the fraud hold) — the payment packet that
proposes open amounts, and executes nothing — and finally a
payment run: the approved records
assembled for a value date, the fraud hold and the exception
refused by name when they are (mistakenly) asked for, and a
treasurer releasing the batch into a bank-instruction export
that still, itself, pays no one — and finally the bank's
answer: a remittance advice naming the released run, reconciled
line by line in code, with the shorts, the overpayment, the
unaccounted members, and one line for an invoice the run never
contained, all itemised for a controller — and finally the
supplier ledger: the whole relationship with Harborlight on
one page, its totals tied to the position, and one stray
document that names a supplier AP has never seen spelled that
way, listed as unmatched rather than attached by resemblance
— and finally the paid-duplicate watch: INV-1001 sent a second
time after its run released, held on the paid evidence with
the gate refusing to decide it, confirmed a duplicate by a
controller; and the supplier's net re-bill, held on the NET
basis and released as not a duplicate — back to the exception
its own match earned, in the open — and finally one invoice
paid in parts: a fresh Harborlight bill taken at 60% by one
run and the remaining 40% by the next, standing PART_PAID in
between, with an overcommit refused by name on the way and
every reader — packet, position, ledger — landing on the
same figures at each step — and finally PO coverage: every
order's accumulated approved billing against the total the
order register states, the orders this world has together
billed past their totals ranked at the top, and the note the
crossing approval left on its record — and finally the
master's past: Cedarline's legitimate bank change, recorded
with its provenance and an effective date, and the two
invoices that prove the screen reads the master as of each
invoice's own date — one dated before the change, quoting the
old bank and clean; one dated after it, quoting the old bank
still, and held for quoting details the master had retired.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .credits import open_amount as _open_amount
from .credits import paid_amount as _paid_amount
from .datasets import _doc, _inv_fields, build_world, remittance_advice_doc
from .errors import GateError, PaymentRunError
from .graph import ApPipeline
from .object_store import InMemoryObjectStore
from .offline_docs import OfflineDocumentAdapter
from .schemas import LineItem
from .service import ApService
from .store import InMemoryRecordStore

APPROVER = "Priya Nair (AP lead)"


def run_demo() -> ApService:
    world = build_world()
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(),
        store=store,
        objects=InMemoryObjectStore(),
        checkpointer=MemorySaver(),
    )
    service = ApService(pipeline=pipeline, store=store)

    print("Trida Blueprint — Reducto Invoice AP, deployed in-VPC (offline adapter)\n")
    for submission in world.submissions:
        records = service.submit(
            submission.submission_id, submission.filename,
            submission.text.encode("utf-8"), "text/plain",
        )
        for record in records:
            match = record.match
            detail = ""
            if match:
                detail = f" match={match.status}"
                if match.exception_codes:
                    detail += f" [{', '.join(match.exception_codes)}]"
            if record.fraud_flags:
                detail += f" FRAUD: {record.fraud_flags[0]}"
            if record.refusal_reason:
                detail += f" — {record.refusal_reason}"
            if record.injection_flags:
                detail += f" injection-screen: {record.injection_flags[0]}"
            print(f"{record.record_id:<16} {record.doc_class:<18} {record.status:<20}{detail}")
            if record.status == "AWAITING_APPROVER":
                service.decide(record.record_id, "approve", APPROVER, "demo: matched, approved")
                print(f"{'':<16} → approved by {APPROVER}")

    print("\nSupplier statement reconciliation — the statements answer back:")
    for statement_id in ("REC-SUB-020-0", "REC-SUB-021-0"):
        recon = service.reconcile(statement_id)
        counts = ", ".join(
            f"{name} {n}" for name, n in recon.counts.items() if n
        )
        print(f"\n{recon.reconciliation_id} — {recon.supplier_name}, period "
              f"{recon.period} — {recon.status}")
        print(f"  totals tie: {'yes' if recon.totals_tie else 'NO'} "
              f"(stated {recon.statement_total_stated:.2f} vs lines "
              f"{recon.statement_total_lines:.2f}) · {counts}")
        for line in recon.lines:
            if line.classification == "matched":
                continue
            stmt = f"{line.statement_amount:.2f}" if line.statement_amount is not None else "—"
            ap = f"{line.ap_amount:.2f}" if line.ap_amount is not None else "—"
            print(f"  BREAK {line.invoice_number:<9} {line.classification:<28} "
                  f"statement {stmt:>8} · AP {ap:>8}")
        if all(n == 0 for name, n in recon.counts.items()
               if name not in ("matched",)):
            decided = service.decide_reconciliation(
                recon.reconciliation_id, "accept", "Marta Chen (controller)",
                "demo: statement agrees with AP",
            )
            print(f"  → {decided.status} by Marta Chen (controller)")
        else:
            decided = service.decide_reconciliation(
                recon.reconciliation_id, "flag_back", "Marta Chen (controller)",
                "demo: breaks itemised above — AP team to take them to the supplier",
            )
            print(f"  → {decided.status} by Marta Chen (controller)")

    print("\nCredit notes — a controller applies them against the invoices they name:")
    controller = "Marta Chen (controller)"
    for credit_id in ("REC-SUB-007-0", "REC-SUB-024-0"):
        applied = service.apply_credit(credit_id, controller)
        entry = next(
            c for c in service.get_record(
                applied.applied_invoice_record_id or ""
            ).credits_applied
            if c.credit_record_id == credit_id
        )
        print(f"  {entry.credit_number:<9} applied by {controller}: "
              f"{entry.amount:>7.2f} {entry.currency} against "
              f"{applied.applied_invoice_record_id}")
    for credit_id in ("REC-SUB-025-0", "REC-SUB-028-0"):
        try:
            service.apply_credit(credit_id, controller)
            print(f"  {credit_id}: applied (unexpected)")
        except GateError as exc:
            print(f"  REFUSED {credit_id}: {exc}")

    packet = service.payment_packet()
    print(f"\nPayment packet {packet.packet_id} — proposed only, nothing executed:")
    for line in packet.lines:
        detail = ""
        if line.credits_applied:
            credits = ", ".join(
                f"{c.credit_number} −{c.amount:.2f}" for c in line.credits_applied
            )
            detail = f"  (face {line.gross_amount:.2f} − credits {credits})"
        print(f"  {line.supplier_name:<28} {line.invoice_number:<10} {line.currency} {line.amount:>10.2f}{detail}")
    for currency, total in packet.totals_by_currency.items():
        print(f"  {'TOTAL':<42} {currency} {total:>10.2f}")
    if packet.unapplied_credits:
        print("  Unapplied credits (approved, on file, reducing nothing yet):")
        for credit in packet.unapplied_credits:
            print(f"    {credit.credit_number:<9} {credit.supplier_name:<28} "
                  f"{credit.currency} {credit.amount:>7.2f} — {credit.reason}")
    print(f"\n{packet.note}")

    print("\nPayment run — the batch a treasurer can release or return:")
    approved_ids = [line.record_id for line in packet.lines]
    run = service.create_payment_run(
        "2026-10-16",
        # The fraud hold and an exception are named too — the run
        # refuses them by name rather than quietly leaving them out.
        record_ids=approved_ids + ["REC-SUB-008-0", "REC-SUB-003-0"],
    )
    totals = ", ".join(
        f"{currency} {total:,.2f}" for currency, total in run.totals_by_currency.items()
    )
    print(f"\n{run.run_id} — value date {run.value_date} — {run.status} · totals: {totals}")
    for line in run.lines:
        detail = ""
        if line.credits_applied:
            credits = ", ".join(
                f"{c.credit_number} −{c.amount:.2f}" for c in line.credits_applied
            )
            detail = f"  (face {line.gross_amount:.2f} − credits {credits})"
        print(f"  MEMBER  {line.record_id:<16} {line.supplier_name:<28} "
              f"{line.invoice_number:<10} {line.currency} {line.amount:>10.2f}{detail}")
    for refusal in run.refusals:
        print(f"  REFUSED {refusal.record_id:<16} {refusal.code}: {refusal.reason}")
    released = service.decide_payment_run(
        run.run_id, "release", "Jonas Weber (treasurer)", "demo: batch reviewed"
    )
    print(f"  → {released.status} by Jonas Weber (treasurer)")
    print(f"  export: {released.export_key} · sha256 {released.export_digest[:16]}…")
    export_text = service.payment_run_export(run.run_id).decode("utf-8")
    for row in export_text.splitlines()[:4]:
        print(f"    {row}")
    print("    …")
    print("\nThe export is a file for the customer's payment systems. "
          "This system has still paid nothing.")

    print("\nRemittance confirmation — the bank answers the run:")
    advice_text = remittance_advice_doc(run.run_id, [
        ("INV-1001", 1200.00, "USD", "2026-10-16", "BNK-2026-3101"),  # exact — the net figure
        ("INV-1002", 1000.00, "USD", "2026-10-16", "BNK-2026-3102"),  # short 10.00
        ("INV-1010", 650.00, "USD", "2026-10-17", "BNK-2026-3104"),   # over 50.00
        ("INV-1013", 1080.00, "EUR", "2026-10-17", "BNK-2026-3105"),  # exact
        ("INV-9999", 123.00, "USD", "2026-10-17", "BNK-2026-3106"),   # not a member
        # INV-1014, INV-2001, INV-2002, INV-3001: no line — missing.
        # INV-2003 is not missing: it was fully credited and never
        # became a member — the run refused it by name above.
    ])
    (advice_record,) = service.submit(
        "SUB-DEMO-ADVICE", "sub-demo-advice.pdf",
        advice_text.encode("utf-8"), "text/plain",
    )
    confirmation = service.confirm_remittance(advice_record.record_id)
    counts = ", ".join(
        f"{name} {n}" for name, n in confirmation.counts.items() if n
    )
    print(f"\n{confirmation.confirmation_id} — answers {confirmation.run_id} "
          f"via advice {advice_record.record_id} — {confirmation.status}")
    print(f"  totals tie: {'yes' if confirmation.totals_tie else 'NO'} · {counts}")
    for line in confirmation.lines:
        proposed = f"{line.proposed_amount:.2f}" if line.proposed_amount is not None else "—"
        paid = f"{line.paid_amount:.2f}" if line.paid_amount is not None else "—"
        print(f"  {line.invoice_number:<9} {line.classification:<18} "
              f"proposed {proposed:>9} · paid {paid:>9} {line.currency}")
    for row in confirmation.totals:
        print(f"  {row.currency}: run {row.run_total:.2f} · confirmed "
              f"{row.confirmed_paid:.2f} · shortfall {row.shortfall:.2f} · "
              f"excess {row.excess:.2f} · missing {row.missing_proposed:.2f} · "
              f"unexpected {row.unexpected_paid:.2f}")
    decided = service.decide_confirmation(
        confirmation.confirmation_id, "accept", "Marta Chen (controller)",
        "demo: breaks noted for follow-up with the bank",
    )
    print(f"  → {decided.status} by Marta Chen (controller)")
    print("\nThe confirmation is evidence over the run. The run, its "
          "export, and every approval above are exactly as they were.")

    print("\nA late credit — CN-1003 names INV-1001, which the run above "
          "already released:")
    try:
        service.apply_credit("REC-SUB-029-0", controller)
        print("  applied (unexpected)")
    except GateError as exc:
        print(f"  REFUSED REC-SUB-029-0: {exc}")
    print("  The credit stays on file, unapplied, with its reason on the "
          "packet — next cycle's business, settled against the supplier's "
          "future invoices. It does not rewrite a released run.")

    print("\nAnd at the close — the AP position as of 2026-11-15, aged off "
          "due dates, tied to the packet in the same pass:")
    position = service.ap_position("2026-11-15")
    for supplier in position.suppliers:
        buckets = " · ".join(
            f"{name} {amount:.2f}"
            for name, amount in supplier.bucket_totals.items() if amount)
        print(f"  {supplier.supplier_name:<28} {supplier.currency} "
              f"open {supplier.open_total:>9.2f}  ({buckets})")
    for tie in position.ties:
        verdict = "TIED" if tie.tied else "POSITION_TIE_BREAK"
        print(f"  {tie.currency}: packet {tie.packet_total:.2f} · position "
              f"{tie.position_total:.2f} — {verdict}")
    for currency, states in position.state_totals_by_currency.items():
        print(f"  {currency} by state: " + " · ".join(
            f"{name} {amount:.2f}" for name, amount in states.items()))
    for held in position.fraud_held:
        print(f"  FRAUD-HELD {held.invoice_number} ({held.supplier_name}) "
              f"{held.currency} {held.open:.2f} — outside the tie, "
              "disclosed, not buried")
    for covered in position.fully_credited:
        print(f"  FULLY CREDITED {covered.invoice_number} "
              f"({covered.supplier_name}) face {covered.face:.2f} — "
              "credits brought it to 0.00; it owes nothing and ages nothing")
    for settled in position.fully_paid:
        print(f"  FULLY PAID {settled.invoice_number} "
              f"({settled.supplier_name}) face {settled.face:.2f} − "
              f"credits {settled.credits:.2f} − paid {settled.paid:.2f} — "
              "released money brought it to 0.00; disclosed here so the "
              "close sees the payment happened")

    print("\nOne more document arrives — from a supplier name AP has "
          "never seen spelled this way:")
    stray_text = _doc("INVOICE", 1, [
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
    (stray,) = service.submit(
        "SUB-DEMO-STRAY", "sub-demo-stray.pdf",
        stray_text.encode("utf-8"), "text/plain",
    )
    joined = stray.supplier_id or (
        "NONE — the name matched no master entry exactly, "
        "and PO-9998 resolves nothing"
    )
    print(f"  {stray.record_id} {stray.doc_class} {stray.status} — "
          f"joined supplier: {joined}")

    print("\nThe supplier ledger — Harborlight Marine Supply (SUP-08), "
          "the whole relationship on one page:")
    ledger = service.supplier_ledger("SUP-08", "2026-11-15")
    doc_counts = {}
    for doc in ledger.documents:
        doc_counts[doc.doc_class] = doc_counts.get(doc.doc_class, 0) + 1
    print("  documents: " + " · ".join(
        f"{name} {n}" for name, n in sorted(doc_counts.items())))
    print(f"  bank (from the master): {ledger.bank.bank_name} · "
          f"{ledger.bank.bank_account} · warning: "
          f"{ledger.bank.warning or 'none — no document disagrees'}")
    for inv in ledger.invoices:
        money_state = inv.payment_state or "not in a run"
        if inv.payment_confirmation:
            money_state += f" · bank: {inv.payment_confirmation}"
        ageing = inv.bucket or ("fully credited" if inv.fully_credited
                                else "outside the position")
        paid_part = (f" − paid {inv.paid_total:>6.2f}"
                     if inv.paid_total else "")
        print(f"  {inv.invoice_number:<9} {inv.status:<12} face "
              f"{inv.face:>8.2f} − credits {inv.credits_total:>6.2f}"
              f"{paid_part} = open "
              f"{inv.open:>8.2f} {inv.currency} · {money_state} · {ageing}")
    for credit in ledger.credits:
        state = credit.credit_state or "—"
        detail = (f"applied against {credit.applied_invoice_record_id}"
                  if credit.credit_state == "APPLIED"
                  else credit.unapplied_reason or "")
        print(f"  {credit.credit_number:<9} credit {credit.amount:>8.2f} "
              f"{credit.currency} · {state} — {detail}")
    for brk in ledger.open_breaks:
        stmt = (f"{brk.statement_amount:.2f}"
                if brk.statement_amount is not None else "—")
        ap = f"{brk.ap_amount:.2f}" if brk.ap_amount is not None else "—"
        print(f"  OPEN BREAK {brk.invoice_number:<9} "
              f"{brk.classification:<28} statement {stmt:>8} · AP {ap:>8} "
              f"({brk.reconciliation_id}, {brk.recon_status})")
    for totals in ledger.totals:
        print(f"  TOTALS {totals.currency}: face {totals.face_total:.2f} − "
              f"credits {totals.credits_total:.2f} − paid "
              f"{totals.paid_total:.2f} = open "
              f"{totals.open_total:.2f} · position "
              f"{totals.position_open_total:.2f} — "
              f"{'TIED' if totals.tied else 'POSITION_TIE_BREAK'}")
    index = service.supplier_index("2026-11-15")
    print(f"  ledger index: {len(index.suppliers)} suppliers on the "
          f"master · unmatched documents: {index.unmatched_count}")
    for doc in index.unmatched:
        print(f"  UNMATCHED {doc.record_id} — wrote its supplier as "
              f"{doc.name_as_written!r}; attached to no supplier's ledger")
    print("\nThe paid-duplicate watch — weeks later, the same bill "
          "arrives again:")
    sub001_text = next(
        s.text for s in world.submissions if s.submission_id == "SUB-001"
    )
    (resend,) = service.submit(
        "SUB-DEMO-RESEND", "sub-demo-resend.pdf",
        sub001_text.encode("utf-8"), "text/plain",
    )
    hold = resend.duplicate_hold
    print(f"  {resend.record_id} invoice INV-1001 again — {resend.status}")
    print(f"  evidence: already released for payment as "
          f"{hold.original_record_id} — {hold.currency} "
          f"{hold.amount_paid:.2f} instructed across released runs, "
          f"most recently run {hold.payment_run_id} (face "
          f"{hold.paid_face_amount:.2f}); this arrival states "
          f"{hold.arrived_amount:.2f} — the {hold.amount_basis} figure")
    try:
        service.decide(resend.record_id, "approve", APPROVER, "demo: pay it")
        print("  gate decided it (unexpected)")
    except GateError as exc:
        print(f"  GATE REFUSED: {exc}")
    confirmed = service.resolve_duplicate_hold(
        resend.record_id, "CONFIRMED_DUPLICATE", "Marta Chen (controller)",
        "demo: the supplier confirmed the original was paid — this is "
        "the same bill, sent twice",
    )
    print(f"  → {confirmed.status} — the hold becomes a permanent "
          "refusal on the record; the original payment is untouched")
    s02 = world.supplier("SUP-02")
    net_text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1001", "PO-6001", 1200.00,
    ), [LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00)])
    (net_resend,) = service.submit(
        "SUB-DEMO-RESEND-NET", "sub-demo-resend-net.pdf",
        net_text.encode("utf-8"), "text/plain",
    )
    net_hold = net_resend.duplicate_hold
    print(f"\n  {net_resend.record_id} — the supplier re-bills at "
          f"{net_hold.arrived_amount:.2f}, the net figure the run "
          f"instructed after CN-1001: {net_resend.status} "
          f"({net_hold.amount_basis} basis)")
    released = service.resolve_duplicate_hold(
        net_resend.record_id, "RELEASED_NOT_DUPLICATE",
        "Marta Chen (controller)",
        "demo: a corrected re-bill for the uncredited goods, not the "
        "paid bill again",
    )
    print(f"  → released as not a duplicate → {released.status} "
          f"{released.match.exception_codes} — the release settles the "
          "duplicate question only; the re-bill still disagrees with "
          "the PO total by exactly the credit, and that break is the "
          "AP team's to work, as an exception, in the open")
    watch = service.list_duplicate_watch("2026-11-15")
    print(f"  watch list now: {len(watch)} standing hold(s)")

    print("\nPaid in parts — a big bill rarely waits for one cycle:")
    s08 = world.supplier("SUP-08")
    part_text = _doc("INVOICE", 1, _inv_fields(
        s08, "INV-2090", "PO-6020", 900.00,
    ), [LineItem(sku="SKU-N1", description="Anchor chain",
                 quantity=10, unit_price=90.00, amount=900.00)])
    (part_invoice,) = service.submit(
        "SUB-DEMO-PARTS", "sub-demo-parts.pdf",
        part_text.encode("utf-8"), "text/plain",
    )
    service.decide(part_invoice.record_id, "approve", APPROVER,
                   "demo: matched, approved")
    print(f"  {part_invoice.record_id} INV-2090 (Harborlight) — "
          "face 900.00, approved; the treasurer pays 60% this cycle")
    part_run = service.create_payment_run(
        "2026-11-20", record_ids=[part_invoice.record_id],
        amounts={part_invoice.record_id: 540.00})
    (part_line,) = part_run.lines
    print(f"  {part_run.run_id} line: part {part_line.amount:.2f} of "
          f"open {part_line.open_amount:.2f} — the line is the "
          "instruction, and it instructs the part only")
    service.decide_payment_run(
        part_run.run_id, "release", "Jonas Weber (treasurer)",
        "demo: first part reviewed")
    standing = service.get_record(part_invoice.record_id)
    print(f"  → released: the invoice stands {standing.payment_state} — "
          f"paid {_paid_amount(standing):.2f}, open "
          f"{_open_amount(standing):.2f}; it is "
          "not paid, and nothing calls it paid")
    try:
        service.create_payment_run(
            "2026-11-27", record_ids=[part_invoice.record_id],
            amounts={part_invoice.record_id: 500.00})
        print("  a 500.00 second part assembled (unexpected)")
    except PaymentRunError as exc:
        print(f"  REFUSED: {exc}")
    rest_run = service.create_payment_run(
        "2026-12-04", record_ids=[part_invoice.record_id],
        amounts={part_invoice.record_id: 360.00})
    service.decide_payment_run(
        rest_run.run_id, "release", "Jonas Weber (treasurer)",
        "demo: the rest, next cycle")
    settled = service.get_record(part_invoice.record_id)
    print(f"  → second release: {settled.payment_state} — paid "
          f"{_paid_amount(settled):.2f} of face 900.00 across two runs, "
          f"open {_open_amount(settled):.2f}; the position discloses it "
          "as fully paid, and a resend of it would be held on the "
          "900.00 the two runs instructed together")

    print("\nPO coverage — what has been billed against each order, "
          "in total (the accumulation no single match could see):")
    coverage = service.po_coverage()
    for row in coverage.orders:
        if not row.lines and row.state == "WITHIN":
            continue
        pct = f"{row.pct:.1f}%" if row.pct is not None else "—"
        total = (f"{row.total:.2f}" if row.total is not None
                 else "not on file")
        print(f"  {row.state:<9} {row.po_number} ({row.supplier_name}) "
              f"— billed {row.currency} {row.billed:>9.2f} of {total} "
              f"· {pct}")
        for line in row.lines:
            print(f"    {line.invoice_number:<9} bills "
                  f"{line.billable:>8.2f} (face {line.face:.2f} − "
                  f"credits {line.credits:.2f}) — cumulative "
                  f"{line.cumulative_billed:.2f}")
    quiet = sum(
        1 for r in coverage.orders if not r.lines and r.state == "WITHIN"
    )
    print(f"  …and {quiet} orders with nothing billed against them, "
          "WITHIN at 0.0%.")
    noted = service.get_record("REC-SUB-030-0")
    for entry in noted.audit:
        if entry["event"] == "po_coverage_at_decision":
            print(f"  On INV-3001's record, from the moment of its "
                  f"approval: {entry['detail']}")
    print("  No approval above was blocked by any of this — coverage "
          "informs the gate. What it removes is the ability to say "
          "the accumulation was never shown.")

    print("\nThe master's past — Cedarline changes banks, "
          "legitimately, and the screen reads history:")
    service.record_bank_version(
        "SUP-03", bank_account="US00 CEDR 9999 8888 7777",
        bank_name="Cedar Bank", effective_from="2026-10-01",
        actor="Priya Nair (AP lead)",
        source=("supplier letter dated 2026-09-25, verified by "
                "callback to the number on file"),
    )

    def cedar_invoice(number: str, inv_date: str, bank: str) -> str:
        return _doc("INVOICE", 1, [
            ("Supplier", "Cedarline Packaging Co"),
            ("Invoice Number", number),
            ("Invoice Date", inv_date),
            ("Due Date", "2026-11-14"),
            ("PO Reference", "PO-6002"),
            ("Currency", "USD"),
            ("Bank Account", bank),
            ("Subtotal", "1000.00"), ("Tax", "0.00"),
            ("Total", "1000.00"),
        ], [LineItem(sku="SKU-B1", description="Shipping boxes",
                     quantity=100, unit_price=10.00, amount=1000.00)])

    old_bank = "US00 CEDR 2222 3333 4444"
    for sid, number, inv_date, bank in (
        ("SUB-D-H1", "INV-D901", "2026-09-20", old_bank),
        ("SUB-D-H2", "INV-D902", "2026-10-05", old_bank),
    ):
        (record,) = service.submit(
            sid, f"{sid.lower()}.pdf",
            cedar_invoice(number, inv_date, bank).encode(),
            "text/plain")
        flags = "; ".join(record.fraud_flags) or "no flags"
        print(f"  {number} dated {inv_date}, quoting the old bank "
              f"…{old_bank[-4:]} → {record.status} ({flags})")
    timeline = service.bank_history("SUP-03", "2026-11-15")
    print("  Cedarline's bank timeline, as the master records it:")
    for v in timeline.versions:
        effective = (
            f"effective from {v.effective_from}" if v.effective_from
            else "the seeded details — effective before the record")
        provenance = (
            f"recorded by {v.recorded_by} from: {v.source}"
            if v.recorded_by else v.source)
        marker = (" · in force at 2026-11-15"
                  if v.version == timeline.current_version else "")
        print(f"    v{v.version} · {v.bank_name} · {v.bank_account} · "
              f"{effective} · {provenance}{marker}")
    print("  Same bank on both invoices, opposite outcomes — the "
          "difference is the date, and the master can now say why.")

    return service


def main() -> None:
    run_demo()


if __name__ == "__main__":
    main()

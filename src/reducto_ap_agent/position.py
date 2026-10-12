"""The AP position at close: what we owe, to whom, how old.

The packet (service.payment_packet) answers "what would we
pay next" — a proposal list. The close asks a different
question: what does the AP subledger say we owe, aged, by
supplier, and does it agree with the packet? This module is
that read, computed in one pass over one snapshot of the
record store:

- **Ageing runs off due dates.** Days past due is
  ``(as_of - due_date).days`` against an explicit as-of date —
  the repo's hermetic idiom, no hidden wall clock. What
  matters at close is how far past its terms a payable
  stands, not how long ago the invoice was issued, so a
  not-yet-due invoice is CURRENT however old it is. Bucket
  edges are authored policy (policy.AgeingPolicy), printed on
  the report, upper-edge inclusive: exactly 30 days past due
  is in 1–30, not 31–60. An invoice with no readable due date
  is UNDATED — its own line class, totalled and shown, never
  silently bucketed by an age it does not carry.
- **One open-amount rule.** Every figure here comes from
  ``credits.open_amount`` — face minus the applied-credit
  ledger minus the payments already released — the same
  computation the packet and the payment runs read. The
  position never recomputes it differently; a second
  definition of "open" is how subledgers drift.
- **The state of the money is a second split of the same
  total.** Each aged line is OPEN_FREE, IN_RUN (held by an
  open payment run), or PART_PAID (released runs have
  instructed part of it; the line ages only the remainder,
  at the invoice's due date — paid money does not age).
  Beside the open split, each line and row carries the memo
  figure the open was reduced by: the cumulative PAID total
  from the same payments ledger, so a row reads face −
  credits − paid = open in one glance. The state columns and
  the ageing columns are two readings of one open figure and
  must both foot to it — the report checks that itself
  (``splits_agree``).
- **The tie-out.** The packet is not as-of parameterised, so
  the position computes BOTH totals from the same record set
  in this one pass: the packet total by the packet's own
  inclusion rule (every APPROVED invoice at its open amount),
  and the position total as the sum of the aged lines. In a
  clean world they are the same number. They can differ only
  when a record's open amount is negative — its applied
  ledger exceeds its face. Application refuses over-credit
  in code, so that state means a corrupted record: it is
  kept out of the buckets, listed as a NEGATIVE_OPEN anomaly
  with both figures, and the difference breaks the tie by
  name — POSITION_TIE_BREAK, per currency. The report never
  presents itself as tied when it is not.
- **Fraud-held money is disclosed, not buried.** An invoice
  on FRAUD_REVIEW is outside the packet's universe and so
  outside the tie, but it is real money in dispute: it is
  listed beside the position with its flags. Fully credited
  invoices (open exactly zero) are listed too — they owe
  nothing and age nothing, and the close should see the
  cover happened. Fully paid invoices (open exactly zero,
  reached by released instructions) are listed the same way,
  with the cumulative the runs instructed: the close should
  see the payment happened, and the report's paid totals —
  the aged lines' paid figures plus the fully paid list —
  are the whole of the money the runs have instructed.
- **Money held at the door is disclosed the same way.** An
  invoice in NEW_SUPPLIER_REVIEW (enrolment.py) is not a
  payable — its payee stands on no master record — so it is
  outside the tie too, listed beside the position with the
  supplier name exactly as its document wrote it: money
  waiting on an enrolment decision is visible at the close,
  never silently absent and never quietly owed.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from .credits import open_amount, paid_amount
from .policy import AGEING_BUCKETS, DEFAULT_AGEING, AgeingPolicy
from .schemas import (
    CurrencyTie,
    DocumentRecord,
    FraudHeldLine,
    FullyCreditedLine,
    FullyPaidLine,
    NewSupplierHeldLine,
    PositionAnomaly,
    PositionLine,
    PositionReport,
    SupplierPosition,
)

_STATE_BY_PAYMENT_STATE = {None: "OPEN_FREE", "IN_RUN": "IN_RUN",
                           "PAID_PROPOSED": "PAID_PROPOSED",
                           "PART_PAID": "PART_PAID"}

_STATE_COLUMNS = ("OPEN_FREE", "IN_RUN", "PAID_PROPOSED", "PART_PAID")


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def _parse_date(value: object) -> date | None:
    """A due date off the record, read strictly. Anything that
    is not an ISO calendar date is not a date the ageing can
    use — the line falls to UNDATED rather than ageing on a
    guess."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def bucket_for(days_past_due: int, policy: AgeingPolicy = DEFAULT_AGEING) -> str:
    """The bucket for a days-past-due figure. Upper edges are
    inclusive: exactly ``first_edge`` days is the first bucket,
    not the second."""
    if days_past_due <= 0:
        return "CURRENT"
    if days_past_due <= policy.first_edge:
        return "1-30"
    if days_past_due <= policy.second_edge:
        return "31-60"
    if days_past_due <= policy.third_edge:
        return "61-90"
    return "90+"


def _face(record: DocumentRecord) -> float:
    assert record.extraction is not None
    return _cents(float(record.extraction.value("total") or 0.0))


def _credits(record: DocumentRecord) -> float:
    return _cents(sum(entry.amount for entry in record.credits_applied))


def _paid(record: DocumentRecord) -> float:
    return paid_amount(record)


def build_position(
    records: list[DocumentRecord],
    as_of: str,
    policy: AgeingPolicy = DEFAULT_AGEING,
) -> PositionReport:
    """The position over ``records`` aged as of ``as_of`` (an
    ISO date). Pure: reads records, writes nothing."""
    as_of_date = date.fromisoformat(str(as_of).strip()[:10])

    packet_totals: dict[str, float] = {}
    suppliers: dict[tuple[str, str, str], SupplierPosition] = {}
    fraud_held: list[FraudHeldLine] = []
    new_supplier_held: list[NewSupplierHeldLine] = []
    fully_credited: list[FullyCreditedLine] = []
    fully_paid: list[FullyPaidLine] = []
    anomalies: list[PositionAnomaly] = []

    for record in records:
        if record.doc_class != "invoice" or record.extraction is None:
            continue
        currency = record.extraction.currency
        invoice_number = str(record.extraction.value("invoice_number") or "")
        if record.status == "FRAUD_REVIEW":
            fraud_held.append(FraudHeldLine(
                record_id=record.record_id,
                supplier_name=record.supplier_name,
                invoice_number=invoice_number,
                currency=currency,
                face=_face(record),
                open=open_amount(record),
                fraud_flags=list(record.fraud_flags),
            ))
            continue
        if record.status == "NEW_SUPPLIER_REVIEW":
            # Stopped at the door (enrolment.py): a payee
            # nobody has enrolled is not a payable, so it
            # stands outside the tie like fraud-held money —
            # disclosed with the name as the document wrote
            # it, there being no master name to show.
            new_supplier_held.append(NewSupplierHeldLine(
                record_id=record.record_id,
                supplier_name=str(
                    record.extraction.value("supplier_name") or ""),
                invoice_number=invoice_number,
                currency=currency,
                face=_face(record),
                open=open_amount(record),
            ))
            continue
        if record.status != "APPROVED":
            continue
        # The packet's own inclusion rule, applied in this same
        # pass: every APPROVED invoice at its open amount.
        open_now = open_amount(record)
        packet_totals[currency] = _cents(
            packet_totals.get(currency, 0.0) + open_now)
        if open_now < 0:
            anomalies.append(PositionAnomaly(
                record_id=record.record_id,
                supplier_name=record.supplier_name,
                invoice_number=invoice_number,
                currency=currency,
                face=_face(record),
                credits=_credits(record),
                paid=_paid(record),
                open=open_now,
                detail=(
                    f"applied credits of {_credits(record):.2f} and "
                    f"released payments of {_paid(record):.2f} exceed the "
                    f"approved face of {_face(record):.2f} — application "
                    "refuses over-credit and assembly refuses an "
                    "over-committed part, so these ledgers could only "
                    "have been written outside those paths; the record "
                    "is kept out of the ageing and the difference breaks "
                    "the tie until the record is repaired"
                ),
            ))
            continue
        if open_now == 0:
            if _paid(record) > 0:
                fully_paid.append(FullyPaidLine(
                    record_id=record.record_id,
                    supplier_name=record.supplier_name,
                    invoice_number=invoice_number,
                    currency=currency,
                    face=_face(record),
                    credits=_credits(record),
                    paid=_paid(record),
                ))
            elif record.credits_applied:
                fully_credited.append(FullyCreditedLine(
                    record_id=record.record_id,
                    supplier_name=record.supplier_name,
                    invoice_number=invoice_number,
                    currency=currency,
                    face=_face(record),
                    credits=_credits(record),
                ))
            continue
        due_raw = record.extraction.value("due_date")
        due = _parse_date(due_raw)
        if due is None:
            bucket = "UNDATED"
            days_past_due = None
        else:
            days_past_due = (as_of_date - due).days
            bucket = bucket_for(days_past_due, policy)
        line = PositionLine(
            record_id=record.record_id,
            supplier_id=record.supplier_id,
            supplier_name=record.supplier_name,
            invoice_number=invoice_number,
            currency=currency,
            face=_face(record),
            credits=_credits(record),
            paid=_paid(record),
            open=open_now,
            invoice_date=str(record.extraction.value("invoice_date") or ""),
            due_date=str(due_raw).strip() if due_raw else "",
            days_past_due=days_past_due,
            bucket=bucket,  # type: ignore[arg-type]
            state=_STATE_BY_PAYMENT_STATE[record.payment_state],  # type: ignore[arg-type]
            payment_run_id=record.payment_run_id,
        )
        key = (record.supplier_name, currency, record.supplier_id or "")
        supplier = suppliers.get(key)
        if supplier is None:
            supplier = SupplierPosition(
                supplier_id=record.supplier_id,
                supplier_name=record.supplier_name,
                currency=currency,
                bucket_totals={name: 0.0 for name in AGEING_BUCKETS},
                state_totals={name: 0.0 for name in _STATE_COLUMNS},
            )
            suppliers[key] = supplier
        supplier.lines.append(line)
        supplier.bucket_totals[bucket] = _cents(
            supplier.bucket_totals[bucket] + open_now)
        supplier.state_totals[line.state] = _cents(
            supplier.state_totals[line.state] + open_now)
        supplier.face_total = _cents(supplier.face_total + line.face)
        supplier.credits_total = _cents(supplier.credits_total + line.credits)
        supplier.paid_total = _cents(supplier.paid_total + line.paid)
        supplier.open_total = _cents(supplier.open_total + open_now)

    supplier_rows = sorted(
        suppliers.values(),
        key=lambda s: (s.supplier_name, s.currency),
    )
    for supplier in supplier_rows:
        supplier.lines.sort(
            key=lambda l: (l.due_date or "9999-99-99", l.invoice_number))

    bucket_totals: dict[str, dict[str, float]] = {}
    state_totals: dict[str, dict[str, float]] = {}
    open_totals: dict[str, float] = {}
    paid_totals: dict[str, float] = {}
    for supplier in supplier_rows:
        cur = supplier.currency
        bt = bucket_totals.setdefault(
            cur, {name: 0.0 for name in AGEING_BUCKETS})
        for name, amount in supplier.bucket_totals.items():
            bt[name] = _cents(bt[name] + amount)
        st = state_totals.setdefault(
            cur, {name: 0.0 for name in _STATE_COLUMNS})
        for name, amount in supplier.state_totals.items():
            st[name] = _cents(st[name] + amount)
        open_totals[cur] = _cents(open_totals.get(cur, 0.0)
                                  + supplier.open_total)
        paid_totals[cur] = _cents(paid_totals.get(cur, 0.0)
                                  + supplier.paid_total)
    for paid_line in fully_paid:
        # The fully paid list is the other half of the paid
        # story: aged lines carry the paid figures of invoices
        # still partly open; these carry the rest.
        paid_totals[paid_line.currency] = _cents(
            paid_totals.get(paid_line.currency, 0.0) + paid_line.paid)

    ties: list[CurrencyTie] = []
    for currency in sorted(set(packet_totals) | set(open_totals)):
        packet_total = _cents(packet_totals.get(currency, 0.0))
        position_total = _cents(open_totals.get(currency, 0.0))
        difference = _cents(packet_total - position_total)
        ties.append(CurrencyTie(
            currency=currency,
            packet_total=packet_total,
            position_total=position_total,
            difference=difference,
            tied=difference == 0,
        ))

    splits_agree = all(
        _cents(sum(bucket_totals.get(cur, {}).values())) == total
        and _cents(sum(state_totals.get(cur, {}).values())) == total
        for cur, total in open_totals.items()
    )

    fraud_totals: dict[str, float] = {}
    for held in fraud_held:
        fraud_totals[held.currency] = _cents(
            fraud_totals.get(held.currency, 0.0) + held.open)

    new_supplier_totals: dict[str, float] = {}
    for held in new_supplier_held:
        new_supplier_totals[held.currency] = _cents(
            new_supplier_totals.get(held.currency, 0.0) + held.open)

    return PositionReport(
        as_of=as_of_date.isoformat(),
        bucket_edges=[policy.first_edge, policy.second_edge,
                      policy.third_edge],
        suppliers=supplier_rows,
        bucket_totals_by_currency=bucket_totals,
        state_totals_by_currency=state_totals,
        open_totals_by_currency=open_totals,
        paid_totals_by_currency=paid_totals,
        ties=ties,
        tie_status="TIED" if all(t.tied for t in ties)
        else "POSITION_TIE_BREAK",
        fraud_held=sorted(
            fraud_held, key=lambda h: (h.supplier_name, h.invoice_number)),
        fraud_held_totals_by_currency=fraud_totals,
        new_supplier_held=sorted(
            new_supplier_held,
            key=lambda h: (h.supplier_name, h.invoice_number)),
        new_supplier_held_totals_by_currency=new_supplier_totals,
        fully_credited=sorted(
            fully_credited,
            key=lambda f: (f.supplier_name, f.invoice_number)),
        fully_paid=sorted(
            fully_paid,
            key=lambda f: (f.supplier_name, f.invoice_number)),
        anomalies=anomalies,
        splits_agree=splits_agree,
    )


def export_key_for_position(as_of: str) -> str:
    """The object-store key for a position export — versioned
    like the payment-run export, so a format change is a new
    key, never a silent rewrite of an artifact a controller
    already filed."""
    return f"positions/{as_of}/ap-position-v1.csv"


def position_csv(report: PositionReport) -> bytes:
    """The position as a CSV a controller can file: supplier
    rows with both splits, the records under them, the tie-out,
    and the disclosed sections (fraud-held, fully credited,
    anomalies). Deterministic — the same report renders the
    same bytes."""
    out = io.StringIO()
    writer = csv.writer(out)
    edges = report.bucket_edges
    writer.writerow(["AP POSITION", f"as of {report.as_of}"])
    writer.writerow([
        "Ageing basis",
        "due date; buckets upper-edge inclusive: CURRENT (not past "
        f"due), 1-{edges[0]}, {edges[0] + 1}-{edges[1]}, "
        f"{edges[1] + 1}-{edges[2]}, {edges[2]}+, UNDATED (no due "
        "date on record)",
    ])
    writer.writerow(["Tie status", report.tie_status])
    writer.writerow([])
    writer.writerow([
        "Supplier", "Currency", "Face", "Credits applied",
        "Payments released", "Open",
        "CURRENT", "1-30", "31-60", "61-90", "90+", "UNDATED",
        "OPEN_FREE", "IN_RUN", "PAID_PROPOSED", "PART_PAID",
    ])
    for s in report.suppliers:
        writer.writerow([
            s.supplier_name, s.currency, f"{s.face_total:.2f}",
            f"{s.credits_total:.2f}", f"{s.paid_total:.2f}",
            f"{s.open_total:.2f}",
            *(f"{s.bucket_totals[name]:.2f}" for name in AGEING_BUCKETS),
            *(f"{s.state_totals[name]:.2f}" for name in _STATE_COLUMNS),
        ])
    writer.writerow([])
    writer.writerow([
        "Record", "Supplier", "Invoice", "Currency", "Due date",
        "Days past due", "Bucket", "State", "Payment run", "Face",
        "Credits applied", "Payments released", "Open",
    ])
    for s in report.suppliers:
        for line in s.lines:
            writer.writerow([
                line.record_id, line.supplier_name, line.invoice_number,
                line.currency, line.due_date,
                "" if line.days_past_due is None else line.days_past_due,
                line.bucket, line.state, line.payment_run_id or "",
                f"{line.face:.2f}", f"{line.credits:.2f}",
                f"{line.paid:.2f}", f"{line.open:.2f}",
            ])
    writer.writerow([])
    writer.writerow(["TIE-OUT", "Currency", "Packet open total",
                     "Position open total", "Difference", "Tied"])
    for tie in report.ties:
        writer.writerow([
            "", tie.currency, f"{tie.packet_total:.2f}",
            f"{tie.position_total:.2f}", f"{tie.difference:.2f}",
            "yes" if tie.tied else "POSITION_TIE_BREAK",
        ])
    if report.fraud_held:
        writer.writerow([])
        writer.writerow(["FRAUD-HELD (outside the tie)", "Supplier",
                         "Invoice", "Currency", "Face", "Open", "Flags"])
        for held in report.fraud_held:
            writer.writerow([
                held.record_id, held.supplier_name, held.invoice_number,
                held.currency, f"{held.face:.2f}", f"{held.open:.2f}",
                "; ".join(held.fraud_flags),
            ])
    if report.new_supplier_held:
        writer.writerow([])
        writer.writerow(["NEW-SUPPLIER HELD (outside the tie)",
                         "Supplier as written", "Invoice", "Currency",
                         "Face", "Open"])
        for held in report.new_supplier_held:
            writer.writerow([
                held.record_id, held.supplier_name, held.invoice_number,
                held.currency, f"{held.face:.2f}", f"{held.open:.2f}",
            ])
    if report.fully_credited:
        writer.writerow([])
        writer.writerow(["FULLY CREDITED (open 0.00)", "Supplier",
                         "Invoice", "Currency", "Face", "Credits applied"])
        for line in report.fully_credited:
            writer.writerow([
                line.record_id, line.supplier_name, line.invoice_number,
                line.currency, f"{line.face:.2f}", f"{line.credits:.2f}",
            ])
    if report.fully_paid:
        writer.writerow([])
        writer.writerow(["FULLY PAID (open 0.00)", "Supplier",
                         "Invoice", "Currency", "Face", "Credits applied",
                         "Payments released"])
        for line in report.fully_paid:
            writer.writerow([
                line.record_id, line.supplier_name, line.invoice_number,
                line.currency, f"{line.face:.2f}", f"{line.credits:.2f}",
                f"{line.paid:.2f}",
            ])
    if report.anomalies:
        writer.writerow([])
        writer.writerow(["ANOMALIES", "Record", "Supplier", "Invoice",
                         "Currency", "Face", "Credits applied",
                         "Payments released", "Open", "Code"])
        for anomaly in report.anomalies:
            writer.writerow([
                "", anomaly.record_id, anomaly.supplier_name,
                anomaly.invoice_number, anomaly.currency,
                f"{anomaly.face:.2f}", f"{anomaly.credits:.2f}",
                f"{anomaly.paid:.2f}",
                f"{anomaly.open:.2f}", anomaly.code,
            ])
    return out.getvalue().encode("utf-8")

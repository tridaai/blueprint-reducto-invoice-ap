"""Unknown suppliers stop at the door: the new-supplier hold.

Every judgement the pipeline makes about a payable document
keys off the supplier master: the match reads the orders the
master's suppliers were issued, the fraud screen compares the
document's bank account against the master's, coverage and
the ledger join on the master's identity. A document whose
supplier the master does not carry therefore had no governed
fate. Its name failed the intake's exact match, no referenced
purchase order resolved it, the fraud screen silently compared
against nothing (a screen with no master record returns no
flags — ``guardrails`` says so), and the record washed up as a
generic match exception, visible only as an unmatched line on
the ledger index. Nothing named what it actually was: the
highest-risk arrival in accounts payable. A new payee is
exactly where payment fraud enters — the first invoice from a
supplier nobody has enrolled is the document an attacker
forges, because there is no history for it to disagree with.

So an unresolvable payable document now stops at the door,
in its own named state (``NEW_SUPPLIER_REVIEW``):

- **Unresolved is defined exactly.** The intake resolves a
  supplier two ways — the name the document states, matched
  exactly against the master under the master's own
  normalisation (strip, lower-case: ``store.ReferenceData``),
  or the supplier of the purchase order the document
  references. Neither resolving is the whole of it: a name
  that ALMOST matches is still unresolved, and the hold names
  the near-miss as evidence (below) — it is never attached by
  resemblance, exactly as the ledger has always refused to
  join by resemblance.
- **The hold precedes every judgement.** A held record has
  not been matched (there is nothing governed to match it
  against) and has not been fraud-screened — the screen's
  input is the master record that does not exist, and the
  record says so rather than wearing a clean screen it never
  had. It is not a payable: the packet, the runs, and the
  position read APPROVED records, and the position discloses
  the held money beside its tie instead. The approver's gate
  cannot decide it (service.decide refuses by name, the
  fraud-hold precedent); a payment run refuses it by name.
- **The near-miss is evidence, never a join.** When the name
  as written stands within ``NEAR_MISS_MAX_EDITS`` edits of a
  master name (the worklist's Levenshtein arithmetic, over
  the normalised forms), the hold names that supplier. The
  commonest case is a misspelling of a governed supplier —
  and the governed response to a misspelling is a refusal
  that says so, not a second master record for the same
  company wearing a typo. The controller reads the near-miss
  and decides which world they are in.
- **Two exits, both a named controller's acts, both recorded
  on the record forever** (service.py owns the acts; this
  module is the evidence and the queue):
  - **Enrol** — create the master record under the identity
    the documents stated, with bank details recorded as
    version 1 under the bankmaster governance whole: a named
    controller, the details, and the source they were
    verified from — because enrolment IS a bank-details
    recording, the most sensitive write in this system, and
    an unattributed one is exactly the fraud pattern wearing
    a master's clothes. Enrolling releases every held record
    of the identity back into the pipeline at the point it
    stopped (the pipeline re-runs resolution, screen, and
    match on it) — and enrolment does not launder a
    document's own claims: a released invoice whose stated
    bank details differ from the enrolled ones lands on
    FRAUD_REVIEW by the ordinary as-of rule, like any
    supplier's document.
  - **Refuse** — the document is refused as
    ``NOT_A_KNOWN_SUPPLIER``, terminally, with the
    controller's reason as its refusal reason; the record is
    retained read-only, like every refused record. Refusal
    is per record — one bad document does not condemn an
    identity's other paperwork, which may still be enrolled.
- **Repeat arrivals group; the group is a reading, not a
  record.** Held records are grouped by the identity they
  stated under the intake's own normalisation — so a group
  is exactly what one enrolment resolves, nothing broader
  (a differently-spelled arrival is its own group and its
  own decision). The queue stores nothing; enrolment and
  refusal write records and the master, never the group.

Where the master record lives: the reference data, beside
the suppliers themselves (store.py states the arrangement —
reference data is persisted nowhere, and the migration
ladder covers the records the pipeline writes). Enrolment
extends that same surface, the way recording a bank version
did in round 11; no migration exists for it to need.
"""

from __future__ import annotations

from .schemas import (
    DocumentRecord,
    NewSupplierGroup,
    NewSupplierReview,
    Supplier,
    SupplierHold,
)
from .worklist import edit_distance

# The near-miss yardstick: a stated name within this many
# edits of a master name (over the normalised forms) is
# named on the hold as the supplier it most resembles. Two
# edits catches the dropped space and the transposed letters
# of a real misspelling without reaching across genuinely
# different names.
NEAR_MISS_MAX_EDITS = 2


def normalise_name(name: str) -> str:
    """The intake's own name normalisation — strip and
    lower-case, exactly the key the supplier master indexes
    by (store.ReferenceData), so a review group is exactly
    the identity one enrolment creates on the master."""
    return str(name or "").strip().lower()


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def find_near_miss(
    name_as_written: str, suppliers: list[Supplier]
) -> Supplier | None:
    """The master supplier the stated name most resembles, or
    None. Exact matches are not near-misses (an exact match
    would have resolved and there would be no hold); the
    winner is the smallest edit distance within the yardstick,
    ties broken by master order — the order the deployment
    loaded — so the evidence is deterministic."""
    key = normalise_name(name_as_written)
    if not key:
        return None
    best: Supplier | None = None
    best_distance = NEAR_MISS_MAX_EDITS + 1
    for supplier in suppliers:
        candidate = normalise_name(supplier.name)
        if candidate == key:
            continue
        distance = edit_distance(key, candidate, NEAR_MISS_MAX_EDITS)
        if distance < best_distance:
            best, best_distance = supplier, distance
    return best


def build_hold(
    name_as_written: str, suppliers: list[Supplier], *, at: str = ""
) -> SupplierHold:
    """The hold's evidence for one arrival, stamped at intake
    and recomputed nowhere — the fraud screen's own
    discipline: later master changes do not rewrite what the
    door saw when the document arrived."""
    near_miss = find_near_miss(name_as_written, suppliers)
    return SupplierHold(
        name_as_written=name_as_written,
        normalised_name=normalise_name(name_as_written),
        near_miss_supplier_id=(
            near_miss.supplier_id if near_miss is not None else None),
        near_miss_supplier_name=(
            near_miss.name if near_miss is not None else None),
        held_at=at,
    )


def hold_summary(hold: SupplierHold) -> str:
    """The hold's evidence in one sentence — the words the
    audit trail, the gate's refusal, and the console all use,
    so a human meets the same account of the stop
    everywhere."""
    summary = (
        f"supplier {hold.name_as_written!r} is not on the "
        "supplier master — the name matched no master entry "
        "exactly, and no referenced purchase order resolved it"
    )
    if hold.near_miss_supplier_id is not None:
        summary += (
            f"; the name most resembles master supplier "
            f"{hold.near_miss_supplier_name} "
            f"({hold.near_miss_supplier_id}) — resemblance is "
            "named as evidence, never attached as a join"
        )
    return summary


def _document_number(record: DocumentRecord) -> str:
    if record.extraction is None:
        return ""
    return str(record.extraction.value("invoice_number") or "")


def build_review_queue(records: list[DocumentRecord]) -> NewSupplierReview:
    """The review queue: every record currently held at the
    door, grouped by the identity it stated. Pure: reads the
    records, writes nothing. Groups stand in first-arrival
    order (store order of their earliest member); the records
    within a group stand in store order."""
    groups: dict[str, NewSupplierGroup] = {}
    order: list[str] = []
    for record in records:
        if record.status != "NEW_SUPPLIER_REVIEW" \
                or record.supplier_hold is None:
            continue
        hold = record.supplier_hold
        key = hold.normalised_name
        if key not in groups:
            groups[key] = NewSupplierGroup(
                name_as_written=hold.name_as_written,
                normalised_name=key,
                near_miss_supplier_id=hold.near_miss_supplier_id,
                near_miss_supplier_name=hold.near_miss_supplier_name,
            )
            order.append(key)
        group = groups[key]
        group.record_ids.append(record.record_id)
        group.document_numbers.append(_document_number(record))
        if record.extraction is not None:
            currency = record.extraction.currency
            total = _cents(float(record.extraction.value("total") or 0.0))
            group.totals_by_currency[currency] = _cents(
                group.totals_by_currency.get(currency, 0.0) + total)
        if not group.held_since or (
            hold.held_at and hold.held_at < group.held_since
        ):
            group.held_since = hold.held_at
    queue = [groups[key] for key in order]
    return NewSupplierReview(
        groups=queue,
        held_count=sum(len(g.record_ids) for g in queue),
    )

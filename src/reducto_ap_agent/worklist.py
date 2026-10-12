"""The unmatched-advice worklist, computed in code.

Round 4 holds an advice UNMATCHED when it names no run this
system can use — no run in its header or its lines, a run that
is not on file, a run the treasurer has not released. Held is
honest, but a held advice is still the bank reporting money
that left: the pile has to be worked, and working it is
detective work. The reference has a typo. The amount is three
units under the line it answers. One lump pays two lines of a
run. This module does that arithmetic — and nothing else:

- For every held advice (UNMATCHED, not yet linked, not
  marked NOT_OURS), every paid line is compared against the
  CONFIRMABLE universe: the lines of RELEASED runs that no
  confirmation has classified ``paid_as_proposed`` for that
  run. A line the bank has already answered exactly is
  settled; suggesting it again would double-count money.
- Each comparison yields at most one single-line candidate,
  under the strongest rule that applies (policy.py states
  the rules and their order), plus any combined-sum
  candidates. Every candidate carries its rule and its
  numbers. Suggestions are not claims: a run line may be
  suggested against several advices at once, because the
  pile is worked by a person who sees all of it.
- The worklist confirms NOTHING. An advice with a perfect
  candidate sits in the pile, unconfirmed, until a named
  controller links it (service.link_confirmation) or marks
  it not ours. The ranking — best candidate strength first,
  no-candidate advices last in the order they were held —
  is printed on the report with the rules that produced it.

Reference comparison follows the house normalisation
(strip, upper — the same idiom as the duplicate watch), and
near references are counted in edits over the normalised
strings. Amounts compare in cents, exactly, the way round 4
compares them: the near rule is a *suggestion* band around a
line the reference names, never a tolerance that settles
anything.
"""

from __future__ import annotations

from itertools import combinations

from .policy import DEFAULT_WORKLIST, WorklistPolicy
from .schemas import (
    AdviceWorklist,
    PaymentConfirmation,
    PaymentRun,
    RemittanceLine,
    WorklistAdviceLine,
    WorklistCandidate,
    WorklistEntry,
)

# Strength per rule: the worklist's ranking currency. Exact
# evidence outranks near evidence; a candidate that names
# its line outranks one that only matches its money.
STRENGTH: dict[str, int] = {
    "exact_line": 100,
    "reference_near": 90,
    "combined_sum": 80,
    "exact_amount": 70,
    "near_amount": 50,
}

RANKING_RULE = (
    "Entries rank by their best candidate's strength "
    "(exact_line 100, reference_near 90, combined_sum 80, "
    "exact_amount 70, near_amount 50); an advice with no "
    "candidate stands last, in the order it was held. "
    "Candidates within a line rank by strength, then run, "
    "then member. Suggestions confirm nothing: only a "
    "controller's link act settles a line."
)


def _normalise(number: str) -> str:
    return str(number).strip().upper()


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance between two strings, counted
    exactly up to ``cap``; anything beyond reads cap + 1.
    Invoice references are short, and the worklist only ever
    asks "one edit or not" — the cap keeps the arithmetic
    honest and bounded."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + cost,  # substitution / match
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > cap:
            return cap + 1
        previous = current
    return previous[len(b)]


class _RunLine:
    """One confirmable line of one released run, flattened
    for comparison."""

    def __init__(self, run: PaymentRun, index: int) -> None:
        line = run.lines[index]
        self.run_id = run.run_id
        self.index = index
        self.record_id = line.record_id
        self.invoice_number = line.invoice_number
        self.number_key = _normalise(line.invoice_number)
        self.supplier_id = line.supplier_id
        self.currency = line.currency
        self.amount = _cents(line.amount)


def confirmable_lines(store) -> list[_RunLine]:
    """The universe candidates are drawn from: lines of
    RELEASED runs, minus the lines some confirmation has
    already classified ``paid_as_proposed`` for that run —
    settled money is never suggested twice."""
    settled: set[tuple[str, str]] = set()
    for confirmation in store.list_confirmations():
        if confirmation.run_id is None:
            continue
        for line in confirmation.lines:
            if line.classification == "paid_as_proposed" \
                    and line.member_record_id is not None:
                settled.add((confirmation.run_id, line.member_record_id))
    universe: list[_RunLine] = []
    for run in store.list_payment_runs():
        if run.status != "RELEASED":
            continue
        for index in range(len(run.lines)):
            candidate = _RunLine(run, index)
            if (run.run_id, candidate.record_id) in settled:
                continue
            universe.append(candidate)
    return universe


def _single_candidate(
    advice_line: RemittanceLine, line: _RunLine, policy: WorklistPolicy
) -> WorklistCandidate | None:
    """The strongest single-line rule for one pair, or None.
    Currency must agree — money in another currency is a
    different break, never a suggestion."""
    if advice_line.currency != line.currency:
        return None
    advice_number = _normalise(advice_line.invoice_number)
    paid = _cents(advice_line.amount_paid)
    distance = edit_distance(advice_number, line.number_key,
                             policy.reference_max_edits)
    number_exact = distance == 0
    number_near = 0 < distance <= policy.reference_max_edits
    delta = _cents(paid - line.amount)
    if delta == 0.0:
        if number_exact:
            rule = "exact_line"
            note = (
                f"The reference names {line.invoice_number} and the "
                f"amount agrees to the cent ({paid:.2f} {line.currency}) "
                "— the round-4 match, missed only because the advice "
                "named no run this confirmation could use."
            )
        elif number_near:
            rule = "reference_near"
            note = (
                f"The reference as written, {advice_line.invoice_number!r}, "
                f"is one edit from {line.invoice_number} and the amount "
                f"agrees to the cent ({paid:.2f} {line.currency}) — a "
                "typo'd reference on the right money."
            )
        else:
            rule = "exact_amount"
            note = (
                f"The amount agrees to the cent ({paid:.2f} "
                f"{line.currency}) with the line for "
                f"{line.invoice_number} in run {line.run_id}, but the "
                f"reference as written, {advice_line.invoice_number!r}, "
                "bears no relation to that number — money matches, "
                "paper does not."
            )
        return WorklistCandidate(
            rule=rule, strength=STRENGTH[rule], run_id=line.run_id,
            member_record_ids=[line.record_id],
            member_invoice_numbers=[line.invoice_number],
            member_amounts=[line.amount],
            member_total=line.amount, advice_amount=paid,
            currency=line.currency, delta=0.0, note=note,
        )
    if (number_exact or number_near) \
            and abs(delta) <= policy.near_amount_abs:
        rule = "near_amount"
        relation = "names" if number_exact else "is one edit from"
        note = (
            f"The reference {relation} {line.invoice_number} and the "
            f"amount is near: {paid:.2f} against the line's "
            f"{line.amount:.2f} {line.currency} — a difference of "
            f"{delta:+.2f}. Near is a suggestion, never a match: "
            "linking classifies the line short or over by exactly "
            "this difference."
        )
        return WorklistCandidate(
            rule=rule, strength=STRENGTH[rule], run_id=line.run_id,
            member_record_ids=[line.record_id],
            member_invoice_numbers=[line.invoice_number],
            member_amounts=[line.amount],
            member_total=line.amount, advice_amount=paid,
            currency=line.currency, delta=delta, note=note,
        )
    return None


def _combined_candidates(
    advice_line: RemittanceLine, universe: list[_RunLine],
    policy: WorklistPolicy,
) -> list[WorklistCandidate]:
    """Combined-sum candidates for one advice line: subsets
    of 2..combined_max_lines unsettled lines of ONE run, ONE
    supplier, the advice line's currency, whose amounts sum
    exactly to what the advice line paid."""
    paid = _cents(advice_line.amount_paid)
    groups: dict[tuple[str, str | None, str], list[_RunLine]] = {}
    for line in universe:
        if line.currency != advice_line.currency or line.amount <= 0:
            continue
        groups.setdefault(
            (line.run_id, line.supplier_id, line.currency), []
        ).append(line)
    found: list[WorklistCandidate] = []
    for (run_id, _supplier, currency), members in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        members = sorted(members, key=lambda m: m.record_id)
        if len(members) < 2:
            continue
        for size in range(2, min(policy.combined_max_lines,
                                 len(members)) + 1):
            for subset in combinations(members, size):
                if _cents(sum(m.amount for m in subset)) != paid:
                    continue
                numbers = ", ".join(m.invoice_number for m in subset)
                found.append(WorklistCandidate(
                    rule="combined_sum",
                    strength=STRENGTH["combined_sum"],
                    run_id=run_id,
                    member_record_ids=[m.record_id for m in subset],
                    member_invoice_numbers=[
                        m.invoice_number for m in subset
                    ],
                    member_amounts=[m.amount for m in subset],
                    member_total=paid, advice_amount=paid,
                    currency=currency, delta=0.0,
                    note=(
                        f"One lump of {paid:.2f} {currency} pays "
                        f"{len(subset)} lines of run {run_id} in full "
                        f"({numbers}) — the bank batched them into a "
                        "single advice line."
                    ),
                ))
                if len(found) >= policy.combined_max_candidates:
                    return found
    return found


def candidates_for_line(
    advice_line: RemittanceLine, universe: list[_RunLine],
    policy: WorklistPolicy = DEFAULT_WORKLIST,
) -> list[WorklistCandidate]:
    """Every candidate for one advice line: the strongest
    single-line rule per confirmable line, plus combined-sum
    candidates, ranked by strength, then run, then member —
    capped at the policy's per-line count."""
    candidates: list[WorklistCandidate] = []
    for line in universe:
        candidate = _single_candidate(advice_line, line, policy)
        if candidate is not None:
            candidates.append(candidate)
    candidates.extend(_combined_candidates(advice_line, universe, policy))
    candidates.sort(key=lambda c: (
        -c.strength, c.run_id, c.member_record_ids,
    ))
    return candidates[:policy.candidates_per_line]


def open_held_confirmations(store) -> list[PaymentConfirmation]:
    """The pile itself: confirmations still UNMATCHED that no
    link act has answered and no controller has marked not
    ours, in the order they were held."""
    answered = {
        c.linked_from for c in store.list_confirmations()
        if c.linked_from
    }
    return [
        c for c in store.list_confirmations()
        if c.status == "UNMATCHED" and c.confirmation_id not in answered
    ]


def build_worklist(
    store, policy: WorklistPolicy = DEFAULT_WORKLIST
) -> AdviceWorklist:
    """The whole worklist: every open held advice with its
    lines as written and the candidates computed for each,
    ranked by best candidate strength. A pure read — it
    writes nothing and confirms nothing."""
    universe = confirmable_lines(store)
    entries: list[WorklistEntry] = []
    for held in open_held_confirmations(store):
        advice = store.get(held.advice_record_id)
        lines: list[WorklistAdviceLine] = []
        best = 0
        if advice is not None and advice.extraction is not None:
            for index, aline in enumerate(
                advice.extraction.remittance_lines
            ):
                candidates = candidates_for_line(aline, universe, policy)
                if candidates:
                    best = max(best, candidates[0].strength)
                lines.append(WorklistAdviceLine(
                    index=index,
                    invoice_number=aline.invoice_number,
                    amount_paid=_cents(aline.amount_paid),
                    currency=aline.currency,
                    value_date=aline.value_date,
                    bank_reference=aline.bank_reference,
                    candidates=candidates,
                ))
        entries.append(WorklistEntry(
            confirmation_id=held.confirmation_id,
            advice_record_id=held.advice_record_id,
            submission_id=held.submission_id,
            unmatched_reason=held.unmatched_reason,
            advice_lines=lines,
            best_strength=best,
            created_at=held.created_at,
        ))
    # Ranked: strongest best-candidate first; no-candidate
    # entries keep the order they were held (stable sort).
    entries.sort(key=lambda e: -e.best_strength)
    return AdviceWorklist(
        entries=entries,
        ranking_rule=RANKING_RULE,
        near_amount_abs=policy.near_amount_abs,
        reference_max_edits=policy.reference_max_edits,
        combined_max_lines=policy.combined_max_lines,
    )

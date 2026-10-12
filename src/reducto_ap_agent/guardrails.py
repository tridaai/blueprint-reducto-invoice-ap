"""Guardrails as code: fraud screen, payable screen, injection screen.

These run before, beside, and after the document engine — never
inside a prompt. Each returns facts the pipeline records; the
pipeline decides what the facts mean for the record's state.

- **Bank-detail screen** — the classic AP fraud: an invoice that
  asks to be paid to a different account than the supplier
  master's approved one. Any difference is a hold, full stop.
  Legitimate bank changes exist, but they are verified out of
  band and recorded in the master — they never ride in on an
  invoice. The master is versioned (bankmaster.py), so the
  comparison is as-of the invoice's own date: the account it
  should name is the one in force when the invoice was issued,
  and the flag says which way the document is wrong — an
  account no version has ever held, one quoted before its
  version takes effect, or one whose version had already been
  superseded by the invoice's date.
- **Payable screen** — a document that is not an invoice or a
  credit note never enters the payment path. The refusal and its
  reason land on the record, so the sender can be answered.
- **Injection screen** — document text is untrusted input. Text
  bound for any model call (the engine's included) is scanned
  for instruction-like payloads; hits are flagged on the record
  and the payload lines are fenced out of the copy that travels
  onward. Extraction reads figures; it does not take orders.
"""

from __future__ import annotations

import re

from .bankmaster import seed_version, version_at
from .policy import INJECTION_MARKERS, PAYABLE_CLASSES
from .schemas import BankVersion, Supplier

_FENCE = "[fenced by injection screen]"


def normalise_account(account: str) -> str:
    """Bank accounts compare without their presentation:
    case, spaces, and separators are not part of the account."""
    return re.sub(r"[^A-Z0-9]", "", account.upper())


def screen_bank_details(
    seen_account: str | None,
    supplier: Supplier | None,
    versions: list[BankVersion] | None = None,
    invoice_date: str = "",
) -> list[str]:
    """Flags for an invoice's bank details vs the supplier
    master, judged as of the invoice's own date.

    Four outcomes, three of them holds — every hold lands the
    record on FRAUD_REVIEW exactly as before; only the reason
    gains the time dimension:

    - the seen account is the one in force on the invoice's
      date → no flag, however the master has moved since;
    - it matches NO version the master has ever held →
      ``BANK_DETAILS_CHANGED``, the original judgement, worded
      as it always was (against a one-version master — the
      seeded world — the as-of screen IS the original screen);
    - it matches a version that takes effect only AFTER the
      invoice's date → ``BANK_DETAILS_NOT_YET_EFFECTIVE``: the
      document quotes the supplier's future bank early, which
      no legitimate invoice does;
    - it matches a version whose window had already closed by
      the invoice's date → ``BANK_DETAILS_SUPERSEDED``: the
      document quotes the retired bank late.

    ``versions`` defaults to the supplier's seed alone, so a
    caller with no history to pass gets the master's standing
    word. An invoice carrying no date is judged against the
    latest version — the pipeline's best present knowledge,
    stated here rather than hidden: extraction reads the date
    off the document, and a dateless payable document is
    already an exception on other grounds."""
    if not seen_account or supplier is None:
        return []
    series = versions if versions is not None else [seed_version(supplier)]
    seen = normalise_account(seen_account)
    in_force = version_at(series, invoice_date)
    if seen == normalise_account(in_force.bank_account):
        return []
    matching = [
        v for v in series if normalise_account(v.bank_account) == seen
    ]
    if not matching:
        return [
            "BANK_DETAILS_CHANGED: invoice bank account "
            f"…{seen[-4:]} differs from the supplier master's "
            f"approved account …{normalise_account(in_force.bank_account)[-4:]}"
        ]
    def superseded_flag(superseded_on: str) -> list[str]:
        return [
            "BANK_DETAILS_SUPERSEDED: invoice bank account "
            f"…{seen[-4:]} matches the supplier master's account "
            f"…{seen[-4:]}, superseded"
            + (f" effective {superseded_on}" if superseded_on else "")
            + " — this invoice quotes details the master had "
              "already retired"
            + (f" by its date {invoice_date}" if invoice_date else "")
        ]

    if invoice_date:
        # A matching version whose successor already stood by
        # the invoice's date: those details had retired by
        # then — the sharper story, and the one that wins when
        # a supplier later reverts to an account it once held.
        for held in matching:
            successor = next(
                (w for w in series if w.version > held.version), None)
            if successor is not None and successor.effective_from \
                    and successor.effective_from <= invoice_date:
                return superseded_flag(successor.effective_from)
        future = [
            v for v in matching
            if v.effective_from and v.effective_from > invoice_date
        ]
        if future:
            first = min(future, key=lambda v: v.effective_from)
            return [
                "BANK_DETAILS_NOT_YET_EFFECTIVE: invoice bank account "
                f"…{seen[-4:]} matches the supplier master's account "
                f"…{seen[-4:]} recorded effective {first.effective_from}, "
                f"which is after this invoice's date {invoice_date} — "
                "details quoted before they take effect"
            ]
    retired = matching[-1]
    superseded_on = next(
        (v.effective_from for v in series
         if v.version > retired.version and v.effective_from),
        "",
    )
    return superseded_flag(superseded_on)


def payable_refusal(doc_class: str) -> str | None:
    """The refusal reason for a class the payment path won't take,
    or None when the class is payable."""
    if doc_class in PAYABLE_CLASSES:
        return None
    return (
        f"NOT_PAYABLE_DOCUMENT: classified as {doc_class!r}, not an invoice "
        "or credit note — refused by the payment path"
    )


def screen_injection(text: str) -> tuple[list[str], str]:
    """Scan text for instruction-like payloads.

    Returns ``(flags, fenced_text)``: flags name each marker hit;
    in the fenced text, offending lines are replaced with a fence
    marker so downstream consumers — engine included — never see
    the payload as text they might act on.
    """
    flags: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        hit = next((m for m in INJECTION_MARKERS if m in lowered), None)
        if hit:
            flags.append(f"INJECTION_MARKER: {hit!r}")
            out_lines.append(_FENCE)
        else:
            out_lines.append(line)
    return flags, "\n".join(out_lines)

"""Guardrails as code: fraud screen, payable screen, injection screen.

These run before, beside, and after the document engine — never
inside a prompt. Each returns facts the pipeline records; the
pipeline decides what the facts mean for the record's state.

- **Bank-detail screen** — the classic AP fraud: an invoice that
  asks to be paid to a different account than the supplier
  master's approved one. Any difference is a hold, full stop.
  Legitimate bank changes exist, but they are verified out of
  band and recorded in the master — they never ride in on an
  invoice.
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

from .policy import INJECTION_MARKERS, PAYABLE_CLASSES
from .schemas import Supplier

_FENCE = "[fenced by injection screen]"


def screen_bank_details(seen_account: str | None, supplier: Supplier | None) -> list[str]:
    """Flags for an invoice's bank details vs the supplier master."""
    if not seen_account or supplier is None:
        return []
    normalise = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())  # noqa: E731
    if normalise(seen_account) != normalise(supplier.bank_account):
        return [
            "BANK_DETAILS_CHANGED: invoice bank account "
            f"…{normalise(seen_account)[-4:]} differs from the supplier master's "
            f"approved account …{normalise(supplier.bank_account)[-4:]}"
        ]
    return []


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

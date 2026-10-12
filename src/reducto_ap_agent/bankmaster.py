"""The supplier master has a past: bank details, versioned.

The fraud screen's whole judgement used to be one comparison —
the invoice's bank account against the master's. But the master
is not a fact of nature: suppliers change banks, legitimately,
and a master that only knows its present makes two errors with
one comparison. It flags an invoice quoting the account the
supplier genuinely used last month (dated before the change),
and it waves through an invoice quoting a bank that only
becomes the supplier's next week (dated before the change that
would bless it). Both errors are the same error: judging a
dated document against an undated master.

So the master keeps its history. Each supplier's bank details
are a series of versions — the details, the date they take
effect, and the provenance of the recording (bankmaster's
callers enforce it: who recorded the version, when, and what
it was recorded from — the supplier's change notice, verified
out of band; the guardrails module states the doctrine that
legitimate changes are verified out of band and recorded in
the master, never ridden in on an invoice). This module is
the arithmetic of that series:

- **The seed is version 1.** The details the deployment loaded
  the master with — undated, effective for every date before
  the first recorded version. ``Supplier.bank_account`` stays
  what it always was: the seed's account, named on the model.
  The version series is the history; the current details are
  whichever version is in force, never a second copy that
  could drift from it.
- **In force is a derivation, never a stored flag.** The
  version in force on a date is the last one whose
  ``effective_from`` is on or before it. Recordings are
  append-only and effective dates monotonic (the service
  enforces both, with named refusals), so the derivation is
  unambiguous — and recording a new version never rewrites an
  earlier one: what the master said about last March stays
  exactly what it said.
- **Recording is the governed act — there is no second gate.**
  Everywhere else this system governs a change, one named
  actor's recorded word is the act: a controller applies a
  credit, a treasurer releases a run, a controller resolves a
  hold. No master edit existed before this round to set a
  different precedent, and a pending/confirmed state for bank
  versions would invent a two-person rule nothing else here
  has — while protecting nothing, because the screen's as-of
  discipline is the protection: a version recorded today with
  next month's effective date blesses no invoice dated before
  it, and an unrecorded change blesses nothing at all.

Where the versions live: the reference data, beside the
suppliers themselves — seeded per deployment, held behind the
store's surface like the orders and receipts (store.py states
the arrangement). Reference data is persisted nowhere in this
system; the migration ladder covers the records the pipeline
writes, and the master's history is the master's, loaded and
recorded the same way the master is.
"""

from __future__ import annotations

from .schemas import BankHistory, BankVersion, Supplier

SEED_SOURCE = "the supplier master as seeded — the deployment's loaded record"


def seed_version(supplier: Supplier) -> BankVersion:
    """Version 1 for a supplier: the details the master was
    loaded with. Undated and unattributed beyond the seed
    itself — it predates the record, and says so."""
    return BankVersion(
        version=1,
        bank_account=supplier.bank_account,
        bank_name=supplier.bank_name,
        effective_from="",
        recorded_by="",
        recorded_at="",
        source=SEED_SOURCE,
    )


def version_at(versions: list[BankVersion], on_date: str) -> BankVersion:
    """The version in force on ``on_date`` (an ISO date): the
    last version whose effective date is on or before it. The
    undated seed is in force from the beginning of time. An
    empty ``on_date`` asks for the latest version outright —
    the caller's way of saying 'the master's present word',
    used where a read carries no date of its own."""
    chosen = versions[0]
    for version in versions:
        if not version.effective_from or not on_date \
                or version.effective_from <= on_date:
            chosen = version
        else:
            break
    return chosen


def build_history(
    supplier: Supplier, versions: list[BankVersion], as_of: str
) -> BankHistory:
    """One supplier's bank timeline as of ``as_of``: every
    version in recording order, and the version in force at
    that date named. A read model — building it writes
    nothing, and the versions it lists are the stored ones,
    never copies that could drift."""
    current = version_at(versions, as_of)
    return BankHistory(
        supplier_id=supplier.supplier_id,
        supplier_name=supplier.name,
        as_of=as_of,
        versions=list(versions),
        current_version=current.version,
    )

"""Error taxonomy for the AP agent.

Every failure the service can hit is one of these, so the API maps
them to honest HTTP responses instead of stack traces:

- :class:`DocumentNotConfigured` — the Reducto backend was selected
  without the configuration it needs (no API key). Startup and
  request paths fail loudly rather than substituting another
  engine's results.
- :class:`DocumentBackendUnavailable` — the document engine was
  configured but could not be reached, or rejected the request.
- :class:`DocumentRefused` — the payment path refused a document
  in code (not an invoice, fraud hold). The reason rides the record.
- :class:`RecordNotFound` / :class:`GateError` — store and gate
  discipline failures.
"""

from __future__ import annotations


class ApError(Exception):
    """Base class for all errors this package raises on purpose."""

    status_code = 500


class DocumentNotConfigured(ApError):
    status_code = 503


class DocumentBackendUnavailable(ApError):
    status_code = 502


class DocumentRefused(ApError):
    """A guardrail refused the document; ``reason`` is record text."""

    status_code = 422

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class RecordNotFound(ApError):
    status_code = 404


class GateError(ApError):
    """A gate action that the record's state does not allow."""

    status_code = 409


class ReconciliationError(ApError):
    """A reconciliation was asked of something that cannot be
    reconciled — a record that is not a statement, a statement
    whose supplier is not on file, a statement with no captured
    extraction."""

    status_code = 422


class ConfirmationError(ApError):
    """A remittance confirmation was asked of something that
    cannot be confirmed — a record that is not a remittance
    advice, an advice whose lines were never captured. (An
    advice that names no run on file is NOT an error: it is
    held UNMATCHED on its confirmation record, reason named.)"""

    status_code = 422


class CreditError(ApError):
    """A credit application was asked of something that cannot
    be applied — a record that is not a credit note, a credit
    note with no captured extraction. (A credit that names its
    invoice but cannot be applied YET — unapproved, over the
    open amount, its invoice already in a run — is NOT this
    error: the blocker is a state conflict and raises
    :class:`GateError` with the blocker's code in the message,
    the way the other gates refuse.)"""

    status_code = 422


class CoverageError(ApError):
    """PO coverage was asked of something it cannot speak for
    — a record that is not an invoice, or an invoice naming no
    purchase order. (An invoice naming an order that is not on
    the register is NOT this error: its coverage is computed
    and reads UNMEASURED, total unknown, billed sum shown.)"""

    status_code = 422


class PaymentRunError(ApError):
    """A payment run could not be assembled or released as asked
    — no eligible records, a member whose supplier is not on
    file at release time, run totals that do not tie. Per-record
    ineligibility is NOT an error: it rides the run as a named
    refusal. This is for the batch as a whole failing loudly."""

    status_code = 422


class InjectionScreened(ApError):
    """Document text carried an instruction-like payload; the
    affected fields were fenced before any model call. Raised only
    where the caller asked for strict handling — the pipeline
    normally records the flag and continues with fenced text."""

    status_code = 422

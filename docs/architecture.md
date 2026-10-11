# Architecture — Reducto Invoice AP, Deployed In-VPC

This document describes the deployment, not just the code: what
runs where, what crosses which boundary, and which decisions are
made by arithmetic rather than by a model. It is written for the
two readers who pick up a blueprint like this — the platform
engineer deciding whether the shape is sound, and the finance
operations lead deciding whether the discipline is real.

## The shape in one paragraph

A LangGraph pipeline takes each submission through intake,
document processing, screening, and a three-way match, then
stops at an interrupt — a named approver's gate — before
anything becomes a proposed payment. The document work
(classification, splitting, schema extraction) is done by
Reducto behind a port; everything that decides money is done by
this codebase, in code, against reference data. Records live in
PostgreSQL, document bytes in S3-compatible object storage, and
the graph's checkpoints in PostgreSQL too, so the API tier is
stateless and a gate decision can land days after the match that
produced it.

## Deployment profiles

The blueprint's reason to exist is that finance documents are
sensitive enough that *where they are read* is an architectural
decision. Two profiles, one codebase:

| | `cloud` | `vpc` |
|---|---|---|
| Document engine | Reducto's hosted API | an engine endpoint inside the customer's boundary |
| Engine endpoint | `https://platform.reducto.ai` | `http://reducto-engine:8080` (compose) or `REDUCTO_BASE_URL` |
| Engine network exposure | n/a (SaaS) | internal-only service; no published port |
| Document traffic | leaves the boundary to Reducto's API, over HTTPS | never leaves the boundary |
| Code, guardrails, data services | identical | identical |

The profile is resolved in `profiles.py` and the difference is
exactly: the base URL handed to the Reducto adapter, and the
compose topology. A config test pins both endpoints so the
profiles cannot silently drift into each other's wiring.

**What the VPC demo stack actually runs.** This repository does
not contain a Reducto engine and does not fake one. The VPC
compose overlay runs `engine_api.py` — a small service that
answers the four Reducto API calls the adapter makes, backed by
the offline text-layer adapter — at the internal endpoint, with
no port published. It exists so the *topology* is real and
testable (an internal-only engine service; document traffic
contained) while the *engine* is honestly a stand-in. A customer
deployment replaces that one service with their licensed
self-hosted engine; nothing else in the stack changes. In the
cloud profile there is no stand-in: the adapter talks to
Reducto's real API, and without a key it refuses to start.

## The document port

`DocumentPort` (`ports.py`) is the only seam the pipeline knows:

```
upload(filename, data, content_type) -> UploadedDocument
classify(doc) -> Classification        # invoice, credit_note, statement, …
split(doc)    -> list[SubDocument]     # page ranges per logical document
extract(doc)  -> ExtractedDocument     # fields + lines, each value cited
```

Two adapters implement it:

- **`reducto`** (`reducto_docs.py`) — Reducto's public API:
  `POST /upload` (multipart), then `/classify`, `/split`,
  `/extract` against the returned `reducto://` reference.
  Extraction requests a JSON schema and asks for citations with
  numerical confidence, so extracted values come back with
  bounding boxes the pipeline stores on the record. The adapter
  is deliberately tolerant of response-shape detail (field
  values arriving raw or as `{value, bbox, confidence}`
  envelopes; results as one dict or chunked lists) and maps
  everything in one module — an API revision is a one-file
  change. Its failure posture is part of the contract: no key →
  `DocumentNotConfigured` at startup; HTTP or network failure →
  `DocumentBackendUnavailable`. There is no fallback path.
- **`offline`** (`offline_docs.py`) — a deterministic adapter
  over the synthetic documents' text layer. It parses labelled
  fields and pipe-delimited line rows, and produces citations as
  page + approximate box with `source="text_layer"`, so the
  record shape downstream is identical to the engine's. It is
  labelled everywhere it appears — readiness payload, eval
  results, this document — because it is a stand-in for tests
  and demos, not an OCR engine.

Contract tests hold both adapters to the same semantics, and
the pipeline's behaviour is identical against either — which is
what makes the offline eval pack meaningful as a regression gate
for the *system* (matching, screening, gating) even though it
says nothing about the engine's extraction quality. That
quality is what the opt-in live eval measures, reported and
not gated.

## The pipeline

```
intake → process → gate (interrupt) → apply
```

- **intake** — the submission's bytes go to object storage
  under `submissions/<id>/<filename>`; the text layer is run
  through the injection screen and any flags ride the state
  into the records.
- **process** — per sub-document from the split: attachments
  (remittance advices, delivery notes) are stored and recorded
  as RECEIVED, never extracted for payment; non-payable classes
  are REFUSED with the reason on the record — a statement first
  has its open-item lines extracted and captured on the record,
  because reconciliation (below) reads them, and a remittance
  advice likewise has its paid lines captured, because
  confirmation (below) reads them; payable documents are
  extracted, screened (bank details), matched, and checked
  for duplicates against live records. Terminal states from
  this node: `RECEIVED`, `REFUSED`, `FRAUD_REVIEW`,
  `EXCEPTION`, or `AWAITING_APPROVER`.
- **gate** — an interrupt listing the submission's awaiting
  records. The graph parks here, checkpointed. The service
  resumes it only when every awaiting record of the submission
  has a named decision.
- **apply** — decisions land on the records: `APPROVED` or
  `REJECTED`, with actor and reason in the audit trail.

One graph run per submission; one thread per submission. The
gate is per-submission because that is how documents arrive —
a bundle's invoice waits for its own approver, and its
remittance advice doesn't need one.

## The match, in code

`matching.py` compares the extraction against the purchase
order and goods receipt. Tolerances come from `policy.py` and
nowhere else:

- a line's unit price is inside tolerance when it is within
  **both** 2% relative and 0.50 absolute of the PO price;
- invoice and PO totals agree when they differ by at most the
  greater of 1% of the PO total and 1.00;
- the invoice's stated total must agree with its own lines at
  the same total tolerance — a document that contradicts itself
  is an exception before it is compared to anything else;
- invoiced quantity may not exceed received quantity when a
  receipt exists (partial receipts are an exception by default;
  `allow_partial_receipt` is the deliberate policy switch).

Outcomes are `MATCHED`, `MATCHED_WITH_TOLERANCE` (the drift is
recorded in the match notes — shown, not buried), or
`EXCEPTION` with named codes: `PRICE_VARIANCE`,
`QUANTITY_VARIANCE`, `TOTAL_MISMATCH`, `LINE_NOT_ON_PO`,
`NO_PO_REFERENCE`, `PO_NOT_FOUND`, `NO_RECEIPT`,
`DUPLICATE_INVOICE`. Line-level codes suppress the PO-total
check — when a line already explains the difference, a second
total code would be noise, and exception queues die of noise.

Duplicates are detected on (supplier, invoice number) against
live records — refused and fraud-held records don't count, so a
corrected resubmission after a refusal is not accused of being
its own duplicate.

## Guardrails

Three screens, all code, all before the gate:

1. **Bank details.** The account on the invoice is compared
   (formatting-insensitively) with the supplier master's
   approved account. Any difference → `FRAUD_REVIEW`, with the
   flag text showing only masked tails. The gate *cannot*
   decide a fraud-held record — the API returns 409 — because
   the fix belongs in the supplier master after out-of-band
   verification, not in an approver's click. This is the
   payment-diversion pattern, and the blueprint treats it as
   the highest-stakes screen it runs: the eval pack's fraud
   gate tolerates zero misses.
2. **Payable classes.** Only `invoice` and `credit_note` enter
   the payment path. Anything else is refused with
   `NOT_PAYABLE_DOCUMENT` on the record — a statement that
   looks like a bill is exactly the document this screen
   exists for.
3. **Injection.** Document text is untrusted input. Known
   instruction-like markers are flagged on the record and
   fenced out of the text copy that travels onward, so a
   payload printed on an invoice is visible to the operator and
   absent from anything an engine or model downstream reads.

## The gate and the packet

A decision is `{decision, actor, reason}`; the actor is
required — an approval nobody signed is not an approval. The
service enforces the discipline the graph can't: only
`AWAITING_APPROVER` records can be decided, fraud holds and
exceptions cannot, and a decision is final (the record leaves
the awaiting set; corrections are new submissions, matching how
AP actually works).

The payment packet is a read model over `APPROVED` records —
supplier, invoice number, currency, amount, credit notes as
negative lines, totals per currency. It is generated on read,
never stored as a mutable balance, and the system contains no
code path that executes a payment: the packet is the input to
the customer's AP system of record, where payment execution,
with its own controls, already lives.

## Statement reconciliation

The second document workflow, and the reason statements are
extracted at all. It runs entirely off records:

**Intake.** The pipeline classifies a statement and the payable
screen refuses it — `NOT_PAYABLE_DOCUMENT`, unchanged from the
payment path's point of view. Before the refusal lands, the
pipeline extracts the statement through the same DocumentPort
and resolves its supplier, so the record carries the open-item
lines (`StatementLine`: number, date, amount, currency) plus
the statement's period and stated total. Other refused classes
(letters, standalone delivery notes) are not extracted — only
statements have a second path waiting for their contents.

**The join.** `reconciliation.py` compares the statement's
lines with AP's live invoice records for that supplier — live
meaning received and not finally disposed of: awaiting the
gate, approved, exception, and fraud-held records count (AP is
acting on the amount); rejected ones do not (the supplier was
told no). Classes, exactly one per line:

- statement side: `matched` · `amount_disagrees` ·
  `missing_in_ap` · `duplicates_within_statement` (the first
  occurrence of a number reconciles; repeats are the break);
- AP side: `missing_on_statement`, for live invoices the
  statement never lists.

An amount agrees when it sits inside the match policy's total
tolerance — `policy.py` again, because "the amount matches"
should mean one thing in this system. A currency difference is
an `amount_disagrees` with the reason named; figures in
different currencies are not compared at all. The statement's
stated total is tied against the sum of its lines at the same
tolerance; a statement whose own arithmetic fails is flagged
`STATEMENT_TOTAL_MISMATCH` (or `STATEMENT_TOTAL_MISSING` when
it states no total) before any comparison with AP is trusted.

**The controller gate.** A reconciliation is a stored record
(`AWAITING_CONTROLLER` → `ACCEPTED` | `FLAGGED_BACK`) with a
summary — counts and totals per class — and an append-only
audit trail. The controller's discipline mirrors the approver
gate's: the actor is required, flagging back requires a note,
and a decision is final; a corrected statement arrives as a
new submission and reconciles afresh (a re-reconciliation of
the same statement is a new record, never an edit).

**Invariance.** Reconciliation reads document records and
writes only its own table. It has no path to approve, alter,
or exclude an invoice, so the payment packet is bit-identical
whether or not a reconciliation exists — the eval pack
snapshots every record before and after reconciling and gates
on nothing having moved.

Surfaces: `POST /statements/{record_id}/reconcile`,
`GET /reconciliations` (per supplier), the decision endpoint,
the console's reconciliation panel (breaks itemised, the two
figures side by side), and the CLI's `reconcile` command — all
over the same service methods.

## Payment runs

The packet is a view; a payment run is a stored batch with a
gate on it — the step an actual AP department takes between
"approved" and "the bank file". `payments.py` holds the
assembly arithmetic and the export; the service orchestrates
persistence, membership, and the gate; migration `0003` gives
runs their own table in the same JSONB idiom.

**Assembly.** A run is created for a value date, optionally
scoped to currencies, from named record ids (in the order
named; an unknown id fails the whole assembly loudly — a
mistyped id must never silently shrink a batch) or, unnamed,
from every `APPROVED` invoice and credit note — the packet's
universe. Each candidate is either a member or a named refusal:

- `NOT_PAYABLE_CLASS` — not an invoice or credit note;
- `FRAUD_HOLD` — on fraud review, with the record's flags in
  the reason; fraud is cleared in the supplier master, never
  by a batch;
- `MATCH_EXCEPTION` — carries an open exception, codes named;
- `NOT_APPROVED` — awaiting the gate, rejected, or otherwise
  not finally approved;
- `ALREADY_IN_RUN` — held by an open run, which is named;
  a record belongs to at most one open run;
- `ALREADY_PAID_PROPOSED` — released in an earlier run, also
  named; this refusal is forever, because the record already
  sits in a bank export;
- `OUT_OF_CURRENCY_SCOPE` — outside the run's currency scope.

Zero eligible records fails assembly (`PaymentRunError`, 422):
an empty batch is an operator error, not a run. Member amounts
are the packet's amounts — the extraction total, credit notes
negative — and the per-currency totals are tied back against
the member lines before the run is stored; a batch whose
header disagrees with its contents is never written.

**Membership, honestly named.** A member's record keeps its
`APPROVED` status — that is the approver's decision, and the
treasurer's batch does not rewrite another human's act.
Membership rides the record as `payment_run_id` +
`payment_state`: `IN_RUN` while held by an open run,
`PAID_PROPOSED` once its run is released. *Paid-proposed*, not
paid: the state names exactly what happened — a proposal for
this payment exists in a released export. Both transitions are
audit events on the record.

**The treasurer gate.** `AWAITING_TREASURER` → `RELEASED` |
`RETURNED`, decided by a named treasurer; returning requires
a reason (the AP team reworks on it) and frees every member
(membership cleared, audited) to join a later run. Release
builds and stores the export *before* any state moves — a
release that cannot produce its artifact has not happened.

**The export.** A payment-instruction CSV with a fixed column
set, generated once at release, stored under
`payment-runs/{run_id}/payment-instructions-v1.csv` (the
version is in the key: a future format is a new artifact,
never an overwrite), and pinned on the run by SHA-256 digest.
The download endpoint serves the stored bytes — never a
recompute — so what the treasurer released is what anyone can
ever retrieve. Bank name and account come from the **supplier
master** (`ReferenceData`), never from the member record: the
invoice's own bank field is a claim by the sender and the
fraud pattern this blueprint screens for is precisely an
invoice asking to be paid somewhere new. A member whose
supplier is not on file fails the release loudly, before
anything is written.

**What it is not.** A released run changes nothing about
execution: there is still no code path in this system that
pays anyone. The export is handed to the customer's payment
systems, where payment execution and its own controls live.
The packet, likewise, is unchanged — it remains the naive read
model over approvals; the run is the controlled path, and the
membership state on the record is what keeps the two from
double-proposing the same invoice.

Surfaces: `POST /payments/runs`, `GET /payments/runs[/{id}]`,
`POST /payments/runs/{id}/decision`,
`GET /payments/runs/{id}/export`, the console's payment-run
panel (members, refusals, totals, gate controls, export
download), and the CLI's `create-run` / `payment-runs` /
`decide-run` / `export-run` commands.

## Remittance confirmation

The run section ended with an honest gap: a released run is a
proposal, and nothing in it says what the bank actually did.
Remittance confirmation closes that loop. A bank advice is a
document like any other here — it arrives through the
DocumentPort, is classified `remittance_advice`, and the
pipeline captures its extraction on its record before filing
it (the same intake pattern statements use), so confirmation
works from records. The record itself stays `RECEIVED`: an
advice is an attachment, never payable, never an instruction.
`confirmations.py` holds the comparison; the service
orchestrates run resolution, persistence, overlays, and the
gate; migration `0004` gives confirmations their own table in
the same JSONB idiom.

**Run resolution.** An advice is tied to a run only by a
reference the advice itself carries: its header's
payment-run field, or one run named unanimously by its paid
lines. Three failures are held `UNMATCHED` on the
confirmation record, with the reason named: the advice names
no run; it names a run that is not on file; or it names a run
the treasurer has not released (only a released run has
proposed payments that could have left the bank). An
unmatched advice is never guessed onto a run and cannot be
decided — there is nothing for a controller to accept until
it answers a run. The confirmation id follows the service's
established idiom, `CONF-<advice record id>` with numeric
suffixes on repeat confirmations of the same advice, so a
corrected advice can be confirmed again against the same run
without overwriting the first answer.

**The exact-match rule.** Confirmation compares paid against
proposed with exact equality (rounded to cents, then equal),
and deliberately never calls the match policy's tolerance
helpers in `matching.py`/`policy.py`. The tolerances exist
because the three-way match compares *documents* — a supplier's
invoice against AP's order — and documents carry reading
noise. A confirmation compares *money*: the amount on the
released instruction against the amount the bank reports
having moved. A one-cent shortfall inside a 1% tolerance is
still a one-cent loss nobody would ever look at, so the rule
is stated in the module that enforces it: money that left
either matches the instruction to the cent, or the difference
is a classified break with both figures on the line.

**Classification.** Each advice line is read in advice order;
members are keyed by invoice number. One line classifies one
member: `paid_as_proposed` (paid equals proposed, same
currency), `short_paid` (less arrived; `difference` carries
paid − proposed), `over_paid` (more arrived). A member whose
line reports another currency is `short_paid` with a null
difference and the shortfall set to the full proposal —
nothing arrived in the currency that was proposed — while the
foreign figure rides its line and stays out of the paid
totals. A second line for an already-classified member, and
any line naming a non-member, is `unexpected_line`: shown,
counted, and never absorbed — its money accumulates only
into the summary's `unexpected_paid`. Members no line names
are `missing`, appended in run order.

**The summary tie.** Per currency, the proposed side of the
classes partitions the run: confirmed + short + over +
missing proposed must recompose the run's total for that
currency, recomputed in the confirmation module *from the
run's own member lines* — not from the classes — so a
classification bug cannot make its own arithmetic look
right. If the tie fails, the confirmation carries the flag
`CONFIRMATION_ARITHMETIC_BREAK` and names it in its notes
rather than smoothing the numbers.

**The overlay.** A classified member gains
`payment_confirmation` (`CONFIRMED` / `SHORT_PAID` /
`OVER_PAID` / `MISSING`) and `payment_confirmation_id` on its
record, beside `payment_state`, with a
`payment_confirmation_recorded` audit event. It is an
overlay, not a state: the record stays `APPROVED` and
`PAID_PROPOSED`, because three different facts are now on
record — what the approver decided, what the treasurer
proposed, and what the bank answered — and none of them
rewrites another. When several confirmations classify the
same member (a corrected advice re-confirmed), the latest
computed confirmation owns the overlay; each confirmation
record itself is immutable in content once written.

**Evidence discipline.** Computing or deciding a confirmation
writes its own record and the member overlays — and nothing
else. The run's payload, its decision, its export bytes and
digest, the members' approvals and memberships, bank data,
and supplier masters are all untouched; the eval pack pins
the export bytes and the run's decision byte-identical
across a full cycle. No advice line can create, alter, or
release a payment: confirmation is the bank's answer being
*read*, never acted on by this system.

**The controller gate.** A matched confirmation ends
`AWAITING_CONTROLLER` — the same gate shape as statement
reconciliation, with the same `ControllerDecision`: a named
controller accepts it, or flags it back with a note (required
— the AP team takes it to the bank); decisions are audited
and final. Flagging back does not edit the confirmation; the
breaks stay on record exactly as classified, and the note
says what happens next.

Surfaces: `POST /remittances/{record_id}/confirm`,
`GET /confirmations[?run_id=]`, `GET /confirmations/{id}`,
`POST /confirmations/{id}/decision`, the console's
remittance-confirmation panel (advices with their named run,
per-run lines with proposed beside paid, the summary,
controller controls), and the CLI's `confirm` /
`confirmations` commands.

## Data services

- **PostgreSQL** — the `records` table (one JSONB payload per
  record, status and supplier promoted to columns for the
  queries the console runs), the `reconciliations` table (same
  pattern, migration `0002`), the `payment_runs` table
  (migration `0003`), the `confirmations` table (migration
  `0004`, run id nullable — held-unmatched advices have none),
  and the LangGraph Postgres checkpointer. Numbered migrations
  in `migrations/` run at startup; both stores share the one
  database.
- **Object storage** — document bytes behind `ObjectStorePort`;
  boto3 against any S3-compatible endpoint (SeaweedFS's S3
  gateway in compose; AWS S3 in a cloud deployment).
- **Doubles** — with `DATABASE_URL` / `S3_*` unset, in-memory
  implementations stand in so tests and the demo run anywhere.
  `/readiness` names which world is live (`store`,
  `checkpointer`, profile, backend, engine endpoint), because a
  console that won't say it's running on doubles is a console
  that will eventually lie about production.

Logs are structured JSON with a request ID threaded through
middleware → logs → response header.

## What a production deploy still needs

Stated plainly, because a blueprint that hides its gaps teaches
the wrong lesson:

- **Live Reducto verification** — pending an API key; the
  adapter's mapping is unit-tested against stubbed transports,
  and the live eval is opt-in until then.
- **Real documents** — the synthetic set is text-layer; scanned
  and photographed invoices are the engine's job in production,
  and the offline adapter does not pretend otherwise.
- **ERP integration** — suppliers, POs, and receipts are seeded
  from the synthetic world behind `ReferenceData`; a deployment
  loads them from the ERP's mirrors or API.
- **Identity** — the gate records the approver's name as
  supplied; a deployment puts SSO in front and takes the actor
  from the session, plus segregation-of-duties rules (an
  approver cannot approve above their limit, or their own
  submissions).
- **Transport security & secrets** — TLS on every hop and a
  secrets manager for the API key and database credentials; the
  compose stacks carry local defaults and say so.

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
  for duplicates against live records — unless the supplier
  resolves to no master record, in which case the document
  stops at the door (the new-supplier hold, below) before any
  of that runs. Terminal states from this node: `RECEIVED`,
  `REFUSED`, `FRAUD_REVIEW`, `PAID_DUPLICATE_HOLD`,
  `NEW_SUPPLIER_REVIEW`, `EXCEPTION`, or `AWAITING_APPROVER`.
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
   approved account — the version of it in force on the
   invoice's own date (the master is versioned: "Supplier bank
   history" below). Any difference → `FRAUD_REVIEW`, with the
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

The payment packet is a read model over `APPROVED` invoices —
supplier, invoice number, currency, and the invoice's **open
amount** (below), with the credits that produced it listed on
the line, totals per currency — plus the approved credit
notes still unapplied, each with the reason it waits. It is
generated on read, never stored as a mutable balance, and the
system contains no code path that executes a payment: the
packet is the input to the customer's AP system of record,
where payment execution, with its own controls, already
lives.

## Credit application

`credits.py` owns the credit discipline; the service exposes
it as `apply_credit`, the API as `POST /credits/{id}/apply`,
and no migration was needed — applications ride the records'
JSONB payloads (the invoice's `credits_applied` ledger, the
credit's `credit_state` + `applied_invoice_record_id`).

**Reference, never resemblance.** A credit note's extraction
carries `credited_invoice_number` — the invoice its own
document names. Resolution looks that number up among the
credit supplier's invoice records (a live copy wins over a
dead duplicate; the one `APPROVED` copy wins among live ones).
A credit naming no invoice (`NO_INVOICE_REFERENCE`) or one
not on file (`INVOICE_NOT_ON_FILE`) is held `UNAPPLIED` with
the reason named, on the record's surfaces and on the packet.
Nothing in the credit path matches by amount: two credits of
the same amount against one invoice would both "fit", and
the payable would fall twice for one debt — amount-matching
is precisely the failure this design refuses to automate.

**Application.** Both sides must be `APPROVED`: the credit
(an unverified document moves no money; a fraud-held credit
is refused `CREDIT_NOT_APPROVED` with the hold in the words)
and its invoice (`TARGET_NOT_APPROVED`; a fraud-held target
is `TARGET_FRAUD_HOLD` — netting must not become a way to
touch a held record). Currencies must agree
(`CURRENCY_MISMATCH`): a credit reduces a payable in the
payable's own currency. The credit applies whole, to the
cent, or not at all — `OVER_CREDIT` names both figures when
it exceeds the open amount; there is no partial application
of a single credit note, because a half-applied note invites
applying the other half twice. Partial *coverage* is the
normal case and accumulates on the ledger.

**Open amount.** `open_amount(record)` = extraction total −
Σ credit ledger − Σ payments ledger (`paid_amount`, also in
`credits.py`). The face never changes (the approver's figure is
history); the open amount is the derived figure the packet,
the runs, the position, the ledger, and the export read — one
computation, in
`credits.py`, reused by all of them, the way round 3 reused the
packet's amounts before it.

**Late credits.** The target's payment state is a hard
boundary, checked before the money: `TARGET_IN_RUN` (the open
run was assembled on the open amount at assembly time; it is
returned or released as assembled) and `TARGET_PAID_PROPOSED`
(terminal — the run and its confirmation own that money's
story, and a later reduction would falsify both). A
`PART_PAID` target is neither: the released part's story is
closed, but the remainder is still open money, so the credit
falls through to the money check and lands on the remainder —
and `OVER_CREDIT` is then judged against the remainder, its
reason naming credits *and* payments already on the record
when payments exist. The credit
stays `UNAPPLIED` and visible; settling it against the
supplier's future invoices is a credit-balance ledger this
blueprint deliberately does not build.

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
from every `APPROVED` invoice — the packet's universe. Each
candidate is either a member or a named refusal:

- `CREDIT_NOTE` — a credit note, refused in its own words:
  applied (its reduction already stands in its invoice's open
  amount) or unapplied (apply it first); a credit is applied
  against an invoice, never paid as a negative instruction,
  so the netting holds whichever batch the invoice lands in;
- `NOT_PAYABLE_CLASS` — not an invoice;
- `FRAUD_HOLD` — on fraud review, with the record's flags in
  the reason; fraud is cleared in the supplier master, never
  by a batch;
- `MATCH_EXCEPTION` — carries an open exception, codes named;
- `NOT_APPROVED` — awaiting the gate, rejected, or otherwise
  not finally approved;
- `FULLY_CREDITED` — approved, but applied credits bring its
  open amount to 0.00 (the credits are named in the reason);
  excluded in the open rather than carried as a zero line;
- `ALREADY_IN_RUN` — held by an open run, which is named;
  a record belongs to at most one open run;
- `ALREADY_PAID_PROPOSED` — released in an earlier run, also
  named; this refusal is forever, because the record already
  sits in a bank export;
- `OUT_OF_CURRENCY_SCOPE` — outside the run's currency scope;
- `INVALID_PART_AMOUNT` — a stated part of zero or less (see
  **Partial payments** below);
- `PART_EXCEEDS_OPEN` — a stated part beyond the record's
  open amount, with the part, the remainder, and the released
  cumulative in the reason.

Zero eligible records fails assembly (`PaymentRunError`, 422):
an empty batch is an operator error, not a run. Member amounts
are the packet's amounts — the open amount, face minus applied
credits minus released payments, with the credits listed on the
member line — or a stated part of that amount, carried on the
line beside the full open figure it was taken from; and the
per-currency totals are tied back against the member lines
before the run is stored; a batch whose header disagrees with
its contents is never written. The export therefore instructs
net figures, and its total is the net sum by construction.

**Membership, honestly named.** A member's record keeps its
`APPROVED` status — that is the approver's decision, and the
treasurer's batch does not rewrite another human's act.
Membership rides the record as `payment_run_id` +
`payment_state`: `IN_RUN` while held by an open run,
`PAID_PROPOSED` once its run is released at the member's full
open amount, and `PART_PAID` when the release covered only
part of it — the record then also carries a `payments_applied`
ledger (run, amount, currency, treasurer, moment), written
only by the release path from the run's lines, from which the
resting state after any later return is recomputed.
*Paid-proposed*, not paid: the state names exactly what
happened — a proposal for this payment exists in a released
export. All transitions are audit events on the record.

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

**The unmatched pile.** Held advices used to end the story:
undecidable, unplaced, a report nobody could act on. The
worklist (`worklist.py`) computes what each held advice
might answer. The candidate universe is precise — lines of
`RELEASED` runs minus the (run, member) pairs some
confirmation has already classified `paid_as_proposed`:
settled money is never suggested twice, while short, over,
and missing lines stay in the universe, because a second
advice answering for them is exactly the pile's business.
For each advice line, each universe line yields at most one
candidate — the strongest rule that applies, evaluated in a
fixed order and stated in `WorklistPolicy` (`policy.py`):
`exact_line` (normalised reference equal, amount equal),
`reference_near` (Levenshtein distance exactly ≤ 1 over the
strip/upper-normalised strings — the duplicate watch's
normalisation idiom — with the amount equal), `exact_amount`
(amount equal, reference unrelated), `near_amount`
(reference equal or near, amount within the policy's 5.00
band and not exact; the delta rides the candidate, signed).
Combined candidates are computed separately: subsets of
2–4 universe lines of one run, one supplier, the advice
line's currency, summing exactly to the advice line, in
deterministic (record-id) order, capped per line. Currency
must agree for every rule — cross-currency money is round
4's own break, never a suggestion. The report ranks entries
by best-candidate strength (exact 100, reference-near 90,
combined 80, exact-amount 70, near 50) and prints the
ranking rule and the policy numbers, because the pile is
worked by a person who should be able to audit the
arithmetic. A candidate is a read-model value: computing
the worklist writes nothing, and the same run line may be
suggested against several advices — suggestions are not
claims.

**The link act.** `link_confirmation` is the pile's exit,
and it is deliberately not a smarter matcher. The controller
names the run and pairs advice lines with members — from a
candidate, or by hand (`rule="manual"` records which). The
service validates the pairing as an act, not a match: one
run per act; members belong to that released run; no member
or advice line pairs twice; no member is already settled
exactly for that run; a multi-member pairing spans one
supplier and one currency and sums exactly to its advice
line — any other lump is refused with both figures, the
difference, and the remedy, because the classification
machinery has no honest way to split one bank line across
members it does not cover in full. Classification itself is
`reconcile_linked_confirmation` in `confirmations.py`: the
controller's pairing stands in for the number match, and
everything else is the exact rule above — a single member
classifies exact/short/over (a currency break short by the
full proposal), combined members settle in full, unpaired
advice lines are `unexpected_line`, unpaired members
`missing`, and the summary arithmetic is the *same*
function the named-run path uses (extracted, not copied),
so a linked confirmation cannot make its own sums look
right either. The new confirmation carries `linked_by`,
`linked_rule`, and `linked_from`; the held record is never
rewritten — it gains a `linked` audit entry and leaves the
worklist by derivation (a confirmation exists with
`linked_from` pointing at it), so the pile's state is always
recomputable from the records. Overlays are written through
the same service helper as the named-run path. The linked
confirmation ends at the same controller gate; the
supplier ledger needs no change, because it reads the
member overlays the link writes.

**The not-ours mark.** The pile's other exit is a refusal
of the premise: `mark_advice_not_ours` stamps the held
confirmation `NOT_OURS` with a `NotOursMark` (actor, reason,
timestamp). It is terminal and named — a marked confirmation
can be neither linked nor decided, and the mark's reason is
what the next person reads. No migration was needed for any
of this round: confirmations are JSONB payloads, so the new
fields, the new status value, and the mark are payload
evolution, and the worklist itself is a pure read model.

Surfaces: `POST /remittances/{record_id}/confirm`,
`GET /confirmations/worklist`,
`POST /confirmations/{id}/link`,
`POST /confirmations/{id}/not-ours`,
`GET /confirmations[?run_id=]`, `GET /confirmations/{id}`,
`POST /confirmations/{id}/decision`, the console's
remittance-confirmation panel (advices with their named run,
per-run lines with proposed beside paid, the summary,
controller controls, plus the worklist section under it —
held advices ranked, candidates with their rules and
figures, link and not-ours controls), and the CLI's
`confirm` / `confirmations` / `worklist` /
`link-confirmation` / `not-ours` commands.

## The AP position

`position.py` is a pure read over the record store — it has
no store of its own, no migration, and no gate, because it
decides nothing: it reports. `build_position(records, as_of)`
takes one snapshot and computes both totals the close needs
from it, because the packet (`service.payment_packet`) is a
"now" read with no as-of parameter: passing the position an
as-of and asking it to tie against a packet computed at some
other instant would compare two different worlds. So the tie
is internal by construction and asserted: the packet total
recomputed under the packet's own inclusion rule (APPROVED +
invoice + extraction, at `credits.open_amount`) against the
position total (the sum of the aged lines), per currency,
inside one pass over one snapshot.

The two totals can differ in exactly one way. The aged lines
hold positive opens only; the packet's rule also carries a
negative open. A negative open means the applied-credit
ledger exceeds the face — application refuses over-credit in
code, so the state is only reachable by a record written
outside the credit path. Rather than age a negative payable
(a meaningless bucket) or silently drop it (a silent tie),
the position lists it as a `NEGATIVE_OPEN` anomaly and lets
the difference stand as a named `POSITION_TIE_BREAK` with
both figures. The report's `tie_status` is the verdict; the
CSV prints it on its face.

Ageing keys off the extraction's `due_date` (an ISO date on
the document's face; both adapters extract it). Bucket edges
are `policy.AgeingPolicy` (30/60/90, upper-edge inclusive),
printed on the report as `bucket_edges` so a reader never has
to trust that the edges are what the prose claims. A missing
or unreadable due date yields the `UNDATED` bucket with
`days_past_due: null` — the money still totals, because it is
still owed; it simply does not pretend to an age.

The state split reads `payment_state` off the same records
(`None` → `OPEN_FREE`, `IN_RUN`, `PAID_PROPOSED`,
`PART_PAID`) and must
foot to the open total exactly as the bucket split does;
`splits_agree` is the report checking itself. A `PART_PAID`
line ages only its remainder; the cumulative paid figure
rides it as a memo and never enters the ageing, because paid
money is not owed. Fraud-held
invoices are deliberately outside both totals: the packet
excludes them, so including them would break the tie by
design rather than by defect — they are carried as a
disclosed section with their flags and their own totals.
Fully credited invoices (open exactly zero, non-empty
ledger) are carried the same way: visible, zero-weighted —
and so are fully paid ones (open zero, released payments on
the record), disclosed on their own list so the close can
see the payment happened; the report's paid totals sum the
two readings of the same ledger.

Surfaces: `ApService.ap_position(as_of)` /
`position_export(as_of)`, `GET /position?as_of=`,
`GET /position/export?as_of=` (CSV download; `X-Export-Key`
and `X-Export-Sha256` headers carry the artifact's identity
in the run-export idiom), the console's close section
(supplier rows are `<details>` elements expanding to their
records), CLI `position <as-of> [--export]`, and the demo's
closing act. The export is re-rendered on demand rather than
served from a stored artifact — the position is a projection
of live records, not a decision, so there is no released
moment to freeze; the digest identifies the bytes of the
rendering that was filed.

## The supplier ledger

`ledger.py` is the second pure read model, and it exists
because the position answers the close's question (what do
we owe, aged, in total) while an AP lead on a supplier call
asks a different one: what is the whole story with *this*
supplier? `build_ledger(supplier, records, recons, as_of)`
projects one supplier's slice of the same stores; it has no
store, no migration, and no gate, for the position's reasons.

**Identity.** The ledger keys on the supplier master
(`store.reference`) and takes a record to be the supplier's
exactly when its `supplier_id` says so — the join the
pipeline already performed in `graph._process_one`: the
extracted `supplier_name` matched against
`suppliers_by_name` (trimmed, case-insensitive, exact), or,
failing that, the supplier of the purchase order the document
references. The ledger adds no matching of its own. Two
consequences are designed, not incidental. First, a document
whose name matches nothing and whose PO resolves nothing
keeps `supplier_id: None` forever — the ledger index lists
such records (invoices, credit notes, statements: the classes
intake resolves identity for) under `unmatched`, carrying the
name as written, so a spelling variant is visible as a data
problem. Attachment classes are excluded from that section by
design: intake files them with their submission and never
resolves a supplier for them (a remittance advice resolves
its payment *run*), so calling them unmatched would invent a
failure nobody committed. Second, an unknown supplier id is
a 404, not an empty page — there is no ledger for a supplier
the master does not govern.

**Figures.** Per invoice the ledger reads the face off the
extraction, the credit ledger off the record, and the open
amount from `credits.open_amount` — the same function the
packet, runs, and position read. Credits carry `credit_amount`
and, while UNAPPLIED, `credits.unapplied_reason`: the ledger
quotes the application path's own words rather than
summarising them. Ageing buckets are not recomputed either:
the ledger builds the position over the same snapshot and
reads each invoice's bucket out of it, so the bucket on a
ledger line is the bucket at close by construction.

**The tie.** The ledger's totals cover every APPROVED
invoice (face, credits, open) per currency. The position's
supplier row covers the same universe restricted to positive
opens, plus a separate fully-credited list at zero — so the
two open totals must be equal, and the as-of cannot matter
to them: ageing moves an open figure between buckets, never
in or out of the total. That is why the ledger can be
all-time and still tie to an as-of-parameterised report,
taking the position at whatever as-of the caller states.
The one honest divergence is the `NEGATIVE_OPEN` record: the
position excludes it from its row as a named anomaly, the
ledger's sum still counts it (it is, on its face, an approved
invoice), and the difference is carried on
`LedgerCurrencyTotals` as `POSITION_TIE_BREAK` with both
figures — the position's tie verdict, reused rather than
rebuilt. Fraud-held invoices are outside
both totals and shown on the ledger with their flags, the
same disclosure posture as the position's.

**Bank.** `LedgerBank` is built from the `Supplier` master
record and nothing else. The warning is assembled from the
supplier's records' `fraud_flags` — which today can only be
the bank screen's `BANK_DETAILS_CHANGED` — so "a warning is
present" and "a document disagreed with the master's account
and was held" are the same fact stated twice, never two
judgements that could drift apart. The account a document
presented is never copied into the ledger as the supplier's
details; the flags themselves quote only its last four
characters, exactly as the guardrail wrote them.

**Open breaks.** A reconciliation's non-`matched` lines are
open while the reconciliation's status is not `ACCEPTED` —
`AWAITING_CONTROLLER` and `FLAGGED_BACK` both count as open,
because flagged back means the AP team is taking the breaks
to the supplier. The ledger copies the `ReconLine`s verbatim;
classification stays reconciliation's job.

Surfaces: `ApService.supplier_index(as_of)` /
`supplier_ledger(supplier_id, as_of)`, `GET /suppliers/index`
and `GET /suppliers/{supplier_id}/ledger` (as-of defaulting
like `/position`'s, and always stated on the payload), the
console's ledger section, CLI `suppliers <as-of>` /
`ledger <supplier-id> <as-of>`, and the demo's closing act.

## The paid-duplicate watch

Two duplicate defences exist in this system, and the
architecture keeps them apart on purpose. The pipeline's
`_duplicate_check` (graph.py) is a *case* check: it compares
an arrival against the records in the store by supplier and
invoice number (raw string equality), and brands a second
copy `DUPLICATE_INVOICE` — an exception, back to the sender,
with no path onward and nothing to say about payment. That
is the right instrument for the co-pending copy. It is the
wrong one for the arrival that comes after its twin was
paid: there, an exception buries the one fact that matters —
**the money already left** — and a dead end gives a human no
honest way to rule on a legitimate late arrival.

`duplicates.py` is the second instrument, keyed on paid
history. The pipeline calls `find_paid_duplicate` in
`_process_one` the moment an invoice's extraction completes
and its supplier is resolved — before the match result can
route the record anywhere, and before the generic check (a
held record is not also branded with the blunter instrument;
its match is the document's own verdict, untouched). The
check is pure over the store snapshot:

- **Supplier** is the resolved `supplier_id` — the intake's
  exact join, never a resemblance.
- **Number** is compared under the normalisation the repo
  already uses for invoice identity (credits.py,
  confirmations.py, reconciliation.py each define the same
  two operations: strip, upper-case). The older pipeline
  check's raw comparison is a real hole — a resend whose
  number the engine read as `inv-1001` is not `INV-1001` to
  it — and the watch closes it for the paid case.
- **The twin must be paid**: released runs on file instruct
  money for it. The watch reads the runs themselves — every
  `RELEASED` run's lines naming the twin — rather than the
  twin's resting state, because the fact that matters is the
  instruction, and a twin paid in parts was instructed by
  more than one run: no single run's line is its paid
  figure, their sum is. (A state-only reading would also
  miss the planted universes the eval pack stages, where a
  released run exists without a record-side ledger.)
- **The amount must equal, to the cent**, one of two figures
  from the twin's paid story: its approved face, or the
  cumulative instructed total across those runs (the face
  minus the credits applied before release when one run paid
  it whole — read off the lines, not recomputed: the lines
  are what the bank was told). The module imports no
  tolerance; `matching.py`'s tolerances exist to absorb
  reading noise, and this is not a reading. Which figure
  matched is recorded as `amount_basis` — `FACE` wins ties,
  because an arrival stating the face of a payment that
  happened to instruct the same figure is most plainly read
  as the paid bill restated. The hold names the most recent
  paying run; `amount_paid` on it is always the cumulative.

A hit is stored on the arrival's own record as a
`PaidDuplicateHold` payload and the record's status becomes
`PAID_DUPLICATE_HOLD`. Everything downstream then refuses it
in its own idiom: the gate (`decide`) raises `GateError`
with the evidence in the message — the `FRAUD_REVIEW`
precedent, a state the gate cannot decide past; run assembly
refuses it by name (`PAID_DUPLICATE_HOLD`, the holding run
named); the packet and the position never see it, because
both read `APPROVED` records only. No new table is needed:
records are JSONB payloads, and the watch writes no record
of its own — the hold is a state of the arrival, not a
second ledger.

Resolution is `ApService.resolve_duplicate_hold`, a
controller's act in the `decide_reconciliation` shape: action,
named actor, **required reason**, refusal of anything not
currently held (with the prior resolution quoted back when
there is one). `CONFIRMED_DUPLICATE` moves the record to
`REFUSED` and writes the evidence plus the ruling into its
`refusal_reason` — a permanent, inspectable refusal, not a
deletion. `RELEASED_NOT_DUPLICATE` recomputes nothing: the
record returns to `EXCEPTION` when its match carries
exception codes, else `AWAITING_APPROVER`. Two consequences
are designed. First, the released record is *decidable*: its
submission's graph ran to completion when it arrived (a held
record never interrupts the gate), so the thread has no
interrupt to resume, and a plain resume would silently apply
nothing — `decide` therefore checks, after resuming, for
records still awaiting with a decision recorded against
them, and applies those decisions directly through
`apply_gate_decision`, the graph's apply write factored into
one implementation so both paths land identically. Second,
the release adjudicates the *record*: identity here is
`record_id`, not the invoice number. A second arrival of the
same document mints a new record, the paid history has not
changed, and the watch holds it again — the eval pack pins
this, because the alternative (a per-number release) is
exactly the laundering path the control exists to close.

The boundaries are part of the design, stated rather than
implied:

- **Not held**: an arrival whose twin is unpaid or held by
  an *open* run (the number rule's case); an arrival at any
  amount other than face or net (the number rule surfaces
  it, codes standing); an arrival under a different number
  (a different bill, however similar the amount); a
  fraud-flagged arrival (the fraud hold is computed first
  and owns the record — a bank-change resend is investigated
  as fraud, and its duplicateness is the least of it).
- **Never done**: no clawback or recovery state, no edit to
  the released run, its export, or the original record (the
  eval pins byte-identity across a hold, a confirmation, and
  a release), and no change to how the paid invoice's own
  credit notes are refused late (`TARGET_PAID_PROPOSED`) or
  how its run is confirmed. Credit notes and advices are not
  invoice-class arrivals and the watch never looks at them.

Surfaces: `ApService.list_duplicate_watch(as_of)` /
`resolve_duplicate_hold(...)`, `GET /duplicates/watch`
(as-of ages the holds, explicit for the position's reasons),
`POST /records/{record_id}/duplicate-resolution`, the
console's watch section (evidence, age, both exits) plus the
record detail's hold block, the ledger's marker on the
supplier's index row (`duplicate_hold_count`) and the hold
carried on the ledger's invoice lines, CLI `duplicate-watch`
/ `resolve-duplicate`, and the demo's closing sequence.

## Partial payments

Round 3's membership was whole: a member joined a run at its
full open amount, and `PAID_PROPOSED` meant exactly that.
This round makes membership partial without moving any of
the load-bearing definitions — the work is in where the new
facts live, not in new arithmetic.

**The part is a line fact, stated at assembly.**
`assemble_run(..., amounts)` takes an optional per-record
part. The member line gains `open_amount` (the full open
amount at assembly) beside `amount` (the instructed part, or
the whole open amount when no part is stated — old runs
serialise with `open_amount=None`, read as the whole). The
line remains the instructed truth: confirmations match
advices to it unchanged, exports instruct it unchanged, and
run totals tie to the lines as before. The money refusals run
last in `_refusal`, after every eligibility check, so a
fraud-held or exception record is refused for what it is
before its part is ever judged: `INVALID_PART_AMOUNT` (part
≤ 0) and `PART_EXCEEDS_OPEN` (part > open, the reason naming
the part, the open remainder, the face, the credits, and —
when any exist — the payments already released). Because the
open amount already nets prior releases, that second refusal
*is* the cumulative-overcommit check; there is no separate
committed-sum ledger to keep in step, and an invoice in an
open run is still refused `ALREADY_IN_RUN` before any of
this — one open run per invoice stands.

**Paid is a ledger, like credited.** `payments_applied` on
the record mirrors `credits_applied`: entries
(`PaymentEntry` — run id, amount, currency, actor, moment)
written only by the release path, from the run's own lines,
in the same act that moves the state. `credits.paid_amount`
sums it, and `open_amount` — the one computation — becomes
face − credits − paid. No migration: records are JSONB
payloads, and the new field defaults empty on every record
written before this round, which recomputes their open
amounts identically (paid 0.00).

**The resting state is derived from the ledger at each
transition.** Release: open 0.00 after the entry lands →
`PAID_PROPOSED` (the round-3 audit event, same words); a
remainder survives → `PART_PAID` (new event
`payment_run_part_paid`, the part, cumulative, and remainder
in its detail). Return: the freed member recomputes from its
ledger — payments on file → `PART_PAID` with `payment_run_id`
pointing at the most recent paying run; none → membership
cleared as before. A part-paid invoice therefore never reads
unpaid again, and a fully paid one is indistinguishable from
a once-paid one — by design: downstream, "paid in full" is
one fact however it was reached.

**The readers needed no new arithmetic — that is the
point.** The packet already proposed `open_amount`; it now
also carries the payments ledger on its lines, so the
remainder's provenance prints. The position's state mapping
gains `PART_PAID` as a fourth column of the *open* split
(the remainder ages; the paid figure is a per-line memo and
a per-supplier/currency memo total, kept out of the ageing
splits so nothing double-counts), and a `FullyPaidLine`
disclosure — open zero *with* payments — beside the
fully-credited one; an invoice both fully credited and
partly paid is disclosed as fully paid, the stronger fact.
The ledger reads the same `paid_amount` per invoice and adds
it to its currency totals; its tie to the position is the
round-7 tie, restated: open still foots, paid is a second
memo both readers source from the one ledger. The duplicate
watch's paid-history scan moved from the twin's resting
state to the released runs' lines (above), which is what
makes its `NET` basis honest under parts: the cumulative
instructed total, with the most recent paying run named on
the hold.

**What was deliberately not built.** No payment scheduling
(a part is stated by the treasurer per run, not derived from
terms or percentages in policy — the 60/40 split is a human
judgement this system records, not one it makes); no
splitting one run's part across bank accounts or value
dates; no interest or discount arithmetic on early/late
parts; and no change to confirmation semantics — an advice
still answers one run's lines, so a part-paid invoice's
remainder is confirmed, if at all, by the later run's own
advice.

## PO coverage

The match (above) is per-invoice arithmetic, and it is correct
to keep it that way — but correctness per invoice does not sum
to correctness per order. Coverage (`coverage.py`, a pure read
model in the position's and the ledger's idiom) is the second
axis: what approved invoices have together charged each
purchase order, against the total the order register states.

The definitions are pinned, because a coverage figure is only
as honest as its terms:

- **Billable** is `credits.billable_amount`: the approved face
  minus the credits applied against it. The credit belongs in
  the figure because a credit is the supplier taking money off
  the charge itself; a payment does not, because payment is
  settlement — the charge stands whether it has been settled
  in part, in full, or not at all. This keeps coverage and the
  open amount from ever being confused: open answers "what is
  still owed on this invoice", billed answers "what has this
  invoice charged its order", and a fully paid invoice is 0.00
  on the first and its full billable figure on the second.
- **The universe** is records with `doc_class == "invoice"`,
  `status == "APPROVED"`, an extraction, and a non-empty
  `po_reference` — the identity invoices already carry, resolved
  against the same register the match reads. Approval is the
  moment money becomes real in this system everywhere else (the
  packet, the runs, the position, the ledger all take exactly
  APPROVED), and coverage takes the same line: unapproved money
  has charged nothing.
- **The total's source is stated per row**: `ORDER_REGISTER`,
  the `PurchaseOrder.total` from the reference data (the
  customer's ERP mirror in production), or `NOT_ON_FILE` — an
  order known only through the invoices naming it. That second
  case gets no derived total and no verdict: state UNMEASURED,
  total `None`, billed sum listed. Deriving a total from the
  billing itself would make every order read exactly 100% by
  construction; the absence of a total is a fact to surface,
  not a gap to paper over.
- **States** come from `CoveragePolicy` in `policy.py`, beside
  the match tolerances and the ageing policy: WITHIN below the
  watch percentage (80.0 by default), WATCH at or above it,
  EXCEEDED when billed passes the total — compared in cents,
  not via the rounded percentage, so exactly 100.0% is a fully
  billed order (WATCH) and one cent past it is EXCEEDED. The
  report carries the threshold on its face.
- **Currency discipline**: a row speaks the order's currency
  (the register's; for an off-register order, its first counted
  contributor's). A contributor billing in another currency is
  not summed and not converted — it is listed on the row as
  excluded with the reason named. Silent conversion is how
  coverage figures become fictions.

**Inform, never block — and why.** The gate's blocking powers
in this codebase are reserved for deception and certainty:
FRAUD_REVIEW stops payment because the document may be an
attack; the paid-duplicate hold stops the gate because the
payment evidence is a fact about money already gone. An
EXCEEDED order is neither. Every contributing invoice matched
on its own merits; over-billing an order is a routine,
legitimate event (change orders, blanket orders, register
totals lagging amendments); and the total it is measured
against is reference data, which can be stale. Letting stale
reference data veto clean invoices would invert the system's
epistemics. So coverage takes the position's NEGATIVE_OPEN
posture instead: name the break loudly, in front of the human
whose decision it is. Concretely: the per-record fact
(`record_coverage`, counting the invoice in front of the
approver into the billed figure) rides the record's surfaces
before the gate; and `ApService.decide` writes one audit entry
— `po_coverage_at_decision`, quoting the fact — onto a record
whose decision lands while its order stands EXCEEDED counting
it. That entry is the module family's one deliberate write,
and it exists so the decision's context is itself auditable:
nobody can later say the approver never saw the accumulation,
and nobody needs to take the console's word for what was on
the screen. Match outcomes are never touched — coverage reads
records and reference data; the match reads a document and an
order; they share inputs, not verdicts.

Deliberately not built: no line-level coverage (which PO
*lines* are exhausted) — the register's receipts already give
the match per-line truth, and order-total coverage is the leak
that was invisible; no per-order budgets beyond the register
total; no blocking threshold at any percentage; and no merge
with the position — billed and owed are different ledgers, and
a reader who wants both can put the two reports side by side,
as the console does.

## Supplier bank history

`bankmaster.py` owns the master's memory; `store.py`'s
reference data holds the series; the service exposes the
acts and reads as `record_bank_version` / `bank_history`, the
API as `POST /suppliers/{id}/bank-versions` and `GET
/suppliers/{id}/bank-history`, the CLI as `record-bank-version`
and `bank-history`, and the console shows the timeline and
the recording form on the supplier's ledger.

**Why the master needed it.** The bank screen compared an
invoice against a single standing account. Legitimate changes
made that comparison wrong in both directions on the same
day: an old invoice quoting last month's genuine bank was
flagged, and a new invoice quoting next week's genuine bank
early — the fraud shape wearing a legitimate future — passed.
The fix is not a smarter comparison; it is a master that can
say what it held on a given date.

**The shape.** A supplier's bank details are a list of
`BankVersion`s in recording order: the account and bank name,
an `effective_from` date, and provenance — `recorded_by`,
`recorded_at`, and `source`, the statement of what the change
was recorded from. Version 1 is the **seed**, built from the
supplier at load: its `effective_from` is empty, which means
*before the record* — the deployment's standing word about
the past. The **version in force** on a date is the last
version effective on or before it; the seed is the floor, so
every date has a version. "Current" is that rule at the clock
the repo already runs on (UTC stamps at the service seam);
every reader that takes a date takes it explicitly, as
`as_of`, exactly like the position and the ledger. There are
**no toggles and no thresholds**: nothing here is policy,
because nothing here is judgement — it is a record.

**No confirmation state, argued from the repo's own idiom.**
Every governed act in this system is one named actor plus a
recorded reason: credits are applied by a named controller,
holds are resolved by a named controller, runs are released
by a named treasurer. None of them carries a second person's
confirmation, because the protection is never the second
signature — it is that the act is named, timestamped, and
permanent. A bank version follows the same idiom, and its
specific protection is the screen: a version blesses no
invoice dated before its effective date, so a bad recording
cannot launder a single document that predates it, and its
blast radius forward is exactly the window until a correcting
version lands. A two-person rule was considered and left out
deliberately: it would be the only one in the system, and it
would protect the recording, which is not where the risk
lives — the risk lives in documents, and documents are
screened.

**The screen's four outcomes.** `guardrails.screen_bank_details`
takes the series and the invoice date (the document's own
issue date, from extraction — the date the record already
carries for its age). Match the in-force version → no flag.
Match nothing ever held → `BANK_DETAILS_CHANGED`, the legacy
text verbatim against a one-version master — the seeded world
screens byte-identically to before this round. Match only a
version effective after the invoice date →
`BANK_DETAILS_NOT_YET_EFFECTIVE`, naming the effective date.
Match a version superseded before the invoice date →
`BANK_DETAILS_SUPERSEDED`, naming when. (If a supplier ever
reverts to an account it once held, a mid-window invoice is
judged SUPERSEDED — the retired window is the sharper story.)
A dateless invoice is judged against the latest version, and
the function says so. Every flag still lands the record on
`FRAUD_REVIEW`; the gate still cannot decide it; the ledger's
bank warning still quotes the flag. Only the comparison
gained a time dimension.

**Append-only, monotonic, never re-judging.** Recording
validates in order and appends: unknown supplier → 404;
missing actor, missing source, missing or unparseable
effective date, or details identical to the standing version
→ `MasterError` (422), each named. Effective dates must move
strictly forward — the check that makes the version in force
on any date unambiguous, and that makes rewriting the past
impossible by construction rather than by policy: to correct
a version you record another one. Nothing anywhere edits or
removes a version. And no read recomputes a verdict: the
screen's outcome is stamped into `record.fraud_flags` at
intake, the ledger quotes those stamps, the gate reads them,
and recording a version changes no stored record by a byte —
the eval pack pins a decided record's whole JSON identical
across a later recording, as it does a held one's.

**Which readers read which date.** The two surfaces that
derive bank details on read take the version in force at
*their* moment, and say which: the **ledger** shows the
version in force at its `as_of`, with the version number, the
count on file, and its effective date — so a September ledger
and a November ledger honestly show different banks for the
same supplier. The **payment export** pays the version in
force on the release day: it instructs money moving now, so
it takes the master's present word, not the version an
invoice was screened against. `Supplier.bank_account` remains
the seed's account — the invariant is that current details
are always derived from the series, never read off the
supplier row.

**Storage: no migration, by the master's own precedent.**
Bank versions are reference data. Suppliers, purchase orders,
and receipts have always lived in the reference store —
loaded per deployment, persisted nowhere in Postgres; the
four migrations cover only what the pipeline writes
(records, reconciliations, runs, confirmations). Versions
extend that same store: seeded at load, appended by the
recording act, held in memory exactly as the master they
version is. A production deployment whose ERP owns the
master loads the ERP's change history at the same seam; if a
deployment ever needs the history to outlive the process
independently of its ERP, that is the deployment's persistence
decision at the seam, not a pipeline table.

Deliberately not built: no bank-change *document* species —
the world's document classes are payable-path classes, and a
change notice is not one; provenance is the actor's statement
of the evidence checked, in the world's own materials. No
routing/IBAN split: the master's account has always been one
string, and the screen's comparison is over that string. No
retroactive re-screening of held records when a version
lands: a fraud hold is resolved in the master out of band and
by a fresh submission, as it always was — the history makes
the *next* screen right, it does not rewrite the last one.

## The new-supplier door

The intake resolves a payable document's supplier two ways —
the stated name, matched exactly against the master under its
normalisation (strip, lower-case), or the supplier of the
purchase order it references. Both failing used to be a
shrug: the fraud screen compared against nothing and returned
no flags, the match failed on the missing order, and the
record stood at a generic EXCEPTION. The judgements this
system makes are master-keyed, so an unresolvable document
was the one arrival no governance touched — while being, as
a first payee, the arrival governance exists for. It now
stops in the pipeline, before the screen, in
`NEW_SUPPLIER_REVIEW` (`enrolment.py` owns the evidence and
the queue; `graph.py` owns the stop; `service.py` owns the
two acts).

**The hold is unstamped by design.** A held record carries
`match = None`, `fraud_flags = []`, `bank_account_seen =
None` — not as gaps but as the truth: no match was computed
and no screen ran, and the audit entry says both in words.
What the record does carry is a `SupplierHold`: the name as
written, its normalised form, and a near-miss — the master
supplier within two edits of the stated name (the worklist's
Levenshtein arithmetic), named as evidence and computed once,
at intake, under the stamped-at-intake discipline the fraud
screen set. The near-miss is the misspelling case made
legible: the controller can see that "Harbor Light Marine
Supply" is Harborlight wearing a space, and refuse it as
exactly that, instead of the system either attaching it by
resemblance (a silent join) or enrolling it (a second payee
for one company — the fraud pattern with paperwork).

**The exits are asymmetric, deliberately.** Enrolment is an
identity act: it creates the master record for the group —
every held record that stated the identity under the intake's
own normalisation — and releases all of them. It is also a
bank-details recording, so its validation is
`record_bank_version`'s, mirrored by name: actor, account,
and the source the details were verified from, all checked
before the master write, so a failed enrolment writes
nothing and a half-enrolment cannot exist. The version it
creates is a version 1 in the seed's shape (undated — the
standing word from before the record), differing from a
deployment seed only in carrying the enrolment's provenance
instead of the seed's. The id defaults to the next free
`SUP-<n>` and accepts a stated id, because the order register
already names suppliers AP has not enrolled — the ERP raised
the purchase order first — and enrolling under the order's
id is what lets the released invoice match the order that was
always on file. Refusal is a document act: per record,
reasoned, terminal (`NOT_A_KNOWN_SUPPLIER`), enrolling
nothing — because a group can hold one genuine document and
one forgery under the same stated name, and the forgery's
refusal must not condemn the genuine paper, nor the genuine
paper's enrolment bless the forgery (which is then screened,
on release, against details it does not match — and lands on
FRAUD_REVIEW).

**Release is a re-run, not a shortcut.** The pipeline's
adjudication — screen, paid-duplicate watch, duplicate check,
match, status — is factored once (`_adjudicate`), called by
intake for fresh documents and by the enrolment path
(`readjudicate`) for released ones, under the enrolled
supplier. A released record therefore carries no mark of
leniency: its audit shows the hold, the enrolment, and then
the ordinary events its contents earn, in order. This is the
same factoring the gate needed in round 8 (`apply_gate_decision`)
— one implementation, so a judgement lands the same way by
either path.

**Visibility without payability.** Held money is not owed
money, and the surfaces keep the two apart the way they
already did for fraud-held money: the packet, runs, and
position totals read APPROVED records only; the position
additionally discloses new-supplier-held lines (name as
written, face, open) beside its tie with their own totals,
and the CSV carries the section; the ledger index counts the
door's subset of its unmatched section (`new_supplier_hold_count`),
the unmatched list itself being where these records already
stood — now with a status that names why. A payment run asked
for a held record refuses it by name (`NEW_SUPPLIER_HOLD`),
before the generic not-approved, in the check order the
refusals already keep.

**Storage: no migration, by the same precedent.** Enrolment
writes reference data — one supplier row and one version-1
entry behind the reference store's surface, exactly where
`record_bank_version` appends. The migration ladder covers
what the pipeline writes; the master is loaded, not written,
and its two governed exceptions (append a version, enrol a
supplier) both live at that seam. In a deployment whose ERP
owns the master, enrolment is the ERP's vendor-onboarding act
and this surface is where its result lands.

Deliberately not built: no fuzzy auto-attach at any threshold
— the near-miss informs a human and joins nothing. No
enrolment from the document's own stated bank details: the
details a controller enrols are the ones they state from
out-of-band verification, which may differ from the
document's — and when they do, the released document is a
fraud case, which is the system working. No provisional or
probationary payee state: a supplier is on the master or it
is not, and a payment run never has to ask which kind. No
door for statements and attachments: a statement from an
unknown name is already refused by the payment path and named
in reconciliation's own refusal when it cannot be reconciled;
the door governs payables, where the money risk is.

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

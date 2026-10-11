<p align="center">
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="96">
</p>

# Blueprint — Reducto Invoice AP, Deployed In-VPC

A working prototype of the deployment an enterprise finance team
needs on top of a document platform: supplier invoices arrive,
are **classified, split, and extracted by Reducto** behind a
document port, **matched three ways** — invoice × purchase order
× goods receipt — **in code**, screened for the bank-detail
fraud pattern, and stopped at a **named approver's gate**. The
output is a proposed payment packet. **Nothing in this system
executes a payment.**

The deployment story is the point. Finance documents are among
the most sensitive bytes a company holds, so the blueprint runs
in two profiles from one codebase:

- **`cloud`** — Reducto's hosted API does the document work.
- **`vpc`** — the document engine is reached at an internal
  endpoint inside the customer's boundary; the engine service
  publishes no port, and document traffic never leaves the
  boundary. Same pipeline, same guardrails — the difference is
  endpoint and topology, and it is configuration, not a fork.

The promises are precise, and they are enforced in code, not in
prompts:

- **A changed bank account is a hold, never an approval.** If an
  invoice asks to be paid to a different account than the
  supplier master's approved one, the record stops at
  FRAUD_REVIEW. The gate cannot clear it; bank changes are
  verified out of band and recorded in the master.
- **What is not an invoice never reaches the payment path.** A
  statement or letter is refused with the reason on its record.
- **The match is arithmetic.** Tolerances live in one policy
  module; every exception names its code and its figures.

This is an independent Trida AI blueprint. It is **not
affiliated with or endorsed by Reducto**; it is built by Trida
AI against Reducto's public API documentation to show how a
forward-deployed team would put the Reducto platform into
production inside an enterprise finance operation. Everything
in the dataset is synthetic — there is no real supplier,
invoice, or bank account anywhere in this repository.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

---

## Try it in 3 commands

```bash
git clone https://github.com/tridaai/blueprint-reducto-invoice-ap
cd blueprint-reducto-invoice-ap
uv sync --extra dev && uv run reducto-ap-agent-demo
```

The demo runs the whole synthetic AP world end to end — the
submissions covering clean matches, a price drift inside
tolerance, a short shipment, a duplicate, a credit note, a
bank-detail fraud attempt, statements that get refused and
then reconciled — and prints the payment packet a named
approver's decisions produce, then assembles a payment run
over it and has a treasurer release the batch.
It runs on the **offline adapter**, the labelled fallback that
implements the same DocumentPort semantics over the documents'
text layer; the standard run is the real Reducto profile (see
[Configuration](#configuration)).

Serve the console instead:

```bash
uv run uvicorn reducto_ap_agent.api:app --port 8000
# → Console: http://localhost:8000/   API docs: http://localhost:8000/docs
```

No uv? The pip fallback is the two-step form in the Makefile:
`make install-pip` (the frozen set first, then the package
itself).

Or the full stack, in the VPC profile:

```bash
docker compose -f docker-compose.yml -f docker-compose.vpc.yml up --build
# → Console: http://localhost:8000/ — engine internal-only, no port published
```

---

## The problem

Accounts payable is where document AI meets money, and the gap
between a demo and a deployment is everything around the model:
Where do the documents live, and whose boundary do they cross
to be read? What decides that an invoice matches — a model's
impression, or arithmetic a controller can re-perform? What
stops the oldest fraud in the book — an invoice that quietly
changes where the money goes? And who, by name, said this
payment could be proposed?

This blueprint is Trida's answer, shaped as a deployment of
Reducto: the platform does what it is for (classification,
splitting, schema extraction with bounding-box citations), and a
production-shaped system around it does the rest — matching,
screening, gating, audit.

## How it works

```
submission ──► intake ──► split & classify ──► extract ──► screen ──► match ──► GATE ──► packet
               (injection   (DocumentPort:        │          (bank +    (three-   (named    (proposed
                screen)      Reducto / offline)    │           payable)   way, code) approver) payments)
                                                   └── non-payable classes refused here, reason on record
```

- **DocumentPort** — one seam, two adapters. `reducto`: Reducto's
  public API (`/upload` → `/classify` → `/split` → `/extract`,
  citations requested on every extract), used in both profiles —
  only the base URL changes. `offline`: a deterministic
  text-layer adapter for tests, evals, and demos. The pipeline
  cannot tell which it is talking to; the contract tests hold
  both to the same semantics. Selecting Reducto without a key
  fails loudly — the service never serves one engine's results
  under another's name.
- **The match** — invoice lines against PO lines against
  received quantities, totals against totals, tolerances from
  one policy module (`policy.py`). Outcomes: `MATCHED`,
  `MATCHED_WITH_TOLERANCE` (the drift is shown, not buried), or
  `EXCEPTION` with named codes — `PRICE_VARIANCE`,
  `QUANTITY_VARIANCE`, `TOTAL_MISMATCH`, `LINE_NOT_ON_PO`,
  `NO_PO_REFERENCE`, `PO_NOT_FOUND`, `NO_RECEIPT`,
  `DUPLICATE_INVOICE`.
- **Guardrails as code** — the bank-detail screen (any difference
  from the supplier master's approved account is a fraud hold),
  the payable screen (non-invoice classes refused, reason
  recorded), and an injection screen that fences
  instruction-like text out of the copy of a document that
  travels onward to any engine.
- **The gate** — matched records stop mid-pipeline at a
  LangGraph interrupt, checkpointed in Postgres, until a named
  approver approves or rejects with a reason. Fraud-held and
  exception records cannot be decided at the gate at all.
- **The packet** — a read model over approved invoices:
  proposed payments by supplier and currency, each at its
  **open amount** — the approved face minus the credit notes
  applied against it, listed on the line — plus the approved
  credits still unapplied, with the reason each one waits.
  Generating it moves no money; this system has no code path
  that could.

## Credit notes

An invoice says what AP owes; a credit note is the supplier
saying some of it is owed back. A credit that lives outside
the payable math is an overpayment waiting for a batch to
carry it, so credits here are **applied, in code, against the
invoice they name**:

- **The document names its invoice.** Both adapters extract
  the credit's own reference (a *Credits Invoice* field). A
  credit naming no invoice, or one that is not on file, is
  held `UNAPPLIED` with the reason named — it is never matched
  to an invoice by amount similarity, because amount-matching
  is exactly how double-crediting happens: two credits for the
  same amount both "fit" one invoice, and the payable falls
  twice for one debt.
- **Application is a controller's act.** A named controller
  applies an approved credit against its approved invoice. The
  application is recorded on the invoice's ledger; the credit
  flips to `APPLIED`, pointing back. Partial coverage is the
  normal case and tracked cumulatively — several credits can
  land on one invoice, each applied in full, to the cent, with
  no tolerance. A credit larger than the open amount is
  refused `OVER_CREDIT` with both figures, and a credit note
  is never split across applications. A credit against a
  fraud-held record is refused with the hold named.
- **The approved amount never changes.** The invoice's face is
  history — an approver approved that figure. What moves is
  the derived **open amount**: face minus applied credits. The
  packet proposes it, payment runs pay it, the bank export
  instructs it — one amount rule, computed once in
  `credits.py`.
- **A credit never joins a payment run.** It is not a negative
  payment instruction; its reduction stands inside the
  invoice's amount, whichever batch the invoice lands in. A
  fully credited invoice is refused by the run
  `FULLY_CREDITED`, with the credits named — excluded in the
  open, not silently dropped.
- **Late credits are refused by name.** Against an invoice
  held by an open run: `TARGET_IN_RUN`. Against one already
  released (`PAID_PROPOSED`): refused terminally — that
  money's story belongs to the run and its remittance
  confirmation, and the credit cannot rewrite it. It stays on
  file, unapplied, on the packet with its reason: next cycle's
  business, settled against the supplier's future invoices.
  (Carrying a supplier credit balance forward is a ledger
  this blueprint deliberately does not build — see Honest
  status.)

## Supplier statement reconciliation

Invoices are only half of AP's document life. Every month the
supplier sends a statement — their list of what they believe is
still open — and somebody has to prove it right or wrong, line
by line. That is the second workflow through the same port:

- **Intake** — a statement arrives like any other document and
  is **refused by the payment path**, exactly as before. But its
  open-item lines (invoice number, date, amount, currency) are
  extracted through the DocumentPort and captured on the record
  first, so reconciliation works from records, not raw files.
- **Reconciliation in code** — `reconciliation.py` joins the
  statement's lines against AP's live invoice records for that
  supplier. Every line lands in exactly one class: `matched`,
  `amount_disagrees` (both figures shown, never collapsed),
  `missing_in_ap` (the classic "never received it"),
  `missing_on_statement` (AP holds it; the supplier no longer
  lists it), or `duplicates_within_statement`. Amounts agree or
  disagree by the match policy's total tolerance — the same
  policy module as the three-way match, no second set of
  numbers. The statement's own stated total is tied against the
  sum of its lines; if the statement's arithmetic is the break,
  that is named on the reconciliation.
- **The controller gate** — a reconciliation is a stored record
  ending `AWAITING_CONTROLLER`. A named controller accepts it,
  or flags it back with a note the AP team takes to the
  supplier; decisions are audited and final.
- **Evidence, never action** — computing or deciding a
  reconciliation changes no document record and no packet line.
  It cannot approve, alter, or exclude an invoice; the eval
  pack pins that invariance.

## The payment run

The packet proposes; a payment run is how a batch becomes
controlled. A treasurer assembles a run for a value date — named
records, or every approved invoice — and the assembly is
the control:

- **One open run per record.** Assembly holds each member on
  its record. Naming a held record in a second run refuses it
  by name, with the holding run's id in the reason — and a
  record from a released run is refused forever: it is already
  in a bank export, and paying twice is the failure this exists
  to prevent.
- **Members are invoices, at their open amounts.** The credits
  applied against a member ride its line, so the treasurer
  sees why the figure that will leave the bank differs from
  the invoice's face; the export instructs the net figure, so
  the run's total is the sum of what actually leaves. A credit
  note offered as a member is refused by name, and so is a
  fully credited invoice (`FULLY_CREDITED`) — nothing is
  silently dropped.
- **Nothing ineligible slips in quietly.** Fraud-held and
  exception records can never enter a run; asking for them
  produces a named refusal (code + reason) while the run
  assembles from the eligible remainder. Totals are computed
  from the member lines and tied back against them before the
  run exists.
- **The treasurer's gate.** The run ends `AWAITING_TREASURER`.
  A named treasurer **releases** it or **returns** it (a reason
  is required to return; the AP team reworks on it). Returning
  frees every member to join a later run. Membership is a state
  layered over the record, never a rewrite: the record stays
  `APPROVED`, because the approver's decision and the
  treasurer's batch are two different humans' acts.
- **The bank export.** Release generates a payment-instruction
  CSV once — deterministic bytes, written to the object store
  under a versioned key, pinned on the run by its SHA-256
  digest, and served back byte-identical on every download.
  Its bank details are read from the **supplier master**, never
  from the invoice: an invoice is a claim by the sender, the
  master is the verified record, and there is no path by which
  an invoice's account reaches a bank file.
- **Still nothing pays itself.** A released run is a
  controlled proposal handed to the customer's payment
  systems — `PAID_PROPOSED`, not paid. This system executes
  no payment, on any path.

## Remittance confirmation

`PAID_PROPOSED` says what the treasurer released. What the bank
*did* comes back later, as a remittance advice — and
confirmation is the discipline of answering it:

- **The advice is read like any other document.** It arrives
  through the same DocumentPort (classify → extract), its paid
  lines captured on its record at intake — number, amount
  paid, currency, value date, bank reference — and it stays an
  attachment: an advice is never payable, and nothing in it
  is an instruction.
- **Tied to a run only by its own reference.** The advice's
  header names the run it answers (or its lines do,
  unanimously). An advice naming no run, a run that is not on
  file, or a run the treasurer has not released is held
  `UNMATCHED` with the reason on its confirmation record —
  never guessed onto a run: attaching the bank's answer to
  the wrong batch would corrupt two reconciliations at once.
- **Exact, never within tolerance.** `confirmations.py`
  compares paid against proposed with an equality, not the
  match policy's tolerances. Tolerances exist to absorb
  document-reading noise in the three-way match; money that
  left the bank is not a document — a one-cent shortfall is
  a break a human explains, not a tolerance case. Every
  member lands in exactly one class: `paid_as_proposed`,
  `short_paid` (both figures and the shortfall),
  `over_paid`, `missing` (released, and no line answers for
  it), or `unexpected_line` (the advice names an invoice
  that was not in the run — shown, never absorbed; a line
  that arrived in the wrong currency is short by the full
  proposal, its figure riding the line in its own currency).
- **A summary that ties, or names why not.** Counts and
  totals per class and currency partition the run: confirmed
  + short + over + missing recompose the run's total exactly,
  recomputed from the run's own lines — or the confirmation
  carries a named arithmetic break. Unexpected money is
  reported beside the tie, never inside it.
- **An overlay, never a rewrite.** The confirmation state
  (`CONFIRMED`, `SHORT_PAID`, `OVER_PAID`, `MISSING`) lands
  on each member record *beside* `PAID_PROPOSED`, under its
  own field, with an audit event naming the confirmation.
  The run, its decision, its export bytes, and every
  approval are untouched — the eval pack pins them
  byte-identical across a full cycle.
- **The controller's gate.** A confirmation is a stored
  record ending `AWAITING_CONTROLLER`, like a statement
  reconciliation: a named controller accepts it, or flags
  it back with a note the AP team takes to the bank. An
  unmatched advice cannot be decided — there is nothing to
  accept until it answers a run.

## The AP position at close

The packet answers "what would we pay next". The close asks a
different question: what does the subledger say we owe, aged,
by supplier — and does it agree with the packet? The position
is that read, computed in one pass over the record store:

- **Aged off due dates, against an explicit as-of.** Days past
  due is computed against a stated date, never a hidden wall
  clock. Not-yet-due is `CURRENT` however old the invoice is —
  what matters at close is how far past its terms a payable
  stands. Buckets (`CURRENT` / `1–30` / `31–60` / `61–90` /
  `90+`) are authored policy in `policy.py`, printed on the
  report, and upper-edge inclusive: exactly 30 days past due
  is `1–30`. An invoice with no due date on record is
  `UNDATED` — its own line class, totalled and shown, never
  silently bucketed by an age it does not carry.
- **One open-amount rule.** Every figure is `credits.py`'s
  open amount — approved face minus the applied-credit
  ledger — the same computation the packet and the runs read.
  The position never recomputes it differently.
- **The state of the money is a second split of the same
  total.** Each line is `OPEN_FREE`, `IN_RUN` (held by an open
  run), or `PAID_PROPOSED` (released, awaiting the bank's
  confirmation). The state columns and the ageing columns both
  foot to the same open total, per supplier and per currency,
  and the report checks its own arithmetic.
- **A tie that can break, by name.** The packet is not
  as-of-parameterised, so the position computes both totals
  from the same records in the same pass — the packet total by
  the packet's own inclusion rule, the position total as the
  sum of the aged lines. They can differ only when a record's
  ledger exceeds its face (open amount negative): application
  refuses over-credit in code, so that state means a corrupted
  record. It is kept out of the buckets, listed as a
  `NEGATIVE_OPEN` anomaly with both figures, and the
  difference breaks the tie — `POSITION_TIE_BREAK`, per
  currency. The report never calls itself tied when it is not.
- **Disclosed, not buried.** Fraud-held invoices sit outside
  the packet's universe and therefore outside the tie — and
  are listed beside the position with their flags, because
  the close should see money in dispute. Fully credited
  invoices (open exactly 0.00) are listed too: they owe
  nothing and age nothing, and the cover should be visible.
- **Surfaces.** `GET /position?as_of=YYYY-MM-DD`, the console's
  close section (supplier rows expand to the records under
  them), CLI `position <as-of> [--export]`, the demo's closing
  act, and a CSV export at `positions/{as_of}/ap-position-v1.csv`
  in the object store, with its SHA-256 returned beside the
  bytes in the run exports' idiom.

## Configuration

Provider-first: the standard run is Reducto. Copy `.env.example`
to `.env` and set:

| Variable | Meaning |
|---|---|
| `DEPLOYMENT_PROFILE` | `cloud` (hosted Reducto API) or `vpc` (internal engine endpoint) |
| `DOCUMENT_BACKEND` | `reducto` (standard) or `offline` (labelled fallback) |
| `REDUCTO_API_KEY` | Reducto API key — required when the backend is `reducto` |
| `REDUCTO_BASE_URL` | Override the profile's endpoint (e.g. a licensed self-hosted engine in the VPC) |
| `DATABASE_URL` | PostgreSQL for records + checkpoints; unset = in-memory doubles, and `/readiness` says so |
| `S3_ENDPOINT_URL`, `S3_BUCKET` | Object storage for document bytes (S3-compatible) |

In the `vpc` profile without `REDUCTO_BASE_URL` set, the adapter
targets `http://reducto-engine:8080` — the compose service name.
The bundled compose stack answers there with a **labelled
stand-in** (the offline adapter behind the Reducto API shape);
a customer deployment puts their licensed self-hosted engine at
the same endpoint. This repository does not contain a Reducto
binary and does not pretend to.

## Honest status

- **Live Reducto verification is pending an API key.** The
  adapter is written against Reducto's public API documentation
  and its response mapping is unit-tested against stubbed
  transports; no live call has been made from this repository
  yet. The offline adapter is what the tests, evals, and demo
  actually run.
- The synthetic documents are **text-layer documents** — the
  offline adapter reads the text a PDF carries, it does not
  OCR scans. Reducto's engine is the OCR story in a real
  deployment.
- Reference data (suppliers, purchase orders, receipts) is
  seeded from the bundled synthetic world; in a customer
  deployment that seam loads the ERP's mirrors instead.
- The payment packet is a proposal for the customer's AP
  system of record. Executing payments stays there, by design.
- A credit that arrives after its invoice was released in a
  payment run is refused and stays unapplied in this system.
  Settling it against the supplier's *next* invoice — the
  real-world handling — needs a supplier credit-balance
  ledger, which this blueprint deliberately does not build;
  the credit, its amount, and its refusal reason stay visible
  on the packet rather than disappearing.

## Evaluations

`make evals` runs the golden pack over the synthetic world and
gates on it:

- **Extraction accuracy** — the offline extraction's fields
  against gold, per field, with a **citation present for every
  extracted value**;
- **Match decisions** — pipeline status and match outcome per
  planted case;
- **Exception precision** — the exception codes named are the
  codes planted, no more;
- **Fraud recall — zero misses tolerated.** The planted
  bank-change case must land on FRAUD_REVIEW; a single miss
  fails the pack.
- **Statement reconciliation** — one statement plants a break of
  every class and each must land in its exact class; a second
  statement must reconcile fully; statement totals must tie;
  and no document record may change as a result of reconciling.
- **Payment runs** — a batch over approved invoices (one at
  its net open amount, its credit applied in code first) must
  tie its totals; the fraud hold and an exception must be
  refused by name; a member of an open run must be refused
  with the holding run named; return must free the members;
  and the released export must be byte-stable and carry only
  the supplier master's bank details.
- **Credit application** — a credit must land only against
  the invoice its document names: applied, it nets the
  invoice's open amount with the face and the approval
  untouched; a full cover must exclude its invoice from a run
  by name; an over-credit, a credit naming no invoice, one
  naming an invoice not on file, one against the fraud hold,
  and the late credit against a paid-proposed invoice must
  each be refused with their reasons; and the released export
  must instruct the net figure with no credit line of its own.
- **Remittance confirmation** — the bank's answer to a
  released run must classify line by line (exact, short,
  over, missing, one line the run never contained); the
  summary must tie to the run's totals; advices naming no
  run or a run not on file must be held unmatched with their
  reasons; and the run, its export, and its members'
  approvals must be byte-identical afterwards.
- **AP position** — bucket placement must be exact including
  the boundary days; an undated invoice must be named
  `UNDATED`; the aged total must tie to the packet's open
  total; the state split must foot to the same figure; and
  a corrupted record whose ledger exceeds its face must
  break the tie by name (`POSITION_TIE_BREAK`).

Live-Reducto eval runs are opt-in (`EVAL_LIVE=1` with a key)
and reported, not gated.

## Layout

```
src/reducto_ap_agent/
  ports.py         DocumentPort + ObjectStorePort — the seams
  reducto_docs.py  the Reducto adapter (cloud + VPC endpoints)
  offline_docs.py  the labelled text-layer fallback adapter
  engine_api.py    the VPC demo stack's internal engine stand-in
  profiles.py      cloud vs vpc, resolved in code
  matching.py      the three-way match, in code
  credits.py       credit application + the open amount, in code
  reconciliation.py statement vs AP records, line by line, in code
  payments.py      run assembly, the refusals, the bank export, in code
  confirmations.py remittance vs the released run, line by line, in code
  position.py      the AP position at close: ageing + the tie-out, in code
  guardrails.py    bank-detail, payable, and injection screens
  policy.py        every tolerance, in one module
  graph.py         the LangGraph pipeline and its gate
  service.py       submissions, the gates, the packet, runs, confirmations
  store.py / db.py records in Postgres (or the in-memory double)
  datasets.py      the synthetic world — gold by construction
migrations/        numbered SQL, applied at startup
evals/             the golden pack and its runner
docs/              architecture.md — the deployment, in depth
```

## License

Apache 2.0 — see [LICENSE](LICENSE).

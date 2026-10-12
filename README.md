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
- **A paid invoice, sent again, is a hold, never a second
  payment.** An arrival that answers to an invoice already
  released for payment — same supplier, same number, at the
  paid face or the net figure the run instructed — stops at
  `PAID_DUPLICATE_HOLD` on the paid evidence, and only a named
  controller can rule on it.
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
- **Guardrails as code** — the bank-detail screen (an invoice
  whose bank differs from the version of the supplier master
  in force on the invoice's own date is a fraud hold — the
  master is versioned, below), the payable screen (non-invoice
  classes refused, reason recorded), and an injection screen
  that fences instruction-like text out of the copy of a
  document that travels onward to any engine.
- **The gate** — matched records stop mid-pipeline at a
  LangGraph interrupt, checkpointed in Postgres, until a named
  approver approves or rejects with a reason. Fraud-held and
  exception records cannot be decided at the gate at all.
- **The packet** — a read model over approved invoices:
  proposed payments by supplier and currency, each at its
  **open amount** — the approved face minus the credit notes
  applied against it and the payments already released
  against it, both listed on the line — plus the approved
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
  the derived **open amount**: face minus applied credits
  minus payments released. The
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
  record whose open amount has been fully released is refused
  forever: it is already in a bank export, and paying twice
  is the failure this exists to prevent. A record released at
  only part of its open amount is a different case — it may
  join the *next* run for its remainder (see **Partial
  payments** below).
- **Members are invoices, at their open amounts — or at a
  stated part of them.** The credits applied against a member
  ride its line, so the treasurer sees why the figure that
  will leave the bank differs from the invoice's face; the
  export instructs the net figure, so the run's total is the
  sum of what actually leaves. A credit note offered as a
  member is refused by name, and so is a fully credited
  invoice (`FULLY_CREDITED`) — nothing is silently dropped.
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

## The unmatched-advice worklist

A held advice is still the bank reporting money that left.
The pile is worked — and working it is detective work: the
reference has a typo, one lump pays two lines, the amount is
three units under the line it answers. `worklist.py` does
that arithmetic in code, and leaves the judgement to a
person:

- **Candidates, computed — never conclusions.** For every
  held advice, each paid line is compared against the
  *confirmable universe*: the lines of released runs that no
  confirmation has settled exactly (`paid_as_proposed`) for
  that run. Settled money is never suggested twice. Each
  comparison yields at most one single-line candidate, under
  the strongest rule that applies — the rules and their
  numbers live in one place (`WorklistPolicy` in
  `policy.py`) and print on the report: `exact_line` (the
  reference names the line and the amount agrees to the
  cent — the round-4 match, missed only because the advice
  named no usable run), `reference_near` (the reference,
  under the house normalisation — strip, upper — is one edit
  from the line's number, amount exact), `exact_amount`
  (amount exact, reference unrelated), `near_amount` (the
  reference names the line and the amount is within a stated
  5.00 but not exact — suggested, labelled near, never
  presented as a match). A `combined_sum` candidate stands
  apart: one advice line whose amount is the exact sum of
  2–4 unsettled lines of one run, one supplier, one
  currency. Every candidate carries its rule, its strength,
  and its numbers — the human sees *why*.
- **Suggestions confirm nothing.** A run line may be
  suggested against several advices at once — suggestions
  are not claims. An advice with a perfect candidate sits
  in the pile, unconfirmed, until a controller acts; the
  eval pack pins the pile and every overlay standing
  untouched under repeated worklist reads. Entries rank by
  best-candidate strength, strongest first, no-candidate
  advices last in the order they were held — the ranking
  rule prints on the report.
- **The link is a human act.** A named controller ties a
  held advice to one released run, pairing advice lines
  with run members — following a candidate, or choosing
  lines directly (the suggestion is advice, not a cage).
  The pairing may be the controller's own (`manual`) or
  carry the candidate rule it came from; either way the new
  confirmation is *built by the link*, stamped with who
  linked it, under which rule, and from which held record,
  and the held record itself is never rewritten into a
  matched one. Classification then runs under the exact
  rule above, with the pairing standing in for the number
  match: a single member classifies exact, short, or over on
  the money as it stands; a combined lump settles only when
  it equals the members' total to the cent — any other lump
  is refused by name, with the figures and the remedy (link
  it against a single member, where it classifies short or
  over, or hold for a corrected advice). Members already
  settled exactly are refused by name; unpaired advice
  lines are `unexpected_line`, unpaired members `missing`,
  exactly as the named-run path leaves them. The linked
  confirmation ends at the same controller gate.
- **Not ours is also an answer.** A named controller may
  mark a held advice `NOT_OURS` with a reason — the bank
  attached another customer's advice, the payment belongs
  to another company's batch. The mark closes the worklist
  item and stays on the record; a marked advice can be
  neither linked nor decided. The pile keeps what nobody
  has judged, and only what somebody has judged leaves it.

Surfaces: `GET /confirmations/worklist`,
`POST /confirmations/{id}/link`,
`POST /confirmations/{id}/not-ours`, the `worklist`,
`link-confirmation`, and `not-ours` CLI verbs, and a
worklist section under Remittance confirmation in the
console. The supplier ledger is untouched by this round: it
reads the member records' confirmation overlays, and a
linked confirmation writes those overlays through the same
path as any other — the ledger simply sees the answer.

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
  open amount — approved face minus the applied-credit ledger
  minus the payments released against it — the same
  computation the packet and the runs read. The position never
  recomputes it differently.
- **The state of the money is a second split of the same
  total.** Each line is `OPEN_FREE`, `IN_RUN` (held by an open
  run), `PAID_PROPOSED` (released in full, awaiting the bank's
  confirmation), or `PART_PAID` — released in part, with the
  cumulative paid figure riding the line as a memo; it is the
  *remainder* that ages, at the invoice's due date, because
  the paid money is no longer owed. The state columns and the
  ageing columns both foot to the same open total, per
  supplier and per currency, and the report checks its own
  arithmetic.
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
  So are **fully paid** invoices — open 0.00 with released
  payments against them: they age nothing either, but the
  close should see that the money went out, and how much.
- **Surfaces.** `GET /position?as_of=YYYY-MM-DD`, the console's
  close section (supplier rows expand to the records under
  them), CLI `position <as-of> [--export]`, the demo's closing
  act, and a CSV export at `positions/{as_of}/ap-position-v1.csv`
  in the object store, with its SHA-256 returned beside the
  bytes in the run exports' idiom.

## The supplier ledger

Every view above follows the money along one axis. The page
an AP lead opens on a supplier call is the supplier's own —
the whole relationship, on one page, every figure tracing to
its record:

- **Identity is the master's, joined the intake's way.** A
  record is on a supplier's ledger because the pipeline joined
  it there: the extracted supplier name matched the master
  exactly, or the supplier of the purchase order it references
  resolved it. The ledger runs no second matching pass. A
  document that joined to nothing is **not** attached by
  resemblance and **not** dropped: the index lists it under
  **Unmatched**, with the name as the document wrote it — a
  supplier trading under three spellings shows up as a data
  problem, not as a third of the story quietly missing.
- **The whole money story per invoice.** Face, the credits
  applied against it (listed, so the figure traces), the open
  amount the packet and the runs read, the match outcome, the
  payment state, the bank's confirmation answer, and the
  ageing bucket the position gives it at the ledger's as-of.
  Credits ride with their application state and — while they
  wait — the reason they wait, in the same words the packet
  uses.
- **Statement breaks, while they are open.** A break is open
  until a controller has ACCEPTED its reconciliation;
  flagged-back breaks are the AP team taking them to the
  supplier, which is the opposite of settled. Both figures
  ride every break line.
- **Bank details from the master, only.** The ledger shows
  the master's approved account. A document that presented
  different details was held at intake with the guardrail's
  flag; that flag is the ledger's bank warning, with the
  records that raised it. No document's bank details are ever
  shown as the supplier's.
- **Totals that tie to the position.** The ledger is
  all-time — a relationship has no as-of — but its payable
  totals are taken in the position's own universe (every
  APPROVED invoice at its open amount), and an open figure
  does not depend on the as-of, only its ageing does. So the
  ledger's total per currency must equal the position's
  supplier row at the stated as-of, to the cent — and the
  ledger carries both figures and names the difference when
  there is one. There can be one honestly: the corrupted
  record the position excludes as a `NEGATIVE_OPEN` anomaly
  is still counted in the ledger's sum, and that difference
  breaks the ledger's tie by name (`POSITION_TIE_BREAK`)
  instead of being smoothed away.
- **Surfaces.** `GET /suppliers/index?as_of=` and
  `GET /suppliers/{supplier_id}/ledger?as_of=`, the console's
  ledger section (the index with its Unmatched count, each
  ledger a click away), CLI `suppliers <as-of>` and
  `ledger <supplier-id> <as-of>`, and the demo's closing act —
  Harborlight's ledger, the richest story in the world:
  credits, a confirmation, and a statement full of breaks.

## The duplicate that arrives second

The pipeline's own duplicate check owns the copy that arrives
while its twin is still in the case: same supplier, same
invoice number — an exception, back to the sender. The
dangerous duplicate is the one that arrives **after** the
original was approved, assembled into a payment run, and
released. It extracts cleanly and matches its PO; nothing
pending resembles it, because its twin is no longer pending.
It is paid.

The watch (`duplicates.py`) stands at intake, on the
completed extraction, and checks the arrival against **paid
history**:

- **The keys.** Same supplier — the intake's resolved
  identity, never a resemblance. Same invoice number,
  normalised the way credits and confirmations normalise
  (strip, upper-case) — the older pipeline check compares raw
  strings, so a resend read with different case or spacing
  slips it; the watch does not. And the arrival's amount
  equal, **to the cent** — no tolerance comes near this check;
  a duplicate judgement is an accusation, not a reading.
- **Two bases, named on the hold.** `FACE`: the arrival
  states the paid invoice's approved face. `NET`: it states
  the total the released runs actually instructed — the face
  minus the credits applied before release when one run paid
  the invoice whole, and the sum of the parts when it was
  paid in parts. A supplier who was credited and re-bills at
  the net figure is the classic second arrival, and the hold
  says which figure it matched. An arrival at any other
  amount is **not held**: the number rule surfaces it as a
  `DUPLICATE_INVOICE` exception, as before — a different
  amount is a different claim. A different number at the paid
  amount is a different bill and is not held either.
- **The hold.** A hit stops the record at
  `PAID_DUPLICATE_HOLD`, with the paid evidence on its face:
  the original record, the most recent run that released
  money for it, who released it and when, and the cumulative
  amount the exports instructed. The
  approver's gate refuses to decide it (**409**, the evidence
  named — the fraud-hold precedent), and a payment run refuses
  it by name. The watch list — `GET /duplicates/watch`, the
  console section, CLI `duplicate-watch` — shows every
  standing hold with its evidence and its age, and the
  supplier's ledger row and page carry it too.
- **Resolution is a controller's act, with a reason, two
  exits only.** `CONFIRMED_DUPLICATE`: the record is refused
  permanently, the evidence and the ruling as its refusal
  reason; nothing is deleted. `RELEASED_NOT_DUPLICATE`: the
  hold lifts and the record returns to **the state its own
  match earned** — awaiting the approver (and decidable,
  even though its submission's gate closed when it arrived),
  or its exception, standing in the open. Identity is the
  record, not the number: a second arrival of the same
  document is a new record and is held afresh, so a release
  cannot be laundered by resending.
- **What it never does.** No clawback, no recovery workflow,
  no touching the released run, its export, or the original
  payment — both are byte-identical across any hold and
  resolution, and the eval pack pins it. The original's own
  credit notes and remittance advices behave exactly as they
  did before the watch existed; a bank-change resend is a
  fraud case first and never the watch's.

## Partial payments

Everything above reads as if an invoice is paid once, whole.
Real AP does not work that way: a large invoice is paid 60%
now and 40% next cycle, against a disputed line, a retention,
a cash calendar. A run that can only take an invoice whole
forces the treasurer to choose between paying early and
paying late — so a run member may now be taken **at a stated
part** of its open amount, and every reader of the money
agrees on what that means:

- **The part is stated at assembly, exact to the cent.** The
  run's request names the part per record (`--amounts
  REC-…=600.00` on the CLI, `amounts` on the API). The line
  carries the part as its instructed amount — the line stays
  the truth a confirmation answers and an export instructs —
  beside the full open amount it was taken from. A part of
  zero or less is refused (`INVALID_PART_AMOUNT`); a part
  beyond the open amount is refused (`PART_EXCEEDS_OPEN`)
  with the requested part, the open remainder, and the
  already-released cumulative in the reason — that refusal
  *is* the cumulative-overcommit check, because the open
  amount already nets everything released before. An invoice
  still sits in at most **one open run** at a time: the
  invariant from the payment run holds; what changes is that
  across *sequential* runs it pays down.
- **Release writes a payments ledger on the record.** Each
  release appends one entry — run, amount, currency,
  treasurer, moment — and the record's open amount becomes
  what it always should have been: **approved face − credits
  applied − payments released**, computed in exactly one
  place (`credits.py`), the figure the packet, the runs, the
  position, and the ledger all read. The approved face and
  the approval never move.
- **The state says what is true.** A member released at its
  full open amount stands `PAID_PROPOSED`, exactly as before.
  A member released at a part stands **`PART_PAID`**, with
  its ledger on the record: proposed for part, owed for the
  rest, and no surface calls it paid. When a later run's
  release brings the cumulative to the full open amount, it
  stands `PAID_PROPOSED` and reads exactly as a once-paid
  invoice does. Returning a later run restores `PART_PAID` —
  never unpaid: the ledger decides the resting state, not
  the run that just left.
- **The readers agree.** The packet proposes the remainder,
  with the released parts on its line. The position ages the
  remainder in the `PART_PAID` state column, carries the paid
  figure as a memo, and discloses the invoice as fully paid
  when the paydown completes; its open total still foots and
  still ties to the packet. The supplier ledger shows each
  part beside the credits on the invoice's line, and its
  per-supplier open still ties to the position.
- **The disciplines follow the parts.** A remittance advice
  answers a run's *line*: an advice for the part confirms
  that part exactly; an advice for the full face against a
  part run is an over-payment break on that run and confirms
  nothing about the remainder, which stays open for a later
  run. A credit note can land on a part-paid invoice's
  remainder — `TARGET_PAID_PROPOSED` is terminal only for an
  invoice paid in full — and an over-credit is judged
  against the remainder's figures. And the duplicate watch
  holds a resend of a part-paid invoice on the **cumulative**
  instructed figure: the `NET` basis is what the released
  runs instructed in total, across every part, named by the
  most recent paying run. A resend at the *remainder* is not
  held — the remainder was never paid; the number rule
  surfaces it as a different claim, as before.

## PO coverage

The match is per-invoice arithmetic: this invoice, against this order,
within these tolerances. It cannot see accumulation — three honest
invoices, each matching cleanly, can together bill one purchase order
past its total, and no per-invoice check anywhere says so. Coverage is
the cumulative view the match does not have: for every order, what has
been billed against it in total (`GET /po-coverage`, `po-coverage` on
the CLI, the console's PO coverage section):

- **Only approved money bills an order.** A record counts once the
  approver has approved it; awaiting, exception, rejected, refused, and
  fraud-held money charges the order nothing, however plausible the
  document. The billed figure per invoice is its **billable amount** —
  the approved face minus the credits applied against it. A credit
  reduces the bill itself, so it reduces billed; a payment only settles
  a bill that stands, so a part-paid invoice bills in full, exactly as
  charged.
- **The total is the order register's, and the row says so.** Totals
  come from the purchase-order register (the customer's ERP mirror in a
  real deployment), named `ORDER_REGISTER` on the row. An order an
  approved invoice names that the register does not carry gets **no
  verdict**: it reads **UNMEASURED**, its billed sum listed, its total
  shown as not on file. Coverage never invents a total from the
  invoices billing against the order — that would grade the leak
  against the leak.
- **Verdicts, thresholds printed on the view.** **WITHIN** below the
  watch threshold, **WATCH** at or above it (80% by default —
  `CoveragePolicy` in `policy.py`, beside the match tolerances and the
  ageing policy), **EXCEEDED** when billed passes the total. Exceeded
  is a comparison to the cent, not a percentage judgement: an order
  billed to exactly 100% is fully billed, and reads WATCH. Rows rank
  EXCEEDED first, then WATCH, then WITHIN, UNMEASURED last — named,
  never hidden. Each row lists its contributing invoices in billing
  order with running cumulatives, so the line that crosses a threshold
  is the line you see crossing it. An invoice billing in a currency
  other than the order's is never silently converted: it is listed on
  the row as excluded, with the reason.
- **Coverage informs; it never blocks, and never rewrites.** An
  exceeded order is not fraud and not a match failure — change orders
  and blanket orders over-bill legitimately, and the register's total
  is reference data that can lag the paperwork, so the gate stays the
  approver's. What coverage does is make sure the fact is in front of
  them: the record's own coverage fact (`GET
  /records/{id}/po-coverage`, and the console's record detail) counts
  the invoice in front of them in — *"Order PO-6010 stands billed at
  200.0% (1200.00 of 600.00 USD) including this invoice — EXCEEDED"* —
  and a decision taken while the order stands EXCEEDED is recorded on
  the record's audit trail with that fact quoted, so the approver's
  context survives the screen they read. Every contributing invoice
  keeps the match outcome it earned: the match is one fact, coverage a
  second fact on a second axis.
- **The other readers, honestly scoped.** The supplier ledger's
  invoice rows name each invoice's order and carry its coverage state
  — display only; no ledger total, tie, or figure reads them. The aged
  position is **untouched**: the position asks what is still *owed*,
  coverage asks what has been *billed*, and the two ledgers are never
  merged.

The seeded world convicts itself here: INV-1010 and INV-3001 each bill
PO-6010's whole 600.00 — every per-invoice check passed both, and
together the order stands at 200%. INV-1002 bills PO-6002 1% past its
total, inside its line tolerances. The demo's final act prints the
ranked view over exactly that world.

## Supplier bank history

The fraud screen's whole judgement used to be one sentence: *the
invoice's bank differs from the master's*. But masters change
legitimately, and a screen that reads only today's master makes two
errors — it flags an invoice quoting the bank the supplier genuinely
used last month, and it waves through an invoice quoting a bank that
only becomes the master's next week. So the master has a past now:
each supplier's bank details are a **series of versions** (the seeded
details are version 1), and the screen reads the series **as of the
invoice's own date**:

- **The four outcomes are distinct and named.** An invoice matching
  the version in force on its date is clean, however the master has
  moved since. One matching **no version that ever existed** is held
  `BANK_DETAILS_CHANGED` — the original judgement, unchanged. One
  matching a version that takes effect only *after* its date is held
  `BANK_DETAILS_NOT_YET_EFFECTIVE` — it quotes the supplier's future
  bank early. One matching a version whose window had already closed
  is held `BANK_DETAILS_SUPERSEDED` — it quotes the retired bank late.
  All three holds land on `FRAUD_REVIEW` exactly as before; the gate
  still cannot decide a fraud hold. Only the reason gained the time
  dimension, and the audit can tell the three apart.
- **Recording is the governed act.** A legitimate change is recorded
  on the master (`POST /suppliers/{id}/bank-versions`,
  `record-bank-version` on the CLI, the ledger's record form on the
  console) with the fields every governed act in this system carries:
  a named actor, and the provenance — what the change was recorded
  *from* (the supplier's change notice, a verified callback: the
  evidence a human checked out of band). No actor, no source, no
  effective date → refused by name. There is no confirmation state:
  recording itself is the act, as it is for credit application and
  hold resolution here, and the as-of screen is the protection — a
  version recorded today blesses no invoice dated before its
  effective date, however it got in.
- **History is append-only, and the past never moves.** Effective
  dates move forward only — a new version takes effect after every
  version on file, so the version in force on any date is
  unambiguous, and a correction is a new version, never a rewrite.
  Prior versions stand byte-identical under any later recording, and
  **a decided record is never re-judged**: the screen's verdict is
  stamped on the record at intake, and recording a version changes no
  record by a byte. The surfaces that derive from the master on read
  — the ledger's bank line, the payment export — read the version in
  force at *their* date: the ledger shows the version in force at its
  as-of (with its number and the count on file), and a released run
  pays the version in force on the day of release, because the export
  instructs money moving now. A supplier's whole timeline — details,
  effective dates, provenance — is one read (`GET
  /suppliers/{id}/bank-history`, `bank-history` on the CLI, the
  timeline on the console ledger).
- **Storage follows the master's own home.** Versions are reference
  data, like the suppliers, orders, and receipts they version: held
  in the reference store, seeded per deployment, written nowhere in
  Postgres — the migration ladder covers the records the pipeline
  writes, and the master is not one of them. In a customer
  deployment the same seam loads the ERP's bank-change history.

The seeded world plants no legitimate change — its only bank change
is the fraud pattern, which is still caught as `BANK_DETAILS_CHANGED`
(the as-of screen against a one-version master *is* the original
screen). The demo's closing act stages the legitimate case: Cedarline's
recorded change, one September invoice quoting the old bank and clean,
one October invoice quoting the same old bank and held SUPERSEDED.

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
- **Unmatched-advice worklist** — over four held advices
  against one released run: a reference one edit from a line
  at its exact amount must top the candidates under the
  reference rule; a lump equal to two lines' sum must draw
  the combined candidate; an amount 3.00 under a named line
  must be suggested near and labelled near; an advice for
  another company's money must draw nothing; the suggestions
  must confirm nothing until a controller links; the typo
  and lump links must settle their lines exactly; the near
  link must classify its line short by exactly the
  difference; and the foreign advice must leave the pile
  under a not-ours mark with its reason.
- **AP position** — bucket placement must be exact including
  the boundary days; an undated invoice must be named
  `UNDATED`; the aged total must tie to the packet's open
  total; the state split must foot to the same figure; and
  a corrupted record whose ledger exceeds its face must
  break the tie by name (`POSITION_TIE_BREAK`).
- **Supplier ledger** — Harborlight's totals (face, credits,
  paid, open) must tie to its position, with its released
  invoices disclosed on the position's fully-paid list, and
  every supplier's ledger must tie; a stray invoice from a
  spelling the master has never seen must land under
  Unmatched, on no supplier's ledger; the credit,
  confirmation, and open-break story must render whole; and
  the bank warning must be present exactly where a document
  disagreed with the master.
- **Paid-duplicate watch** — a released invoice re-submitted
  verbatim must be held on its paid evidence, the gate's
  refusal must name that evidence, and a controller's release
  must make the record decidable at its long-closed gate; in
  a planted universe, a re-bill at the net figure must be
  held on the `NET` basis, a different amount must surface
  under the number rule unheld, a different number at the
  paid amount must not be held, resolutions without actor or
  reason must be refused, a second arrival must re-hold after
  a release, and the paid record and its run must be
  byte-identical throughout.
- **Partial payments** — an invoice taken at 1200.00 of a
  2000.00 face must stand `PART_PAID` with the part on its
  payments ledger; a further 900.00 against the 800.00
  remainder must be refused as an overcommit with the figures
  named; a 200.00 credit must land on the remainder and a
  900.00 credit must be judged against the remainder, not the
  face; the position must age only the remainder with the
  paid figure beside it; an advice for the part must confirm
  the part, and one for the full face must read as an
  over-payment on that run; the second run must complete the
  paydown (1200 + 600 = 2000 − 200) with each export
  instructing only its own part; and resends must be held on
  the cumulative instructed figure — 1200.00 after the first
  run, 1800.00 after both.
- **PO coverage** — planted invoices accumulate against one
  order through WITHIN, WATCH, and EXCEEDED with hand-derived
  cumulatives; a credit applied to a member reduces billed
  back to exactly the total, a released part payment against
  a member reduces it not at all; an unapproved invoice and
  one naming no order contribute nothing, and an order no
  register carries reads UNMEASURED, billed sum listed,
  ranked last; and on the main service the world's own pair
  stands at 200% of PO-6010, the crossing approval unblocked,
  its match untouched, the coverage fact on its record.
- **Bank history** — a recorded Cedarline change must judge
  five invoices by their own dates: old bank before the
  change clean, old bank after it held `SUPERSEDED`, new bank
  before it held `NOT_YET_EFFECTIVE`, a never-held account
  held `CHANGED`, new bank after it clean; recordings without
  an actor, a source, or a date, and one rewinding the
  series, must be refused by name; earlier versions and a
  decided record must stand byte-identical under a later
  recording; the ledger must show the version in force at
  its as-of and a released run must pay the version in force
  on release day.

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
  worklist.py      the unmatched pile: candidates in code, links by a human
  position.py      the AP position at close: ageing + the tie-out, in code
  coverage.py      PO coverage: cumulative billed per order, against its total
  bankmaster.py    the master's bank versions: effective-dated, provenance-stamped
  ledger.py        the supplier ledger: one supplier, the whole relationship
  duplicates.py    the paid-duplicate watch: arrivals vs paid history, in code
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

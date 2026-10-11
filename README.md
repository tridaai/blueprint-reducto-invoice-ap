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

The demo runs the whole synthetic AP world end to end — sixteen
submissions covering clean matches, a price drift inside
tolerance, a short shipment, a duplicate, a credit note, a
bank-detail fraud attempt, a statement that gets refused — and
prints the payment packet a named approver's decisions produce.
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
- **The packet** — a read model over approved records: proposed
  payments by supplier and currency, credit notes netted as
  negative lines. Generating it moves no money; this system has
  no code path that could.

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
  guardrails.py    bank-detail, payable, and injection screens
  policy.py        every tolerance, in one module
  graph.py         the LangGraph pipeline and its gate
  service.py       submissions, gate discipline, the packet
  store.py / db.py records in Postgres (or the in-memory double)
  datasets.py      the synthetic world — gold by construction
migrations/        numbered SQL, applied at startup
evals/             the golden pack and its runner
docs/              architecture.md — the deployment, in depth
```

## License

Apache 2.0 — see [LICENSE](LICENSE).

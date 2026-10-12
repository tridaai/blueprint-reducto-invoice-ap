"""Command line: submit a document, list records, print the packet.

The CLI drives the same service as the API, wired from the
environment — so `DOCUMENT_BACKEND=reducto` works here exactly
as it does over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_dotenv
from .datasets import build_world
from .errors import ApError
from .service import ApService
from .wiring import build_service


def _seed_demo(service: ApService) -> None:
    world = build_world()
    for submission in world.submissions:
        service.submit(
            submission.submission_id, submission.filename,
            submission.text.encode("utf-8"), "text/plain",
        )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="reducto-ap-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="process every bundled synthetic submission")
    p_submit = sub.add_parser("submit", help="submit one text-layer document file")
    p_submit.add_argument("path")
    p_submit.add_argument("--id", default=None)
    sub.add_parser("records", help="list all records as JSON")
    sub.add_parser("credits", help="list credit notes with their application state and unapplied reasons")
    p_apply = sub.add_parser("apply-credit", help="a controller applies one credit note against the invoice it names")
    p_apply.add_argument("record_id")
    p_apply.add_argument("--actor", required=True)
    p_watch = sub.add_parser("duplicate-watch", help="list the records held by the paid-duplicate watch, with their paid evidence and hold ages")
    p_watch.add_argument("--as-of", default=None,
                         help="age the holds as of this date, YYYY-MM-DD (default: today)")
    p_resolve = sub.add_parser("resolve-duplicate", help="a controller resolves one paid-duplicate hold: confirm it, or release it as not a duplicate")
    p_resolve.add_argument("record_id")
    p_resolve.add_argument("action", choices=["CONFIRMED_DUPLICATE", "RELEASED_NOT_DUPLICATE"])
    p_resolve.add_argument("--actor", required=True)
    p_resolve.add_argument("--reason", required=True)
    sub.add_parser("packet", help="print the proposed payment packet as JSON")
    p_run = sub.add_parser("create-run", help="assemble a payment run and hold its members")
    p_run.add_argument("--value-date", required=True)
    p_run.add_argument("--records", default=None,
                       help="comma-separated record ids (default: every approved payable record)")
    p_run.add_argument("--currencies", default=None,
                       help="comma-separated currency scope (default: all)")
    p_run.add_argument("--amounts", default=None,
                       help="comma-separated record_id=amount part payments "
                            "(default: every member at its whole open amount)")
    sub.add_parser("payment-runs", help="list payment runs as JSON")
    p_decide_run = sub.add_parser("decide-run", help="the treasurer's word on a payment run")
    p_decide_run.add_argument("run_id")
    p_decide_run.add_argument("action", choices=["release", "return"])
    p_decide_run.add_argument("--actor", required=True)
    p_decide_run.add_argument("--reason", default="")
    p_export = sub.add_parser("export-run", help="write a released run's bank-instruction CSV")
    p_export.add_argument("run_id")
    p_export.add_argument("--out", default=None, help="output path (default: stdout)")
    p_recon = sub.add_parser("reconcile", help="reconcile one statement record against AP's records")
    p_recon.add_argument("record_id")
    sub.add_parser("reconciliations", help="list statement reconciliations as JSON")
    p_confirm = sub.add_parser("confirm", help="reconcile one remittance advice against the payment run it answers")
    p_confirm.add_argument("record_id")
    sub.add_parser("confirmations", help="list remittance confirmations as JSON")
    p_position = sub.add_parser("position", help="print the AP position aged as of a date (JSON), tied to the packet")
    p_position.add_argument("as_of", help="the close date, YYYY-MM-DD — ageing is explicit, never a hidden today")
    p_position.add_argument("--export", action="store_true",
                            help="also write the position CSV to the object store and print its key + SHA-256")
    sub.add_parser("po-coverage", help="print PO coverage (JSON): every order's cumulative approved billing against its register total, ranked worst first")
    p_suppliers = sub.add_parser("suppliers", help="print the supplier ledger index (JSON): every master supplier with its counts, plus the unmatched documents")
    p_suppliers.add_argument("as_of", help="the close date, YYYY-MM-DD — the date the tied position is aged at")
    p_ledger = sub.add_parser("ledger", help="print one supplier's ledger (JSON): the whole relationship, totals tied to the position")
    p_ledger.add_argument("supplier_id", help="the supplier's id on the master (e.g. SUP-08)")
    p_ledger.add_argument("as_of", help="the close date, YYYY-MM-DD — the date the tied position is aged at")
    p_bank_history = sub.add_parser("bank-history", help="print one supplier's bank timeline (JSON): every master version with its provenance, and the version in force at the as-of")
    p_bank_history.add_argument("supplier_id", help="the supplier's id on the master (e.g. SUP-03)")
    p_bank_history.add_argument("as_of", help="the date the timeline is read at, YYYY-MM-DD")
    p_bank_version = sub.add_parser("record-bank-version", help="record a legitimate bank change on the supplier master: a new version, effective from a date, with its provenance")
    p_bank_version.add_argument("supplier_id", help="the supplier's id on the master (e.g. SUP-03)")
    p_bank_version.add_argument("--account", required=True, help="the new bank account, as the master records it")
    p_bank_version.add_argument("--bank-name", default="", help="the new bank's name")
    p_bank_version.add_argument("--effective-from", required=True, help="the date the new details take effect, YYYY-MM-DD")
    p_bank_version.add_argument("--actor", required=True, help="who recorded the version")
    p_bank_version.add_argument("--source", required=True, help="what it was recorded from — the change notice, the verified callback: the provenance")
    args = parser.parse_args(argv)

    service = build_service()
    try:
        if args.command == "seed-demo":
            _seed_demo(service)
            print(f"seeded: {len(service.list_records())} records")
        elif args.command == "submit":
            path = Path(args.path)
            records = service.submit(
                args.id or path.stem.upper(), path.name,
                path.read_bytes(), "text/plain",
            )
            print(json.dumps([r.model_dump() for r in records], indent=2))
        elif args.command == "records":
            print(json.dumps([r.model_dump() for r in service.list_records()], indent=2))
        elif args.command == "credits":
            print(json.dumps(service.list_credits(), indent=2))
        elif args.command == "apply-credit":
            print(service.apply_credit(args.record_id, args.actor).model_dump_json(indent=2))
        elif args.command == "duplicate-watch":
            from datetime import datetime, timezone

            as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
            print(json.dumps(service.list_duplicate_watch(as_of), indent=2))
        elif args.command == "resolve-duplicate":
            record = service.resolve_duplicate_hold(
                args.record_id, args.action, args.actor, args.reason
            )
            print(record.model_dump_json(indent=2))
        elif args.command == "packet":
            print(service.payment_packet().model_dump_json(indent=2))
        elif args.command == "create-run":
            record_ids = (
                [r.strip() for r in args.records.split(",") if r.strip()]
                if args.records else None
            )
            currencies = (
                [c.strip() for c in args.currencies.split(",") if c.strip()]
                if args.currencies else None
            )
            amounts = None
            if args.amounts:
                amounts = {}
                for pair in args.amounts.split(","):
                    record_id, _, value = pair.partition("=")
                    amounts[record_id.strip()] = float(value.strip())
            run = service.create_payment_run(
                args.value_date, record_ids=record_ids,
                currencies=currencies, amounts=amounts,
            )
            print(run.model_dump_json(indent=2))
        elif args.command == "payment-runs":
            print(json.dumps(
                [r.model_dump() for r in service.list_payment_runs()], indent=2
            ))
        elif args.command == "decide-run":
            run = service.decide_payment_run(
                args.run_id, args.action, args.actor, args.reason
            )
            print(run.model_dump_json(indent=2))
        elif args.command == "export-run":
            data = service.payment_run_export(args.run_id)
            if args.out:
                Path(args.out).write_bytes(data)
                print(f"wrote {len(data)} bytes to {args.out}")
            else:
                sys.stdout.write(data.decode("utf-8"))
        elif args.command == "reconcile":
            print(service.reconcile(args.record_id).model_dump_json(indent=2))
        elif args.command == "reconciliations":
            print(json.dumps(
                [r.model_dump() for r in service.list_reconciliations()], indent=2
            ))
        elif args.command == "confirm":
            print(service.confirm_remittance(args.record_id).model_dump_json(indent=2))
        elif args.command == "confirmations":
            print(json.dumps(
                [c.model_dump() for c in service.list_confirmations()], indent=2
            ))
        elif args.command == "position":
            if args.export:
                key, digest, _ = service.position_export(args.as_of)
                print(f"export: {key}  sha256 {digest}")
            print(service.ap_position(args.as_of).model_dump_json(indent=2))
        elif args.command == "po-coverage":
            print(service.po_coverage().model_dump_json(indent=2))
        elif args.command == "suppliers":
            print(service.supplier_index(args.as_of).model_dump_json(indent=2))
        elif args.command == "ledger":
            print(service.supplier_ledger(
                args.supplier_id, args.as_of).model_dump_json(indent=2))
        elif args.command == "bank-history":
            print(service.bank_history(
                args.supplier_id, args.as_of).model_dump_json(indent=2))
        elif args.command == "record-bank-version":
            version = service.record_bank_version(
                args.supplier_id,
                bank_account=args.account,
                bank_name=args.bank_name,
                effective_from=args.effective_from,
                actor=args.actor,
                source=args.source,
            )
            print(version.model_dump_json(indent=2))
    except ApError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

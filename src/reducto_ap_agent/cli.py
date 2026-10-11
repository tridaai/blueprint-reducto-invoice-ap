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
    sub.add_parser("packet", help="print the proposed payment packet as JSON")
    p_run = sub.add_parser("create-run", help="assemble a payment run and hold its members")
    p_run.add_argument("--value-date", required=True)
    p_run.add_argument("--records", default=None,
                       help="comma-separated record ids (default: every approved payable record)")
    p_run.add_argument("--currencies", default=None,
                       help="comma-separated currency scope (default: all)")
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
            run = service.create_payment_run(
                args.value_date, record_ids=record_ids, currencies=currencies
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
    except ApError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

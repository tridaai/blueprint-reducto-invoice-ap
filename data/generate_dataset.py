"""Regenerate the packaged dataset snapshot from the generator.

The dataset itself is built in code
(``reducto_ap_agent.datasets.build_world``) so gold labels are
true by construction; this script snapshots that world to JSON
for inspection, diffs, and downstream tooling:

    uv run python data/generate_dataset.py

writes ``src/reducto_ap_agent/data/dataset.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reducto_ap_agent.datasets import build_world  # noqa: E402


def main() -> None:
    world = build_world()
    snapshot = {
        "suppliers": [s.model_dump() for s in world.suppliers],
        "purchase_orders": [p.model_dump() for p in world.purchase_orders],
        "receipts": [r.model_dump() for r in world.receipts],
        "submissions": [
            {
                "submission_id": s.submission_id,
                "filename": s.filename,
                "text": s.text,
                "expected": [e.__dict__ for e in s.expected],
            }
            for s in world.submissions
        ],
    }
    out = (
        Path(__file__).resolve().parents[1]
        / "src" / "reducto_ap_agent" / "data" / "dataset.json"
    )
    out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"wrote {out} — {len(world.submissions)} submissions, "
          f"{len(world.suppliers)} suppliers, {len(world.purchase_orders)} POs")


if __name__ == "__main__":
    main()

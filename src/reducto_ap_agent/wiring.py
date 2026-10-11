"""Wiring: build the service from the environment, honestly.

Selection rules, in order:

- ``DOCUMENT_BACKEND=reducto`` → the Reducto adapter, at the
  base URL the deployment profile resolves. A missing API key
  fails startup with :class:`DocumentNotConfigured` — the service
  never silently serves offline results under Reducto's name.
- ``DOCUMENT_BACKEND=offline`` (or unset with no key present) →
  the labelled offline adapter over the text-layer documents.
- The engine service (``engine_api``) uses the offline adapter
  by definition: it is the labelled stand-in that answers the
  Reducto API shape at the VPC profile's internal endpoint.

Reference data (suppliers, POs, receipts) is seeded from the
synthetic world — in a customer deployment this seam is where
the ERP mirrors load instead.
"""

from __future__ import annotations

from .checkpoints import default_checkpointer
from .config import document_backend, env_str
from .datasets import build_world
from .errors import DocumentNotConfigured
from .graph import ApPipeline
from .object_store import default_object_store
from .offline_docs import OfflineDocumentAdapter
from .profiles import resolve_profile
from .reducto_docs import ReductoDocumentAdapter
from .service import ApService
from .store import default_store


def build_document_port():
    backend = document_backend()
    if backend == "reducto":
        profile = resolve_profile()
        return ReductoDocumentAdapter(
            api_key=env_str("REDUCTO_API_KEY"),
            base_url=profile.reducto_base_url,
            timeout_seconds=float(env_str("REDUCTO_TIMEOUT_SECONDS", "120") or "120"),
        )
    if backend == "offline":
        return OfflineDocumentAdapter()
    raise DocumentNotConfigured(
        f"unknown DOCUMENT_BACKEND {backend!r} — expected 'reducto' or 'offline'"
    )


def build_service() -> ApService:
    store = default_store()
    world = build_world()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=build_document_port(),
        store=store,
        objects=default_object_store(),
        checkpointer=default_checkpointer(),
    )
    return ApService(pipeline=pipeline, store=store)

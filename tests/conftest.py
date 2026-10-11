from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.datasets import build_world
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore


@pytest.fixture()
def world():
    return build_world()


@pytest.fixture()
def service(world) -> ApService:
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(),
        store=store,
        objects=InMemoryObjectStore(),
        checkpointer=MemorySaver(),
    )
    return ApService(pipeline=pipeline, store=store)


@pytest.fixture()
def seeded(service, world) -> ApService:
    for submission in world.submissions:
        service.submit(
            submission.submission_id, submission.filename,
            submission.text.encode("utf-8"), "text/plain",
        )
    return service

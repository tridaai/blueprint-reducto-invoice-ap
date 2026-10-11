"""LangGraph checkpointing: Postgres in production, memory in tests.

The pipeline pauses at the approver gate — possibly for days —
so its state cannot live in the API process. With
``DATABASE_URL`` set, checkpoints go to the same PostgreSQL as
the record store via ``langgraph-checkpoint-postgres``; without
one, an in-memory saver serves tests and smoke runs, and
``/readiness`` names the difference.
"""

from __future__ import annotations

from .db import database_url, ensure_migrated

_SAVER = None


def default_checkpointer():
    global _SAVER
    if _SAVER is not None:
        return _SAVER
    url = database_url()
    if url:
        ensure_migrated(url)
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(conninfo=url, max_size=4, kwargs={"autocommit": True})
        saver = PostgresSaver(pool)
        saver.setup()
        _SAVER = saver
    else:
        from langgraph.checkpoint.memory import MemorySaver

        _SAVER = MemorySaver()
    return _SAVER


def checkpointer_name() -> str:
    return "postgres" if database_url() else "memory"

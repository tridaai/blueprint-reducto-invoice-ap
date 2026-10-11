"""PostgreSQL: connections and the numbered-migration runner.

PostgreSQL is the system of record for a production-shaped run:
document records and the LangGraph checkpointer live in the one
database ``DATABASE_URL`` points at. Schema is owned by numbered
SQL files in the repo-root ``migrations/`` directory, applied in
order by :func:`run_migrations` and recorded in a
``schema_migrations`` table, so a database is never half-migrated
silently. The runner executes at startup — the API lifespan calls
it, and the Postgres-backed collaborators call
:func:`ensure_migrated` (a per-process, per-URL once-guard) when
constructed, so the CLI and demo take the same path.

Without ``DATABASE_URL`` there is no database: the store falls
back to an in-memory test double. That mode exists for tests and
offline smoke runs — nothing persists, and the docs say so.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .config import env_str, load_dotenv

_MIGRATED_URLS: set[str] = set()
_MIGRATE_LOCK = threading.Lock()


def database_url() -> str | None:
    """The configured ``DATABASE_URL``, or None (test-double mode)."""
    load_dotenv()
    return env_str("DATABASE_URL")


def migrations_dir() -> Path:
    configured = env_str("MIGRATIONS_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "migrations"


def connect(url: str | None = None, *, autocommit: bool = False):
    import psycopg
    from psycopg.rows import dict_row

    target = url or database_url()
    if not target:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(target, autocommit=autocommit, row_factory=dict_row)


def migration_files() -> list[Path]:
    return sorted(migrations_dir().glob("[0-9]*.sql"))


def run_migrations(url: str | None = None) -> list[str]:
    """Apply pending migrations in order; return the names applied."""
    target = url or database_url()
    if not target:
        return []
    applied: list[str] = []
    with connect(target) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {
            row["name"]
            for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for path in migration_files():
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
        conn.commit()
    return applied


def ensure_migrated(url: str | None = None) -> None:
    target = url or database_url()
    if not target:
        return
    with _MIGRATE_LOCK:
        if target in _MIGRATED_URLS:
            return
        run_migrations(target)
        _MIGRATED_URLS.add(target)

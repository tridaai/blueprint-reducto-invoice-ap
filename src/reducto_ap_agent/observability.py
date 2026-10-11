"""Structured JSON logs with request IDs.

Every log line the API emits is one JSON object on stdout, carrying
a ``request_id`` that is also returned as the ``X-Request-ID``
header, so a conversation with the customer's platform team can
name the exact request that touched a document. The ID comes from
the caller's header when present and is generated otherwise. The
CLI and demo keep human-readable output; this module serves the
API surface.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_ctx: ContextVar[str | None] = ContextVar(
    "reducto_ap_request_id", default=None
)


def new_request_id() -> str:
    return uuid.uuid4().hex


class JsonLogFormatter(logging.Formatter):
    _EXTRA_KEYS = ("method", "path", "status", "duration_ms", "event")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        for key in self._EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

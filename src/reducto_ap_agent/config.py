"""Runtime configuration: environment variables + an optional ``.env``.

All provider and deployment configuration comes from the
environment — nothing is hardcoded beyond the defaults documented
in ``.env.example``. At startup the entry points call
:func:`load_dotenv`, which reads a ``.env`` in the working
directory or the repo root when one exists; a variable already set
in the real environment always wins.

Two knobs shape the deployment more than any other:

- ``DEPLOYMENT_PROFILE`` — ``cloud`` (Reducto's hosted API) or
  ``vpc`` (the document engine reached at an internal endpoint
  inside the customer's boundary). The profile decides which
  endpoint the Reducto adapter talks to by default; see
  :mod:`reducto_ap_agent.profiles`.
- ``DOCUMENT_BACKEND`` — ``reducto`` (the real engine) or
  ``offline`` (the labelled deterministic fallback used by tests,
  evals, and no-key smoke runs).
"""

from __future__ import annotations

import os
from pathlib import Path


def _candidate_paths() -> list[Path]:
    return [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",  # repository root
    ]


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, _, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return (key, value) if key else None


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load ``.env`` values without overriding the real environment."""
    candidates = [path] if path is not None else _candidate_paths()
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def deployment_profile() -> str:
    """``cloud`` or ``vpc`` — where the document engine lives."""
    return (env_str("DEPLOYMENT_PROFILE", "cloud") or "cloud").lower()


def document_backend() -> str:
    """Which DocumentPort adapter serves: ``reducto`` (the target
    deployment) or ``offline``. Code defaults to offline when no
    Reducto key is present — see :mod:`reducto_ap_agent.wiring`."""
    return (env_str("DOCUMENT_BACKEND", "offline") or "offline").lower()

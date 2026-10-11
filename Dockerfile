# Trida AI Blueprint — Reducto Invoice AP, deployed in-VPC
#
# Multi-stage: the builder resolves the locked environment with uv;
# the runtime stage carries only the venv, the package, the packaged
# dataset, and the migrations — no build tooling, no test suite.
# The process runs as a non-root user and keeps NO local state: with
# DATABASE_URL / S3_* set (the compose stacks set them) records and
# graph checkpoints live in PostgreSQL and document bytes in object
# storage. Without them it serves on in-memory test doubles, which
# the /readiness payload names.
#
# Build:  docker build -t reducto-invoice-ap .
# Run:    docker run --rm -p 8000:8000 \
#           -e DOCUMENT_BACKEND=reducto -e REDUCTO_API_KEY=... \
#           reducto-invoice-ap
#
# DOCUMENT_BACKEND defaults to the labelled offline adapter when
# no backend is configured; selecting reducto without a key fails
# startup honestly rather than serving local results as Reducto's.
# The same image runs the VPC demo stack's internal engine service
# (command override: reducto-ap-engine) — see docker-compose.vpc.yml.

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml README.md uv.lock constraints.txt ./
COPY src ./src
# --frozen installs exactly uv.lock; --no-dev keeps the test
# toolchain (pytest & friends) out of the production image.
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY data ./data
COPY migrations ./migrations

ENV PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

# Liveness only: /health answers without touching dependencies.
# (Readiness — profile, backend, store — is /readiness, for the
# orchestrator's readiness probe.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "reducto_ap_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]

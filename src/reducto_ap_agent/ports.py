"""Ports: the seams a deployment is allowed to vary.

Two ports carry the whole blueprint:

- :class:`DocumentPort` — the document engine. Reducto in a real
  deployment (cloud API, or the same API served from an internal
  endpoint in the VPC profile); the offline text-layer adapter in
  tests, evals, and no-key runs. The pipeline cannot tell which
  one it is talking to, and the contract tests hold both to the
  same semantics.
- :class:`ObjectStorePort` — where document bytes live: S3 (or
  any S3-compatible store) in production, an in-memory double in
  tests.

Port methods speak the schemas in :mod:`.schemas` — never vendor
types. A Reducto job id or bounding box crosses the seam only as
record data, never as a type the engine has to know about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .schemas import ExtractedDocument


@dataclass
class UploadedDocument:
    """A document the engine has accepted, in engine terms."""

    engine_ref: str  # reducto://… URL, or the offline adapter's ref
    filename: str
    byte_size: int


@dataclass
class SubDocument:
    """One piece of an uploaded bundle, after a split."""

    engine_ref: str
    page_start: int
    page_end: int
    label: str = ""


@dataclass
class Classification:
    doc_class: str
    confidence: float | None = None


@runtime_checkable
class DocumentPort(Protocol):
    """Classify → split → extract, the document engine's surface."""

    name: str

    def upload(self, filename: str, data: bytes, content_type: str) -> UploadedDocument:
        ...

    def classify(self, doc: UploadedDocument | SubDocument) -> Classification:
        ...

    def split(self, doc: UploadedDocument) -> list[SubDocument]:
        ...

    def extract(self, doc: UploadedDocument | SubDocument) -> ExtractedDocument:
        ...


@runtime_checkable
class ObjectStorePort(Protocol):
    """Document bytes, by key. ``put`` returns the key."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        ...

    def get(self, key: str) -> bytes:
        ...

    def exists(self, key: str) -> bool:
        ...


@dataclass
class PortHealth:
    ok: bool
    detail: str = ""
    checks: dict[str, str] = field(default_factory=dict)

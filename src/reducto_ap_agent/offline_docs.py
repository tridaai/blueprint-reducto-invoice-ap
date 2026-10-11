"""The offline document adapter: labelled, deterministic, local.

Implements the same :class:`~.ports.DocumentPort` semantics as
the Reducto adapter over the synthetic documents' **text layer**
— the machine-readable text a PDF carries under its rendering.
A document is a plain-text bundle whose sections are marked
``[DOCUMENT: <TYPE>]``; fields are ``Key: value`` lines; line
items are pipe-separated rows. The adapter splits on the section
markers, classifies from them, and parses fields deterministically.

What this adapter is FOR: tests, evals, demos, and — in the VPC
profile's demo stack — standing behind the internal endpoint in
place of a licensed self-hosted engine. What it is NOT: an OCR
engine. It reads a clean text layer and says so; its citations
are text-layer citations (line-derived boxes), labelled
``source="text_layer"`` wherever they surface. Selecting it is
always explicit — production wiring leads with Reducto.
"""

from __future__ import annotations

import re

from .errors import DocumentBackendUnavailable
from .ports import Classification, SubDocument, UploadedDocument
from .schemas import Citation, ExtractedDocument, ExtractedField, LineItem

_SECTION_RE = re.compile(r"\[DOCUMENT:\s*([A-Z ]+)\]\s*$", re.MULTILINE)
_PAGE_RE = re.compile(r"^=== PAGE (\d+) ===\s*$", re.MULTILINE)

_CLASS_BY_MARKER = {
    "INVOICE": "invoice",
    "CREDIT NOTE": "credit_note",
    "REMITTANCE ADVICE": "remittance_advice",
    "DELIVERY NOTE": "delivery_note",
    "PACKING SLIP": "packing_slip",
    "STATEMENT": "statement",
    "LETTER": "letter",
}

_FIELD_BY_LABEL = {
    "Supplier": "supplier_name",
    "Invoice Number": "invoice_number",
    "Credit Note Number": "invoice_number",
    "Invoice Date": "invoice_date",
    "Issue Date": "invoice_date",
    "Due Date": "due_date",
    "PO Reference": "po_reference",
    "Currency": "currency",
    "Bank Account": "bank_account",
    "Subtotal": "subtotal",
    "Tax": "tax",
    "Total": "total",
}

_NUMERIC_FIELDS = {"subtotal", "tax", "total"}


class _Section:
    def __init__(self, marker: str, text: str, page_start: int, page_end: int) -> None:
        self.marker = marker
        self.text = text
        self.page_start = page_start
        self.page_end = page_end


def _sections(text: str) -> list[_Section]:
    """Split a bundle into its marked sections, tracking pages."""
    page_marks = [(m.start(), int(m.group(1))) for m in _PAGE_RE.finditer(text)]

    def page_at(pos: int) -> int:
        page = 1
        for mark_pos, number in page_marks:
            if mark_pos <= pos:
                page = number
        return page

    marks = list(_SECTION_RE.finditer(text))
    if not marks:
        return [_Section("UNKNOWN", text, 1, page_at(len(text)))]
    out: list[_Section] = []
    for i, mark in enumerate(marks):
        start = mark.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append(
            _Section(
                marker=mark.group(1).strip(),
                text=text[start:end],
                page_start=page_at(start),
                page_end=page_at(max(start, end - 1)),
            )
        )
    return out


class OfflineDocumentAdapter:
    """DocumentPort over text-layer documents, held in memory."""

    name = "offline"

    def __init__(self) -> None:
        self._docs: dict[str, bytes] = {}
        self._counter = 0

    # -- port ------------------------------------------------------

    def upload(self, filename: str, data: bytes, content_type: str) -> UploadedDocument:
        self._counter += 1
        ref = f"offline://{self._counter}"
        self._docs[ref] = data
        return UploadedDocument(engine_ref=ref, filename=filename, byte_size=len(data))

    def _text_of(self, ref: str) -> str:
        base = ref.split("#", 1)[0]
        data = self._docs.get(base)
        if data is None:
            raise DocumentBackendUnavailable(f"offline adapter holds no document {base}")
        return data.decode("utf-8", errors="replace")

    def _section_for(self, doc: UploadedDocument | SubDocument) -> _Section:
        text = self._text_of(doc.engine_ref)
        sections = _sections(text)
        if isinstance(doc, SubDocument) and "#" in doc.engine_ref:
            index = int(doc.engine_ref.rsplit("#part", 1)[1])
            return sections[index]
        return sections[0]

    def classify(self, doc: UploadedDocument | SubDocument) -> Classification:
        section = self._section_for(doc)
        return Classification(
            doc_class=_CLASS_BY_MARKER.get(section.marker, "unknown"),
            confidence=0.99,
        )

    def split(self, doc: UploadedDocument) -> list[SubDocument]:
        sections = _sections(self._text_of(doc.engine_ref))
        return [
            SubDocument(
                engine_ref=f"{doc.engine_ref}#part{i}",
                page_start=section.page_start,
                page_end=section.page_end,
                label=section.marker.title(),
            )
            for i, section in enumerate(sections)
        ]

    def extract(self, doc: UploadedDocument | SubDocument) -> ExtractedDocument:
        section = self._section_for(doc)
        fields: dict[str, ExtractedField] = {}
        lines: list[LineItem] = []
        page_lines: dict[int, int] = {}
        current_page = section.page_start
        for raw_line in section.text.splitlines():
            page_match = _PAGE_RE.match(raw_line.strip())
            if page_match:
                current_page = int(page_match.group(1))
                continue
            page_lines[current_page] = page_lines.get(current_page, 0) + 1
            line_no = page_lines[current_page]
            citation = Citation(
                page=current_page,
                bbox=(36.0, float((line_no * 24) % 940), 964.0, float((line_no * 24) % 940) + 18.0),
                source="text_layer",
            )
            stripped = raw_line.strip()
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|")]
                if len(parts) == 5:
                    try:
                        lines.append(
                            LineItem(
                                sku=parts[0],
                                description=parts[1],
                                quantity=float(parts[2]),
                                unit_price=float(parts[3]),
                                amount=float(parts[4]),
                            )
                        )
                        continue
                    except ValueError:
                        pass
            if ":" in stripped:
                label, _, value = stripped.partition(":")
                field_name = _FIELD_BY_LABEL.get(label.strip())
                if field_name and value.strip():
                    parsed: object = value.strip()
                    if field_name in _NUMERIC_FIELDS:
                        try:
                            parsed = float(str(parsed).replace(",", ""))
                        except ValueError:
                            parsed = value.strip()
                    fields[field_name] = ExtractedField(
                        value=parsed, citation=citation, confidence=0.99
                    )
        return ExtractedDocument(
            doc_class=_CLASS_BY_MARKER.get(section.marker, "unknown"),
            fields=fields,
            lines=lines,
            currency=str(fields["currency"].value) if "currency" in fields else "USD",
            raw_text=section.text,
        )

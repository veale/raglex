"""Manual import + attach/annotate (§1.9, §8).

The design treats your own material — commentary PDFs, saved articles, textbook
extracts, notes, LLM summaries — as **secondary documents that share the corpus
model and graph** (§1.9). So a PDF/HTML you drop in becomes a ``document``
(``added_by=user``), gets a typed ``relations`` edge to the case/statute it's
about, and (optionally) embedded chunks — searchable and graph-linked alongside
harvested law. Files that belong to a document but aren't themselves a document
(an annotated copy, a scanned exhibit) attach via ``document_assets``.

``added_by`` keeps user/machine material visually and analytically separable from
authoritative primary law (§10) — an LLM summary is never mistaken for a holding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import (
    AddedBy,
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
    sha256_bytes,
)
from ..extraction import extract_bytes
from ..storage import Catalogue, RawStore, TextStore

# Default treatment edge from an imported secondary doc to the primary doc it's
# about, by secondary type (§1A commentary family).
_DEFAULT_RELATIONSHIP = {
    DocType.COMMENTARY: RelationshipType.ANALYSES,
    DocType.ANNOTATION: RelationshipType.ANNOTATES,
    DocType.NOTE: RelationshipType.SUMMARISES,
    DocType.ARTICLE: RelationshipType.CRITICISES,
}


# A numbered paragraph heading a line: "42. Where a controller …" — regulatory
# guidance (EDPB/A29WP/Ofcom) numbers its paragraphs, and the paragraph, not the
# page, is the citable unit ("Guidelines 05/2020, para 42").
_PARA_LINE = re.compile(r"^(\d{1,3})\.\s+\S")


def _numbered_para_segments(text: str) -> list[Segment]:
    """Paragraph segments from an ascending "N. " numbering in extracted text (the
    same seam-detection the BAILII HTML importer uses for judgments). Only trusted
    when the numbering behaves like numbering — several paragraphs, strictly
    ascending against both neighbours — so stray "3." prose can't fake structure."""
    marks: list[tuple[int, int]] = []
    offset = 0
    for block in text.split("\n\n"):
        m = _PARA_LINE.match(block)
        if m:
            marks.append((int(m.group(1)), offset))
        offset += len(block) + 2
    ascending = [
        (n, at) for i, (n, at) in enumerate(marks)
        if (i == 0 or n > marks[i - 1][0]) and (i + 1 == len(marks) or n < marks[i + 1][0])
    ]
    if len(ascending) < 5:
        return []
    segs: list[Segment] = []
    for i, (n, at) in enumerate(ascending):
        end = ascending[i + 1][1] if i + 1 < len(ascending) else len(text)
        segs.append(Segment(label=f"para {n}", char_start=at, char_end=end, kind="paragraph"))
    return segs


@dataclass(slots=True)
class ImportResult:
    stable_id: str
    doc_type: str
    chars: int
    linked_to: str | None = None
    relationship: str | None = None
    needs_ocr: bool = False


def _surrogate_id(doc_type: DocType, payload_hash: str) -> str:
    """Stable surrogate id where no ECLI exists (§1.1)."""
    return f"user:{doc_type.value}:{payload_hash[:16]}"


def import_file(
    catalogue: Catalogue,
    rawstore: RawStore,
    textstore: TextStore,
    *,
    data: bytes,
    filename: str,
    doc_type: DocType = DocType.COMMENTARY,
    title: str | None = None,
    added_by: AddedBy = AddedBy.USER,
    link_to: str | None = None,
    relationship: RelationshipType | None = None,
    language: str | None = None,
) -> ImportResult:
    """Import a user PDF/HTML/text file as a secondary document (§1.9)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    extracted = extract_bytes(data, ext=ext)
    payload_hash = sha256_bytes(data)
    stable_id = _surrogate_id(doc_type, payload_hash)

    # Make pages addressable (a typeset handbook) so "pp. 45-47" fragment links
    # are meaningful — each page becomes a Segment (§1.9, §6b). GUIDANCE prefers
    # its numbered paragraphs when they're detectable: "Guidelines 05/2020,
    # para 42" is how these documents are actually pinpoint-cited.
    segments: list[Segment] = []
    if doc_type == DocType.GUIDANCE and extracted.text:
        segments = _numbered_para_segments(extracted.text)
    if not segments:
        for page_no, start, end in (extracted.page_spans or []):
            segments.append(Segment(label=f"p. {page_no}", char_start=start, char_end=end, kind="page"))

    relations: list[TypedRelation] = []
    rel_type = None
    if link_to:
        rel_type = relationship or _DEFAULT_RELATIONSHIP.get(doc_type, RelationshipType.ANALYSES)
        resolved = catalogue.find_document_id(link_to) is not None
        relations.append(
            TypedRelation(
                relationship_type=rel_type,
                raw_citation_string=link_to,
                dst_id=link_to,
                extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED if resolved else ResolutionStatus.PENDING,
            )
        )

    record = Record(
        source="user-import",
        stable_id=stable_id,
        doc_type=doc_type,
        title=title or filename,
        language=language,
        source_language=language,
        raw_bytes=data,
        raw_ext=ext or "bin",
        payload_hash=payload_hash,
        text=extracted.text or None,
        segments=segments,
        relations=relations,
        extracted_via=ExtractedVia.MANUAL,
        added_by=added_by,
        extra={"engine": extracted.engine, "needs_ocr": extracted.needs_ocr},
    )

    raw_path = str(rawstore.path_for(rawstore.put(data, ext=ext or "bin"), ext or "bin"))
    text_path = None
    if extracted.text and extracted.text.strip():
        text_path = str(textstore.put(payload_hash, extracted.text))
        textstore.put_segments(payload_hash, segments)  # persist page anchors
    catalogue.upsert_document(record, raw_path=raw_path, text_path=text_path)

    return ImportResult(
        stable_id=stable_id,
        doc_type=doc_type.value,
        chars=len(extracted.text or ""),
        linked_to=link_to,
        relationship=rel_type.value if rel_type else None,
        needs_ocr=extracted.needs_ocr,
    )


def _url_filename_and_ext(url: str, ctype: str = "") -> tuple[str, str]:
    """Derive (filename, extension) from a URL, inferring the extension from the URL
    *path* only. A query/fragment ("file.pdf?dl=1") must be stripped first — otherwise
    the extension comes out "pdf?dl=1", matching no extractor and falling through to the
    HTML fallback. Falls back to the content-type, then HTML, when the path has none."""
    from urllib.parse import urlsplit

    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    if not ext:
        ext = {"application/pdf": "pdf", "text/html": "html"}.get(ctype, "html")
    filename = last or "download"
    if "." not in filename:
        filename = f"{filename}.{ext}"
    return filename, ext


def import_url(
    catalogue: Catalogue,
    rawstore: RawStore,
    textstore: TextStore,
    *,
    url: str,
    doc_type: DocType = DocType.COMMENTARY,
    title: str | None = None,
    link_to: str | None = None,
    relationship: RelationshipType | None = None,
    http=None,
) -> ImportResult:
    """Fetch a PDF/HTML from a URL and import it (an agent posting a link it found,
    §1.9). The extension is inferred from the URL path or content-type."""
    from ..core.http import build_client

    client = http or build_client(timeout=60)  # proxy-aware (§5a)
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.content
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    filename, _ext = _url_filename_and_ext(url, ctype)
    return import_file(
        catalogue, rawstore, textstore, data=data, filename=filename,
        doc_type=doc_type, title=title or url, link_to=link_to, relationship=relationship,
    )


def link_documents(
    catalogue: Catalogue,
    *,
    src_id: str,
    dst_id: str,
    relationship: RelationshipType,
    src_anchor: str | None = None,
    dst_anchor: str | None = None,
) -> bool:
    """Add a manual typed edge between two existing documents (§1.3a). Optional
    pinpoint anchors record *which fragment* relates to *which fragment* — e.g. a
    practitioner handbook's ``src_anchor='pp. 45-47'`` ``analyses`` the GDPR's
    ``dst_anchor='Article 17'`` (§1.9, JuriConnect-style)."""
    resolved = catalogue.find_document_id(dst_id) is not None
    catalogue.add_relation(
        src_id,
        TypedRelation(
            relationship_type=relationship,
            raw_citation_string=dst_id,
            dst_id=dst_id,
            extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED if resolved else ResolutionStatus.PENDING,
            src_anchor=src_anchor,
            dst_anchor=dst_anchor,
        ),
    )
    return resolved


def tag_document(catalogue: Catalogue, doc_id: str, tag: str) -> bool:
    """Add a manual tag (never overwritten by a rule, §4a)."""
    return catalogue.upsert_document_tag(doc_id, tag, method="manual")


def add_note(
    catalogue: Catalogue,
    textstore: TextStore,
    *,
    text: str,
    title: str | None = None,
    link_to: str | None = None,
    relationship: RelationshipType = RelationshipType.SUMMARISES,
    added_by: AddedBy = AddedBy.USER,
) -> ImportResult:
    """Write a note/summary against a case as a first-class secondary document."""
    payload_hash = sha256_bytes(text.encode("utf-8"))
    stable_id = _surrogate_id(DocType.NOTE, payload_hash)
    relations: list[TypedRelation] = []
    if link_to:
        resolved = catalogue.find_document_id(link_to) is not None
        relations.append(
            TypedRelation(
                relationship_type=relationship,
                raw_citation_string=link_to,
                dst_id=link_to,
                extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED if resolved else ResolutionStatus.PENDING,
            )
        )
    record = Record(
        source="user-import",
        stable_id=stable_id,
        doc_type=DocType.NOTE,
        title=title or "Note",
        text=text,
        raw_bytes=text.encode("utf-8"),
        raw_ext="txt",
        payload_hash=payload_hash,
        relations=relations,
        extracted_via=ExtractedVia.MANUAL,
        added_by=added_by,
    )
    text_path = str(textstore.put(payload_hash, text))
    catalogue.upsert_document(record, text_path=text_path)
    return ImportResult(
        stable_id=stable_id, doc_type="note", chars=len(text),
        linked_to=link_to, relationship=relationship.value if link_to else None,
    )


def attach_asset(
    catalogue: Catalogue,
    rawstore: RawStore,
    *,
    doc_id: str,
    data: bytes,
    filename: str,
    kind: str = "exhibit",
    mime: str | None = None,
    added_by: AddedBy = AddedBy.USER,
) -> int:
    """Attach a file to an existing document without making it its own document
    (an annotated copy, a scanned exhibit) — kept provenance-separable (§1.9)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    payload_hash = rawstore.put(data, ext=ext)
    path = str(rawstore.path_for(payload_hash, ext))
    return catalogue.add_asset(
        doc_id, kind, path=path, mime=mime, payload_hash=payload_hash,
        added_by=added_by.value, title=filename,
    )

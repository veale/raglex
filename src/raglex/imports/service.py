"""Manual import + attach/annotate (§1.9, §8).

The design treats material you supply yourself — a commentary PDF, a saved
article, a textbook extract, but equally a judgment or a statute no adapter can
reach — as **documents that share the corpus model and graph** (§1.9). So a file
you drop in becomes a ``document`` (``added_by=user``), optionally gets a typed
``relations`` edge to the case/statute it's about, and is embedded and
citation-extracted alongside harvested law. Files that belong to a document but
aren't themselves a document (an annotated copy, a scanned exhibit) attach via
``document_assets``.

``added_by`` keeps user/machine material visually and analytically separable from
authoritative primary law (§10) — an LLM summary is never mistaken for a holding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

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
from ..core.segmentation import synthesise_numbered_segments
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


# --- where an imported document sits in the world ---------------------------
# Every jurisdiction guard in the citation pipeline keys on the SOURCE PREFIX —
# `_is_irish_host` on "ie-", `_allows_us_reporters` on "us-", the common-law
# reporter allowance on "uk-"/"ie-"/"au-caselaw"…, and Facade._jurisdiction_of on
# the same prefixes for every facet, filter and export in the UI. So a manual
# import declares its jurisdiction by taking that prefix in its own source key:
# a UK judgment you upload arrives as `uk-user-import` and is thereafter treated
# exactly as a harvested UK judgment is. Nothing else needs to know about it.
#
# Codes match Facade._JURISDICTIONS; ``tests/test_import_metadata.py`` asserts
# every one of them still round-trips to the label the rest of the app shows.
JURISDICTIONS: tuple[tuple[str, str], ...] = (
    ("uk", "United Kingdom"),
    ("ie", "Ireland"),
    ("eu", "European Union"),
    ("echr", "Council of Europe"),
    ("fr", "France"),
    ("de", "Germany"),
    ("nl", "Netherlands"),
    ("it", "Italy"),
    ("us", "United States"),
    ("ca", "Canada"),
    ("au", "Australia"),
    ("nz", "New Zealand"),
    ("sg", "Singapore"),
    ("hk", "Hong Kong"),
    ("in", "India"),
)
_JURISDICTION_CODES = {code for code, _label in JURISDICTIONS}
BASE_SOURCE = "user-import"


def import_source_key(jurisdiction: str | None) -> str:
    """``uk`` → ``uk-user-import``; anything unrecognised → the unplaced default.

    Deliberately a prefix on the existing source rather than a new column: it is what
    makes the jurisdiction grammar gates, the facets and the export scoping apply to a
    hand-uploaded document without any of them being taught a new concept.
    """
    code = (jurisdiction or "").strip().lower()
    return f"{code}-{BASE_SOURCE}" if code in _JURISDICTION_CODES else BASE_SOURCE


def jurisdiction_of_source(source: str | None) -> str | None:
    """The code back out of a source key — ``uk-user-import`` → ``uk``."""
    src = (source or "").lower()
    if not src.endswith(BASE_SOURCE):
        return None
    code = src[: -len(BASE_SOURCE)].rstrip("-")
    return code if code in _JURISDICTION_CODES else None


# --- best-effort structure --------------------------------------------------
# A flat PDF has no structure the way an Akoma Ntoso file does, but most legal
# documents number themselves, and that numbering is the citable unit ("para 42",
# "s 5"). Each parser below refuses to guess: it wants several units in strict
# ascending order before it believes the numbering is numbering. When one finds
# nothing the import still succeeds — it simply falls back to pages.
STRUCTURE_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "Best effort (by type)"),
    ("paragraphs", "Paragraphs [42] / 42."),
    ("sections", "Sections / articles"),
    ("pages", "Pages (PDF)"),
    ("none", "No structure"),
)
STRUCTURE_KEYS = {key for key, _label in STRUCTURE_CHOICES}

# A numbered paragraph heading a line: "42. Where a controller …" — regulatory
# guidance (EDPB/A29WP/Ofcom) numbers its paragraphs, and the paragraph, not the
# page, is the citable unit ("Guidelines 05/2020, para 42").
_PARA_LINE = re.compile(r"^(\d{1,3})\.\s+\S")

# "Section 5", "Article 17", "Regulation 3", "s 5" / "s. 5A" at the head of a line —
# the heading forms a statute-shaped document uses. The number is what must ascend.
_SECTION_LINE = re.compile(
    r"^[ \t]{0,8}(?:(?:Section|Article|Regulation|Rule|Clause|Para(?:graph)?|s\.?|art\.?|reg\.?)"
    r"[ \t]+)(\d{1,4})([A-Z]{0,2})\b",
    re.IGNORECASE | re.MULTILINE,
)


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


def _section_segments(text: str) -> list[Segment]:
    """Segments from statute-shaped headings ("Section 5", "Article 17", "s. 5A").

    Same discipline as the paragraph parsers: the numbers must strictly ascend against
    both neighbours and there must be several of them, so a passing "see article 6" in
    running prose cannot invent a section break.
    """
    marks: list[tuple[int, str, int]] = []
    for m in _SECTION_LINE.finditer(text):
        marks.append((int(m.group(1)), m.group(0).strip(), m.start()))
    ascending = [
        row for i, row in enumerate(marks)
        if (i == 0 or row[0] > marks[i - 1][0])
        and (i + 1 == len(marks) or row[0] < marks[i + 1][0])
    ]
    if len(ascending) < 3:
        return []
    segs: list[Segment] = []
    for i, (_n, label, at) in enumerate(ascending):
        end = ascending[i + 1][2] if i + 1 < len(ascending) else len(text)
        segs.append(Segment(label=label, char_start=at, char_end=end,
                            kind="section", level=1))
    return segs


def _page_segments(page_spans) -> list[Segment]:
    return [Segment(label=f"p. {no}", char_start=start, char_end=end, kind="page")
            for no, start, end in (page_spans or [])]


def structure_segments(structure: str, doc_type: DocType, extracted) -> list[Segment]:
    """The segments an import gets, given what the operator asked for.

    Every choice is best effort and every choice falls back to pages, because a parser
    that finds nothing must not cost the reader their page anchors. ``auto`` reproduces
    what the importer has always done, extended to judgments — which number their
    paragraphs as surely as guidance does, and are pinpoint-cited by them.
    """
    text = extracted.text or ""
    pages = _page_segments(extracted.page_spans)
    if structure == "none":
        return []
    if structure == "pages":
        return pages
    found: list[Segment] = []
    if text:
        if structure == "sections":
            found = _section_segments(text)
        elif structure == "paragraphs":
            # The bracketed "[42]" judgment form first, then the dotted "42. " form
            # guidance uses — one option covers both, which is what "best effort" means.
            found = synthesise_numbered_segments(text) or _numbered_para_segments(text)
        elif doc_type == DocType.GUIDANCE:
            found = _numbered_para_segments(text)
        elif doc_type in _NUMBERED_DOC_TYPES:
            found = synthesise_numbered_segments(text)
        elif doc_type == DocType.LEGISLATION:
            found = _section_segments(text)
    return found or pages


# Document types that number their own paragraphs, and are pinpoint-cited by that
# number rather than by page ("[2019] UKSC 4 at [42]").
_NUMBERED_DOC_TYPES = {DocType.JUDGMENT, DocType.DECISION, DocType.OPINION}


@dataclass(slots=True)
class ImportResult:
    stable_id: str
    doc_type: str
    chars: int
    linked_to: str | None = None
    relationship: str | None = None
    needs_ocr: bool = False
    source: str = BASE_SOURCE
    jurisdiction: str | None = None
    title: str | None = None
    segments: int = 0
    structure: str = "auto"
    tags: tuple[str, ...] = ()
    citation: str | None = None


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
    jurisdiction: str | None = None,
    court: str | None = None,
    decision_date: date | None = None,
    citation: str | None = None,
    tags: tuple[str, ...] | list[str] = (),
    structure: str = "auto",
) -> ImportResult:
    """Import a user-supplied PDF/HTML/text file as a document (§1.9).

    ``jurisdiction`` is the one field that changes how the rest of the pipeline treats
    the result: it selects the source key, and every jurisdiction-sensitive citation
    guard reads that key (see :func:`import_source_key`). ``citation`` registers the
    document's own identifier as an alias, so edges elsewhere in the corpus that already
    point at that citation resolve onto this upload.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    extracted = extract_bytes(data, ext=ext)
    payload_hash = sha256_bytes(data)
    stable_id = _surrogate_id(doc_type, payload_hash)
    structure = structure if structure in STRUCTURE_KEYS else "auto"

    # Make the document's own units addressable, so "para 42" / "pp. 45-47" fragment
    # links are meaningful (§1.9, §6b).
    segments = structure_segments(structure, doc_type, extracted)

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

    citation = (citation or "").strip() or None
    record = Record(
        source=import_source_key(jurisdiction),
        stable_id=stable_id,
        doc_type=doc_type,
        title=title or filename,
        court=(court or "").strip() or None,
        decision_date=decision_date,
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
        extra={"engine": extracted.engine, "needs_ocr": extracted.needs_ocr,
               "import_structure": structure,
               **({"import_citation": citation} if citation else {})},
    )

    raw_path = str(rawstore.path_for(rawstore.put(data, ext=ext or "bin"), ext or "bin"))
    text_path = None
    if extracted.text and extracted.text.strip():
        text_path = str(textstore.put(payload_hash, extracted.text))
        textstore.put_segments(payload_hash, segments)  # persist the anchors
    catalogue.upsert_document(record, raw_path=raw_path, text_path=text_path)

    # The citation the operator typed is how the REST of the corpus already refers to
    # this document, so alias it: pending edges keyed on that citation now land here.
    if citation:
        catalogue.put_alias(citation.casefold(), stable_id, source="user-import")

    applied: list[str] = []
    for tag in tags or ():
        tag = str(tag).strip()
        if tag and tag_document(catalogue, stable_id, tag):
            applied.append(tag)

    return ImportResult(
        stable_id=stable_id,
        doc_type=doc_type.value,
        chars=len(extracted.text or ""),
        linked_to=link_to,
        relationship=rel_type.value if rel_type else None,
        needs_ocr=extracted.needs_ocr,
        source=record.source,
        jurisdiction=jurisdiction_of_source(record.source),
        title=record.title,
        segments=len(segments),
        structure=structure,
        tags=tuple(applied),
        citation=citation,
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

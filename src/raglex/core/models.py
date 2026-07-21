"""Core domain models — the normalised shapes every adapter produces.

These are deliberately jurisdiction-agnostic (§1.5): a scraped table and a SPARQL
result both arrive here as a ``Record``, and the pipeline downstream never learns
how the bytes were fetched (§5a quarantine rule). Mirrors Appendix A.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class DocType(StrEnum):
    """Polymorphic document type (§1.3). Primary law + secondary material share
    one table so a regulator's guideline and the case it cites live in one graph."""

    # primary
    JUDGMENT = "judgment"
    DECISION = "decision"
    GUIDANCE = "guidance"
    OPINION = "opinion"
    LEGISLATION = "legislation"
    # secondary (§1.9)
    COMMENTARY = "commentary"
    ANNOTATION = "annotation"
    NOTE = "note"
    ARTICLE = "article"


class RelationshipType(StrEnum):
    """Typed edges (§1.3a, §1A). One vocabulary, two families, one table."""

    # citation / treatment (primary -> primary)
    FOLLOWS = "follows"
    DISTINGUISHES = "distinguishes"
    OVERRULES = "overrules"
    APPLIES = "applies"
    CONSIDERS = "considers"
    CITES_FOR_FACT = "cites_for_fact"
    MENTIONS = "mentions"
    IMPLEMENTS = "implements"  # statute -> directive
    # a national transposition measure (NIM/MNE) implementing an EU directive: the
    # EU directive -> the national measure that transposes it, minted from CELLAR's
    # transposition relations. Usually dangling until the national statute is
    # harvested by fr-legislation / de-neuris, so it feeds the §5b worklist and turns
    # "GDPR ⇐ transposed by ⇒ BDSG / loi Informatique et Libertés" into a live edge.
    TRANSPOSES = "transposes"
    INTERPRETS = "interprets"  # case -> statute
    # a CJEU judgment answering a preliminary reference made by a national court
    # (CJEU case -> the national referring case). Usually dangling until the
    # national case is harvested/scraped (§5b worklist).
    PRELIMINARY_REFERENCE = "preliminary_reference"
    # an Advocate General's Opinion delivered in a case (AG opinion -> the judgment)
    OPINION_IN = "opinion_in"
    # a deferred backward-citation: a held case -> a (not-yet-held) LATER case that cites
    # it, recorded from CELLAR's citation graph WITHOUT downloading the citing case. It's a
    # dangling edge, so the citing case surfaces in the §5b harvest worklist and its full
    # text is pulled later — keeping the expand-citing sweep fast (edges, not downloads).
    CITED_BY = "cited_by"
    # commentary / annotation (secondary -> primary or secondary)
    ANALYSES = "analyses"
    SUMMARISES = "summarises"
    CRITICISES = "criticises"
    CITED_BY_COMMENTARY = "cited_by_commentary"
    ANNOTATES = "annotates"
    SUPERSEDES = "supersedes"
    # the UK assimilated (formerly "retained EU law") version of an EU instrument →
    # the EU original it derives from (legislation.gov.uk /european/… → its CELEX)
    ASSIMILATED_VERSION_OF = "assimilated_version_of"
    # a point-in-time copy of legislation (the law as it stood on a date) → the
    # base instrument, so an old case can cite the live provisions, not today's text
    POINT_IN_TIME_OF = "point_in_time_of"
    # an amendment effect recorded in legislation.gov.uk metadata: the affected
    # instrument → the affecting (amending) one. Minted from <ukm:UnappliedEffects>;
    # dangling until the amending act is harvested, so it feeds the §5b worklist and
    # the amending act gets pulled. See storage.catalogue.effects_refresh for the
    # "outstanding effects" re-check queue.
    AMENDED_BY = "amended_by"
    # the same fact from the *affecting* side: the amending instrument → the one it
    # changes. Minted from the affecting-side "Changes to Legislation" feed when an
    # amending act is imported, so the change emanates FROM the new act to the (old,
    # maybe-never-repulled) instruments it affects — which then get flagged for re-pull.
    AMENDS = "amends"


class ExtractedVia(StrEnum):
    """Provenance for an edge or an extracted field (§1A, §10). Lets machine
    inference stay distinguishable from authoritative structured data."""

    STRUCTURED = "structured"
    REGEX = "regex"
    LLM = "llm"
    MANUAL = "manual"
    SCRAPE = "scrape"
    # a heuristic guess, not a literal match — e.g. a bare "section 5" carried
    # forward to the last-named statute. Distinguishable so the UI can flag it as
    # uncertain and a human can confirm/reject.
    INFERRED = "inferred"


class AddedBy(StrEnum):
    """Who put this in the corpus (§10). Keeps machine/human material separable."""

    HARVEST = "harvest"
    USER = "user"
    LLM = "llm"


class ResolutionStatus(StrEnum):
    """State of a citation edge's destination (§5b, Appendix B)."""

    RESOLVED = "resolved"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"
    UNRESOLVABLE = "unresolvable"


class UpstreamStatus(StrEnum):
    """Corpus is append-only (§1.4a): disappearance is a *state change*, never a
    row deletion. ``GONE_404`` etc. are flags for human review, never triggers."""

    LIVE = "live"
    GONE_404 = "gone_404"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class Stub:
    """Lightweight discovery result (Appendix A). Cheap to produce in bulk before
    deciding whether to fetch the full document."""

    stable_id: str
    landing_url: str | None = None
    raw_url: str | None = None
    hint_date: date | None = None
    # cheap fields the discovery feed already gives us, used by the stage-1 gate
    title: str | None = None
    court: str | None = None
    # adapter-private metadata carried from discover() to fetch() (e.g. a CELLAR
    # CELEX + legislation link type) — never read by the shared pipeline.
    hints: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Segment:
    """A structural unit of a document's text (§6b): a numbered paragraph, a
    functional zone (France's *motivations*), a Formex section, an Act article.

    The adapter knows the structure (the source XML has it), so it emits segments
    with char offsets *into the document's flat text* — the chunker then chunks on
    these native seams instead of re-guessing them from flattened prose. ``label``
    is the citable unit (e.g. "[42]", "motivations", "Article 17")."""

    label: str
    char_start: int
    char_end: int
    kind: str = "paragraph"  # paragraph | section | zone | article | ruling
    level: int = 0


@dataclass(frozen=True, slots=True)
class TypedRelation:
    """An edge the adapter extracted (Appendix A/B). ``dst_id`` is nullable: the
    edge exists from the moment a citation string is found, before it resolves to
    a node (§5b). ``raw_citation_string`` is kept even after resolution so a bad
    match is auditable."""

    relationship_type: RelationshipType
    raw_citation_string: str | None = None
    dst_id: str | None = None
    extracted_via: ExtractedVia = ExtractedVia.STRUCTURED
    resolution_status: ResolutionStatus = ResolutionStatus.PENDING
    # pinpoint anchors (§1.9): a fragment of src relates to a fragment of dst,
    # e.g. src_anchor="pp. 45-47", dst_anchor="Article 17" (JuriConnect-style).
    src_anchor: str | None = None
    dst_anchor: str | None = None
    # char span of the citation in the source text → context for treatment
    # classification (§1.3a).
    context_start: int | None = None
    context_end: int | None = None


@dataclass(slots=True)
class Record:
    """A fully normalised document, ready for the shared pipeline (Appendix A).

    Every source — pristine REST or screen-scrape — produces this same shape.
    """

    source: str
    stable_id: str
    doc_type: DocType
    title: str | None = None
    court: str | None = None
    decision_date: date | None = None
    language: str | None = None
    source_language: str | None = None
    ecli: str | None = None
    landing_url: str | None = None

    raw_bytes: bytes | None = None
    raw_ext: str | None = None  # 'xml', 'html', 'pdf', ...
    payload_hash: str | None = None  # SHA-256 of raw_bytes; dedup before downstream work

    text: str | None = None
    # structural units of `text` (char offsets into it), where the source has
    # structure (§6b). Empty → the chunker derives units from the flat text.
    segments: list[Segment] = field(default_factory=list)
    relations: list[TypedRelation] = field(default_factory=list)

    topic_tags: list[str] = field(default_factory=list)
    topic_score: float | None = None

    version: int = 1
    extracted_via: ExtractedVia = ExtractedVia.STRUCTURED
    added_by: AddedBy = AddedBy.HARVEST
    extra: dict = field(default_factory=dict)  # needs_ocr, source_language, ...

    def ensure_payload_hash(self) -> str | None:
        """Content-hash dedup (§5): hash raw bytes so a feed bumping 'last
        modified' without changing a byte short-circuits before extraction.

        Local bulk-import adapters (the A2AJ Canadian parquet corpus, the Open
        Australian Legal Corpus JSONL…) hand over already-extracted ``text`` with no
        ``raw_bytes`` at all — there is no original file to hash. Without a fallback,
        ``payload_hash`` stays ``None`` forever, which silently breaks two things
        downstream: the pipeline only writes ``text`` into the TextStore when a
        ``payload_hash`` is present (§1.2), and the reader only serves text when the
        document row carries one — so the document is stored (title, court, citation
        edges all resolve) but its text is never persisted and the UI shows
        "No extracted text". Hash ``text`` itself in that case so these adapters get
        the same dedup + storage behaviour as byte-fetching ones.
        """
        if self.payload_hash is None:
            if self.raw_bytes is not None:
                self.payload_hash = sha256_bytes(self.raw_bytes)
            elif self.text:
                self.payload_hash = sha256_bytes(self.text.encode("utf-8"))
        return self.payload_hash


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

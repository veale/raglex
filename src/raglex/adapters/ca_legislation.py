"""Canada federal legislation — the Justice Laws open XML corpus.

Canada's federal tier is the cleanest bulk-ingest target in the series, and the reason
is that **the corpus is a git repository**: Justice Canada publishes every consolidated
Act and Regulation as XML at ``github.com/justicecanada/laws-lois-xml``, updated on the
site's biweekly consolidation cadence. So unlike every other adapter here, discovery
involves no crawling, no pagination and — in the normal case — no HTTP at all:

* **Enumeration** comes from the repo's own ``lookup/lookup.xml`` manifest, which lists
  every Act and Regulation in both languages with its ``LastConsolidationDate``. That
  date *is* the change signal: a document whose consolidation date moved has been
  re-consolidated, and nothing else needs fetching to know it. (``lookupfull.xml`` adds
  repealed laws, which we keep addressable so old case-law citations still resolve.)
* **Content** is read straight off disk from the clone.
* **Refresh** is ``git pull`` (opt-in via ``pull=true``) — version-controlled primary
  law, so the commit history is a precise change feed rather than a date-diff heuristic.

Three edge families come out structured, with no citation grammar involved:

1. **regulation → enabling Act** — ``Identification/EnablingAuthority`` names the Act by
   code (the parser mints this; Australia's ``based_on`` analogue).
2. **Act → regulations made under it** — the manifest's ``<Relationships rid=…>`` is the
   same fact from the Act's side, and is the one edge that exists *only* in the manifest.
3. **provision → amending instrument** — parsed from the ``HistoricalNote`` chains the
   format parser lifts out of each provision.

**Bilinguality.** English and French are *equally authoritative* under Canadian law —
not translations — so they are modelled as two co-equal Expressions of one Work, paired
by the manifest's ``olid`` (other-language id) and addressed by distinct stable_ids
(``ca/act/a-1`` / ``ca/act/a-1/fra``). ``lang`` selects which to ingest; the default is
English-only, and switching it to ``fra``/``both`` adds nodes without renaming any.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)
from ..formats.lims_xml import ca_id, parse_lims_xml

__all__ = ["ca_id", "CanadaFederalAdapter", "LookupEntry", "load_lookup",
           "parse_historical_citation"]

REPO_URL = "https://github.com/justicecanada/laws-lois-xml"
SITE = "https://laws-lois.justice.gc.ca"

# Where each (language, kind) lives in the repo. The French tree uses French directory
# names — lois/reglements, not acts/regulations.
_TREES = {
    ("eng", "act"): "eng/acts",
    ("eng", "regulation"): "eng/regulations",
    ("fra", "act"): "fra/lois",
    ("fra", "regulation"): "fra/reglements",
}
_LANGS = {"eng", "fra"}
_KINDS = {"act", "regulation"}


def _filename(code: str) -> str:
    """An Act code or regulation instrument number → its file stem in the repo.

    ``SOR/2007-151`` → ``SOR-2007-151``; ``C.R.C., c. 870`` → ``C.R.C.,_c._870``. The
    repo encodes the instrument number literally, swapping only the two characters that
    are illegal or awkward in a path.
    """
    return (code or "").strip().replace("/", "-").replace(" ", "_")


# -- the manifest ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LookupEntry:
    """One row of ``lookup.xml`` — a document in one language."""
    lims_id: str            # "167e" — the manifest's own id (language-suffixed)
    kind: str               # act | regulation
    code: str               # "A-1" | "SOR/2007-151"
    language: str           # eng | fra
    title: str | None
    consolidation_date: date | None
    official_number: str | None = None   # "2019, c. 10" — the annual-statute citation
    other_language_id: str | None = None  # olid: the co-equal Expression's manifest id
    regulation_ids: tuple[str, ...] = ()  # rids: regulations made under this Act

    @property
    def stable_id(self) -> str:
        return ca_id(self.kind, self.code, self.language)

    @property
    def filename(self) -> str:
        return f"{_filename(self.code)}.xml"


def _yyyymmdd(raw: str | None) -> date | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


_LANG_MAP = {"en": "eng", "fr": "fra"}


def load_lookup(path: Path, *, include_repealed: bool = False) -> list[LookupEntry]:
    """Parse the repo's lookup manifest into entries.

    ``lookupfull.xml`` is the same shape as ``lookup.xml`` but includes repealed laws;
    repealed material is worth holding (a judgment from 2003 cites the law as it then
    was), so ``include_repealed`` selects it where present.
    """
    manifest = path / "lookup" / ("lookupfull.xml" if include_repealed else "lookup.xml")
    if not manifest.exists():
        manifest = path / "lookup" / "lookup.xml"
    if not manifest.exists():
        return []
    try:
        root = ET.parse(manifest).getroot()
    except ET.ParseError:
        return []

    out: list[LookupEntry] = []
    for group, kind, code_tag in (("Statutes", "act", "ChapterNumber"),
                                  ("Regulations", "regulation", "AlphaNumber")):
        container = root.find(group)
        if container is None:
            continue
        for row in container:
            code = (row.findtext(code_tag) or "").strip()
            if not code:
                continue
            rels = row.find("Relationships")
            out.append(LookupEntry(
                lims_id=row.get("id") or "",
                kind=kind,
                code=code,
                language=_LANG_MAP.get((row.findtext("Language") or "en").strip(), "eng"),
                title=(row.findtext("ShortTitle") or "").strip() or None,
                consolidation_date=_yyyymmdd(row.findtext("LastConsolidationDate")),
                official_number=(row.findtext("OfficialNumber") or "").strip() or None,
                other_language_id=row.get("olid"),
                regulation_ids=tuple(r.get("rid") for r in rels if r.get("rid"))
                if rels is not None else (),
            ))
    return out


# -- historical-note citations ----------------------------------------------
# A provision's amendment chain, as published. Four forms occur:
#   "R.S., 1985, c. A-1, s. 3"   → consolidated Act code, directly usable
#   "2019, c. 18, s. 2"          → an ANNUAL statute chapter, needs the manifest index
#   "SOR/2024-244, s. 1"         → a regulation, directly usable
#   "1980-81-82-83, c. 111, …"   → a pre-consolidation annual cite, usually unresolvable
_RS_RE = re.compile(r"R\.S\.(?:C\.)?(?:,\s*\d{4})?,\s*c\.\s*([A-Z]{1,2}-[\d.]+)", re.I)
_REG_RE = re.compile(r"\b(SOR|SI|DORS|TR)/(\d{2,4}-\d+)", re.I)
_ANNUAL_RE = re.compile(r"\b(\d{4}),\s*c\.\s*(\d+(?:\.\d+)?)")
_SECTION_RE = re.compile(r"\bs{1,2}\.\s*([\d.]+(?:\(\d+\))?)")


def parse_historical_citation(
    citation: str, annual_index: dict[str, str] | None = None, lang: str = "eng",
) -> tuple[str | None, str | None]:
    """One ``HistoricalNote`` chain item → ``(stable_id, section anchor)``.

    Returns ``(None, anchor)`` when the citing form can't be mapped to a document we
    could hold — chiefly pre-1985 annual chapters ("1980-81-82-83, c. 111"), which name
    a statute volume that the consolidated corpus doesn't carry as its own document.
    Minting an id for those would create edges that can never resolve.

    ``annual_index`` maps an annual citation ("2019, c. 18") to the consolidated chapter
    code it became ("A-0.6"), built from the manifest's ``OfficialNumber``. Without it,
    annual citations stay unresolved rather than guessing.
    """
    text = " ".join((citation or "").split())
    anchor = None
    m = _SECTION_RE.search(text)
    if m:
        anchor = f"s. {m.group(1)}"

    m = _REG_RE.search(text)
    if m:
        # DORS/TR are the French series names for SOR/SI — the same instrument.
        series = {"dors": "SOR", "tr": "SI"}.get(m.group(1).lower(), m.group(1).upper())
        return ca_id("regulation", f"{series}/{m.group(2)}", lang), anchor

    m = _RS_RE.search(text)
    if m:
        return ca_id("act", m.group(1).upper(), lang), anchor

    m = _ANNUAL_RE.search(text)
    if m and annual_index:
        code = annual_index.get(f"{m.group(1)}, c. {m.group(2)}")
        if code:
            return ca_id("act", code, lang), anchor
    return None, anchor


def build_annual_index(entries: list[LookupEntry]) -> dict[str, str]:
    """``{"2019, c. 10": "A-0.6"}`` — the annual-statute citation → consolidated chapter
    code map, which is what makes the majority of ``HistoricalNote`` chains resolvable.
    Built from the manifest's own ``OfficialNumber`` field, so it is the register's
    mapping rather than an inferred one."""
    index: dict[str, str] = {}
    for e in entries:
        if e.kind != "act" or not e.official_number:
            continue
        key = " ".join(e.official_number.split())
        m = _ANNUAL_RE.match(key)
        if m:
            index.setdefault(f"{m.group(1)}, c. {m.group(2)}", e.code)
    return index


# -- the adapter -------------------------------------------------------------
class CanadaFederalAdapter(BaseAdapter):
    """Justice Laws consolidated Acts + Regulations, read from a local clone.

    ``path`` points at a checkout of ``justicecanada/laws-lois-xml``. Discovery reads the
    shipped manifest (falling back to a directory walk if it is absent), so a run
    enumerates the whole corpus without a single request; ``since`` compares against each
    document's ``LastConsolidationDate``, making incremental runs pull exactly the
    re-consolidated documents.
    """

    source = "ca-federal"
    min_interval = 0.0        # local filesystem — no pacing needed
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | Path | None = None, lang: str = "eng",
                 types: str | tuple[str, ...] = ("act", "regulation"),
                 ids: str | tuple[str, ...] | None = None,
                 include_repealed: bool = False, pull: bool | str = False) -> None:
        self.path = Path(path).expanduser() if path else None
        langs = {l.strip().lower() for l in str(lang).split(",") if l.strip()}
        if "both" in langs or "all" in langs:
            langs = set(_LANGS)
        self.langs = {l for l in langs if l in _LANGS} or {"eng"}
        if isinstance(types, str):
            types = tuple(t.strip() for t in types.split(",") if t.strip())
        self.types = {t.lower().rstrip("s") for t in types} & _KINDS or set(_KINDS)
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.include_repealed = _flag(include_repealed)
        self.pull = _flag(pull)
        self._entries: list[LookupEntry] | None = None
        self._annual: dict[str, str] = {}
        # manifest id ("638933e") → entry, so an Act's <Relationship rid=…> can be
        # turned into the regulation's stable_id without a second pass.
        self._by_lims_id: dict[str, LookupEntry] = {}
        # (language, chapter code) → entry, for pairing an Act with its other-language
        # Expression (statutes carry no olid pointer; they share a chapter code).
        self._acts_by_code: dict[tuple[str, str], LookupEntry] = {}

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is None or not self.path.exists():
            return
        if self.pull:
            self._git_pull()
        entries = self._load()
        wanted = {i.strip().lower() for i in self.ids}

        count = 0
        for entry in entries:
            if entry.language not in self.langs or entry.kind not in self.types:
                continue
            if wanted and not self._matches(entry, wanted):
                continue
            # Incremental: the manifest's consolidation date is the change signal, so a
            # document that hasn't been re-consolidated is skipped without opening it.
            stamp = entry.consolidation_date.isoformat() if entry.consolidation_date else None
            if since and stamp and stamp <= since[:10]:
                continue
            file = self._file_for(entry)
            if file is None:
                continue
            yield Stub(
                stable_id=entry.stable_id,
                title=entry.title,
                landing_url=self._landing(entry),
                raw_url=str(file),
                hint_date=entry.consolidation_date,
                hints={"kind": entry.kind, "code": entry.code, "language": entry.language,
                       "path": str(file), "lims_id": entry.lims_id,
                       "regulation_ids": entry.regulation_ids,
                       "other_language_id": entry.other_language_id,
                       "watermark": stamp},
            )
            count += 1
            if max_pages is not None and count >= max_pages * 100:
                return

    def _matches(self, entry: LookupEntry, wanted: set[str]) -> bool:
        return bool({entry.code.lower(), entry.stable_id.lower(),
                     (entry.official_number or "").lower()} & wanted)

    def _load(self) -> list[LookupEntry]:
        if self._entries is None:
            entries = load_lookup(self.path, include_repealed=self.include_repealed)
            if not entries:
                entries = self._walk_tree()
            self._entries = entries
            self._annual = build_annual_index(entries)
            self._by_lims_id = {e.lims_id: e for e in entries if e.lims_id}
            self._acts_by_code = {(e.language, e.code.lower()): e
                                  for e in entries if e.kind == "act"}
        return self._entries

    def _walk_tree(self) -> list[LookupEntry]:
        """Fallback enumeration when the manifest is missing: walk the XML trees.

        Loses the consolidation dates (so runs can't be incremental) and the Act →
        regulations edges, but keeps the corpus ingestible from a partial checkout.
        """
        out: list[LookupEntry] = []
        for (lang, kind), rel in _TREES.items():
            folder = self.path / rel
            if not folder.is_dir():
                continue
            for file in sorted(folder.glob("*.xml")):
                out.append(LookupEntry(
                    lims_id="", kind=kind, code=file.stem.replace("_", " "),
                    language=lang, title=None, consolidation_date=None))
        return out

    def _file_for(self, entry: LookupEntry) -> Path | None:
        rel = _TREES.get((entry.language, entry.kind))
        if rel is None:
            return None
        file = self.path / rel / entry.filename
        return file if file.exists() else None

    def _other_language(self, stub: Stub, lang: str) -> LookupEntry | None:
        """The co-equal Expression in the other official language.

        Regulations carry an explicit ``olid`` pointer (their French instrument number
        differs — ``C.R.C., ch. 870`` vs ``C.R.C., c. 870`` — so it can't be derived).
        Statutes have no ``olid``: both languages share one ``ChapterNumber``, so the
        pair is found by looking up the same code in the other language.
        """
        other = self._by_lims_id.get(stub.hints.get("other_language_id") or "")
        if other is not None:
            return other
        if stub.hints.get("kind") != "act":
            return None
        target_lang = "fra" if lang == "eng" else "eng"
        code = (stub.hints.get("code") or "").lower()
        return self._acts_by_code.get((target_lang, code))

    def _landing(self, entry: LookupEntry) -> str:
        lang = "eng" if entry.language == "eng" else "fra"
        seg = "acts" if entry.kind == "act" else "regulations"
        return f"{SITE}/{lang}/{seg}/{_filename(entry.code)}/"

    def _git_pull(self) -> None:
        """Refresh the clone. Best-effort: a failed pull (offline, dirty tree, not a
        checkout) leaves the existing corpus perfectly usable, so it must not abort
        the run."""
        try:
            subprocess.run(["git", "-C", str(self.path), "pull", "--ff-only"],
                           capture_output=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        file = Path(stub.hints["path"])
        try:
            data = file.read_bytes()
        except OSError:
            return None
        doc = parse_lims_xml(data)
        if not doc.text:
            return None

        lang = stub.hints.get("language", "eng")
        meta = doc.metadata
        relations = [r for r in doc.relations if r.dst_id != stub.stable_id]

        # Provision-level amendment provenance → amended_by edges. Deduped on
        # (target, provision) because a chain repeats the same amending Act across many
        # provisions, and one edge per affected provision is the useful granularity.
        self._load()
        seen: set[tuple] = set()
        for note in meta.get("historical_notes") or []:
            dst, anchor = parse_historical_citation(note.citation, self._annual, lang)
            if not dst or dst == stub.stable_id:
                continue
            key = (dst, note.provision)
            if key in seen:
                continue
            seen.add(key)
            relations.append(TypedRelation(
                relationship_type=RelationshipType.AMENDED_BY,
                raw_citation_string=note.citation, dst_id=dst,
                src_anchor=note.provision, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        # The Act → "regulations made under it" edges, which live ONLY in the manifest
        # (the regulation's own XML carries the reverse direction).
        for rid in stub.hints.get("regulation_ids") or ():
            target = self._by_lims_id.get(rid)
            if target is None:
                continue
            # AMENDED_BY + "made under this Act" is the idiom the Irish adapter already
            # uses for this exact reverse edge ([[irish-legislation]]) — same shape here.
            relations.append(TypedRelation(
                relationship_type=RelationshipType.AMENDED_BY,
                raw_citation_string=target.title or target.code,
                dst_id=target.stable_id, dst_anchor="made under this Act",
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        other = self._other_language(stub, lang)
        pit = meta.get("pit_date")

        extra = {
            "jurisdiction": "ca",
            "kind": meta.get("kind"),
            "code": meta.get("code"),
            "format": "lims-xml",
            "long_title": meta.get("long_title"),
            "regulation_type": meta.get("regulation_type"),
            "bill_origin": meta.get("bill_origin"),
            "bill_type": meta.get("bill_type"),
            "in_force": meta.get("in_force"),
            # Justice Laws is the official source AND both languages are equally
            # authoritative — neither is a translation of the other.
            "is_authoritative": True,
            "authoritative_languages": ["eng", "fra"],
            "point_in_time": pit.isoformat() if pit else None,
            "last_amended_date": _iso_str(meta.get("last_amended_date")),
            "current_date": _iso_str(meta.get("current_date")),
            "consolidation_date": _iso_str(meta.get("consolidation_date")),
            "inforce_start_date": _iso_str(meta.get("inforce_start_date")),
            "has_previous_version": meta.get("has_previous_version"),
            "repealed": meta.get("repealed"),
            "enabling_act_id": meta.get("enabling_act_id"),
            "lims_id": meta.get("lims_id"),
            # the co-equal Expression in the other official language
            "other_language_id": other.stable_id if other else None,
            # provision-level in-force data, the finest-grained point-in-time we hold:
            # enough to reconstruct the text in force on a given date per section.
            "provisions": [
                {"label": p.label,
                 "inforce_start": _iso_str(p.inforce_start),
                 "last_amended": _iso_str(p.last_amended),
                 "enacted": _iso_str(p.enacted),
                 "repealed": p.repealed}
                for p in (meta.get("provisions") or [])
            ],
        }

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=stub.title or doc.title or meta.get("code") or stub.stable_id,
            language="en" if lang == "eng" else "fr",
            source_language="en" if lang == "eng" else "fr",
            decision_date=doc.decision_date,
            landing_url=stub.landing_url,
            raw_bytes=data, raw_ext="xml",
            text=doc.text, segments=doc.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )


def _iso_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _flag(value: bool | str) -> bool:
    return str(value).strip().lower() not in ("false", "0", "no", "", "none")

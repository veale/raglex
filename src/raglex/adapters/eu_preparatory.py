"""European Commission preparatory and policy documents from EUR-Lex/CELLAR.

Sector 5 is deliberately separate from enacted EU legislation.  It contains COM
proposals and communications, JOIN papers, and SEC/SWD material (including impact
assessments).  CELLAR supplies both the renditions and the legislative-procedure graph.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Iterator

from ..core.models import DocType, ExtractedVia, RelationshipType, ResolutionStatus, Stub, TypedRelation
from .eu_legislation import CDM, EULegislationAdapter

DEFAULT_PREP_TYPES = ("PC", "DC", "JC", "SC", "XC")
_TYPE_RE = re.compile(r"^5\d{4}(PC|DC|JC|SC|XC)", re.I)
_PRINTED_ID = re.compile(r"\b(COM|SWD|SEC|JOIN)\s*[(/]\s*(\d{4})\s*[)/]\s*(\d{1,6})\b", re.I)
_TITLE_HEAD = re.compile(
    r"^(?:COMMISSION STAFF WORKING DOCUMENT|COMMUNICATION FROM THE COMMISSION|"
    r"REPORT FROM THE COMMISSION|GREEN PAPER|WHITE PAPER|JOINT COMMUNICATION|"
    r"PROPOSAL FOR (?:A|AN)\b)", re.I)


def preparatory_subtype(celex: str) -> tuple[str, str]:
    m = _TYPE_RE.match(celex or "")
    desc = m.group(1).upper() if m else "OTHER"
    return {
        "PC": ("proposals", "Commission legislative proposals (COM)"),
        "DC": ("communications", "Commission communications, reports and policy papers"),
        "JC": ("joint", "Joint Commission documents (JOIN)"),
        "SC": ("staff-working", "Staff working documents and impact assessments (SWD/SEC)"),
        "XC": ("other-commission", "Other Commission preparatory documents"),
    }.get(desc, ("other", "Other EU preparatory documents"))


def printed_aliases(celex: str, title: str | None = None, text: str | None = None) -> list[str]:
    """Human identifiers used in citations: ``COM(2024) 123``, ``SWD/2024/123``."""
    out: list[str] = []
    # Only the first header identifier belongs to THIS work. Later identifiers in an
    # impact assessment name its accompanying proposal and sibling SWDs; aliasing all
    # of them to this document would corrupt resolution.
    for value in (title or "", (text or "")[:700]):
        m0 = _PRINTED_ID.search(value)
        if m0:
            kind, year, number = m0.group(1).upper(), m0.group(2), str(int(m0.group(3)))
            out.extend((f"{kind}({year}) {number}", f"{kind}/{year}/{number}"))
            break
    # PC/DC are unambiguously COM identifiers even when a metadata-only record has no title.
    m = re.match(r"^5(\d{4})(PC|DC)(\d+)$", celex, re.I)
    if m:
        year, number = m.group(1), str(int(m.group(3)))
        out.extend((f"COM({year}) {number}", f"COM/{year}/{number}"))
    m = re.match(r"^5(\d{4})JC(\d+)$", celex, re.I)
    if m:
        year, number = m.group(1), str(int(m.group(2)))
        out.extend((f"JOIN({year}) {number}", f"JOIN/{year}/{number}"))
    return list(dict.fromkeys(out))


def title_from_text(text: str | None) -> str | None:
    lines = [re.sub(r"\s+", " ", x).strip() for x in (text or "").splitlines()]
    lines = [x for x in lines if x][:80]
    for i, line in enumerate(lines):
        if not _TITLE_HEAD.match(line):
            continue
        parts = [line.title() if line.isupper() else line]
        for nxt in lines[i + 1:i + 5]:
            if (re.match(r"^(?:Accompanying|Brussels,|\{|EN\s+EN$)", nxt, re.I)
                    or _PRINTED_ID.search(nxt)):
                break
            parts.append(nxt.title() if nxt.isupper() else nxt)
            if len(" — ".join(parts)) >= 180:
                break
        return " — ".join(parts)[:300]
    return None


class EUPreparatoryAdapter(EULegislationAdapter):
    source = "eu-preparatory"

    def __init__(self, *, celex=None, types: str | None = None, years: str | None = None,
                 page_size: int = 200, client=None) -> None:
        super().__init__(celex=celex, types=types or ",".join(DEFAULT_PREP_TYPES),
                         years=years, page_size=page_size, client=client)

    def _enumerate_query(self, since: str | None, offset: int) -> str:
        desc = "|".join(re.escape(t) for t in self.types if t in DEFAULT_PREP_TYPES)
        filters = []
        if since:
            filters.append(f'STR(?date) > "{since[:10]}"')
        if self.years:
            filters += [f'STR(?date) >= "{self.years[0]}-01-01"',
                        f'STR(?date) <= "{self.years[1]}-12-31"']
        where = " && ".join(filters)
        return f"""
PREFIX cdm: <{CDM}>
SELECT ?celex ?date (SAMPLE(?title0) AS ?title)
       (GROUP_CONCAT(DISTINCT STR(?proposal0); separator="|") AS ?proposalCelex)
       (GROUP_CONCAT(DISTINCT STR(?adopted0); separator="|") AS ?adopted)
       (GROUP_CONCAT(DISTINCT STR(?adoptedRelated0); separator="|") AS ?adoptedRelated) WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(REGEX(STR(?celex), "^5[0-9]{{4}}({desc})[0-9]+(?:\\\\([0-9]+\\\\))?$"))
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
              ?exp cdm:expression_uses_language ?lang . FILTER(STRENDS(STR(?lang), "/ENG"))
              ?exp cdm:expression_title ?title0 }}
  OPTIONAL {{ ?final cdm:resource_legal_adopts_resource_legal ?work ;
                     cdm:resource_legal_id_celex ?adopted0 . }}
  OPTIONAL {{ ?work cdm:work_related_to_work ?proposal .
              ?proposal cdm:resource_legal_id_celex ?proposal0 .
              ?final2 cdm:resource_legal_adopts_resource_legal ?proposal ;
                      cdm:resource_legal_id_celex ?adoptedRelated0 . }}
  {f'FILTER({where})' if where else ''}
}}
GROUP BY ?celex ?date
ORDER BY DESC(?date)
LIMIT {self.page_size} OFFSET {offset}
"""

    def _target_metadata(self, celex: str) -> dict:
        q = f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?title ?proposalCelex ?adopted ?adoptedRelated WHERE {{
  ?work cdm:resource_legal_id_celex ?id . FILTER(STR(?id) = "{celex}")
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
              ?exp cdm:expression_uses_language ?lang . FILTER(STRENDS(STR(?lang), "/ENG"))
              ?exp cdm:expression_title ?title }}
  OPTIONAL {{ ?final cdm:resource_legal_adopts_resource_legal ?work ;
                     cdm:resource_legal_id_celex ?adopted . }}
  OPTIONAL {{ ?work cdm:work_related_to_work ?proposal .
              ?proposal cdm:resource_legal_id_celex ?proposalCelex .
              OPTIONAL {{ ?final2 cdm:resource_legal_adopts_resource_legal ?proposal ;
                          cdm:resource_legal_id_celex ?adoptedRelated . }} }}
}}"""
        out = {"title": None, "adopted_as": [], "related_to": []}
        try:
            rows = self._sparql(q)
        except Exception:
            return out
        for row in rows:
            out["title"] = out["title"] or row.get("title")
            for key in ("adopted", "adoptedRelated"):
                if row.get(key) and row[key] not in out["adopted_as"]:
                    out["adopted_as"].append(row[key])
            if row.get("proposalCelex") and row["proposalCelex"] not in out["related_to"]:
                out["related_to"].append(row["proposalCelex"])
        return out

    def _discover_enumerate(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        offset = pages = 0
        seen: set[str] = set()
        while True:
            try:
                rows = self._sparql(self._enumerate_query(since, offset))
            except Exception:
                return
            if not rows:
                return
            grouped: OrderedDict[str, dict] = OrderedDict()
            for row in rows:
                celex = (row.get("celex") or "").strip().upper()
                if not celex:
                    continue
                g = grouped.setdefault(celex, {"titles": [], "adopted": [], "related": [], "date": row.get("date")})
                for key in ("title",):
                    if row.get(key) and row[key] not in g["titles"]:
                        g["titles"].append(row[key])
                for key in ("adopted", "adoptedRelated"):
                    for value in (row.get(key) or "").split("|"):
                        if value and value not in g["adopted"]:
                            g["adopted"].append(value)
                for value in (row.get("proposalCelex") or "").split("|"):
                    if value and value not in g["related"]:
                        g["related"].append(value)
            for celex, g in grouped.items():
                if celex in seen:
                    continue
                seen.add(celex)
                yield Stub(stable_id=celex,
                           landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                           raw_url=f"https://publications.europa.eu/resource/celex/{celex}",
                           hints={"watermark": g["date"], "title": (g["titles"] or [None])[0],
                                  "adopted_as": g["adopted"], "related_to": g["related"]})
            pages += 1
            offset += len(rows)
            if len(rows) < self.page_size or (max_pages is not None and pages >= max_pages):
                return

    def fetch(self, stub: Stub):
        if not any(k in stub.hints for k in ("title", "adopted_as", "related_to")):
            stub.hints.update(self._target_metadata(stub.stable_id))
        rec = super().fetch(stub)
        if rec is None:
            return None
        # Sector-5 works frequently have no Formex or rendered HTML even though an
        # official English PDF exists.  Do not leave impact assessments as metadata
        # stubs: fetch the PDF and use the shared text extractor (OCR can be queued by
        # the normal extraction metadata when a scan has no text layer).
        if not rec.text:
            try:
                # CELLAR content negotiation follows to the English manifestation's
                # actual item URL. EUR-Lex's /TXT/PDF front door often returns an empty
                # asynchronous HTTP 202 response to non-browser clients.
                pdf = self._client.get(
                    stub.raw_url or f"https://publications.europa.eu/resource/celex/{stub.stable_id}",
                    headers={"Accept": "application/pdf", "Accept-Language": "eng"})
                if getattr(pdf, "status_code", 200) < 400 and pdf.content.startswith(b"%PDF"):
                    from ..extraction import extract_bytes
                    extracted = extract_bytes(pdf.content, ext="pdf", mime="application/pdf")
                    rec.raw_bytes, rec.raw_ext, rec.text = pdf.content, "pdf", extracted.text
                    rec.extra.pop("metadata_only", None)
                    if not rec.text:
                        rec.extra["needs_ocr"] = True
            except Exception:  # best-effort: the metadata node and graph still have value
                pass
        rec.source = self.source
        rec.doc_type = DocType.PREPARATORY
        source_title = stub.hints.get("title")
        if source_title and (not rec.title or rec.title == rec.stable_id):
            rec.title = source_title
        if not rec.title or rec.title == rec.stable_id:
            rec.title = title_from_text(rec.text) or rec.stable_id
        aliases = printed_aliases(rec.stable_id, rec.title, rec.text)
        rec.extra.update({"celex": rec.stable_id, "preparatory_subtype": preparatory_subtype(rec.stable_id)[0],
                          "aliases": list(dict.fromkeys([*(rec.extra.get("aliases") or []), *aliases]))})
        for target in stub.hints.get("adopted_as") or ():
            rec.relations.append(TypedRelation(
                relationship_type=RelationshipType.ADOPTED_AS,
                raw_citation_string=f"Adopted as {target}", dst_id=target,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        for target in stub.hints.get("related_to") or ():
            rec.relations.append(TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string=f"Accompanies {target}", dst_id=target,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        return rec

"""France — DILA OPENDATA bulk seed (``fr-dila``), the no-auth offline seed.

Reads the ``echanges.dila.gouv.fr/OPENDATA`` archives from local disk — the *bulk seed*
whose live increments are the PISTE adapters (``fr-legislation``, ``fr-judilibre``) and
the Conseil d'État open-data platform (the ``us-caselaw`` / ``us-caselaw-bulk`` split).
One adapter across the funds, distinguished by ``fond``:

    LEGI (legislation) · CASS/CAPP (judicial case law) · JADE (administrative case law)
    · CONSTIT (Conseil constitutionnel) · CNIL (DPA deliberations)

``path`` is either a directory of extracted XML (walked recursively) or a ``.tar.gz``
archive (members read on demand). Because the seed carries the same identifiers (ECLI,
Légifrance CID/LEGIARTI) the live APIs and the extractor mint, importing it **resolves
the pending citations the corpus already holds** with no API call. Apply the DILA daily
deltas after the ``Freemium_*_global`` snapshot to be current; the delta timestamp is the
watermark. Licence: Licence Ouverte / Etalab 2.0 (attribution).
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.dila_xml import dila_root_kind, parse_dila_article, parse_dila_juri

# fund → (DocType for its jurisprudence, default court label). LEGI is legislation and
# handled separately (article-level).
_FUND_JURI = {
    "CASS": (DocType.JUDGMENT, "Cour de cassation"),
    "CAPP": (DocType.JUDGMENT, "Cour d'appel"),
    "INCA": (DocType.JUDGMENT, "Cour de cassation"),
    "JADE": (DocType.JUDGMENT, "Juridiction administrative"),
    "CONSTIT": (DocType.DECISION, "Conseil constitutionnel"),
    "CNIL": (DocType.DECISION, "CNIL"),
}


class FrDilaAdapter(BaseAdapter):
    source = "fr-dila"
    min_interval = 0.0  # local disk
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | None = None, fond: str = "CASS", **_kw) -> None:
        self.path = Path(path) if path else None
        self.fond = (fond or "CASS").upper()

    # -- discover ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is None or not self.path.exists():
            return
        if self.path.is_dir():
            for xml in sorted(self.path.rglob("*.xml")):
                yield Stub(stable_id=xml.stem, hints={"file": str(xml)})
        elif tarfile.is_tarfile(self.path):
            with tarfile.open(self.path, "r:*") as tar:
                for member in tar:
                    if member.isfile() and member.name.endswith(".xml"):
                        yield Stub(stable_id=Path(member.name).stem,
                                   hints={"tar": str(self.path), "member": member.name})

    # -- fetch -------------------------------------------------------------
    def _read(self, stub: Stub) -> bytes | None:
        if stub.hints.get("file"):
            return Path(stub.hints["file"]).read_bytes()
        if stub.hints.get("tar"):
            with tarfile.open(stub.hints["tar"], "r:*") as tar:
                f = tar.extractfile(stub.hints["member"])
                return f.read() if f else None
        return None

    def fetch(self, stub: Stub) -> Record | None:
        data = self._read(stub)
        if not data:
            return None
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return None
        kind = dila_root_kind(root)
        if kind == "article":
            return self._article_record(root, data, stub)
        if kind == "juri":
            return self._juri_record(root, data, stub)
        return None

    def _article_record(self, root: ET.Element, data: bytes, stub: Stub) -> Record | None:
        art = parse_dila_article(root)
        stable_id = art.art_id or f"fr/legi/{stub.stable_id}"
        title = f"{art.code_title} — Article {art.num}" if art.code_title and art.num else (
            f"Article {art.num}" if art.num else art.code_title)
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.LEGISLATION,
            title=title,
            decision_date=art.date_debut,
            language="fr",
            source_language="fr",
            landing_url=f"https://www.legifrance.gouv.fr/codes/article_lc/{stable_id}",
            raw_bytes=data,
            raw_ext="xml",
            text=art.text,
            segments=art.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "fond": "LEGI", "etat": art.etat, "code_cid": art.code_cid,
                "date_debut": art.date_debut.isoformat() if art.date_debut else None,
                "date_fin": art.date_fin.isoformat() if art.date_fin else None,
            }.items() if v},
        )

    def _juri_record(self, root: ET.Element, data: bytes, stub: Stub) -> Record | None:
        j = parse_dila_juri(root)
        doc_type, default_court = _FUND_JURI.get(self.fond, (DocType.JUDGMENT, None))
        ecli = j.ecli if (j.ecli and j.ecli.startswith("ECLI:")) else None
        stable_id = ecli or f"fr/{self.fond.lower()}/{j.doc_id or stub.stable_id}"
        return Record(
            source=self.source,
            stable_id=stable_id,
            ecli=ecli,
            doc_type=doc_type,
            title=j.title or ", ".join(x for x in (j.jurisdiction, j.number) if x) or ecli,
            court=j.jurisdiction or default_court,
            decision_date=j.date,
            language="fr",
            source_language="fr",
            landing_url=f"https://www.legifrance.gouv.fr/juri/id/{j.doc_id}" if j.doc_id else None,
            raw_bytes=data,
            raw_ext="xml",
            text=j.text,
            relations=j.relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "fond": self.fond, "number": j.number, "solution": j.solution,
                "formation": j.formation,
            }.items() if v},
        )

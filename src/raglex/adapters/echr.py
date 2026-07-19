"""European Court of Human Rights adapter — HUDOC (echr.coe.int).

Harvest ECtHR judgments by **either** their ECLI (``ECLI:CE:ECHR:2021:0525JUD005817013``)
**or** their application number (``58170/13``) — the two ways human-rights cases are cited.
The trick that unifies them: the ECHR ECLI *embeds* the application number
(``…JUD005817013`` → app no. 58170/13), so we resolve both through HUDOC's well-supported
``appno`` query, which returns the document ``itemid`` used to fetch the full text.

HUDOC API (no key):
- metadata: ``/app/query/results?query=contentsitename:ECHR AND <field>:"<value>"&select=…``
- full text: ``/app/conversion/docx/html/body?library=ECHR&id=<itemid>`` (HTML)

The stable_id is the ECLI when HUDOC gives one (the canonical, citable key), else a
fallback ``echr/<appno>`` slug. The application number(s) + itemid ride in ``extra``.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import assemble

BASE = "https://hudoc.echr.coe.int"
# Pull the rich HUDOC metadata, not just the bare keys — importance level, the
# conclusion/outcome, the Convention articles engaged, violations found, the respondent
# state, separate opinions, keywords (kpthesaurus), representation, etc. — and keep all
# of it in the document's meta_json (nothing the source gives is discarded).
_META_FIELDS = (
    "itemid", "ecli", "appno", "extractedappno", "docname", "doctype", "doctypebranch",
    "importance", "conclusion", "article", "violation", "nonviolation", "scl",
    "respondent", "separateopinion", "representedby", "issue", "kpthesaurus",
    "judgementdate", "kpdate", "originatingbody", "languageisocode", "rulesofcourt",
)
_SELECT = ",".join(_META_FIELDS)

# ECHR ECLI → application number: ECLI:CE:ECHR:YYYY:MMDD{JUD|DEC|…}{7-digit no}{2-digit yr}
_ECLI_APPNO = re.compile(r"ECHR:\d{4}:\d{4}[A-Z]{2,4}(?P<num>\d{5,7})(?P<yr>\d{2})$", re.IGNORECASE)
_APPNO = re.compile(r"^\d{1,5}/\d{2,4}$")
_ITEMID = re.compile(r"^00[0-9]-\d+$")

# the canonical English Court judgment among the many HUDOC docs for one case
# (judgment vs. legal summary "CLIN" vs. resolution "…RES…").
_JUDGMENT_DOCTYPES = {"HEJUD", "HFJUD", "GRANDCHAMBER", "CHAMBER", "COMMITTEE", "DECGRANDCHAMBER"}


def appno_from_ecli(ecli: str) -> str | None:
    """The application number an ECHR ECLI encodes, e.g.
    ``ECLI:CE:ECHR:2021:0525JUD005817013`` → ``58170/13``."""
    m = _ECLI_APPNO.search(ecli or "")
    if not m:
        return None
    return f"{int(m.group('num'))}/{m.group('yr')}"


def _hudoc_query(value_field: str, value: str) -> str:
    from urllib.parse import quote
    q = f'contentsitename:ECHR AND {value_field}:"{value}"'
    return (f"{BASE}/app/query/results?query={quote(q)}"
            f"&select={_SELECT}&sort={quote('kpdate Descending')}&start=0&length=20")


def _pick_judgment(rows: list[dict], appno: str | None) -> dict | None:
    """Choose the authoritative English Court judgment from HUDOC's result set."""
    def score(c: dict) -> tuple:
        name = (c.get("docname") or "").upper()
        return (
            (c.get("doctype") or "").upper() in _JUDGMENT_DOCTYPES,  # a judgment doctype
            name.startswith("CASE OF"),                              # the English judgment
            bool(c.get("ecli")),                                     # has an ECLI
            (c.get("languageisocode") or "") == "ENG",
        )
    cols = [r["columns"] for r in rows if r.get("columns")]
    cols = [c for c in cols if not (c.get("docname") or "").upper().startswith(("[", "INFORMATION NOTE"))]
    return max(cols, key=score) if cols else None


_PARA_NUM = re.compile(r"^(\d{1,4})\.\s")


def parse_body_html(html: bytes | str) -> tuple[str | None, list]:
    """ECHR judgment HTML → flat text + structural segments on the **numbered paragraphs**
    (``1.``, ``2.``, …), the citable units the Court pinpoints with ``§``. So "§ 35"
    deep-links to paragraph 35 (like CJEU ``§``/UK ``[n]`` paragraphs). Text before the
    first number is the header; the operative part ("FOR THESE REASONS") trails it."""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.extract()
    paras = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
             for p in (soup.body or soup).find_all(["p", "li"])]
    paras = [p for p in paras if p]
    if not paras:
        return None, []
    blocks: list[tuple[str, str, str]] = []
    label, kind, cur = "Header", "section", []
    for p in paras:
        m = _PARA_NUM.match(p)
        if m:
            if cur:
                blocks.append((label, kind, "\n".join(cur)))
            label, kind, cur = m.group(1), "paragraph", [p[m.end():].strip() or p]
        elif re.match(r"^FOR\s+THESE\s+REASONS", p, re.IGNORECASE) and len(p) < 80:
            if cur:
                blocks.append((label, kind, "\n".join(cur)))
            label, kind, cur = "Operative part", "section", []
        else:
            cur.append(p)
    if cur:
        blocks.append((label, kind, "\n".join(cur)))
    return assemble(blocks)


class ECHRAdapter(BaseAdapter):
    source = "echr"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _lookup(self, ident: str) -> dict | None:
        """Resolve an ECLI / app-number / itemid to a HUDOC judgment's metadata columns."""
        ident = ident.strip()
        if ident.lower().startswith("echr:"):  # an "echr:<case name>" candidate from the EHRR grammar
            ident = ident[5:].strip()
        appno = None
        if _ITEMID.match(ident):
            field, value = "itemid", ident
        elif (appno := appno_from_ecli(ident) or (ident if _APPNO.match(ident) else None)):
            field, value = "appno", appno
        elif ident.upper().startswith("ECLI:"):
            field, value = "ecli", ident
        else:
            # a case NAME ("Osman v. United Kingdom") — HUDOC has no EHRR-number index, but
            # it does index docname, so we resolve human-rights cases cited only by name/EHRR
            # via a name search. Fuzzier (inferred), but it's the only handle EHRR gives.
            field, value = "docname", re.sub(r"\bv\b\.?", "v.", ident).strip()
        try:
            resp = self._client.get(_hudoc_query(field, value))
        except FetchError:
            return None
        try:
            rows = json.loads(resp.content)["results"]
        except (ValueError, KeyError, TypeError):
            return None
        return _pick_judgment(rows, appno)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        for ident in self.ids:
            meta = self._lookup(ident)
            if not meta or not meta.get("itemid"):
                continue
            itemid = meta["itemid"]
            ecli = (meta.get("ecli") or "").strip()
            appnos = (meta.get("appno") or "").replace(";", ", ")
            first_app = (meta.get("appno") or "").split(";")[0]
            stable_id = ecli or (f"echr/{first_app}" if first_app else f"echr/{itemid}")
            # keep every non-empty HUDOC field so nothing the source gives is lost
            meta_kept = {k: v for k, v in meta.items() if v not in (None, "", [])}
            yield Stub(
                stable_id=stable_id,
                title=meta.get("docname"),
                court="echr",
                landing_url=f"{BASE}/?i={itemid}",
                raw_url=f"{BASE}/app/conversion/docx/html/body?library=ECHR&id={itemid}",
                hints={"itemid": itemid, "appno": appnos, "ecli": ecli,
                       "date": meta.get("judgementdate") or meta.get("kpdate"),
                       "meta": meta_kept},
            )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            resp = self._client.get(stub.raw_url)
        except FetchError as exc:
            # A transient failure (transport error, 5xx after retries) is NOT an
            # absence — returning None here files a routable reference onto the 90-day
            # harvest-miss list on a blip. Re-raise so the pipeline freezes the cursor
            # and retries; only a genuine 404-class failure counts as "nothing there".
            if exc.transient:
                raise
            return None
        raw = resp.content
        text, segments = parse_body_html(raw)
        if not text:
            # An empty HUDOC HTML conversion is far likelier a transient upstream hiccup
            # than a genuinely empty judgment — treat it as transient, not an absence.
            raise FetchError(f"empty HUDOC conversion for {stub.stable_id}", transient=True)
        date_raw = (stub.hints.get("date") or "")[:10]
        try:
            from datetime import date as _date
            dec_date = _date.fromisoformat("-".join(reversed(date_raw.split("/")))) if "/" in date_raw else None
        except ValueError:
            dec_date = None
        ecli = stub.hints.get("ecli") or (stub.stable_id if stub.stable_id.startswith("ECLI:") else None)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=ecli,
            doc_type=DocType.JUDGMENT,
            title=stub.title,
            court="echr",
            decision_date=dec_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            text=text,
            segments=segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={**(stub.hints.get("meta") or {}), "itemid": stub.hints.get("itemid"),
                   "appno": stub.hints.get("appno"), "format": "hudoc-html"},
        )

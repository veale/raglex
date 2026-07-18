"""Parse one row of the *BAILII parquet dump* into everything the importer needs.

This is the columnar sibling of :mod:`.bailii_html`. That module parses a **saved
BAILII page** — whose chrome carries the ``URL:`` line and the ``Cite as:`` header. A
bulk Scrapy crawl (the ``bailii_260505`` dataset) throws that chrome away: it keeps a
*cleaned* ``html_content`` fragment and lifts the header facts into **columns**
(``path``, ``title``, ``citation``, ``date``, ``court``, ``parties``). So the same facts
survive — just in a different shape — and this parser reads them from the row.

Three things this dump gives that the saved page did not carry in a machine form:

* the **parallel-report equivalence** — a case's ICLR report citations — survives as
  in-body ``iclr.co.uk/pubrefLookup/redirectTo?ref=2009+1+WLR+348`` links in the header
  block. Decoded (``[2009] 1 WLR 348``) these are exactly the report aliases a
  report-only citation resolves by, so they are minted as **self-aliases** of the case;
* the **ECLI** of an EU judgment sits in a ``<meta name="ECLI">`` — the identity RAGLex
  already holds CJEU/GC cases under (``ECLI:EU:C:…``), so the BAILII ``euecj/…`` slug and
  ``[YYYY] EUECJ …`` citation become aliases of the held ECLI document rather than a
  duplicate;
* an **ECtHR application number** (``- 22695/03``) sits in the title — the key RAGLex's
  ECHR corpus aliases its ``ECLI:CE:ECHR:…`` cases by, so a BAILII ``echr/…`` page can be
  matched to the case already held.

The body text + numbered-paragraph segmentation reuse :mod:`.bailii_html` unchanged (they
operate on any HTML fragment). Everything here is pure and row-at-a-time so it unit-tests
without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape as _unescape

from ..core.models import Segment
from .bailii_corpus import bailii_path_to_slug, clean_case_name
from .bailii_html import _body_text, _para_segments, _pdf_only_stub

# path juris-prefix → the RAGLex case-law source key. The prefix is the most reliable
# jurisdiction signal the dump carries (``/je/cases/…`` is Jersey no matter what the
# court token is). ``eu`` is handled separately (echr vs euecj split on the court token).
_JURIS_SOURCE: dict[str, str] = {
    "ew": "uk-caselaw", "uk": "uk-caselaw", "scot": "uk-caselaw", "nie": "uk-caselaw",
    "ie": "ie-caselaw",
    "je": "ci-caselaw", "gg": "ci-caselaw", "im": "ci-caselaw",
    "ky": "offshore-caselaw", "ae": "offshore-caselaw", "qa": "offshore-caselaw",
    "sh": "offshore-caselaw", "io": "offshore-caselaw", "bm": "offshore-caselaw",
    "gi": "offshore-caselaw",
    "sg": "sg-caselaw",
}

# the ICLR "buy this report" redirector: the ref query is the report citation, '+'-joined
_ICLR_REF = re.compile(r'iclr\.co\.uk/pubrefLookup/redirectTo\?ref=([^"\'&<>]+)', re.I)
# an EU ECLI declared in a <meta> tag (curia pages carry it with or without the ECLI: head)
_ECLI_META = re.compile(
    r'<meta[^>]+name=["\']ECLI["\'][^>]+content=["\']((?:ECLI:)?[A-Z]{2}:[A-Z]:\d{4}:\d+)["\']',
    re.I)
# an ECtHR application number in the title tail: "… - 22695/03 [2008] ECHR 1230"
_APPNO = re.compile(r"-\s*(\d{1,5}/\d{2})\b")
# the BAILII "<TITLE>" the older curia scrape sometimes doubles into the title column
_TITLE_JUNK = re.compile(r"^\s*(?:<title>|&lt;title&gt;)", re.I)

# BAILII page chrome that survives in the cleaned ``html_content`` fragment (the saved-page
# importer sliced this off between markers this dump has thrown away). Any body line
# containing one of these is dropped before the text is stored.
_BOILERPLATE = (
    "if you found bailii useful",
    "your donation will help us maintain",
    "thank you very much for your support",
    "this judgment text has undergone conversion",
    "this judgment is subject to final editorial corrections",
    "no contribution is too small",
)
# Markers of a page that carries NO transcript — only a pointer to a paywalled/PDF copy.
# A body reduced to (essentially) one of these is stored as a metadata stub, never as text.
_STUB_MARKERS = (
    "there is no available html version",
    "the document you wish to view is available to registered users",
    "a html version of this file is not available",
    "only available to download and view as",
)
# A redirect placeholder BAILII leaves when a case is re-filed under another citation
# ("Moved to: [2025] EWHC 1466 (KB)") — no judgment, just the forwarding citation.
_MOVED_TO = re.compile(r"^\s*\[?\s*moved to\b", re.I)


def _clean_body(text: str) -> str:
    """Drop BAILII chrome lines (donation banner, conversion notices) from a flattened
    body, so the stored transcript starts at the judgment, not the page furniture."""
    keep = []
    for para in text.split("\n\n"):
        low = para.strip().lower()
        if any(b in low for b in _BOILERPLATE):
            continue
        keep.append(para)
    return "\n\n".join(keep).strip()


def _is_stub_body(text: str) -> bool:
    """A body with no real transcript: a paywall/PDF pointer, a redirect placeholder, or
    just too short to be a judgment (a bare title + one boilerplate line)."""
    low = text.lower()
    if any(m in low for m in _STUB_MARKERS) or _MOVED_TO.match(text):
        return True
    # length is only a backstop — the real paywall/PDF stubs are marker-caught above, so
    # keep this floor low enough not to swallow a genuinely short decision.
    return len(text.strip()) < 120


@dataclass(slots=True)
class ParsedRow:
    """One parquet row reduced to the fields the importer stores. ``primary_id`` is the
    identity to key the document by (an ECLI when the dump states one — matching how the
    corpus already holds EU cases — else the neutral-citation slug)."""

    slug: str | None                       # bailii path → FCL-style court/year/num slug
    primary_id: str                        # ECLI if present, else slug
    source: str                            # uk-caselaw / ie-caselaw / echr / eu-cellar / …
    bailii_url: str | None
    title: str | None
    decision_date: date | None = None
    court_label: str | None = None
    ecli: str | None = None
    appno: str | None = None
    # citations that NAME THIS CASE — minted as self-aliases so report-only references
    # resolve. Neutral citation, ICLR parallel reports, EU/ECHR ids.
    self_citations: tuple[str, ...] = ()
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    pdf_only: bool = False
    pdf_url: str | None = None


def decode_iclr_ref(ref: str) -> str | None:
    """A pubrefLookup ``ref`` query → the report citation it encodes.

    >>> decode_iclr_ref("2009+1+WLR+348")
    '[2009] 1 WLR 348'
    >>> decode_iclr_ref("2013+WLR(D)+458")
    '[2013] WLR(D) 458'
    >>> decode_iclr_ref("2017+Bus+LR+1816")
    '[2017] Bus LR 1816'
    """
    parts = [p for p in _unescape(ref).replace("%20", "+").split("+") if p]
    if not parts or not re.fullmatch(r"(?:1[6-9]|20)\d{2}", parts[0]):
        return None
    rest = " ".join(parts[1:]).strip()
    return f"[{parts[0]}] {rest}" if rest else None


def _normalise_ecli(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().upper()
    return e if e.startswith("ECLI:") else f"ECLI:{e}"


def _parse_bailii_date(raw: str | None) -> date | None:
    """The ``date`` column is a free-text string ("22 June 2011", "09 October 2025").
    Parse the common day-month-year form; give up (None) on anything unusual."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
    if not m:
        return None
    months = {mn: i for i, mn in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"], start=1)}
    mon = months.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def _clean_title(raw: str | None) -> str | None:
    """The title column occasionally leaks a literal ``<TITLE>`` prefix from the curia
    scrape; strip it, then run the shared case-name cleaner to drop the trailing
    citation/date tail the way the saved-page importer does."""
    if not raw:
        return None
    t = _TITLE_JUNK.sub("", raw).strip()
    t = _unescape(re.sub(r"\s+", " ", t)).strip()
    cleaned = clean_case_name(t)
    return cleaned.title or (t or None)


def parse_parquet_row(row: dict) -> ParsedRow | None:
    """Parse one parquet row (a dict of the dump's columns) into a :class:`ParsedRow`,
    or None when the path isn't a recognisable ``/…/cases/…`` judgment (legislation,
    treaties and index pages have no case slug and are not this importer's job)."""
    path = row.get("path") or ""
    if "/cases/" not in path:
        return None
    slug = bailii_path_to_slug(path)
    if not slug:
        return None

    html = row.get("html_content") or ""
    juris = path.strip("/").split("/", 1)[0].lower()
    court_head = slug.split("/", 1)[0]

    # source: EU splits echr vs eu-cellar on the court token; everything else by juris.
    if juris == "eu":
        source = "echr" if court_head == "echr" else (
            "eu-cellar" if court_head == "euecj" else "eu-cellar")
    else:
        source = _JURIS_SOURCE.get(juris, "uk-caselaw")

    title = _clean_title(row.get("title"))
    ecli = _normalise_ecli(_ECLI_META.search(html).group(1) if _ECLI_META.search(html) else None)

    # self-citations: the neutral citation column + decoded ICLR parallel reports + ECLI.
    self_cites: list[str] = []
    col_cite = (row.get("citation") or "").strip()
    if col_cite:
        self_cites.append(col_cite)
    for ref in _ICLR_REF.findall(html):
        dec = decode_iclr_ref(ref)
        if dec:
            self_cites.append(dec)
    if ecli:
        self_cites.append(ecli)

    appno = None
    if source == "echr":
        am = _APPNO.search(row.get("title") or "")
        appno = am.group(1) if am else None

    # primary identity: hold EU cases under their ECLI (how the corpus already holds them),
    # everything else under the neutral-citation slug.
    primary_id = ecli if (ecli and source == "eu-cellar") else slug

    text = _clean_body(_body_text(html))
    _, pdf_url = _pdf_only_stub(html, text)
    # A stub is any page with no usable transcript: a PDF/paywall pointer, a "Moved to"
    # redirect, or a body too thin to be a judgment. Stored as metadata-only (has_text=0)
    # so its identity + report aliases still land, and a later fuller copy supersedes it.
    stub = _is_stub_body(text)
    bailii_url = f"https://www.bailii.org{path}" if path.startswith("/") else path

    return ParsedRow(
        slug=slug, primary_id=primary_id, source=source, bailii_url=bailii_url,
        title=title, decision_date=_parse_bailii_date(row.get("date")),
        court_label=(row.get("court") or "").strip() or None,
        ecli=ecli, appno=appno,
        self_citations=tuple(dict.fromkeys(c for c in self_cites if c)),
        text="" if stub else text,
        segments=[] if stub else _para_segments(text),
        pdf_only=stub, pdf_url=pdf_url,
    )

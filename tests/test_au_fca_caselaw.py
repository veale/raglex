"""Federal Court adapter — SERP parsing, neutral-cite identity from the URL segment,
incremental watermark stop, and judgment fetch. Network-free (fake fetcher)."""

from __future__ import annotations

from raglex.adapters.au_fca_caselaw import FCACaselawAdapter, fca_slug, parse_serp
from raglex.core.models import DocType


def _serp(rows) -> str:
    # rows: list of (url, title, "DD Mon YYYY")
    out = []
    for url, title, meta in rows:
        out.append(f'<a href="{url}" title="{title}">x</a>')
        out.append(f'<p class=meta>{meta}<span class="divide">·</span></p>')
    return "<html>" + "".join(out) + "</html>"


R1 = [
    ("https://www.judgments.fedcourt.gov.au/judgments/Judgments/fca/single/2026/2026fca0981",
     "Smith v Commonwealth", "15 Jul 2026"),
    ("https://www.judgments.fedcourt.gov.au/judgments/Judgments/fcafc/full/2026/2026fcafc0012",
     "Jones v Minister", "10 Jul 2026"),
    ("https://www.judgments.fedcourt.gov.au/judgments/Judgments/nfsc/2026/2026nfsc0003",
     "Norfolk Matter", "5 Jul 2026"),
]
R2 = [
    ("https://www.judgments.fedcourt.gov.au/judgments/Judgments/fca/single/2026/2026fca0900",
     "Older v Thing", "1 Jun 2026"),
]


def test_fca_slug_from_url_segment():
    assert fca_slug(".../Judgments/fca/single/2026/2026fca0981") == ("fca/2026/981", "[2026] FCA 981")
    assert fca_slug(".../Judgments/fcafc/full/2026/2026fcafc0012") == ("fcafc/2026/12", "[2026] FCAFC 12")
    assert fca_slug("no-segment-here") is None


def test_parse_serp_extracts_rows():
    rows = parse_serp(_serp(R1))
    assert [r["slug"] for r in rows] == ["fca/2026/981", "fcafc/2026/12", "nfsc/2026/3"]
    assert rows[0]["date"] == "2026-07-15"
    assert rows[2]["jurisdiction"] == "norfolk_island"
    assert rows[0]["jurisdiction"] == "commonwealth"


class _Page:
    def __init__(self, html):
        self.html = html


class _FakeFetcher:
    def __init__(self, serps, judgment_html="<html><body><p>Reasons for judgment.</p></body></html>"):
        self.serps = serps          # list of SERP html per page
        self.judgment_html = judgment_html
        self.name = "fake"

    def fetch(self, url, *, headers=None):
        if "search.html" in url:
            import re
            m = re.search(r"start_rank=(\d+)", url)
            page = (int(m.group(1)) - 1) // 20 if m else 0
            return _Page(self.serps[page] if page < len(self.serps) else "<html></html>")
        return _Page(self.judgment_html)

    def close(self):
        pass


def _adapter():
    return FCACaselawAdapter(fetcher=_FakeFetcher([_serp(R1), _serp(R2)]))


def test_discovery_dedups_and_paginates():
    stubs = list(_adapter().discover(None))
    assert [s.stable_id for s in stubs] == [
        "fca/2026/981", "fcafc/2026/12", "nfsc/2026/3", "fca/2026/900"]
    assert stubs[0].court == "fca" and stubs[1].court == "fcafc"


def test_incremental_stops_at_watermark():
    stubs = list(_adapter().discover("2026-07-01"))
    # everything on/before 1 Jul is cut → only the three July decisions from page 1
    assert {s.stable_id for s in stubs} == {"fca/2026/981", "fcafc/2026/12", "nfsc/2026/3"}


def test_fetch_builds_judgment_with_alias():
    ad = _adapter()
    stub = next(s for s in ad.discover(None) if s.stable_id == "fca/2026/981")
    rec = ad.fetch(stub)
    assert rec.doc_type is DocType.JUDGMENT
    assert rec.court == "fca"
    assert rec.decision_date.isoformat() == "2026-07-15"
    assert "Reasons for judgment" in rec.text
    assert rec.extra["neutral_citation"] == "[2026] FCA 981"
    assert "[2026] fca 981" in rec.extra["aliases"]
    assert rec.extra["jurisdiction"] == "commonwealth"


def test_pre_1976_date_rejected():
    rows = parse_serp(_serp([(
        "https://www.judgments.fedcourt.gov.au/judgments/Judgments/fca/single/0202/0202fca0001",
        "Bad Date", "20 Mar 202")]))
    assert rows[0]["date"] is None

"""Ireland case-law adapter — listing/detail parse, neutral-cite identity, lead-opinion
selection for multi-judgment cases, paragraph segmentation, and a fetch() round-trip.
Network-free (fixtures + a fake HTTP client)."""

from __future__ import annotations

from pathlib import Path

import pytest

from raglex.adapters.ie_caselaw import (
    IrishCaseLawAdapter,
    _clean_title,
    _court_bucket,
    _lead_case_uuids,
    filename_slug,
    ie_case_slug,
    parse_detail,
    parse_listing,
)
from raglex.core.models import DocType
from raglex.formats.ie_courts_pdf import (
    _coalesce_hanging_numbers,
    _paragraph_segments,
)

DATA = Path(__file__).parent / "data" / "ie_caselaw"


# ── identity ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cite,slug", [
    ("[2026] IEHC 509", "iehc/2026/509"),
    ("[2025] IESC 49", "iesc/2025/49"),
    ("[2024] IECA 194", "ieca/2024/194"),
    ("2026 IEHC 507", "iehc/2026/507"),          # unbracketed form the field sometimes uses
    ("[2026]  IESC  36", "iesc/2026/36"),        # stray double spaces
    ("Murphy v Start Mortgages [2025] IESC 49", "iesc/2025/49"),
    ("no citation here", None),
])
def test_ie_case_slug(cite, slug):
    assert ie_case_slug(cite) == slug


def test_slug_matches_grammar():
    """The slug the adapter mints must equal what the citation grammar mints, so a stored
    judgment is the node a "[2025] IESC 49" reference resolves onto."""
    from raglex.citations.extractor import extract_citations

    cands = [c.candidate_id for c in extract_citations("see [2025] IESC 49 at [42]")]
    assert ie_case_slug("[2025] IESC 49") in cands


@pytest.mark.parametrize("fn,slug", [
    ("2026_IEHC_509.pdf", "iehc/2026/509"),
    ("2025_IESC_49_Donnelly.pdf", "iesc/2025/49"),
    ("[2026] IECA 144.pdf", "ieca/2026/144"),
    # freeform / court-first names don't match the year-first grammar → no slug, so no
    # false pre-filter match; the detail page mints the real id (the point of the design).
    ("IESC 30.2026 BOI v Murray & anor.pdf", None),
    ("[IESC] 2025 21 People (DPP) v Flynn Final.pdf", None),
    ("some-scanned-thing.pdf", None),
])
def test_filename_slug_best_effort(fn, slug):
    assert filename_slug(fn) == slug


def test_clean_title_strips_dashed_v():
    assert _clean_title("Murphy -v- Start Mortgages DAC") == "Murphy v Start Mortgages DAC"
    assert _clean_title("The DPP -V- Babington") == "The DPP v Babington"


def test_court_bucket():
    assert _court_bucket("High Court", "iehc/2026/1") == "iehc"
    assert _court_bucket("Court of Appeal", None) == "ieca"
    assert _court_bucket(None, "iesc/2025/49") == "iesc"       # fall back to the slug head


# ── listing / detail parse ───────────────────────────────────────────────────
def test_parse_listing_fixture():
    rows = parse_listing((DATA / "listing.html").read_text(encoding="utf-8"))
    assert len(rows) > 50
    r0 = rows[0]
    assert r0["date"] == "21/07/2026"
    assert r0["title"].startswith("Murphy -v- Start Mortgages")
    assert r0["doc_uuid"] and r0["case_uuid"]
    assert r0["view_path"].startswith("/view/judgments/")


def test_parse_year_search_listing():
    """The year-search table (used for backfill) parses with the same shape, even though its
    row links use the /view/judgments-year/ path."""
    rows = parse_listing((DATA / "year_highcourt_2026.html").read_text(encoding="utf-8"))
    assert rows
    assert all(r["doc_uuid"] for r in rows)
    assert any("judgments-year" in r["view_path"] for r in rows)


def test_parse_detail_authoritative_metadata():
    det = parse_detail((DATA / "view_iehc509.html").read_text(encoding="utf-8"))
    assert det["citation"] == "[2026] IEHC 509"
    assert det["record_number"] == "2022 4507 P"
    assert det["court"] == "High Court"
    assert det["judge"] == "Cregan J."
    assert det["date"] == "22 July 2026"
    assert det["pdf_path"].startswith("/acc/alfresco/")


# ── multi-opinion grouping ───────────────────────────────────────────────────
def test_lead_case_uuids_deterministic():
    rows = [
        {"case_uuid": "case-A", "doc_uuid": "doc-9"},
        {"case_uuid": "case-A", "doc_uuid": "doc-3"},
        {"case_uuid": "case-A", "doc_uuid": "doc-7"},
        {"case_uuid": "case-B", "doc_uuid": "doc-5"},
    ]
    leads = _lead_case_uuids(rows)
    assert leads["case-A"] == "doc-3"      # lexicographically smallest, stable across runs
    assert leads["case-B"] == "doc-5"


# ── paragraph segmentation ───────────────────────────────────────────────────
def test_coalesce_hanging_numbers():
    raw = "1. \nMandamus is a discretionary remedy.\n2. \nIn making that assessment."
    out = _coalesce_hanging_numbers(raw)
    assert "1. Mandamus is a discretionary remedy." in out
    assert "2. In making that assessment." in out


def test_paragraph_segments_sequential_only():
    text = ("1. First paragraph mentions 1978 and a sub-point.\n"
            "2. Second paragraph.\n"
            "3. Third paragraph.\n")
    segs = _paragraph_segments(text)
    assert [s.label for s in segs] == ["[1]", "[2]", "[3]"]
    # a stray in-prose number that breaks the sequence must not open a paragraph
    text2 = "1. Alpha.\n99. Not the next number.\n2. Beta.\n"
    assert [s.label for s in _paragraph_segments(text2)] == ["[1]", "[2]"]


def test_paragraph_segments_offsets_into_text():
    text = "1. Alpha.\n2. Beta.\n"
    segs = _paragraph_segments(text)
    assert text[segs[0].char_start:segs[0].char_start + 2] == "1."
    assert text[segs[1].char_start:segs[1].char_start + 2] == "2."


# ── fetch() round-trip ───────────────────────────────────────────────────────
class _FakeClient:
    """Serves the vendored detail page for any /view/ URL and fixed bytes for the PDF."""

    def __init__(self, detail_html: str):
        self.detail_html = detail_html

    def get(self, url: str):
        class _R:
            pass
        r = _R()
        if "/acc/alfresco/" in url:
            r.content = b"%PDF-1.7 fake"
            r.text = ""
        else:
            r.text = self.detail_html
            r.content = b""
        return r


def _fetch_with_stub(monkeypatch, *, is_lead: bool):
    from raglex.adapters import ie_caselaw
    from raglex.core.models import Stub
    from raglex.formats.ie_courts_pdf import ParsedIrishJudgment
    from raglex.core.models import Segment

    # avoid needing a PDF engine: parse_ie_pdf returns our fixed paragraphs
    parsed = ParsedIrishJudgment(
        text="1. The first paragraph.\n2. The second paragraph.\n",
        segments=[Segment(label="[1]", char_start=0, char_end=23, kind="paragraph")],
    )
    monkeypatch.setattr(ie_caselaw, "parse_ie_pdf", lambda data: parsed)

    detail = (DATA / "view_iesc49_donnelly.html").read_text(encoding="utf-8")
    ad = IrishCaseLawAdapter(client=_FakeClient(detail))
    stub = Stub(
        stable_id="doc-uuid",
        title="G v Ireland",
        landing_url="https://ww2.courts.ie/view/judgments/c830c367/c8385051/x.pdf/pdf",
        hints={"is_lead": is_lead, "sibling_count": 3, "case_uuid": "c8385051",
               "doc_uuid": "c830c367", "acc_path": None, "listing_title": "G v Ireland"},
    )
    return ad.fetch(stub)


def test_fetch_lead_opinion_owns_bare_slug(monkeypatch):
    rec = _fetch_with_stub(monkeypatch, is_lead=True)
    assert rec is not None
    assert rec.stable_id == "iesc/2025/49"            # the resolution target
    assert rec.doc_type is DocType.JUDGMENT
    assert rec.court == "iesc"
    assert rec.extra["case_citation"] == "[2025] IESC 49"
    assert rec.extra["case_id"] == "iesc/2025/49"
    assert rec.extra["judge"] == "Donnelly J."
    assert rec.extra["is_lead_opinion"] is True
    assert rec.extra["sibling_opinions"] == 2
    assert rec.extra["record_number"]
    assert rec.segments and rec.segments[0].label == "[1]"


def test_fetch_non_lead_opinion_namespaced_by_judge(monkeypatch):
    rec = _fetch_with_stub(monkeypatch, is_lead=False)
    assert rec.stable_id == "iesc/2025/49/donnelly-j"   # can't collide with the lead
    assert rec.extra["case_id"] == "iesc/2025/49"       # still grouped to the case
    assert rec.extra["is_lead_opinion"] is False


# ── saved-HTML discovery ─────────────────────────────────────────────────────
def test_saved_html_discovery():
    ad = IrishCaseLawAdapter(path=str(DATA / "listing.html"))
    stubs = list(ad.discover(None))
    assert stubs
    # every stub carries the case/doc UUIDs and a normalised /view/judgments/ landing URL
    assert all(s.landing_url.startswith("https://ww2.courts.ie/view/judgments/") for s in stubs)
    assert all("doc_uuid" in s.hints for s in stubs)
    # lead flag is set; at least the single-opinion majority of rows are their own lead
    assert sum(1 for s in stubs if s.hints["is_lead"]) > 0
    # the stub id is the best-effort filename slug (so the pre-filter can skip a held case
    # before fetching) — a neutral-cite slug, or an ie-caselaw/<uuid> provisional fallback
    assert all(s.stable_id.startswith(("iesc/", "ieca/", "iehc/", "iedc/", "iecc/", "ie-caselaw/"))
               for s in stubs)
    # a lead opinion owns the bare slug; the id is never the naked doc UUID
    assert all(s.stable_id != s.hints["doc_uuid"] for s in stubs)


def test_keep_current_stops_at_watermark():
    """A keep-current pass over the landing page stops as soon as a row's upload date is at
    or below the watermark, so a routine run yields only the genuinely new rows."""
    ad = IrishCaseLawAdapter(path=str(DATA / "listing.html"))
    rows = parse_listing((DATA / "listing.html").read_text(encoding="utf-8"))
    uploads = sorted({r["uploaded"] for r in rows if r["uploaded"]}, reverse=True)
    # every listing row here was uploaded 22/07/2026; use an earlier watermark so all pass,
    # then a same-or-later watermark so none do.
    from raglex.adapters.ie_caselaw import _parse_date
    newest = _parse_date(uploads[0]).isoformat()

    # saved-HTML discovery ignores the watermark (it's a bulk import path); assert the
    # live-path helper directly instead.
    class _StaticClient:
        def __init__(self, html):
            self._html = html
        def get(self, url):
            class _R:
                pass
            r = _R()
            r.text = "" if "page=" in url else self._html  # only page 0 has rows
            return r

    ad2 = IrishCaseLawAdapter(client=_StaticClient((DATA / "listing.html").read_text(encoding="utf-8")))
    # watermark strictly before the upload date → all rows yielded
    older = "2026-07-01"
    assert list(ad2._discover_landing(older))
    # watermark at the newest upload date → nothing new, stops immediately
    assert list(ad2._discover_landing(newest)) == []

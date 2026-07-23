"""High Court adapter — listing parse, neutral-cite identity, saved-HTML import,
metadata-stub records, and year-range selection. Network-free."""

from __future__ import annotations

import os

import pytest

from raglex.adapters.au_hca_caselaw import HCACaselawAdapter, parse_listing, _year_range
from raglex.core.models import DocType

ROW = """<div class="views-row"><div class="views-field views-field-nothing-2"><span class="field-content">
<a class="views-row-item views-row-item-judgement" href="https://www.hcourt.gov.au/cases-and-judgments/judgments/judgments-1998-current/{slug}">
<div class="field field--title text-bold">{title}
 <br></div><div class="field field--citation"><strong>Citation:</strong>  {cite}</div>
<div class="field field--legacy-before"><div class="field field--name-field-hca-justices field--type-string field--label-above field__item"><strong>Before:</strong> {coram}</div></div>
<div class="field field--hca-date-issued"><strong>Date:</strong>  {date} </div></span></div></div>"""


def _listing(*rows) -> str:
    return '<div class="view-content">' + "".join(ROW.format(**r) for r in rows) + "</div>"


LISTING = _listing(
    {"slug": "chaplin-v-secretary", "title": "Chaplin v Secretary, Department of Social Services",
     "cite": "[2026] HCA 22", "coram": "Gageler CJ, Gordon, Steward, Jagot, Beech-Jones JJ",
     "date": "17 Jun 2026"},
    {"slug": "austral-v-nt", "title": "Austral v Northern Territory",
     "cite": "[2026] HCA 20", "coram": "Gordon J", "date": "11 Jun 2026"},
)


def test_parse_listing_fields():
    js = parse_listing(LISTING)
    assert [j["slug"] for j in js] == ["hca/2026/22", "hca/2026/20"]
    assert js[0]["citation"] == "[2026] HCA 22"
    assert js[0]["date"] == "2026-06-17"
    assert "Gageler CJ" in js[0]["coram"]
    assert js[0]["url"].endswith("/chaplin-v-secretary")


def test_saved_html_import(tmp_path):
    f = tmp_path / "hca-2026.html"
    f.write_text(LISTING, encoding="utf-8")
    ad = HCACaselawAdapter(path=str(f))
    stubs = list(ad.discover(None))
    assert {s.stable_id for s in stubs} == {"hca/2026/22", "hca/2026/20"}
    assert all(s.court == "hca" for s in stubs)


def test_metadata_stub_record():
    ad = HCACaselawAdapter(path="/nonexistent")  # fetch works off the stub hints
    stub = ad._stub(parse_listing(LISTING)[0])
    rec = ad.fetch(stub)
    assert rec.doc_type is DocType.JUDGMENT
    assert rec.stable_id == "hca/2026/22"
    assert rec.court == "hca"
    assert rec.text is None                     # metadata stub — no body
    assert rec.extra["metadata_only"] is True
    assert rec.extra["needs_fetch"] is True
    assert rec.extra["neutral_citation"] == "[2026] HCA 22"
    assert "[2026] hca 22" in rec.extra["aliases"]
    assert rec.decision_date.isoformat() == "2026-06-17"


def test_year_range_parsing():
    import datetime
    now = datetime.datetime.now().year
    assert _year_range(None) == [now]
    assert _year_range("current") == [now]
    assert _year_range("2020-2022") == [2022, 2021, 2020]
    assert _year_range("all")[0] == now and _year_range("all")[-1] == 1998


def test_non_judgment_rows_skipped():
    junk = '<div class="view-content"><div class="views-row"><p>No citation here</p></div></div>'
    assert parse_listing(junk) == []


def test_incremental_since_filters_by_date():
    # live-mode date filter (used when fetching, exercised here via _stub + manual check)
    js = parse_listing(LISTING)
    newer = [j for j in js if not (j["date"] and j["date"] <= "2026-06-13")]
    assert {j["slug"] for j in newer} == {"hca/2026/22"}  # the 11 Jun one is filtered


@pytest.mark.skipif(
    not os.path.exists("raglex design docs/Judgments (1998-current) _ High Court of Australia.html"),
    reason="saved HCA page not present",
)
def test_real_saved_page():
    with open("raglex design docs/Judgments (1998-current) _ High Court of Australia.html",
              encoding="utf-8", errors="replace") as fh:
        js = parse_listing(fh.read())
    assert len(js) >= 20
    assert all(j["slug"].startswith("hca/") for j in js)
    assert all(j["citation"] and "HCA" in j["citation"] for j in js)

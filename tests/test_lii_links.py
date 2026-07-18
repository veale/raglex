"""Constructing canonical LII URLs from neutral-citation slugs, and the fetch worklist.

The point of this module is that the URLs are built **locally** — the AustLII-family
naming scheme is deterministic enough that no resolver has to be hit — so the tests pin
the exact path shape each institute uses, including the two segments a bare citation does
not encode: BAILII's court division and AustLII's inner jurisdiction.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.citations.lii import LIILink, lii_links
from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.facade import Facade


def _urls(slug: str) -> list[str]:
    return [l.url for l in lii_links(slug)]


# ── the shared Sino grammar ─────────────────────────────────────────────────

def test_bailii_keeps_the_court_division_the_citation_omits():
    """"[2005] EWCA Civ 324" doesn't say where BAILII files it; the slug does."""
    assert _urls("ewca/civ/2005/324") == [
        "https://www.bailii.org/ew/cases/EWCA/Civ/2005/324.html"]
    assert _urls("ewhc/admin/2025/1466") == [
        "https://www.bailii.org/ew/cases/EWHC/Admin/2025/1466.html"]
    # UK-wide courts sit under /uk/, Irish under /ie/, Scottish under /scot/
    assert _urls("uksc/2023/50") == ["https://www.bailii.org/uk/cases/UKSC/2023/50.html"]
    assert _urls("iehc/2008/49") == ["https://www.bailii.org/ie/cases/IEHC/2008/49.html"]
    assert _urls("scotcs/1998/1") == ["https://www.bailii.org/scot/cases/ScotCS/1998/1.html"]


def test_nzlii_saflii_hklii_use_the_bare_court_folder():
    assert _urls("nzhc/2012/2551") == ["https://www.nzlii.org/nz/cases/NZHC/2012/2551.html"]
    assert _urls("nzsc/2007/103") == ["https://www.nzlii.org/nz/cases/NZSC/2007/103.html"]
    assert _urls("zasca/2011/73") == ["https://www.saflii.org/za/cases/ZASCA/2011/73.html"]
    assert _urls("zacc/2004/7") == ["https://www.saflii.org/za/cases/ZACC/2004/7.html"]
    assert _urls("hkcfa/2020/1") == ["https://www.hklii.hk/hk/cases/HKCFA/2020/1.html"]


def test_austlii_inserts_the_inner_jurisdiction():
    """AustLII nests a second jurisdiction segment (cth/nsw/qld…) that the citation omits;
    it is recoverable from the court code."""
    assert _urls("hca/2020/1") == [
        "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/HCA/2020/1.html"]
    assert _urls("nswsc/2012/115") == [
        "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/nsw/NSWSC/2012/115.html"]
    assert _urls("qsc/2019/5") == [
        "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/qld/QSC/2019/5.html"]


def test_canlii_uses_its_own_squashed_citation_scheme():
    """CanLII is Lexum, not Sino: the neutral citation squashes into one token."""
    assert _urls("scc/2011/10") == [
        "https://www.canlii.org/en/ca/scc/doc/2011/2011scc10/2011scc10.html"]
    assert _urls("bcsc/2000/394") == [
        "https://www.canlii.org/en/bc/bcsc/doc/2000/2000bcsc394/2000bcsc394.html"]


def test_paclii_is_marked_probable_not_derived():
    """Many Pacific courts never issued neutral citations, so PacLII's number is an
    LII-assigned database id — a constructed path is a good guess, not a guarantee."""
    links = lii_links("fjsc/2019/1")
    assert links and links[0].url == "https://www.paclii.org/fj/cases/FJSC/2019/1.html"
    assert links[0].certainty == "probable"


def test_undecodable_identifiers_yield_no_link():
    """A report series carries no court/number, and an unknown court has no folder — both
    must return nothing rather than a plausible-looking URL that 404s."""
    assert lii_links("[2008] 2 NZLR 321") == []
    assert lii_links("ECLI:EU:C:2019:203") == []
    assert lii_links("zzzz/2020/1") == []
    assert lii_links(None) == []
    assert lii_links("") == []


# ── the fetch worklist ──────────────────────────────────────────────────────

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def _hold(facade, stable_id, *, text="Some judgment text.", title="A v B",
          source="uk-caselaw", landing_url=None):
    with facade._open() as (cat, rs, ts):
        rec = Record(source=source, stable_id=stable_id, doc_type=DocType.JUDGMENT,
                     title=title, court=stable_id.split("/")[0],
                     decision_date=date(2020, 1, 1), language="en",
                     landing_url=landing_url,
                     raw_bytes=b"<x/>", raw_ext="html", text=text,
                     extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER)
        rec.ensure_payload_hash()
        text_path = str(ts.put(rec.payload_hash, text)) if text else None
        cat.upsert_document(rec, raw_path=None, text_path=text_path)
        cat.commit()


def test_textless_documents_get_links_and_a_round_trip_filename(facade):
    _hold(facade, "nzhc/2012/2551", text="", title="Smith v Jones", source="nz-caselaw")
    rows = facade.lii_link_targets(scope="textless")
    assert len(rows) == 1
    row = rows[0]
    assert row["url"] == "https://www.nzlii.org/nz/cases/NZHC/2012/2551.html"
    assert row["status"] == "held-no-text"
    # the filename is what lets a manually-saved page be mapped back to its document
    assert row["filename"] == "nzhc_2012_2551.html"


def test_documents_with_text_are_not_in_the_worklist(facade):
    _hold(facade, "nzhc/2012/2551", text="Full judgment text here.", source="nz-caselaw")
    assert facade.lii_link_targets(scope="textless") == []


def test_recorded_landing_url_is_preferred_over_a_constructed_one(facade):
    """BAILII filenames are case-sensitive in ways a lowercased slug cannot reproduce
    (``JLR98N013a``), so a URL the importer actually recorded wins."""
    exact = "https://www.bailii.org/je/cases/JLR/1998/JLR98N013a.html"
    _hold(facade, "jlr/1998/jlr98n013a", text="", source="ci-caselaw", landing_url=exact)
    links = facade.lii_links_for("jlr/1998/jlr98n013a")
    assert links[0]["url"] == exact
    assert links[0]["certainty"] == "recorded"

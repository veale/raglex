"""BAILII saved-page parsing + the zip importer's synthesis rules (§5b)."""

from __future__ import annotations

import zipfile
from datetime import date

import pytest

from raglex.adapters.bailii_html import parse_bailii_html, slug_from_filename
from raglex.config import Config
from raglex.facade import Facade


def _page(*, url="https://www.bailii.org/uk/cases/UKHL/2000/57.html",
          title="Turkington and Others v. Times Newspapers Limited [2000] UKHL 57 (2nd November, 2000)",
          cites="[2000] UKHL 57,\n[2001] 2 AC 277,\n[2000] 3 WLR 1670",
          body="<P><B>LORD BINGHAM</B></P><OL><LI VALUE=\"1.\">First numbered "
               "paragraph about the Data Protection Act 2018.</LI><LI VALUE=\"2.\">Second "
               "paragraph.</LI><LI VALUE=\"3.\">Third paragraph.</LI></OL>") -> bytes:
    html = f"""<HTML><HEAD><TITLE>{title}</TITLE>
<META http-equiv=Content-Type content="text/html; charset=iso-8859-1"></HEAD>
<BODY>
<TABLE><TR><TD><H1>United Kingdom House of Lords Decisions</H1></TD></TR>
<TR><TD><SMALL><B>You are here:</B> BAILII &gt;&gt; Databases &gt;&gt; {title}
<BR>URL: <I>{url}</I>
<BR>Cite as: {cites}
</SMALL><HR></TD></TR></TABLE>
<p>[<a href="/form/search_cases.html">New search</a>]</p>
<hr>
{body}
<P><HR><SMALL><B>BAILII:</B> <A HREF="/bailii/copyright.html">Copyright Policy</A></SMALL></P>
</BODY></HTML>"""
    return html.encode("iso-8859-1")


def test_parse_extracts_slug_title_date_citations_and_court():
    p = parse_bailii_html(_page())
    assert p.slug == "ukhl/2000/57"
    assert p.bailii_url == "https://www.bailii.org/uk/cases/UKHL/2000/57.html"
    assert p.title == "Turkington and Others v Times Newspapers Limited"
    assert p.decision_date == date(2000, 11, 2)
    assert p.court_label == "United Kingdom House of Lords Decisions"
    assert "[2001] 2 AC 277" in p.citations and "[2000] 3 WLR 1670" in p.citations


def test_parse_body_excludes_chrome_and_numbers_li_value_paragraphs():
    p = parse_bailii_html(_page())
    assert "You are here" not in p.text and "Copyright Policy" not in p.text
    assert "New search" not in p.text
    # <LI VALUE="2."> becomes a literal numbered paragraph, and ≥3 make segments
    assert "2. Second paragraph." in p.text
    assert [s.label for s in p.segments] == ["para 1", "para 2", "para 3"]
    for s in p.segments:
        assert p.text[s.char_start:s.char_end].startswith(f"{s.label.split()[1]}. ")


def test_slug_falls_back_to_the_saved_filename():
    p = parse_bailii_html(_page(url="https://example.org/nothing"),
                          filename="ew_cases_EWCA_Civ_2000_18 copy.html")
    assert p.slug == "ewca/civ/2000/18"
    assert slug_from_filename("uk_cases_UKHL_1986_10.html") == "ukhl/1986/10"
    assert slug_from_filename("notes.html") is None


def test_parse_rejects_non_bailii_bytes():
    assert parse_bailii_html(b"<html><body>hello</body></html>") is None


# -- the zip importer's synthesis with the held corpus -----------------------

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        topic_threshold=3.0, embed_provider="local-hashing", embed_model=None,
    ))


def _zip(tmp_path, pages: dict[str, bytes]) -> str:
    zp = tmp_path / "bailii.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, data in pages.items():
            zf.writestr(name, data)
    return str(zp)


def test_zip_import_creates_judgment_with_report_aliases(facade, tmp_path):
    res = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"a.html": _page()}))
    assert res["imported"] == 1 and res["unparseable"] == 0
    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document("ukhl/2000/57")
        assert doc["title"].startswith("Turkington")
        assert doc["decision_date"] == "2000-11-02"
        # the "Cite as:" report citation resolves to the imported judgment
        assert cat.find_document_id("[2001] 2 ac 277") == "ukhl/2000/57"
    # idempotent: the same zip again changes nothing
    again = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"a.html": _page()}))
    assert again.get("unchanged") == 1 and again["imported"] == 0


def test_zip_import_supersedes_plaintext_corpus_copy_but_keeps_it(facade, tmp_path):
    from raglex.core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes

    old = "short plain-text dump"
    ph = sha256_bytes(old.encode())
    with facade._open() as (cat, rs, ts):
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id="ukhl/2000/57", doc_type=DocType.JUDGMENT,
            title=None, court="ukhl", language="en", raw_bytes=old.encode(), raw_ext="txt",
            payload_hash=ph, text=old, extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
            extra={"imported": "bailii-corpus"},
        ), raw_path=None, text_path=str(ts.put(ph, old)))

    res = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"a.html": _page()}))
    assert res["superseded"] == 1
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document("ukhl/2000/57")
        meta = cat.document_meta("ukhl/2000/57")
        assert doc["version"] == 2 and doc["title"].startswith("Turkington")
        assert meta["imported"] == "bailii-html"
        # the replaced text survives as a secondary rendition
        assert any(a["payload_hash"] == ph for a in meta["alt_texts"])


def test_zip_import_attaches_secondary_to_authoritative_copy(facade, tmp_path):
    from raglex.core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes

    xml_text = "authoritative Find Case Law text " * 50
    ph = sha256_bytes(xml_text.encode())
    with facade._open() as (cat, rs, ts):
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id="ukhl/2000/57", doc_type=DocType.JUDGMENT,
            title="Turkington (FCL)", court="ukhl", language="en",
            raw_bytes=b"<akomaNtoso/>", raw_ext="xml", payload_hash=ph, text=xml_text,
            extracted_via=ExtractedVia.STRUCTURED, added_by=AddedBy.HARVEST,
        ), raw_path=None, text_path=str(ts.put(ph, xml_text)))

    res = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"a.html": _page()}))
    assert res["secondary"] == 1 and res["superseded"] == 0
    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document("ukhl/2000/57")
        meta = cat.document_meta("ukhl/2000/57")
        assert doc["payload_hash"] == ph          # the authoritative text is untouched
        assert meta["alt_texts"][0]["source"] == "bailii-html"
        # but the report-citation aliases are still minted
        assert cat.find_document_id("[2001] 2 ac 277") == "ukhl/2000/57"


def test_zip_import_aliases_bare_header_report_citation(facade, tmp_path):
    """An ICLR-sourced page (pre-neutral-citation) opens with the bare report citation
    the case was published at ('12 QBD 271'). It names THIS case: every year-form must
    alias to the imported slug, and the header mention must NOT become an outgoing edge."""
    page = _page(
        url="https://www.bailii.org/ew/cases/EWHC/QB/1884/1.html",
        title="Bradlaugh v Gossett [1884] EWHC 1 (QB) (9 February 1884)",
        cites="[1884] EWHC 1 (QB)",
        body="<DIV class=topline_right>12 QBD 271</DIV>"
             "<P>BRADLAUGH v. GOSSETT.</P>"
             "<P>The court gave judgment for the defendant.</P>"
             "<BR><BR><DIV class=topline_right><P>The permission for BAILII to publish "
             "the text of this judgment was granted by: ICLR</P></DIV>")
    res = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"a.html": page}))
    assert res["imported"] == 1
    with facade._open() as (cat, _rs, ts):
        # the citation heads the text (the ICLR permission block does not survive)
        assert ts.get(cat.get_document("ewhc/qb/1884/1")["payload_hash"]).startswith("12 QBD 271")
        for form in ("12 qbd 271", "(1884) 12 qbd 271", "[1884] 12 qbd 271"):
            assert cat.find_document_id(form) == "ewhc/qb/1884/1"
        assert not [e for e in cat.relations_for("ewhc/qb/1884/1")
                    if "qbd" in (e["raw_citation_string"] or "").lower()]

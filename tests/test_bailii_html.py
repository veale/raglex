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
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    ))


def _zip(tmp_path, pages: dict[str, bytes]) -> str:
    zp = tmp_path / "bailii.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, data in pages.items():
            zf.writestr(name, data)
    return str(zp)


def _folder(dir_path, pages: dict[str, bytes]):
    from pathlib import Path
    d = Path(dir_path)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in pages.items():
        (d / name).write_bytes(data)
    return d


def test_dir_import_walks_folder_recursively_ignores_non_html(facade, tmp_path):
    # the no-zip path: a Finder folder streamed up as loose files, nested, with cruft
    d = tmp_path / "bailii-folder"
    (d / "sub").mkdir(parents=True)
    (d / "a.html").write_bytes(_page())
    (d / "sub" / "b.html").write_bytes(_page(
        url="https://www.bailii.org/ew/cases/EWCA/Civ/2000/18.html",
        title="Banner Homes v Luff [2000] EWCA Civ 18 (28 January 2000)",
        cites="[2000] EWCA Civ 18"))
    (d / "notes.txt").write_text("not a judgment")
    res = facade.import_bailii_dir(dir_path=str(d))
    assert res["total"] == 2 and res["imported"] == 2  # the .txt is skipped
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("ukhl/2000/57") is not None
        assert cat.get_document("ewca/civ/2000/18") is not None


def _pdf_stub_page(*, url="https://www.bailii.org/ie/cases/IEHC/1984/1984_IEHC_87.html",
                   title="Pfizer Chemical Corporation v. The Commissioner of Valuation [1984] IEHC 87 (31 July 1984)",
                   pdf="/ie/cases/IEHC/1984/1984_IEHC_87.pdf") -> bytes:
    html = f"""<html><head><title>{title}</title></head><BODY>
<TABLE><TR><TD><H1>High Court of Ireland Decisions</H1></TD></TR>
<TR><TD><SMALL><B>You are here:</B> BAILII &gt;&gt; {title}
<BR>URL: <I>{url}</I>
<BR>Cite as: [1984] IEHC 87
</SMALL><HR></TD></TR></TABLE>
<p>[<a href="/form/search_cases.html">New search</a>] [<a href="{pdf}">Printable PDF version</a>]</p>
<hr>
<center><a href="http://www.bailii.org{pdf}">{title}</a>
<P>We are providing this link to the original pdf version of this report.</P></center>
<P><HR><SMALL><B>BAILII:</B> <A HREF="/bailii/copyright.html">Copyright Policy</A></SMALL></P>
</BODY></html>"""
    return html.encode("iso-8859-1")


def test_pdf_only_stub_imported_as_metadata_not_junk_text(facade, tmp_path):
    res = facade.import_bailii_dir(dir_path=str(_folder(tmp_path, {"a.html": _pdf_stub_page()})))
    assert res.get("pdf_stub") == 1 and res["imported"] == 0 and res["unparseable"] == 0
    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document("iehc/1984/87")
        assert doc is not None and doc["has_text"] == 0          # text-less stub, not junk
        assert doc["title"].startswith("Pfizer") and doc["decision_date"] == "1984-07-31"
        assert cat.document_meta("iehc/1984/87")["bailii_pdf_url"].endswith("1984_IEHC_87.pdf")
        # the case is resolvable by name even with no transcript
        assert cat.find_document_id("pfizer chemical corporation v the commissioner of valuation") == "iehc/1984/87"
    # the reader gets a clickable PDF link
    body = facade.document_body("iehc/1984/87")
    assert body["text"] is None and body["external_pdf"].endswith("1984_IEHC_87.pdf")


def test_pdf_stub_is_superseded_by_a_later_full_transcript(facade, tmp_path):
    facade.import_bailii_dir(dir_path=str(_folder(tmp_path / "a", {"a.html": _pdf_stub_page()})))
    # a later real transcript for the same slug replaces the stub
    full = _page(url="https://www.bailii.org/ie/cases/IEHC/1984/1984_IEHC_87.html",
                 title="Pfizer Chemical Corporation v Commissioner of Valuation [1984] IEHC 87 (31 July 1984)",
                 cites="[1984] IEHC 87",
                 body="<P>1. The plaintiff is a company. Judgment follows.</P>"
                      "<P>2. The valuation was excessive.</P><P>3. Appeal allowed.</P>")
    res = facade.import_bailii_dir(dir_path=str(_folder(tmp_path / "b", {"a.html": full})))
    assert res["superseded"] == 1
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document("iehc/1984/87")
        assert doc["has_text"] == 1
        assert "plaintiff is a company" in ts.get(doc["payload_hash"])


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


# -- Irish senior courts (BAILII /ie/ paths) ----------------------------------

def test_irish_composite_paths_and_filenames_reduce_to_citation_slug():
    from raglex.adapters.bailii_corpus import bailii_path_to_slug

    assert bailii_path_to_slug("/ie/cases/IEHC/2008/2008_IEHC_56.html") == "iehc/2008/56"
    assert bailii_path_to_slug("/ie/cases/IESC/2004/2004_IESC_1.html") == "iesc/2004/1"
    assert bailii_path_to_slug("/ie/cases/IEHC/1974/1.html") == "iehc/1974/1"
    assert slug_from_filename("ie_cases_IEHC_2008_2008_IEHC_56.html") == "iehc/2008/56"
    assert slug_from_filename("ie_cases_IEHC_1974_1.html") == "iehc/1974/1"
    # a saved year-INDEX page has no judgment slug
    assert slug_from_filename("ie_cases_IEHC_1974.html") is None


def test_zip_import_keys_irish_case_as_ie_caselaw(facade, tmp_path):
    page = _page(
        url="https://www.bailii.org/ie/cases/IEHC/2008/2008_IEHC_56.html",
        title="D. (E.) v. Refugee Appeals Tribunal & Anor [2008] IEHC 56 (22 February 2008)",
        cites="[2008] IEHC 56",
        body="<P>JUDGMENT of Mr. Justice Hedigan delivered on the 22nd day of February 2008.</P>"
             "<P>The applicant relied on the Refugee Act 1996 and Article 3 of the "
             "European Convention on Human Rights; see also A v B [2005] IEHC 182.</P>")
    res = facade.import_bailii_zip(zip_path=_zip(tmp_path, {"ie_cases_IEHC_2008_2008_IEHC_56.html": page}))
    assert res["imported"] == 1
    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document("iehc/2008/56")
        assert doc["source"] == "ie-caselaw" and doc["court"] == "iehc"
        assert doc["decision_date"] == "2008-02-22"
        # cited Irish case hangs as a resolvable iehc candidate (same keying scheme)
        assert any(e["dst_id"] == "iehc/2005/182" for e in cat.relations_for("iehc/2008/56"))


def test_export_retrieval_citations_batches_and_filters(facade):
    # seed pending report citations at varying mention frequencies
    from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                    ResolutionStatus, TypedRelation)
    from datetime import date
    with facade._open() as (cat, rs, ts):
        reports = {"[1987] AC 460": 5, "[2020] 1 WLR 100": 8, "[1974] ECR 837": 9,
                   "[2016] EHRR 12": 4, "Smith v Jones": 7, "[1999] Cr App R 1": 2}
        di = 0
        for raw, cnt in reports.items():
            for _ in range(cnt):
                sid = f"c/{di}"; di += 1
                rec = Record(source="x", stable_id=sid, doc_type=DocType.JUDGMENT,
                             decision_date=date(2024, 1, 1), text="t", raw_bytes=b"t",
                             extracted_via=ExtractedVia.STRUCTURED,
                             relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                                        raw_citation_string=raw, dst_id=None,
                                        resolution_status=ResolutionStatus.PENDING)])
                rec.ensure_payload_hash()
                cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash + sid, "t")))

    res = facade.export_retrieval_citations(min_citing=2, batch_size=2)
    flat = [i["citation"] for b in res["batches"] for i in b["items"]]
    # ranked by mentions; ECR/EHRR (own sources) and bare names excluded
    assert flat == ["[2020] 1 WLR 100", "[1987] AC 460", "[1999] Cr App R 1"]
    assert all(b["count"] <= 2 for b in res["batches"]) and res["batch_count"] == 2
    assert res["batches"][0]["text"] == "[2020] 1 WLR 100\n[1987] AC 460"  # newline-joined
    # names included on request
    withn = facade.export_retrieval_citations(min_citing=2, include_names=True)
    assert "Smith v Jones" in [i["citation"] for b in withn["batches"] for i in b["items"]]


def test_export_retrieval_citations_jurisdiction_filter(facade):
    from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                    ResolutionStatus, TypedRelation)
    from datetime import date
    with facade._open() as (cat, rs, ts):
        reports = {"[1987] AC 460": 5, "[1990] ILRM 12": 5, "(1990) 70 DLR (4th) 385": 5}
        di = 0
        for raw, cnt in reports.items():
            for _ in range(cnt):
                sid = f"j/{di}"; di += 1
                rec = Record(source="x", stable_id=sid, doc_type=DocType.JUDGMENT,
                             decision_date=date(2024, 1, 1), text="t", raw_bytes=b"t",
                             extracted_via=ExtractedVia.STRUCTURED,
                             relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                                        raw_citation_string=raw, dst_id=None,
                                        resolution_status=ResolutionStatus.PENDING)])
                rec.ensure_payload_hash()
                cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash + sid, "t")))

    # UK-only: the Irish (ILRM) and Canadian (DLR) reports are filtered out —
    # a Westlaw UK / Lexis+ UK run can't retrieve them anyway
    uk = facade.export_retrieval_citations(min_citing=2, jurisdictions=("uk",))
    flat = [i["citation"] for b in uk["batches"] for i in b["items"]]
    assert "[1987] AC 460" in flat
    assert all("ILRM" not in c and "DLR" not in c for c in flat)
    # each exported item says which bucket it's in
    assert all(i["jurisdiction"] == "uk" for b in uk["batches"] for i in b["items"])
    # asking for Ireland gets the ILRM row
    ie = facade.export_retrieval_citations(min_citing=2, jurisdictions=("ie",))
    assert [i["citation"] for b in ie["batches"] for i in b["items"]] == ["[1990] ILRM 12"]


def test_export_jurisdiction_filter_uses_candidate_for_neutral_citations(facade):
    # An Irish NEUTRAL citation ("[2019] IESC 4") is not a report series, so its
    # jurisdiction must come from the candidate's court token (iesc → Irish) — else it
    # defaults to "uk" and leaks into a UK-only Westlaw batch (the reported bug).
    from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                    ResolutionStatus, TypedRelation)
    from datetime import date
    with facade._open() as (cat, rs, ts):
        refs = {("[2019] IESC 4", "iesc/2019/4"): 4, ("[1987] AC 460", None): 4}
        di = 0
        for (raw, cand), cnt in refs.items():
            for _ in range(cnt):
                sid = f"n/{di}"; di += 1
                rec = Record(source="x", stable_id=sid, doc_type=DocType.JUDGMENT,
                             decision_date=date(2024, 1, 1), text="t", raw_bytes=b"t",
                             extracted_via=ExtractedVia.STRUCTURED,
                             relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                                        raw_citation_string=raw, dst_id=cand,
                                        resolution_status=ResolutionStatus.PENDING)])
                rec.ensure_payload_hash()
                cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash + sid, "t")))

    uk = facade.export_retrieval_citations(min_citing=2, jurisdictions=("uk",))
    flat = [i["citation"] for b in uk["batches"] for i in b["items"]]
    assert "[1987] AC 460" in flat and "[2019] IESC 4" not in flat  # Irish NC excluded
    ie = facade.export_retrieval_citations(min_citing=2, jurisdictions=("ie",))
    assert "[2019] IESC 4" in [i["citation"] for b in ie["batches"] for i in b["items"]]

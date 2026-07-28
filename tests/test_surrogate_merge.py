"""Duplicate-identity collapse: a case held under a Westlaw surrogate AND its real
citation, plus the alias hop that made the pair invisible to ``lookup``.

The Westlaw importer only ever checked forwards — "is this case already held?" — so a
Westlaw RTF imported before the same case's BAILII/FCL copy stayed a permanent duplicate
(Donoghue v Stevenson, held as both ``westlaw:1932-a-c-562`` and ``ukhl/1932/100``).
These tests pin the reverse direction at import time, the retrospective repair, and the
lookup fallback that reaches the alias table when the grammars yield no candidate id.
"""

from __future__ import annotations

import zipfile

import pytest

from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.core.text import fold
from raglex.facade import Facade

REPORT = "[2001] 2 AC 277"
SLUG = "ukhl/2000/57"


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def _page(*, url=f"https://www.bailii.org/uk/cases/UKHL/2000/57.html",
          title="Turkington v Times Newspapers Limited [2000] UKHL 57",
          cites=f"[2000] UKHL 57,\n{REPORT}") -> bytes:
    html = f"""<HTML><HEAD><TITLE>{title}</TITLE></HEAD>
<BODY>
<TABLE><TR><TD><H1>United Kingdom House of Lords Decisions</H1></TD></TR>
<TR><TD><SMALL><B>You are here:</B> BAILII &gt;&gt; Databases &gt;&gt; {title}
<BR>URL: <I>{url}</I>
<BR>Cite as: {cites}
</SMALL><HR></TD></TR></TABLE>
<p>[<a href="/form/search_cases.html">New search</a>]</p>
<hr>
<P><B>LORD BINGHAM</B></P><OL><LI VALUE="1.">First numbered paragraph.</LI>
<LI VALUE="2.">Second paragraph.</LI></OL>
<P><HR><SMALL><B>BAILII:</B> Copyright Policy</SMALL></P>
</BODY></HTML>"""
    return html.encode("iso-8859-1")


def _westlaw_doc(facade, stable_id: str, *, report: str = REPORT, alias: bool = True,
                 text: str = "The Westlaw rendition, with headnote.") -> None:
    """A Westlaw RTF import as it sits in the corpus: a surrogate id, the report citation
    in its meta, and (as the importer mints) that citation aliased to it."""
    with facade._open() as (cat, _rs, ts):
        rec = Record(
            source="uk-caselaw", stable_id=stable_id, doc_type=DocType.JUDGMENT,
            title="Turkington v Times Newspapers", text=text, raw_bytes=b"{\\rtf1}",
            raw_ext="rtf", extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
            extra={"imported": "westlaw-rtf", "westlaw": {"report_citations": [report]}})
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))
        if alias:
            cat.put_alias(fold(report), stable_id, source="westlaw-report-alias")
        cat.conn.execute(
            "INSERT INTO relations (src_id, dst_id, candidate_id, relationship_type, "
            "resolution_status, extracted_via) VALUES (?,?,?,?,?,?)",
            ("some/citing/doc", stable_id, stable_id, "mentions", "resolved", "grammar"))
        cat.commit()


def _held(facade, stable_id: str, *, title="Turkington v Times Newspapers") -> None:
    with facade._open() as (cat, _rs, ts):
        rec = Record(source="uk-caselaw", stable_id=stable_id, doc_type=DocType.JUDGMENT,
                     title=title, text="The BAILII transcript.", raw_bytes=b"<html/>",
                     raw_ext="html", extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        cat.commit()


# -- lookup reaches the alias table ------------------------------------------

def test_lookup_resolves_a_law_report_citation_held_only_as_an_alias(facade):
    _held(facade, SLUG)
    with facade._open() as (cat, _rs, _ts):
        cat.put_alias(fold(REPORT), SLUG, source="bailii-report-alias")
        cat.put_alias(fold("Turkington v Times Newspapers"), SLUG, source="bailii-name:full")
        cat.commit()

    # a classic law report: no grammar candidate, so this used to come back "not held"
    r = facade.lookup(citation=REPORT, autofetch=False, cited_by=False, similar=False)
    assert r["held"] is True and r["stable_id"] == SLUG
    # …the dotted form of the same report converges on the de-dotted alias key
    r = facade.lookup(citation="[2001] 2 A.C. 277", autofetch=False, cited_by=False,
                      similar=False)
    assert r["held"] is True and r["stable_id"] == SLUG
    # …and a case cited by name
    r = facade.lookup(citation="Turkington v Times Newspapers", autofetch=False,
                      cited_by=False, similar=False)
    assert r["held"] is True and r["stable_id"] == SLUG


def test_lookup_still_reports_a_genuinely_unheld_citation(facade):
    _held(facade, SLUG)
    r = facade.lookup(citation="[1932] AC 562", autofetch=False, cited_by=False,
                      similar=False)
    assert r["held"] is False


# -- import time: the incoming copy absorbs the held surrogate ----------------

def test_bailii_page_import_absorbs_a_westlaw_surrogate_of_the_same_case(facade, tmp_path):
    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)
    d = tmp_path / "pages"
    d.mkdir()
    (d / "uk_cases_UKHL_2000_57.html").write_bytes(_page())

    res = facade.import_bailii_dir(dir_path=str(d))

    assert res["merged_surrogate"] == 1
    # the held Westlaw text is the better rendition, so the page attaches as a secondary
    assert res["imported"] == 0 and res["secondary"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is None        # folded away, not duplicated
        doc = cat.get_document(SLUG)
        assert doc is not None
        meta = cat.document_meta(SLUG)
        assert meta.get("westlaw")                        # Westlaw metadata carried over
        assert any(a["source"] == "bailii-html" or a.get("payload_hash")
                   for a in meta.get("alt_texts", []))
        assert cat.get_alias(fold(REPORT)) == SLUG        # report alias re-pointed
        assert cat.get_alias(fold(surrogate)) == SLUG     # old id still resolvable
        rel = cat.conn.execute(
            "SELECT dst_id, candidate_id FROM relations WHERE src_id='some/citing/doc'"
        ).fetchone()
        assert rel["dst_id"] == SLUG and rel["candidate_id"] == SLUG


def test_import_leaves_an_unrelated_surrogate_alone(facade, tmp_path):
    # the surrogate's report citation is NOT one of this page's "Cite as" citations
    surrogate = "westlaw:1932-a-c-562"
    _westlaw_doc(facade, surrogate, report="[1932] A.C. 562")
    d = tmp_path / "pages"
    d.mkdir()
    (d / "uk_cases_UKHL_2000_57.html").write_bytes(_page())

    res = facade.import_bailii_dir(dir_path=str(d))

    assert res["merged_surrogate"] == 0 and res["imported"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is not None


def test_bailii_zip_import_absorbs_the_surrogate_too(facade, tmp_path):
    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)
    zp = tmp_path / "b.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("uk_cases_UKHL_2000_57.html", _page())

    res = facade.import_bailii_zip(zip_path=str(zp))

    assert res["merged_surrogate"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is None and cat.get_document(SLUG) is not None


def test_parquet_row_ingest_absorbs_a_westlaw_surrogate(facade):
    from datetime import date

    from raglex.adapters.bailii_parquet import ParsedRow

    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)
    parsed = ParsedRow(
        slug=SLUG, primary_id=SLUG, source="uk-caselaw",
        bailii_url="https://www.bailii.org/uk/cases/UKHL/2000/57.html",
        title="Turkington v Times Newspapers", decision_date=date(2000, 11, 2),
        self_citations=("[2000] UKHL 57", REPORT),
        text="1. First paragraph.\n\n2. Second paragraph.", segments=[])
    st: dict = {"total": 0, "imported": 0, "superseded": 0, "secondary": 0,
                "enriched": 0, "stub": 0, "skipped": 0, "aliases": 0}
    with facade._open() as (cat, rs, ts):
        facade._ingest_bailii_row(cat, rs, ts, parsed=parsed, raw_bytes=b"<div>x</div>",
                                  st=st, files=[])
        cat.commit()

    assert st["merged_surrogate"] == 1 and st["imported"] == 0
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is None
        assert cat.get_document(SLUG) is not None
        assert cat.get_alias(fold(REPORT)) == SLUG


# -- retrospective repair for the pairs already in the corpus ----------------

def test_refix_merges_a_report_slug_surrogate_into_its_later_neutral_twin(facade):
    # exactly the Donoghue shape: the Westlaw copy was imported first (so its merge check
    # found nothing), the neutral-cite copy arrived later and re-pointed the report alias.
    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)
    _held(facade, SLUG)
    with facade._open() as (cat, _rs, _ts):
        cat.put_alias(fold(REPORT), SLUG, source="bailii-report-alias")
        cat.commit()

    dry = facade.refix_westlaw_imports(apply=False)
    assert dry["scanned"] == 1 and dry["merged"] == 0          # dry run changes nothing
    assert dry["changes"] == [{"old": surrogate, "new": SLUG, "kind": "merged"}]
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is not None

    res = facade.refix_westlaw_imports(apply=True)
    assert res["merged"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is None
        assert cat.get_document(SLUG) is not None
        assert cat.get_alias(fold(surrogate)) == SLUG          # old id still resolvable
        rel = cat.conn.execute(
            "SELECT dst_id, candidate_id FROM relations WHERE src_id='some/citing/doc'"
        ).fetchone()
        assert rel["dst_id"] == SLUG and rel["candidate_id"] == SLUG
    # and the case is now findable by the report citation that named both copies
    assert facade.lookup(citation=REPORT, autofetch=False, cited_by=False,
                         similar=False)["stable_id"] == SLUG


def test_refix_never_yields_a_report_slug_to_another_surrogate(facade):
    # a report-slug id is already a reasonable key: it may only be folded into a REAL
    # citation-derived identity, never shuffled onto another westlaw: surrogate.
    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)
    _held(facade, "westlaw:deadbeefdeadbeef")
    with facade._open() as (cat, _rs, _ts):
        cat.put_alias(fold(REPORT), "westlaw:deadbeefdeadbeef", source="westlaw-report-alias")
        cat.commit()

    res = facade.refix_westlaw_imports(apply=False)
    assert res["changes"] == [] and res["unchanged"] == 1


def test_refix_leaves_a_surrogate_with_no_twin_alone(facade):
    surrogate = "westlaw:2001-2-ac-277"
    _westlaw_doc(facade, surrogate)

    res = facade.refix_westlaw_imports(apply=False)
    assert res["changes"] == [] and res["unchanged"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(surrogate) is not None

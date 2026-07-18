"""BAILII parquet-dump parsing + the ``import_bailii_parquet`` synthesis rules.

Two things this suite guards that the saved-page path (``test_bailii_html``) does not:

* the columnar parser — slugging offshore/Crown-Dependency paths, decoding ICLR
  parallel-report links, reconciling an EU case to its ECLI and an ECtHR case to its
  application number, and stripping BAILII chrome / detecting text-less stubs;
* the ingest — that a new case's **text is actually persisted** (the payload_hash /
  TextStore trap that silently dropped bulk-import text before), and that a case already
  held under its ECLI / application number is **enriched, not duplicated**.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.bailii_parquet import (
    ParsedRow, decode_iclr_ref, parse_parquet_row,
)
from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.facade import Facade


# ── pure parser ─────────────────────────────────────────────────────────────

def test_decode_iclr_ref_forms():
    assert decode_iclr_ref("2009+1+WLR+348") == "[2009] 1 WLR 348"
    assert decode_iclr_ref("2013+WLR(D)+458") == "[2013] WLR(D) 458"
    assert decode_iclr_ref("2017+Bus+LR+1816") == "[2017] Bus LR 1816"
    assert decode_iclr_ref("not-a-year+x") is None
    assert decode_iclr_ref("2015") is None  # year only, no report


def _row(path, *, title="", citation="", date_="", court="", db="", html=""):
    return {"path": path, "title": title, "citation": citation, "date": date_,
            "court": court, "database_name": db, "html_content": html}


def test_parses_uk_case_with_iclr_report_alias():
    html = ('<div><a href="http://iclr.co.uk/pubrefLookup/redirectTo?ref=2009+1+WLR+348">'
            '[2009] 1 WLR 348</a> [<hr><p>1. First paragraph.</p><p>2. Second paragraph.'
            '</p><p>3. Third paragraph.</p></div>')
    p = parse_parquet_row(_row("/uk/cases/UKHL/2009/6.html", title="ZT v SSHD",
                               citation="[2009] UKHL 6", date_="4 February 2009", html=html))
    assert p is not None
    assert p.slug == "ukhl/2009/6"
    assert p.primary_id == "ukhl/2009/6"       # no ECLI → keyed by slug
    assert p.source == "uk-caselaw"
    assert p.decision_date == date(2009, 2, 4)
    # both the neutral citation and the decoded ICLR parallel report are self-citations
    assert "[2009] UKHL 6" in p.self_citations
    assert "[2009] 1 WLR 348" in p.self_citations


def test_eu_case_keyed_by_ecli():
    html = ('<meta name="ECLI" content="ECLI:EU:C:2019:203"><div><p>ARRÊT DE LA COUR</p>'
            '<p>1. Un.</p><p>2. Deux.</p><p>3. Trois.</p></div>')
    p = parse_parquet_row(_row("/eu/cases/EUECJ/2019/C55717.html",
                               title="Y.Z. v Staatssecretaris [2019] EUECJ C-557/17",
                               citation="[2019] EUECJ C-557/17", db="EUECJ", html=html))
    assert p.slug == "euecj/2019/c55717"
    assert p.ecli == "ECLI:EU:C:2019:203"
    assert p.primary_id == "ECLI:EU:C:2019:203"   # held under the ECLI, not the slug
    assert p.source == "eu-cellar"
    assert "ECLI:EU:C:2019:203" in p.self_citations


def test_echr_case_extracts_application_number():
    p = parse_parquet_row(_row("/eu/cases/ECHR/2008/1230.html",
                               title="DEMSKI v. POLAND - 22695/03",
                               citation="[2008] ECHR 1230", db="ECHR",
                               html="<div><p>1. One.</p><p>2. Two.</p><p>3. Three.</p></div>"))
    assert p.slug == "echr/2008/1230"
    assert p.source == "echr"
    assert p.appno == "22695/03"


def test_offshore_and_crown_dependency_paths_slug_and_route():
    je = parse_parquet_row(_row("/je/cases/UR/2023/2023_020.html", title="XY v Jersey Police",
                                html="<div><p>1. a</p><p>2. b</p><p>3. c</p></div>"))
    assert je.slug == "ur/2023/2023_020" and je.source == "ci-caselaw"
    ky = parse_parquet_row(_row("/ky/cases/GCCI/FSD/2021/16.html", title="X v Y",
                                html="<div><p>1. a</p><p>2. b</p><p>3. c</p></div>"))
    assert ky.slug == "gcci/fsd/2021/16" and ky.source == "offshore-caselaw"


def test_moved_to_redirect_is_a_stub():
    p = parse_parquet_row(_row("/ew/cases/EWHC/Admin/2025/1466.html", db="Admin",
                               html="<div>[<hr>Moved to: [2025] EWHC 1466 (KB)</div>"))
    assert p.pdf_only is True and p.text == ""


def test_donation_banner_is_stripped_from_body():
    html = ("<div><p>If you found BAILII useful today, could you please make a "
            "contribution?</p><p>1. The real first paragraph of the judgment begins here "
            "and is long enough to be a judgment body rather than a thin stub, so it is "
            "kept.</p><p>2. Second paragraph.</p><p>3. Third paragraph.</p></div>")
    p = parse_parquet_row(_row("/ae/cases/DIFC/2017/arb_005.html", db="DIFC", html=html))
    assert "found BAILII useful" not in p.text
    assert "real first paragraph" in p.text


def test_non_case_path_returns_none():
    assert parse_parquet_row(_row("/uk/legis/num_act/2024/ukpga_202420_en_1.html")) is None


# ── ingest synthesis ─────────────────────────────────────────────────────────

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def _ingest(facade, parsed: ParsedRow, raw="<div>x</div>"):
    st = {"total": 0, "imported": 0, "superseded": 0, "secondary": 0, "enriched": 0,
          "stub": 0, "skipped": 0, "aliases": 0}
    files = []
    with facade._open() as (cat, rs, ts):
        facade._ingest_bailii_row(cat, rs, ts, parsed=parsed, raw_bytes=raw.encode(),
                                  st=st, files=files)
        cat.commit()
    return st, files


def _uk_parsed(**over) -> ParsedRow:
    defaults = dict(
        slug="ukhl/2009/6", primary_id="ukhl/2009/6", source="uk-caselaw",
        bailii_url="https://www.bailii.org/uk/cases/UKHL/2009/6.html",
        title="ZT v SSHD", decision_date=date(2009, 2, 4),
        self_citations=("[2009] UKHL 6", "[2009] 1 WLR 348"),
        text="1. First paragraph.\n\n2. Second paragraph.\n\n3. Third paragraph.",
        segments=[])
    defaults.update(over)
    return ParsedRow(**defaults)


def test_new_case_persists_text_and_report_alias(facade):
    """The bulk-import text-persistence trap: a slug-keyed case with no raw file must still
    store its text so the reader serves it (payload_hash falls back to hashing text)."""
    st, _files = _ingest(facade, _uk_parsed())
    assert st["imported"] == 1
    body = facade.document_body("ukhl/2009/6")
    assert body["text"] and "First paragraph" in body["text"]
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("ukhl/2009/6")["has_text"] == 1
        # the parallel-report citation resolves to the case
        assert cat.get_alias("[2009] 1 wlr 348") == "ukhl/2009/6"


def test_reimport_same_text_only_tops_up_aliases(facade):
    _ingest(facade, _uk_parsed())
    st, _f = _ingest(facade, _uk_parsed())
    assert st["imported"] == 0 and st["skipped"] == 1


def test_echr_case_enriches_held_ecli_via_appno(facade):
    """A held ECtHR case (keyed by ECLI, with an appno alias) must be ENRICHED by the
    BAILII page — its text attached as a secondary rendition — not duplicated under the
    echr/YYYY/N slug."""
    ecli = "ECLI:CE:ECHR:2008:1104JUD002269503"
    with facade._open() as (cat, rs, ts):
        rec = Record(source="echr", stable_id=ecli, doc_type=DocType.JUDGMENT,
                     title="CASE OF DEMSKI v. POLAND", court="echr",
                     decision_date=date(2008, 11, 4), language="en",
                     raw_bytes=b"<x>held authoritative echr text</x>", raw_ext="xml",
                     text="The Court's authoritative judgment text.",
                     extracted_via=ExtractedVia.STRUCTURED, added_by=AddedBy.HARVEST)
        rec.ensure_payload_hash()
        raw_path = str(rs.path_for(rs.put(rec.raw_bytes, ext="xml"), "xml"))
        text_path = str(ts.put(rec.payload_hash, rec.text))
        cat.upsert_document(rec, raw_path=raw_path, text_path=text_path)
        cat.put_alias("22695/03", ecli, source="echr-appno")
        cat.commit()

    parsed = ParsedRow(
        slug="echr/2008/1230", primary_id="echr/2008/1230", source="echr",
        bailii_url="https://www.bailii.org/eu/cases/ECHR/2008/1230.html",
        title="DEMSKI v. POLAND", decision_date=date(2008, 11, 4), appno="22695/03",
        self_citations=("[2008] ECHR 1230",),
        text="1. BAILII English rendition.\n\n2. Second.\n\n3. Third.", segments=[])
    st, _files = _ingest(facade, parsed)

    assert st["enriched"] == 1 and st["imported"] == 0
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("echr/2008/1230") is None      # no slug-keyed duplicate
        # the BAILII echr slug now resolves to the held ECLI case
        assert cat.get_alias("echr/2008/1230") == ecli
        meta = cat.document_meta(ecli)
        assert meta.get("alt_texts"), "BAILII text attached as a secondary rendition"


def test_thin_stub_keeps_identity_without_storing_junk(facade):
    parsed = _uk_parsed(slug="ukpc/1809/1809_3", primary_id="ukpc/1809/1809_3",
                        title="Zulema", self_citations=("[1809] UKPC 3",),
                        text="", segments=[], pdf_only=True,
                        pdf_url="https://www.bailii.org/uk/cases/UKPC/1809/1809_3.pdf")
    st, _f = _ingest(facade, parsed)
    assert st["stub"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("ukpc/1809/1809_3")["has_text"] == 0
        # the [1809] UKPC 3 neutral citation still resolves to the stub (identity kept)
        assert cat.get_alias("ukpc/1809/3") == "ukpc/1809/1809_3"


def test_only_unextracted_selects_just_the_backlog(facade):
    """The resume set after an interrupted bulk import: documents that have text but no
    citation rows. Without this, picking up a killed 200k-document run means re-extracting
    the whole source from scratch — and the documents the crash orphaned (queued in memory,
    never extracted) would never be reached at all."""
    _ingest(facade, _uk_parsed())                                   # ukhl/2009/6
    _ingest(facade, _uk_parsed(slug="uksc/2020/1", primary_id="uksc/2020/1",
                               title="A v B", self_citations=("[2020] UKSC 1",)))
    with facade._open() as (cat, _rs, _ts):
        all_ids = cat.text_document_ids(doc_types=["judgment"])
        assert set(all_ids) == {"ukhl/2009/6", "uksc/2020/1"}
        # nothing extracted yet → the whole set is the backlog
        assert set(cat.text_document_ids(doc_types=["judgment"],
                                         only_unextracted=True)) == set(all_ids)
        # once one has citation rows it drops out of the backlog
        cat.conn.execute(
            "INSERT INTO citations (src_id, raw, created_at) VALUES (?,?,?)",
            ("ukhl/2009/6", "[2000] UKHL 57", "2026-07-18T00:00:00Z"))
        cat.commit()
        assert cat.text_document_ids(doc_types=["judgment"],
                                     only_unextracted=True) == ["uksc/2020/1"]

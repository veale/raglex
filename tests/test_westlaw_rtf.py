"""Westlaw RTF parsing + the folder/zip importer's synthesis rules (§5b).

The fixtures are hand-built RTF that reproduces the quirks of a real Westlaw export —
a ``{\\header}`` running title, a ``{\\*\\shppict{\\pict …}}`` status image, hyperlink
fields wrapping the citations, and curly punctuation carried as ``\\rquote`` /
``\\ldblquote`` control words rather than bytes — so the tests pin the tokenizer's
destination-skipping and character mapping, not just the happy path.
"""

from __future__ import annotations

import zipfile
from datetime import date

import pytest

from raglex.adapters.westlaw_rtf import parse_westlaw_rtf, rtf_to_text, westlaw_identity
from raglex.config import Config
from raglex.facade import Facade

# A short PNG-ish hex blob standing in for the judicial-consideration status image.
_PNGHEX = "89504e470d0a1a0a0000000d49484452" * 4


def _rtf(running: str, body: str) -> bytes:
    """Wrap a running-header line + a body fragment in a faithful Westlaw skeleton."""
    doc = (
        r"{\rtf1\ansi\ansicpg1252"
        r"{\colortbl;\red0\green0\blue0;\red255\green255\blue255;}"
        r"{\fonttbl{\f0 Arial;}{\f2 Times New Roman;}}"
        r"{\*\generator Apache XML Graphics RTF Library;}"
        r"\fet0\ftnbj\sectd "
        r"{\header {\trowd\itap0\cellx10080\intbl {\b1\fs18 " + running + r"\cell }\row }}"
        r"{\footer {\trowd\itap0\cellx10080\intbl {\fs20 \u169\'3f 2026 Thomson Reuters.\cell }\row }}"
        r"{\b0\f2\fs20 " + body + r"}"
        r"}"
    )
    return doc.encode("latin-1")


def _blocks(*blocks: str) -> str:
    """Join labelled blocks the way Westlaw does: a blank paragraph separates blocks, so
    the parser's own block splitter sees the boundaries. Each argument is one whole
    block — a label plus its value lines already joined by single ``\\par``."""
    return r"\par \par ".join(blocks) + r"\par "


def _b(label: str, *values: str) -> str:
    """One label→value block: 'Label\\par value1\\par value2'."""
    return r"\par ".join((label, *values))


# A hyperlink field: the ``\*\fldinst`` half (the URL) is dropped, the ``\fldrslt``
# display text is kept.
def _link(display: str) -> str:
    return (r"{\field{\*\fldinst HYPERLINK \"https://uk.westlaw.com/Document/ABC/View/"
            r"FullText.html?vr=3.0\"}{\fldrslt " + display + r"}}")


_IMG = r"{\*\shppict{\pict\pngblip " + _PNGHEX + r"}}"


# ── the two front-matter surfaces ────────────────────────────────────────────

def _digest_case() -> bytes:
    """Westlaw 'Case Analysis' digest: a Where Reported list, Subject/Keywords, Judge,
    Counsel/Solicitor, an editorial Abstract/Held. A pre-neutral case → WL-id keyed."""
    body = _blocks(
        r"Macob Civil Engineering Ltd v Morrison Construction Ltd",
        _IMG + r"Positive/Neutral Judicial Consideration",
        _b(r"Court", r"Queen\rquote s Bench Division (Technology & Construction Court)"),
        _b(r"Judgment Date", r"12 February 1999"),
        _b(r"Where Reported", r"[1999] 2 WLUK 258", _link(r"[1999] C.L.C. 739"),
           r"[1999] B.L.R. 93", r"64 Con. L.R. 1"),
        _b(r"Subject", r"Construction law"),
        _b(r"Keywords", r"Challenges to awards; Construction contracts; Enforcement"),
        _b(r"Judge", _link(r"Dyson J")),
        _b(r"Counsel", r"For the plaintiff: Delia Dumaresq.", r"For the defendant: Stephen Furst Q.C."),
        _b(r"Solicitor", r"For the plaintiff: Morgan Cole (Cardiff)."),
        r"Case Digest",
        _b(r"Abstract",
           r"MCE sought to enforce an adjudicator\rquote s decision. The word "
           r"\ldblquote decision\rdblquote had not been qualified."),
    )
    return _rtf(r"Macob Civil Engineering Ltd v Morrison Construction Ltd, 1999 WL 250101 (1999)", body)


def _transcript_case() -> bytes:
    """A modern transcript: the neutral citation sits in the dateline (no Where Reported
    list) and the opinion is numbered — so it keys by the neutral-citation slug."""
    body = _blocks(
        r"The Queen on the application of Mynydd v Secretary of State",
        r"No Substantial Judicial Treatment",
        _b(r"Court", r"Queen\rquote s Bench Division (Administrative Court)"),
        _b(r"Judgment Date", r"19 October 2016"),
        r"[2016] EWHC 2581 (Admin), 2016 WL 06065959",
        r"Before : Mr Justice Hickinbottom",
        r"Approved Judgment",
        r"1. On 30 July 2014, the Claimant applied under section 37 of the Planning Act 2008.",
        r"2. On 27 October 2014, an inspector was appointed.",
        r"3. From July 2016, the functions were transferred to the Defendant.",
    )
    return _rtf(r"R. (on the application of Mynydd) v Secretary of State, 2016 WL 06065959 (2016)", body)


def _eu_case() -> bytes:
    """An EU case reported in UK series: an ECLI plus UK reporters and an 'EU Decision'
    link-label. Must key by the normalised ECLI, not a UK report citation."""
    body = _blocks(
        r"Criminal Proceedings against Aranyosi (C-404/15 PPU)",
        r"Also known as: Execution of a European Arrest Warrant against Aranyosi",
        _IMG + r"Positive/Neutral Judicial Consideration",
        _b(r"Court", r"European Court of Justice (Grand Chamber)"),
        _b(r"Judgment Date", r"5 April 2016"),
        _b(r"Where Reported", r"EU:C:2016:198", _link(r"[2016] Q.B. 921"),
           r"[2016] 4 WLUK 30", r"42 B.H.R.C. 551", _link(r"EU Decision")),
        _b(r"Subject", r"European Union"),
        r"Case Digest",
        _b(r"Summary", r"The execution of a European arrest warrant was to be deferred."),
    )
    return _rtf(r"Criminal Proceedings against Aranyosi (C-404/15 PPU), 2016 WL 1311973 (2016)", body)


# ── tokenizer / parser unit tests ────────────────────────────────────────────

def test_tokenizer_drops_images_and_hyperlink_urls_keeps_display_text():
    text = rtf_to_text(_digest_case())
    assert "HYPERLINK" not in text and "westlaw.com" not in text
    assert _PNGHEX[:32] not in text          # the status image is gone
    assert "[1999] C.L.C. 739" in text       # the field's display text survives
    assert "Dyson J" in text


def test_tokenizer_maps_punctuation_control_words_not_deletes_them():
    # the "investorsfunds" bug: \rquote must become an apostrophe, not vanish with the space
    text = rtf_to_text(_digest_case())
    assert "adjudicator’s decision" in text
    assert "“decision”" in text     # \ldblquote … \rdblquote
    assert "Queen’s Bench" in text


def test_digest_layout_fields_and_wl_identity():
    p = parse_westlaw_rtf(_digest_case())
    assert p.title == "Macob Civil Engineering Ltd v Morrison Construction Ltd"
    assert p.court_label.startswith("Queen") and p.court_code == "ewhc"
    assert p.decision_date == date(1999, 2, 12)
    assert p.wl_number == "1999 WL 250101"
    assert "[1999] C.L.C. 739" in p.report_citations
    assert "64 Con. L.R. 1" in p.report_citations
    assert p.judges == ("Dyson J",)
    assert any("Dumaresq" in c for c in p.counsel)
    assert "Construction law" in p.subjects
    assert p.neutral_citation is None and p.ecli is None
    assert westlaw_identity(p) == ("westlaw:1999-wl-250101", "wl")


def test_transcript_layout_lifts_neutral_from_dateline_and_numbers_paragraphs():
    p = parse_westlaw_rtf(_transcript_case())
    assert p.neutral_citation == "[2016] EWHC 2581 (Admin)"
    assert westlaw_identity(p) == ("ewhc/admin/2016/2581", "neutral")
    assert p.decision_date == date(2016, 10, 19)
    assert p.court_code == "ewhc"
    assert [s.label for s in p.segments] == ["para 1", "para 2", "para 3"]


def _law_report_case() -> bytes:
    """A reproduced ICLR law report whose header citation IS the report (no Westlaw id):
    keyed by a stable slug of that preferred citation, not a content hash."""
    body = _blocks(
        r"*149 Tate & Lyle Food and Distribution Ltd v Greater London Council",
        _IMG + r"Negative Judicial Consideration",
        _b(r"Court", r"Queen\rquote s Bench Division"),
        _b(r"Judgment Date", r"22 May 1981"),
        _b(r"Report Citation", r"[1982] 1 W.L.R. 149"),
        _b(r"Representation", r"Solicitors: Morgan Cole ; Wragge & Co ."),
    )
    return _rtf(r"Tate & Lyle Industries Ltd v Greater London Council, [1982] 1 W.L.R. 149 (1981)", body)


def test_wl_less_law_report_keys_by_a_stable_report_slug_not_a_hash():
    p = parse_westlaw_rtf(_law_report_case())
    assert p.wl_number is None and p.neutral_citation is None
    assert "[1982] 1 W.L.R. 149" in p.report_citations
    sid, kind = westlaw_identity(p)
    assert kind == "report"
    assert sid == "westlaw:1982-1-w-l-r-149"     # stable, re-download-safe (no content hash)
    assert p.title == "Tate & Lyle Industries Ltd v Greater London Council"


def test_eu_case_keys_by_ecli_not_the_uk_report_citations():
    p = parse_westlaw_rtf(_eu_case())
    assert p.is_eu and p.court_code == "cjeu"
    assert p.ecli == "ECLI:EU:C:2016:198"
    assert p.case_number == "C-404/15"
    assert westlaw_identity(p) == ("ECLI:EU:C:2016:198", "ecli")
    # the UK reporters are captured as aliases, but "EU Decision" (a link label) is not
    assert "[2016] Q.B. 921" in p.report_citations
    assert "EU Decision" not in p.report_citations
    assert any("Execution of a European Arrest Warrant" in a for a in p.also_known_as)


def test_rejects_non_rtf_bytes():
    assert parse_westlaw_rtf(b"<html>not rtf</html>") is None


# ── the importer's synthesis with the held corpus ────────────────────────────

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    ))


def _folder(dir_path, files: dict[str, bytes]):
    from pathlib import Path
    d = Path(dir_path)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def test_dir_import_keys_each_case_by_its_strongest_identity(facade, tmp_path):
    d = _folder(tmp_path / "wl", {
        "01 - Macob.rtf": _digest_case(),
        "02 - Mynydd.rtf": _transcript_case(),
        "03 - Aranyosi.rtf": _eu_case(),
        "notes.txt": b"not a judgment",
    })
    res = facade.import_westlaw_dir(dir_path=str(d))
    assert res["total"] == 3 and res["imported"] == 3   # the .txt is skipped
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("westlaw:1999-wl-250101") is not None
        assert cat.get_document("ewhc/admin/2016/2581") is not None
        eu = cat.get_document("ECLI:EU:C:2016:198")
        assert eu is not None and eu["source"] == "eu-cellar"


def test_uk_report_citation_of_an_eu_case_aliases_to_the_ecli_document(facade, tmp_path):
    d = _folder(tmp_path / "wl", {"eu.rtf": _eu_case()})
    facade.import_westlaw_dir(dir_path=str(d))
    with facade._open() as (cat, _rs, _ts):
        from raglex.core.text import fold
        # a citer writing "[2016] Q.B. 921" resolves to the canonical EU judgment
        assert cat.get_alias(fold("[2016] Q.B. 921")) == "ECLI:EU:C:2016:198"


def test_duplicate_case_files_merge_via_a_shared_report_citation(facade, tmp_path):
    # the same pre-neutral case exported twice (different leading numbers) — the second
    # must adopt the first's id via the shared parallel citation, not duplicate it.
    d = _folder(tmp_path / "wl", {
        "26 - Macob.rtf": _digest_case(),
        "31 - Macob.rtf": _digest_case(),
    })
    res = facade.import_westlaw_dir(dir_path=str(d))
    with facade._open() as (cat, _rs, _ts):
        docs = cat.list_documents(source="uk-caselaw", limit=100)
    assert len(docs) == 1


def _bailii_page(*, url="https://www.bailii.org/uk/cases/UKHL/2000/57.html",
                 title="Turkington v Times Newspapers [2000] UKHL 57 (2nd November, 2000)") -> bytes:
    html = (f"<HTML><HEAD><TITLE>{title}</TITLE></HEAD><BODY>"
            f"<TABLE><TR><TD><H1>House of Lords</H1></TD></TR>"
            f"<TR><TD><SMALL>Cite as: [2000] UKHL 57<BR>URL: <I>{url}</I></SMALL><HR></TD></TR></TABLE>"
            f"<hr><P>A judgment about the Data Protection Act 1998.</P>"
            f"<P><HR><SMALL><B>BAILII:</B> copyright</SMALL></P></BODY></HTML>")
    return html.encode("iso-8859-1")


def test_unified_dir_import_routes_html_to_bailii_and_rtf_to_westlaw(facade, tmp_path):
    d = _folder(tmp_path / "mix", {
        "turkington.html": _bailii_page(),
        "aranyosi.rtf": _eu_case(),
        "mynydd.rtf": _transcript_case(),
    })
    res = facade.import_caselaw_dir(dir_path=str(d))
    assert res["total"] == 3 and res["imported"] == 3   # 1 BAILII + 2 Westlaw, merged
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("ukhl/2000/57") is not None            # BAILII page
        assert cat.get_document("ECLI:EU:C:2016:198") is not None      # Westlaw EU
        assert cat.get_document("ewhc/admin/2016/2581") is not None    # Westlaw transcript


def test_unified_zip_import_handles_a_mixed_zip(facade, tmp_path):
    zp = tmp_path / "mix.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("t.html", _bailii_page())
        zf.writestr("macob.rtf", _digest_case())
    res = facade.import_caselaw_zip(zip_path=str(zp))
    assert res["total"] == 2
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("ukhl/2000/57") is not None
        assert cat.get_document("westlaw:1999-wl-250101") is not None


def test_merges_into_an_existing_record_sharing_a_report_citation_alias(facade, tmp_path):
    # a held record (e.g. from BAILII/ICLR) already carries the report citation as an
    # alias — the Westlaw import of the same case must adopt that id, not mint a westlaw:
    # duplicate.
    from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
    from raglex.core.text import fold
    with facade._open() as (cat, _rs, ts):
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id="ukhl/1981/tate", doc_type=DocType.JUDGMENT,
            title="Tate & Lyle v GLC", text="held elsewhere", raw_bytes=b"x", raw_ext="html",
            payload_hash="beef", extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER))
        cat.put_alias(fold("[1982] 1 W.L.R. 149"), "ukhl/1981/tate", source="cite-as")
        cat.commit()
    d = _folder(tmp_path / "wl", {"tate.rtf": _law_report_case()})
    res = facade.import_caselaw_dir(dir_path=str(d))
    # merged into the held record (attached as a secondary rendition, since that record is
    # authoritative) — NOT imported as a fresh document
    assert res["imported"] == 0 and res["secondary"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("westlaw:1982-1-w-l-r-149") is None   # no duplicate minted
        # the report alias still resolves to the pre-existing record
        assert cat.get_alias(fold("[1982] 1 W.L.R. 149")) == "ukhl/1981/tate"
        # the Westlaw rendition + its rich metadata landed on the held record
        assert "westlaw" in cat.document_meta("ukhl/1981/tate")


def test_refix_rekeys_a_legacy_hash_id_to_a_report_slug(facade, tmp_path):
    # a doc imported under the OLD rules: a WL-less law report keyed by a content hash.
    # refix recomputes its identity from meta_json and re-keys it to the report-citation
    # slug, cascading every reference.
    from raglex.adapters.westlaw_rtf import parse_westlaw_rtf
    from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
    from raglex.core.text import fold
    parsed = parse_westlaw_rtf(_law_report_case())
    old = "westlaw:deadbeefdeadbeef"
    with facade._open() as (cat, _rs, ts):
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id=old, doc_type=DocType.JUDGMENT, title=parsed.title,
            text=parsed.text, raw_bytes=b"x", raw_ext="rtf", payload_hash="ph1",
            extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
            extra={"imported": "westlaw-rtf", "westlaw": facade._westlaw_meta(parsed)}))
        cat.put_alias(fold("[1982] 1 W.L.R. 149"), old, source="westlaw-report-alias")
        # an incoming citation already resolved to the hash id
        cat.conn.execute(
            "INSERT INTO relations (src_id, dst_id, candidate_id, relationship_type, "
            "resolution_status, extracted_via) VALUES (?,?,?,?,?,?)",
            ("some/citing/doc", old, old, "mentions", "resolved", "grammar"))
        cat.commit()

    dry = facade.refix_westlaw_imports(apply=False)
    assert dry["scanned"] == 1 and len(dry["changes"]) == 1
    assert dry["changes"][0] == {"old": old, "new": "westlaw:1982-1-w-l-r-149", "kind": "report"}

    res = facade.refix_westlaw_imports(apply=True)
    assert res["rekeyed"] == 1
    with facade._open() as (cat, _rs, _ts):
        new = "westlaw:1982-1-w-l-r-149"
        assert cat.get_document(old) is None and cat.get_document(new) is not None
        assert cat.get_alias(fold("[1982] 1 W.L.R. 149")) == new          # alias repointed
        rel = cat.conn.execute("SELECT dst_id, candidate_id FROM relations WHERE src_id='some/citing/doc'").fetchone()
        assert rel["dst_id"] == new and rel["candidate_id"] == new         # relation repointed


def test_zip_import_supersedes_a_textless_stub(facade, tmp_path):
    # a held metadata-only stub for the same neutral slug is replaced by the full text
    from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
    with facade._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id="ewhc/admin/2016/2581", doc_type=DocType.JUDGMENT,
            title="stub", raw_bytes=b"", raw_ext="html", payload_hash="deadbeef",
            text=None, extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
        ))
        cat.commit()
    zp = tmp_path / "wl.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("mynydd.rtf", _transcript_case())
    res = facade.import_westlaw_zip(zip_path=str(zp))
    assert res["superseded"] == 1
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document("ewhc/admin/2016/2581")
        assert doc["has_text"] and "Planning Act 2008" in ts.get(doc["payload_hash"])

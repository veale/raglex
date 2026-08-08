"""German normalization-first citation graph regressions."""

from raglex.citations.extractor import extract_citations
from raglex.citations.german import case_alias
from raglex.citations.taxonomy import classify_candidate, classify_document
from raglex.core.models import DocType, ExtractedVia, Record


def _de(text: str, method: str = "de_law_reference"):
    return [c for c in extract_citations(text) if c.method == method]


def test_ivm_expands_to_two_edges():
    cites = _de("§ 312 i.V.m. § 355 BGB")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("de/gesetz/bgb", "§ 312"), ("de/gesetz/bgb", "§ 355")]


def test_numeric_range_expands_to_individual_pinpoints():
    cites = _de("§§ 12–15 BGB")
    assert [c.pinpoint for c in cites] == ["§ 12", "§ 13", "§ 14", "§ 15"]


def test_compound_subprovisions_expand_and_inherit_levels():
    cites = _de("§ 2 Abs. 1 Nr. 1, Nr. 7, Abs. 2 UrhG")
    assert [c.pinpoint for c in cites] == [
        "§ 2 Abs. 1 Nr. 1", "§ 2 Abs. 1 Nr. 7", "§ 2 Abs. 2"]
    assert {c.candidate_id for c in cites} == {"de/gesetz/urhg"}


def test_compound_german_eu_article_resolves_to_celex_work():
    cites = _de("Art. 38 Abs. 3 Satz 2 DSGVO", "de_eu_article")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("32016R0679", "Article 38(3)")
    ]
    assert _de("Art. 4 Nr. 1 DSGVO", "de_eu_article")[0].pinpoint == "Article 4(1)"
    assert _de("Art. 6 Abs. 1 DSA", "de_eu_article")[0].candidate_id == "32022R2065"


def test_conflicting_protected_statutory_shorthand_is_ignored():
    from raglex.citations.extractor import attach_stored_shorthands

    kept = _de("Art. 6 Abs. 1 DSGVO", "de_eu_article")
    cites = attach_stored_shorthands(
        "Art. 6 Abs. 1 DSGVO in Verbindung mit dem BDSG", kept,
        [("BDSG", "32016R0679", "regulation", True)],
    )
    assert [c.raw for c in cites if c.method == "shorthand_global"] == []

    # Even a target-correct learned alias is not safe outside its language/context:
    # this is a Canadian company, not the Dutch name of the GDPR.
    assert not attach_stored_shorthands(
        "The two companies were ASU Dominica and ASU AVG.", [],
        [("AVG", "32016R0679", "regulation", True)],
    )

    ai = extract_citations("Article 50 of the AI Act")
    assert any(c.candidate_id == "32024R1689" and c.pinpoint == "Article 50" for c in ai)
    assert not [c for c in attach_stored_shorthands(
        "Article 50 of the AI Act", ai,
        [("AI Act", "32022R2065", "regulation", True)],
    ) if c.method == "shorthand_global"]


def test_compact_roman_and_parenthesised_forms_converge():
    explicit = _de("§ 19 Abs. 4 S. 1 BVerfGG")[0]
    roman = _de("§ 19 IV 1 BVerfGG")[0]
    parenthesised = _de("§ 19 (4) 1 BVerfGG")[0]
    assert {(c.candidate_id, c.pinpoint) for c in (explicit, roman, parenthesised)} == {
        ("de/gesetz/bverfgg", "§ 19 Abs. 4 Satz 1")}


def test_case_docket_and_randnummer_are_preserved():
    cite = _de("BGH, Urteil vom 12. Mai 2021 – VIII ZR 295/01, Rn. 15",
               "de_case_reference")[0]
    assert cite.candidate_id == "de:case:BGH:VIIIZR295/01"
    assert cite.pinpoint == "Rn. 15"
    assert case_alias("Bundesgerichtshof", "VIII ZR 295/01") == cite.candidate_id


def test_french_cedh_marker_is_not_minted_as_german_legislation():
    assert not _de("§ 95, CEDH 19")


def test_lowercase_german_word_is_not_safe_as_global_bare_shorthand():
    from raglex.citations.extractor import attach_stored_shorthands
    assert not attach_stored_shorthands(
        "Das Gericht kann dies prüfen.", [],
        [("kann", "de/gesetz/bgb", "act", True)])


def test_german_sources_and_candidates_leave_other_bucket():
    held = classify_document(source="de-rii", doc_type=str(DocType.JUDGMENT),
                             court="Bundesgerichtshof", stable_id="ECLI:DE:BGH:2021:X")
    pending = classify_candidate("de/gesetz/bgb", "act")
    assert held.category == "de-caselaw"
    assert pending.category == "de-legislation"


def test_migration_mints_aliases_for_held_german_nodes(catalogue):
    catalogue.upsert_document(Record(
        source="de-rii", stable_id="ECLI:DE:BGH:2021:TEST", ecli="ECLI:DE:BGH:2021:TEST",
        doc_type=DocType.JUDGMENT, title="BGH VIII ZR 295/01", court="Bundesgerichtshof",
        extracted_via=ExtractedVia.STRUCTURED, extra={"aktenzeichen": "VIII ZR 295/01"},
    ))
    catalogue.upsert_document(Record(
        source="de-neuris", stable_id="eli/bund/bgbl-1/1896/s195", doc_type=DocType.LEGISLATION,
        title="Bürgerliches Gesetzbuch", extracted_via=ExtractedVia.STRUCTURED,
        extra={"jurabk": "BGB"},
    ))
    catalogue.backfill_alias_from_meta()
    assert catalogue.find_document_id("de:case:BGH:VIIIZR295/01") == "ECLI:DE:BGH:2021:TEST"
    assert catalogue.find_document_id("de/gesetz/bgb") == "eli/bund/bgbl-1/1896/s195"


# -- precision: what the open-ended German grammars used to mint ---------------

def test_ordinary_german_word_is_not_a_law_abbreviation():
    """The pattern must END on a law and German capitalises its nouns, so backtracking
    handed back whatever word sat where a law belongs: de/gesetz/rn (cited from 7,676
    documents), satz1, ziff, and the section headings of the statutes themselves."""
    assert not _de("§ 100 Absatz 1 Satz 1, Absatz 2 Satz 2")
    assert not _de("§ 100a Rn")
    assert not _de("vgl. § 5 ziff 3")
    assert not _de("§ 4 Geltungsbereich")
    # …while a real abbreviation in the same shape still resolves
    assert [c.candidate_id for c in _de("§ 823 Abs. 1 BGB")] == ["de/gesetz/bgb"]


def test_next_word_is_not_swallowed_as_a_book_numeral():
    """Case-insensitivity read the "i" of "i.V.m." and the "v" of an English case name as
    a book number, minting phantom siblings (markeng1, bgb5) of laws already held."""
    assert [c.candidate_id for c in _de("§ 8 Abs. 2 Nr. 1 MarkenG i.V.m. § 42 MarkenG")] == [
        "de/gesetz/markeng", "de/gesetz/markeng"]
    assert [c.candidate_id for c in _de("§ 1004 Abs. 1 BGB v Smith")] == ["de/gesetz/bgb"]
    assert [c.candidate_id for c in _de("§ 5 BGB 5. Aufl.")] == ["de/gesetz/bgb"]
    # a genuine book numeral is the key gesetze-im-internet itself uses
    assert [c.candidate_id for c in _de("§ 44 Abs. 1 SGB V")] == ["de/gesetz/sgb5"]


def test_senate_prefix_with_a_letter_is_kept():
    """BGH VIa ZR 335/21 lost its senate and merged with IVa ZR 335/21 — one node for
    two different cases, and neither matching the alias the adapters mint."""
    via = _de("BGH, Urteil vom 26. Juni 2023 - VIa ZR 335/21", "de_case_reference")[0]
    iva = _de("BGH, Urteil vom 26. Juni 2023 - IVa ZR 335/21", "de_case_reference")[0]
    assert via.candidate_id == "de:case:BGH:VIAZR335/21"
    assert iva.candidate_id == "de:case:BGH:IVAZR335/21"
    assert via.candidate_id == case_alias("Bundesgerichtshof", "VIa ZR 335/21")


def test_report_series_is_not_read_as_a_docket():
    """"BSG SozR 4-1500 § 160a" is the social-law REPORT series; its "R 4-1500" was minted
    as de:case:BSG:ZR4-1500 — the most-cited German "case" in the corpus."""
    assert not _de("BSG SozR 4-1500 § 160a Nr. 7", "de_case_reference")


def test_docket_of_the_court_below_is_not_attributed_to_the_court_above():
    """A judgment's header lists the courts below it; the 100-character window stepped
    over "vorgehend KG Berlin" and gave the BGH the Kammergericht's docket."""
    cites = _de("BGH, 15. September 2022, Az: VI ZA 19/22, Beschluss vorgehend "
                "KG Berlin, 7. Juli 2022, Az: 10 U 54/19", "de_case_reference")
    assert [c.candidate_id for c in cites] == ["de:case:BGH:VIZA19/22"]


def test_repair_drops_the_phantoms_the_grammar_no_longer_mints(tmp_path, monkeypatch):
    """The standing migration for a German grammar fix: re-extract each distinct
    (candidate, raw) pair and drop the candidates nothing mints any more. Pending edges
    only — a German citation that resolved to a held judgment is a real link."""
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    text = "Vgl. § 823 Abs. 1 BGB sowie BGH, Urteil vom 26. Juni 2023 - VIa ZR 335/21."
    with f._open() as (cat, _rs, ts):
        rec = Record(source="de-rii", stable_id="de-1", doc_type=DocType.JUDGMENT,
                     text=text, raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))
    f.extract_citations(stable_id="de-1")
    # what the old grammar left behind
    with f._open() as (cat, _rs, _ts), cat._atomic():
        for cand, raw, method, kind in (
                ("de/gesetz/rn", "§ 100a Rn", "de_law_reference", "act"),
                ("de:case:BSG:ZR4-1500", "BSG SozR 4-1500", "de_case_reference", "case")):
            cat.conn.execute(
                "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, method, "
                "confidence, created_at) VALUES (?,?,?,?,?,?,?)",
                ("de-1", raw, kind, cand, method, 1.0, "2026-07-21"))
            cat.conn.execute(
                "INSERT INTO relations (src_id, raw_citation_string, candidate_id, "
                "resolution_status, relationship_type, extracted_via) VALUES (?,?,?,?,?,?)",
                ("de-1", raw, cand, "pending", "mentions", "regex"))

    assert f.repair_de_citations(dry_run=True)["phantom_candidates"] == 2
    res = f.repair_de_citations()
    assert res["citations_deleted"] == 2 and res["edges_deleted"] == 2
    assert f.repair_de_citations()["phantom_candidates"] == 0  # re-runnable
    with f._open() as (cat, _rs, _ts):
        left = {r["candidate_id"] for r in
                cat.conn.execute("SELECT candidate_id FROM citations").fetchall()}
    assert left == {"de:case:BGH:VIAZR335/21"}


# -- two registers, one judgment ---------------------------------------------

def _de_facade(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    import os
    os.environ["RAGLEX_DATA_DIR"] = str(tmp_path)
    return Facade(Config.from_env())


def test_a_second_register_does_not_fork_the_corpus(tmp_path, monkeypatch):
    """NeuRIS and rechtsprechung-im-internet publish the SAME federal decisions, but
    NeuRIS answers `ecli: null`, so its copies were stored under a synthetic id that
    nothing could link to the ECLI-keyed original — and the docket alias followed the
    copy, so citations resolved to the rendition with no ECLI and no edges."""
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade
    from raglex.pipeline import Pipeline

    f = Facade(Config.from_env())
    docket = case_alias("BGH", "AnwZ (Brfg) 40/25")
    with f._open() as (cat, rs, ts):
        original = Record(
            source="de-rii", stable_id="ECLI:DE:BGH:2026:180526BANWZ.BRFG.40.25.0",
            ecli="ECLI:DE:BGH:2026:180526BANWZ.BRFG.40.25.0", doc_type=DocType.JUDGMENT,
            title="BGH AnwZ (Brfg) 40/25", court="Bundesgerichtshof",
            text="Der Antrag wird abgelehnt.", raw_bytes=b"x",
            extracted_via=ExtractedVia.STRUCTURED, extra={"aliases": [docket]})
        original.ensure_payload_hash()
        cat.upsert_document(original, text_path=str(ts.put(original.payload_hash, original.text)))
        cat.put_alias(docket.casefold(), original.stable_id, source="adapter-alias")

        # the same decision arriving from the other register, with no ECLI
        copy = Record(source="de-neuris", stable_id="de/KORE615362026",
                      doc_type=DocType.JUDGMENT, title="BGH, AnwZ (Brfg) 40/25",
                      court="BGH", text="Der Antrag wird abgelehnt. (NeuRIS)",
                      raw_bytes=b"y", extracted_via=ExtractedVia.STRUCTURED,
                      extra={"aliases": [docket]})
        stats = type("S", (), {"deduped": 0, "stored": 0})()
        stored = Pipeline(cat, rs, textstore=ts)._store(copy, stats) \
            if hasattr(Pipeline(cat, rs, textstore=ts), "_store") else None

    # the docket still names the ECLI-keyed judgment, whichever way the store went
    with f._open() as (cat, _rs, _ts):
        assert cat.find_document_id(docket) == "ECLI:DE:BGH:2026:180526BANWZ.BRFG.40.25.0"


def test_repair_folds_an_existing_duplicate_back_into_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    docket = case_alias("BGH", "AnwZ (Brfg) 40/25")
    with f._open() as (cat, _rs, ts):
        for source, sid, ecli in (
                ("de-rii", "ECLI:DE:BGH:2026:1805", "ECLI:DE:BGH:2026:1805"),
                ("de-neuris", "de/KORE615362026", None)):
            rec = Record(source=source, stable_id=sid, ecli=ecli, doc_type=DocType.JUDGMENT,
                         title="BGH AnwZ (Brfg) 40/25", court="BGH", text=f"text {source}",
                         raw_bytes=source.encode(), extracted_via=ExtractedVia.STRUCTURED,
                         extra={"aliases": [docket]})
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        # the state the live corpus was in: the docket points at the COPY
        cat.put_alias(docket.casefold(), "de/KORE615362026", source="adapter-alias")

    res = f.repair_de_duplicate_renditions(dry_run=True)
    assert res["duplicates"] == 1 and res["documents_deleted"] == 0

    res = f.repair_de_duplicate_renditions()
    assert res["duplicates"] == 1 and res["documents_deleted"] == 1
    with f._open() as (cat, _rs, _ts):
        assert cat.find_document_id(docket) == "ECLI:DE:BGH:2026:1805"
        assert cat.get_document("de/KORE615362026") is None
        assert cat.document_meta("ECLI:DE:BGH:2026:1805")["renditions"][0]["id"] == "de/KORE615362026"


def test_unterabsatz_does_not_break_the_reference_or_mint_a_law():
    """"Art. 6 Abs. 1 UAbs. 1 Buchst. f DSGVO" is the canonical long form of the most
    cited provision in European data-protection law. The pattern has to run unbroken
    from Art. to the abbreviation, so an unknown rung in the middle did not merely lose
    the pinpoint — the match ended at "UAbs", which then read as the law."""
    for text in ("Art. 6 Abs. 1 UAbs. 1 Buchst. f DSGVO",
                 "Art. 6 Abs. 1 UAbs. 1 lit. f DS-GVO",
                 "Art. 6 Abs. 1 Unterabs. 1 Buchst. f DSGVO"):
        cites = _de(text, "de_eu_article")
        assert [(c.candidate_id, c.pinpoint) for c in cites] == [
            ("32016R0679", "Article 6(1)(f)")], text
    assert not [c for c in extract_citations("Art. 53 Abs. 2 UAbs. 1 GG")
                if c.candidate_id in ("de/gesetz/uabs", "de/gesetz/unterabs")]


def test_german_primary_law_resolves_to_the_treaty_work_not_a_phantom_statute():
    """AEUV/EUV/GRCh/EMRK have the surface form of a domestic statute reference, so
    without a mapping each minted a de/gesetz/… node the corpus can never hold — and
    a German "Art. 267 AEUV" could not meet an English "Article 267 TFEU"."""
    expected = {
        "Art. 267 AEUV": "12016E",
        "Artikel 78 Absatz 3 AEUV": "12016E",
        "Art. 6 EUV": "12016M",
        "Art. 31 Abs. 2 GRC": "12012P",
        "Art. 7 GRCh": "12012P",
        "Art. 8 EMRK": "echr/convention",
    }
    for text, candidate in expected.items():
        cites = _de(text, "de_eu_article")
        assert [c.candidate_id for c in cites] == [candidate], text
        assert cites[0].entity_kind == "treaty"
        assert cites[0].pinpoint.startswith("Article ")


def test_pre_lisbon_ec_treaty_keeps_its_own_work():
    """Article 81 EG is not Article 81 TFEU — the treaty was renumbered in 2009, so the
    old abbreviation must cluster on the EC Treaty's own consolidated CELEX."""
    assert [c.candidate_id for c in _de("Art. 81 EG", "de_eu_article")] == ["12002E"]
    assert [c.candidate_id for c in _de("Artikel 95 EGV", "de_eu_article")] == ["12002E"]


def test_repair_folds_a_same_register_copy_once_that_register_supplies_an_ecli(
        tmp_path, monkeypatch):
    """The reason NeuRIS forked the corpus has since gone away — the beta answered
    `ecli: null` and now populates it — so a decision held as de/<documentNumber> comes
    back ECLI-keyed and lands beside its own older copy. Both carry `de-neuris`, and a
    rule that only ever compared different registers could not see it."""
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    docket = case_alias("BGH", "VII ZR 128/25")
    with f._open() as (cat, _rs, ts):
        for sid, ecli in (("de/KORE601272026", None),
                          ("ECLI:DE:BGH:2026:220726BVIIZR128.25.0",
                           "ECLI:DE:BGH:2026:220726BVIIZR128.25.0")):
            rec = Record(source="de-neuris", stable_id=sid, ecli=ecli,
                         doc_type=DocType.JUDGMENT, title="BGH VII ZR 128/25",
                         court="BGH", text=f"text {sid}", raw_bytes=sid.encode(),
                         extracted_via=ExtractedVia.STRUCTURED,
                         extra={"aliases": [docket]})
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        cat.put_alias(docket.casefold(), "de/KORE601272026", source="adapter-alias")

    res = f.repair_de_duplicate_renditions()
    assert res["duplicates"] == 1 and res["documents_deleted"] == 1
    with f._open() as (cat, _rs, _ts):
        assert cat.get_document("de/KORE601272026") is None
        assert cat.find_document_id(docket) == "ECLI:DE:BGH:2026:220726BVIIZR128.25.0"

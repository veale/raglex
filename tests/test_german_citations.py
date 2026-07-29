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

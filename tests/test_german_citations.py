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

"""French extraction/resolution regressions, based on SNE Ref-Lex + REGLEX forms."""

from raglex.citations.extractor import extract_citations
from raglex.citations.french import code_article_alias
from raglex.citations.taxonomy import classify_candidate, classify_document
from raglex.core.models import DocType
from raglex.core.models import ExtractedVia, Record


def _one(text: str, method: str):
    return next(c for c in extract_citations(text) if c.method == method)


def test_french_code_article_is_canonical_and_pinpointed():
    cite = _one(
        "Vu l'article L. 112-1 du code des relations entre le public et l'administration.",
        "fr_code_article",
    )
    assert cite.candidate_id == "fr:code:crpa:L112-1"
    assert cite.pinpoint == "Article L112-1"
    assert code_article_alias("Code des relations entre le public et l’administration",
                              "L. 112-1") == cite.candidate_id


def test_cour_de_cassation_number_and_paragraph_pinpoint():
    cite = _one("Cass. civ. 1re, 5 avril 2023, n° 21-15.442, § 12.",
                "fr_national_case")
    assert cite.candidate_id == "fr:pourvoi:21-15.442"
    assert cite.pinpoint == "para 12"


def test_conseil_etat_decision_number():
    cite = _one("Conseil d'État, 13 juillet 2021, n° 437815.", "fr_national_case")
    assert cite.candidate_id == "fr:decision:437815"


def test_french_eu_instruments_resolve_to_celex():
    cites = {c.raw: c for c in extract_citations(
        "règlement (UE) 2016/679 et directive 95/46/CE")}
    assert cites["règlement (UE) 2016/679"].candidate_id == "32016R0679"
    assert cites["directive 95/46/CE"].candidate_id == "31995L0046"


def test_legifrance_native_identifiers_are_preserved():
    cite = _one("https://www.legifrance.gouv.fr/juri/id/JURITEXT000051856547",
                "fr_legifrance_id")
    assert cite.candidate_id == "JURITEXT000051856547"
    assert cite.entity_kind == "case"


def test_fr_sources_and_candidates_leave_other_bucket():
    held = classify_document(source="fr-dila", doc_type=str(DocType.JUDGMENT),
                             court="Cour de cassation", stable_id="ECLI:FR:CCASS:2023:X")
    pending = classify_candidate("fr:code:cciv:L112-1", "act")
    assert held.category == "fr-caselaw"
    assert pending.category == "fr-legislation"


def test_migration_mints_aliases_for_already_imported_french_nodes(catalogue):
    catalogue.upsert_document(Record(
        source="fr-dila", stable_id="ECLI:FR:CCASS:2023:C100001",
        ecli="ECLI:FR:CCASS:2023:C100001", doc_type=DocType.JUDGMENT,
        title="Cour de cassation, 21-15.442", landing_url=
        "https://www.legifrance.gouv.fr/juri/id/JURITEXT000051856547",
        extracted_via=ExtractedVia.STRUCTURED,
        extra={"fond": "CASS", "number": "21-15.442"},
    ))
    catalogue.upsert_document(Record(
        source="fr-dila", stable_id="LEGIARTI000006419292",
        doc_type=DocType.LEGISLATION,
        title="Code civil — Article L. 112-1", extracted_via=ExtractedVia.STRUCTURED,
        extra={"fond": "LEGI"},
    ))
    catalogue.backfill_alias_from_meta()
    assert catalogue.find_document_id("fr:pourvoi:21-15.442") == \
        "ECLI:FR:CCASS:2023:C100001"
    assert catalogue.find_document_id("JURITEXT000051856547") == \
        "ECLI:FR:CCASS:2023:C100001"
    assert catalogue.find_document_id("fr:code:cciv:L112-1") == "LEGIARTI000006419292"

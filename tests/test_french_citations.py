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


def test_french_eu_articles_keep_pinpoints_before_numeric_instrument():
    cites = extract_citations(
        "L'article 13 du règlement (UE) 2016/679 s'applique. "
        "Les articles 3, 4, 5, 6 et 12 du règlement (UE) n° 2016/679 aussi. "
        "Voir l'article 14, paragraphe 5, sous a), du règlement (UE) 2016/679."
    )
    got = {(c.candidate_id, c.pinpoint, c.method) for c in cites}
    assert {
        ("32016R0679", "Article 13", "fr_eu_articles"),
        ("32016R0679", "Article 3", "fr_eu_articles"),
        ("32016R0679", "Article 4", "fr_eu_articles"),
        ("32016R0679", "Article 5", "fr_eu_articles"),
        ("32016R0679", "Article 6", "fr_eu_articles"),
        ("32016R0679", "Article 12", "fr_eu_articles"),
        ("32016R0679", "Article 14(5)(a)", "fr_eu_articles"),
    } <= got


def test_french_echr_article_list_is_not_donated_to_later_domestic_code():
    text = ("articles 3 et 8 de la convention européenne de sauvegarde des droits "
            "de l'homme et des libertés fondamentales ainsi que les dispositions "
            "du code de l'entrée et du séjour des étrangers et du droit d'asile")
    cites = [c for c in extract_citations(text) if c.method == "fr_echr_articles"]
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("echr/convention", "Article 3"), ("echr/convention", "Article 8")]
    assert not any(c.candidate_id == "fr:code:ceseda:3" for c in extract_citations(text))


def test_french_code_article_list_expands_every_pinpoint():
    cites = [c for c in extract_citations(
        "articles 452 et 456 du code de procédure civile")
        if c.method == "fr_code_articles"]
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("fr:code:cprociv:452", "Article 452"),
        ("fr:code:cprociv:456", "Article 456"),
    ]


def test_french_article_list_scan_is_bounded_on_non_french_eu_text():
    # This shape previously triggered catastrophic backtracking in both the code and
    # ECHR list expressions when no French host followed an otherwise valid Article.
    text = ("Article 8 has effect. Article 13 may apply. " * 2_000)
    assert not [c for c in extract_citations(text)
                if c.method in {"fr_code_articles", "fr_echr_articles"}]


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


def test_digital_acquis_refresh_selector_only_returns_relevant_french_text(catalogue):
    for sid, source in (
        ("fr/gdpr", "fr-dila"),
        ("fr/ordinary", "fr-dila"),
        ("de/gdpr", "de-rii"),
    ):
        catalogue.upsert_document(Record(
            source=source, stable_id=sid, doc_type=DocType.JUDGMENT,
            title=sid, text="text", extracted_via=ExtractedVia.STRUCTURED,
        ))
    for sid, candidate in (
        ("fr/gdpr", "32016R0679"),
        ("fr/ordinary", "32004L0038"),
        ("de/gdpr", "32016R0679"),
    ):
        catalogue.add_citations(sid, [{
            "raw": candidate, "entity_kind": "regulation",
            "candidate_id": candidate, "pinpoint": None,
            "char_start": 0, "char_end": len(candidate),
            "method": "test", "confidence": 1.0,
        }])
    assert catalogue.text_document_ids_citing(
        ["32016R0679"], source_prefix="fr-") == ["fr/gdpr"]
    # ...and with no national filter it is the corpus-wide acquis worklist: the scope
    # for a change to grammars or shorthand rules, which apply in every jurisdiction.
    assert catalogue.text_document_ids_citing(
        ["32016R0679"]) == ["de/gdpr", "fr/gdpr"]


def test_exact_flagged_document_worklist_preserves_order(catalogue):
    for stable_id in ("flag/b", "flag/a"):
        catalogue.upsert_document(Record(
            source="user-import", stable_id=stable_id, doc_type=DocType.COMMENTARY,
            title=stable_id, text="Article 6 GDPR",
            extracted_via=ExtractedVia.STRUCTURED,
        ))
    assert catalogue.held_text_document_ids(
        ["missing", "flag/b", "flag/a", "flag/b"]
    ) == ["flag/b", "flag/a"]


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


# -- every code the corpus HOLDS must have a key ------------------------------

def test_register_titles_key_to_the_same_code_as_the_cited_name():
    """DILA publishes articles under a decorated title ("Code général des impôts, CGI.")
    and judgments cite the plain name. The exact-equality match keyed only the latter, so
    all 22,832 held CGI articles were unreachable: 3,042 hanging references over 145,000
    citations, pointing at text the corpus already had."""
    from raglex.citations.french import code_article_alias, code_key

    assert code_key("Code général des impôts, CGI.") == "cgi"
    assert code_key("code général des impôts") == "cgi"
    assert code_key("Code de la sécurité sociale.") == "css"
    assert code_article_alias("Code général des impôts, CGI.", "1729") == "fr:code:cgi:1729"


def test_codes_beyond_the_original_twenty_are_keyed():
    """The whitelist covered 20 codes; the corpus holds 80-odd."""
    from raglex.citations.french import code_key

    assert code_key("Code rural et de la pêche maritime") == "crural"
    assert code_key("Code monétaire et financier") == "cmf"
    assert code_key("Code de la construction et de l'habitation.") == "cch"
    assert code_key("Code de l'action sociale et des familles") == "casf"


def test_superseded_and_territorial_codes_do_not_share_a_key():
    """Article L. 411-1 of the Code rural ancien is a different text from the same-numbered
    article of the current code — one key for both would resolve citations to whichever
    was minted last."""
    from raglex.citations.french import code_key

    assert code_key("Code rural ancien") == "cruralanc" != code_key("Code rural et de la pêche maritime")
    assert code_key("Code forestier (nouveau)") != code_key("Code forestier")
    assert code_key("Code minier (nouveau)") != code_key("Code minier")


def test_longest_code_name_wins_across_the_whole_table():
    """The alternation is first-match: "code des douanes" listed before "code des douanes
    de Mayotte" would key every Mayotte citation to the metropolitan code."""
    cites = [c for c in extract_citations("article 8 du code des douanes de Mayotte")
             if c.candidate_id]
    assert [c.candidate_id for c in cites] == ["fr:code:cdouanesmayotte:8"]


def test_new_code_names_extract_from_prose():
    cites = [c for c in extract_citations(
        "article L. 511-1 du code monétaire et financier et article 1729 du code général "
        "des impôts") if c.candidate_id]
    assert {c.candidate_id for c in cites} == {"fr:code:cmf:L511-1", "fr:code:cgi:1729"}

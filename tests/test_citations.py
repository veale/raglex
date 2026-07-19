from __future__ import annotations

from datetime import date

from raglex.citations import extract_citations, extract_document
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.resolve import Resolver
from raglex.storage import TextStore


# -- extraction (entity-level + pinpoints) ----------------------------------
def test_extracts_eu_regulation_with_article_pinpoint():
    cites = extract_citations("breach of Article 17 of Regulation (EU) 2016/679 occurred")
    c = next(c for c in cites if c.candidate_id == "32016R0679")
    assert c.entity_kind == "regulation" and c.pinpoint == "Article 17"


def test_extracts_gdpr_by_name_and_directive():
    cites = {c.candidate_id: c for c in extract_citations("under Article 22 GDPR and Directive 2002/58/EC")}
    assert cites["32016R0679"].pinpoint == "Article 22"
    assert "32002L0058" in cites  # directive → CELEX


def test_lowercase_article_keeps_pinpoint_on_acronym():
    # the acronym stays uppercase-only, but a lowercase "article" prefix must still
    # attach the pinpoint (previously "article 17 GDPR" dropped Article 17).
    c = next(c for c in extract_citations("a breach of article 17 GDPR") if c.candidate_id == "32016R0679")
    assert c.pinpoint == "Article 17"
    c = next(c for c in extract_citations("art. 6 of the DMA") if c.candidate_id == "32022R1925")
    assert c.pinpoint == "Article 6"


def test_eprivacy_regulation_does_not_map_to_the_directive():
    # "ePrivacy Regulation" is the withdrawn proposal, not Directive 2002/58 — it must
    # not mint a confidently-wrong edge to the existing Directive.
    assert not any(c.candidate_id == "32002L0058"
                   for c in extract_citations("the proposed ePrivacy Regulation"))


def test_old_style_eu_case_numbers_and_ecr_reports():
    # pre-1989 bare number (no court letter) → Court of Justice CELEX
    assert any(c.candidate_id == "61983CJ0240"
               for c in extract_citations("Case 240/83 Procureur de la Republique v ADBHU [1985] ECR 531."))
    # spaced dash, General Court (T) and Court of Justice (C)
    assert any(c.candidate_id == "61999TJ0344"
               for c in extract_citations("Case T - 344/99 Arne Mathisen AS v Council [2002] ECR II-2905."))
    assert any(c.candidate_id == "62003CJ0176"
               for c in extract_citations("Case C - 176/03 Commission v Council [2005] ECR I-7879, paras 47-48."))
    # ECR report citations (incl. OCR-garbled volume) → candidate-less "maybe" to dispose
    for s in ["[2002] ECR II-2905", "[2005] ECR I-7879", "[2002] ECR 11-2905", "[2001] ECR 1-1234"]:
        cs = extract_citations(s)
        assert cs and all(c.candidate_id is None for c in cs), s
    # a bare number with no "Case" cue must NOT be mistaken for a case (it's a ratio etc.)
    assert not any(c.method == "cjeu_case_number_old" for c in extract_citations("a 240/83 split"))
    # pre-1960 bare-number cases are 19xx, not 20xx: the bracketless form only existed
    # 1952–1989, so Case 9/56 (Meroni) is 1956 → 61956CJ0009, never 62056CJ0009.
    assert any(c.candidate_id == "61956CJ0009"
               for c in extract_citations("Case 9/56 Meroni v High Authority [1958] ECR 133."))
    assert any(c.candidate_id == "61955CJ0008"
               for c in extract_citations("Case 8/55 Fédéchar v High Authority [1956] ECR 245."))


def test_cjeu_case_number_accepts_various_pdf_dashes():
    for dash in ["-", "‑", "–", "—", "−"]:  # hyphen, non-breaking, en, em, minus
        cites = extract_citations(f"Case C{dash}311/18")
        assert any(c.candidate_id == "62018CJ0311" for c in cites), dash


def test_bare_eu_ecli_without_prefix_is_recognised():
    # PDF-stripped / OSCOLA style: "EU:C:2020:559" → the full ECLI
    assert any(c.candidate_id == "ECLI:EU:C:2020:559"
               for c in extract_citations("as held in EU:C:2020:559"))
    assert any(c.candidate_id == "ECLI:EU:T:2019:114"
               for c in extract_citations("EU:T:2019:114"))
    # the full prefixed form still works
    assert any(c.candidate_id == "ECLI:EU:C:2020:559"
               for c in extract_citations("ECLI:EU:C:2020:559"))
    # guards: not inside another word, and bare non-EU ECLIs stay ambiguous (no match)
    assert not extract_citations("xDEU:C:2020:1")
    assert not [c for c in extract_citations("NL:HR:2021:123") if c.candidate_id]


def test_eur_typecode_classifies_as_assimilated_uk_legislation():
    from raglex.citations.snowball import _classify
    from raglex.resolve.matchers import assimilated_celex
    assert _classify("eur/2008/1272", "eu_instrument") == ("Assimilated EU law (UK)", "GB", "uk-legislation")
    assert _classify("eudr/2000/60", "eu_instrument")[2] == "uk-legislation"
    assert assimilated_celex("eur/2008/1272") == "32008R1272"
    assert assimilated_celex("eudr/2000/60") == "32000L0060"


def test_bracketless_grammar_ignores_statute_abbreviations():
    # tax-statute abbreviations and report series must not mint fake neutral citations
    assert not [c for c in extract_citations("liability under 2009 CTA 2010") if c.candidate_id]
    assert not [c for c in extract_citations("reported at [1998] NI 9") if c.candidate_id]
    # a genuine bracketless citation still resolves
    assert any(c.candidate_id == "scc/2024/1" for c in extract_citations("see 2024 SCC 1"))


def test_statute_gazetteer_resolves_by_title_and_year_exactly():
    from raglex.citations.statute_gazetteer import resolve

    assert resolve("Freedom of Information Act", "2000") == "ukpga/2000/36"
    assert resolve("the Equality Act", "2010") == "ukpga/2010/15"
    assert resolve("AIDS (Control) Act", "1987") == "ukpga/1987/33"  # brackets normalised
    # the same title across years must match exactly — never a wrong-year guess
    assert resolve("Data Protection Act", "1998") == "ukpga/1998/29"
    assert resolve("Data Protection Act", None) is None  # ambiguous (1984/1998) → no guess
    assert resolve("Totally Made Up Act", "2099") is None


def test_named_statute_grammar_resolves_arbitrary_acts():
    # not in the curated handful — resolved purely via the vendored gazetteer
    cites = {c.candidate_id: c for c in extract_citations(
        "an order under section 31 of the Senior Courts Act 1981 and the Equality Act 2010")}
    assert cites["ukpga/2010/15"].entity_kind == "act"
    eq = next(c for c in extract_citations("breach of section 7 of the Data Protection Act 1998")
              if c.candidate_id == "ukpga/1998/29")
    assert eq.pinpoint == "s. 7"


def test_section_with_subsection_keeps_pinpoint():
    # the bracketed subsection must not break the pinpoint capture (it used to)
    cites = {c.candidate_id: c for c in extract_citations(
        "a breach of Section 166(2) of the Data Protection Act 2018")}
    assert cites["ukpga/2018/12"].pinpoint == "s. 166(2)"


def test_carry_forward_attaches_bare_provision_to_last_statute():
    t = ("Section 166 of the Data Protection Act 2018 applies. "
         "The tribunal also considered section 167 and Article 5.")
    by_pin = {c.pinpoint: c for c in extract_citations(t)}
    s167 = by_pin["s. 167"]
    assert s167.candidate_id == "ukpga/2018/12"  # carried forward to the DPA
    assert s167.method == "carry_forward" and s167.confidence < 1.0


def test_carry_forward_needs_a_legislation_antecedent():
    # no statute mentioned → a bare "section 5" is left alone (no false edge)
    cites = extract_citations("The judge said section 5 was decisive.")
    assert not [c for c in cites if c.method == "carry_forward"]


def test_self_citation_in_header_never_becomes_an_edge(catalogue, tmp_path):
    # a judgment's header prints its OWN neutral citation — that must not become
    # an outgoing edge (it used to resolve into a silent self-loop)
    ts = TextStore(tmp_path / "text")
    t = ("Neutral Citation Number: [2024] UKSC 12\n\nThe court considered "
         "Case C-311/18 and section 5 of the Data Protection Act 2018.")
    _doc(catalogue, ts, "uksc/2024/12", t, source="uk-caselaw")
    extract_document(catalogue, ts, "uksc/2024/12")
    edges = catalogue.relations_for("uksc/2024/12")
    assert not [e for e in edges if e["dst_id"] == "uksc/2024/12"]  # no self-loop
    assert {e["dst_id"] for e in edges} >= {"62018CJ0311", "ukpga/2018/12"}  # real cites kept
    # the observation row survives for the reader; only the edge is dropped
    assert any(c["candidate_id"] == "uksc/2024/12"
               for c in catalogue.citations_for("uksc/2024/12"))


def test_self_citation_via_alias_also_dropped(catalogue, tmp_path):
    # the report citation a case was published at aliases to the case itself —
    # the case's own header mention of it must not become an edge either
    ts = TextStore(tmp_path / "text")
    catalogue.put_alias("(1884) 12 qbd 271", "ewhc/qb/1884/1", source="bailii-self-report")
    _doc(catalogue, ts, "ewhc/qb/1884/1", "(1884) 12 QBD 271\n\nBRADLAUGH v. GOSSETT.",
         source="uk-caselaw")
    extract_document(catalogue, ts, "ewhc/qb/1884/1")
    assert not [e for e in catalogue.relations_for("ewhc/qb/1884/1")
                if (e["raw_citation_string"] or "").lower().find("qbd") >= 0]


def test_carry_forward_suppressed_inside_legislation(catalogue, tmp_path):
    # Inside an act/directive, a bare "Article 3" is the instrument referring to
    # ITSELF — it must NOT be carried forward onto the directive named earlier
    # (it used to link to whatever the recitals mentioned last).
    ts = TextStore(tmp_path / "text")
    t = ("This Directive complements Directive 2011/83/EU. "
         "Article 3 shall apply to any contract.")
    _doc(catalogue, ts, "32019L0770", t, doc_type=DocType.LEGISLATION)
    extract_document(catalogue, ts, "32019L0770")
    assert not [c for c in catalogue.citations_for("32019L0770")
                if c["method"] == "carry_forward"]
    # the same text in a JUDGMENT keeps the heuristic (it's built for judgments)
    _doc(catalogue, ts, "uksc/2024/9", t)
    extract_document(catalogue, ts, "uksc/2024/9")
    assert [c for c in catalogue.citations_for("uksc/2024/9")
            if c["method"] == "carry_forward"]


def test_named_alias_rule_links_phrase_to_target():
    # a user shorthand rule ("UK GDPR" → a document) links every occurrence
    aliases = {"UK GDPR": "european/regulation/2016/0679"}
    cites = {c.candidate_id: c for c in extract_citations(
        "processing under the UK GDPR was unlawful", aliases=aliases)}
    c = cites["european/regulation/2016/0679"]
    assert c.method == "named_alias" and c.entity_kind == "named"


def test_named_alias_longer_phrase_wins():
    aliases = {"GDPR": "a", "UK GDPR": "b"}
    cites = extract_citations("the UK GDPR applies", aliases=aliases)
    named = [c for c in cites if c.method == "named_alias"]
    assert {c.candidate_id for c in named} == {"b"}  # not the shorter "GDPR" → a


def test_named_alias_matches_phrase_with_non_word_edges():
    # a phrase beginning/ending in a non-word char ("(UK) GDPR") must still match —
    # a bare \b at those edges demands an impossible boundary and never fires.
    aliases = {"(UK) GDPR": "european/regulation/2016/0679"}
    cites = [c for c in extract_citations("processing under the (UK) GDPR regime", aliases=aliases)
             if c.method == "named_alias"]
    assert cites and cites[0].candidate_id == "european/regulation/2016/0679"


def test_act_abbreviation_not_matched_inside_longer_word():
    # "FOIA" must not match inside "FOIAs"/"FOIAble" (word-boundary after the name).
    assert not any(c.method == "uk_act_section"
                   for c in extract_citations("multiple FOIAs were filed"))
    # but the standalone abbreviation still resolves
    assert any(c.candidate_id == "ukpga/2000/36"
               for c in extract_citations("a request under FOIA was refused"))


def test_directive_two_digit_year_resolves_to_celex():
    # old EU instruments use 2-digit years ("Directive 95/46" = the 1995 Data
    # Protection Directive); directives are year/number, regulations number/year.
    by = {c.candidate_id for c in extract_citations(
        "the Directive 95/46 was repealed; see also Regulation (EEC) No 1612/68")}
    assert "31995L0046" in by   # Directive 95/46/EC
    assert "31968R1612" in by   # Regulation 1612/68


def test_extracts_cases_ecli_ncn_and_cjeu_number():
    cites = {c.candidate_id for c in extract_citations(
        "see Case C-311/18, ECLI:EU:C:2020:559 and [2024] UKSC 12")}
    assert "62018CJ0311" in cites  # CJEU case number → CELEX
    assert "ECLI:EU:C:2020:559" in cites and "uksc/2024/12" in cites


def test_extracts_uk_act_section_and_uri():
    cites = extract_citations(
        "refused under section 14 of the Freedom of Information Act 2000; "
        "see legislation.gov.uk/ukpga/2000/36/section/40")
    by = {(c.candidate_id, c.pinpoint) for c in cites}
    assert ("ukpga/2000/36", "s. 14") in by and ("ukpga/2000/36", "s. 40") in by


def test_overlapping_match_keeps_most_specific():
    # "Article 17 of Regulation (EU) 2016/679" should win over the bare CELEX-less
    # number; the article-scoped citation carries the pinpoint
    cites = extract_citations("Article 17 of Regulation (EU) 2016/679")
    assert len(cites) == 1 and cites[0].pinpoint == "Article 17"


# -- stage: hanging edges that resolve later --------------------------------
def _doc(catalogue, ts, stable_id, text, **kw):
    rec = Record(source=kw.get("source", "x"), stable_id=stable_id,
                 ecli=kw.get("ecli"), doc_type=kw.get("doc_type", DocType.JUDGMENT),
                 decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                 extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def _prelim_edge(catalogue, stable_id, ref_text):
    from raglex.core.models import ExtractedVia, RelationshipType, ResolutionStatus, TypedRelation
    catalogue.add_relations(stable_id, [TypedRelation(
        relationship_type=RelationshipType.PRELIMINARY_REFERENCE,
        raw_citation_string=ref_text, extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING)])


def test_cjeu_uk_statute_only_resolves_for_uk_referred_preliminary_ruling(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    txt = "The Court considered the Equality Act 2010 in its reasoning."

    # 1) CJEU case referred by a German court → the UK-statute name must NOT resolve
    _doc(catalogue, ts, "ECLI:EU:C:2020:001", txt, source="eu-cellar", ecli="ECLI:EU:C:2020:001")
    _prelim_edge(catalogue, "ECLI:EU:C:2020:001", "Bundesgerichtshof | country: Germany")
    extract_document(catalogue, ts, "ECLI:EU:C:2020:001")
    named = [c for c in catalogue.citations_for("ECLI:EU:C:2020:001") if c["method"] == "uk_statute_named"]
    assert named and all(c["candidate_id"] is None for c in named)  # suppressed → name-only

    # 2) CJEU case referred by a UK court → it resolves normally
    _doc(catalogue, ts, "ECLI:EU:C:2020:002", txt, source="eu-cellar", ecli="ECLI:EU:C:2020:002")
    _prelim_edge(catalogue, "ECLI:EU:C:2020:002", "Upper Tribunal | country: United Kingdom")
    extract_document(catalogue, ts, "ECLI:EU:C:2020:002")
    named2 = [c for c in catalogue.citations_for("ECLI:EU:C:2020:002") if c["method"] == "uk_statute_named"]
    assert any(c["candidate_id"] == "ukpga/2010/15" for c in named2)

    # 3) an ordinary UK judgment (not CJEU) is never gated
    _doc(catalogue, ts, "uksc/2024/1", txt, source="uk-caselaw")
    extract_document(catalogue, ts, "uksc/2024/1")
    named3 = [c for c in catalogue.citations_for("uksc/2024/1") if c["method"] == "uk_statute_named"]
    assert any(c["candidate_id"] == "ukpga/2010/15" for c in named3)


def test_extracted_citation_hangs_then_resolves_on_harvest(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "case-1", "The court applied Article 17 of Regulation (EU) 2016/679.")

    n = extract_document(catalogue, ts, "case-1")
    assert n >= 1
    edge = catalogue.relations_for("case-1")[0]
    assert edge["dst_id"] == "32016R0679" and edge["dst_anchor"] == "Article 17"
    assert edge["resolution_status"] == "pending"  # GDPR not in corpus yet

    # harvest the GDPR; resolution turns the pinpoint citation into a live edge
    _doc(catalogue, ts, "32016R0679", "Article 17 Right to erasure.", doc_type=DocType.LEGISLATION)
    Resolver(catalogue).run()
    edge = catalogue.relations_for("case-1")[0]
    assert edge["resolution_status"] == "resolved" and edge["dst_anchor"] == "Article 17"


def test_celex_to_ecli_alias_resolves_case_number(catalogue, tmp_path):
    """A 'C-311/18' citation (candidate = CELEX) resolves to the ECLI-keyed
    judgment once the CELEX→ECLI alias is known (registered on harvest)."""
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "citing", "following Case C-311/18 the court held")
    # the judgment is keyed by ECLI; register the CELEX→ECLI alias (as the pipeline does)
    _doc(catalogue, ts, "ECLI:EU:C:2020:559", "judgment text", ecli="ECLI:EU:C:2020:559")
    catalogue.put_alias("62018cj0311", "ECLI:EU:C:2020:559", source="celex-ecli")

    extract_document(catalogue, ts, "citing")
    Resolver(catalogue).run()
    edge = next(e for e in catalogue.relations_for("citing") if e["raw_citation_string"].startswith("Case C-311"))
    assert edge["resolution_status"] == "resolved" and edge["dst_id"] == "ECLI:EU:C:2020:559"


def test_idempotent_reextraction(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "case-1", "Article 22 GDPR applies.")
    extract_document(catalogue, ts, "case-1")
    extract_document(catalogue, ts, "case-1")  # re-run clears prior regex edges
    regex_edges = [e for e in catalogue.relations_for("case-1") if e["extracted_via"] == "regex"]
    assert len(regex_edges) == 1


# -- CJEU procedure suffixes + generic neutral citations --------------------
def test_cjeu_case_number_with_procedure_suffix():
    # "C-11/26 P" (appeal), "C-619/18 PPU" (urgent), "T-1/24 R" (interim) must
    # parse (suffix consumed, not breaking the match) → CELEX candidates.
    cites = {c.candidate_id: c.raw for c in extract_citations(
        "the appeal C-11/26 P, the order T-1/24 R and the urgent reference C-619/18 PPU")}
    assert "62026CJ0011" in cites and "P" in cites["62026CJ0011"]
    assert "62024TJ0001" in cites and "62018CJ0619" in cites


def test_generic_neutral_citation_detects_unknown_courts():
    # The detector recognises the *shape* even for a court we don't know — the
    # snowball signal. Divisions ("EWCA Civ") fold into the slug.
    cites = {c.candidate_id for c in extract_citations(
        "[2023] EWCA Civ 1631, 2024 SCC 9, [2022] NZSC 3 and [2021] ZZTOP 5")}
    assert "ewca/civ/2023/1631" in cites  # division captured
    assert "scc/2024/9" in cites          # bracketless (Canada)
    assert "nzsc/2022/3" in cites
    assert "zztop/2021/5" in cites          # unknown court still detected


def test_law_report_citations_are_candidate_less_maybes():
    # report-series citations look like neutral citations but have no neutral cite —
    # they must NOT mint a wrong candidate; a real neutral citation still resolves.
    by = {c.raw: c for c in extract_citations(
        "see [2023] 1 WLR 1327 and [2022] AACR 4, cf [2021] UKUT 299 (AAC)")}
    assert by["[2023] 1 WLR 1327"].candidate_id is None        # law report → maybe
    assert by["[2023] 1 WLR 1327"].method == "law_report"
    assert by["[2022] AACR 4"].candidate_id is None            # report series, not a court
    assert by["[2021] UKUT 299 (AAC)"].candidate_id == "ukut/aac/2021/299"  # real neutral cite


def test_neutral_citation_recovers_chamber_from_parenthetical():
    # the chamber/division is in the citation (in parens after the number) and must
    # fold into the slug so it matches the Find Case Law URI (not 404).
    cites = {c.candidate_id for c in extract_citations(
        "[2012] UKUT 440 (AAC), [2024] EWHC 22 (Admin) and [2024] UKFTT 100 (GRC)")}
    assert "ukut/aac/2012/440" in cites
    assert "ewhc/admin/2024/22" in cites
    assert "ukftt/grc/2024/100" in cites


def test_ecli_matches_any_jurisdiction():
    cites = {c.candidate_id for c in extract_citations(
        "ECLI:NL:HR:2021:1234, ECLI:DE:BGH:2019:abc and ECLI:EU:C:2020:559")}
    assert {"ECLI:NL:HR:2021:1234", "ECLI:DE:BGH:2019:ABC", "ECLI:EU:C:2020:559"} <= cites


# -- snowball: the citation frontier ----------------------------------------
def test_snowball_classifies_frontier_and_flags_missing_adapter(catalogue, tmp_path):
    from raglex.citations import snowball

    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "case-1",
         "applying Article 17 of Regulation (EU) 2016/679, following Case C-311/18, "
         "see also [2024] EWHC 5 and the foreign [2021] ZZTOP 9")
    extract_document(catalogue, ts, "case-1")  # writes the citations audit rows

    rows = snowball(catalogue, limit=50)
    forms = {r["form"]: r for r in rows}
    # EU regulation + CJEU judgment are harvestable; the unknown ZZTOP court is not
    assert forms["EU regulation"]["adapter"] == "eu-legislation"
    assert forms["CJEU judgment"]["adapter"] == "eu-cellar"
    zz = next(r for r in rows if "ZZTOP" in r["form"])
    assert zz["adapter"] is None and zz["harvestable"] is False

    needs = snowball(catalogue, limit=50, only_unharvestable=True)
    assert all(not r["harvestable"] for r in needs)
    assert any("ZZTOP" in r["form"] for r in needs)


def test_echr_convention_article_not_carried_forward_to_eu_instrument():
    # "Article 10 of the Convention" after an EU directive must resolve to the ECHR,
    # NOT carry forward to the directive
    cites = extract_citations("breach of Directive 2002/58/EC; Article 10 of the Convention engaged")
    by = {c.candidate_id: c for c in cites}
    assert "32002L0058" in by
    conv = by.get("echr/convention")
    assert conv and conv.pinpoint == "Article 10" and conv.method == "echr_convention_article"
    # the bare "Article 10" was NOT also carried forward to the directive
    assert not any(c.method == "carry_forward" and c.candidate_id == "32002L0058"
                   and c.pinpoint == "Article 10" for c in cites)
    assert any(c.candidate_id == "echr/convention" for c in extract_citations("Article 8 ECHR"))
    # a *named* convention (Geneva) is not the ECHR
    assert not any(c.candidate_id == "echr/convention"
                   for c in extract_citations("Article 33 of the Geneva Convention"))


def test_carry_forward_respects_cue_kind_section_not_eu_directive():
    # A bare "section N" must attach to a UK Act, never an EU directive (which has Articles),
    # even when the directive is the nearer antecedent — the Environmental-Information bug.
    txt = ("The Communications Act 2003 is in point. It gave effect to Directive 2003/4. "
           "Ofcom relied on section 66 in this appeal, and on Article 4 of the measure.")
    cf = {c.raw.lower(): c.candidate_id for c in extract_citations(txt) if c.method == "carry_forward"}
    assert cf.get("section 66") == "ukpga/2003/21"     # → Communications Act, not the directive
    assert cf.get("article 4") == "32003L0004"          # → the directive, not the Act


def test_uk_statute_names_stay_name_only_inside_irish_judgments(catalogue, tmp_path):
    # "<X> Act 2018" inside an IRISH judgment is an Act of the Oireachtas — the UK
    # candidate must be dropped (name-only), while EU instruments and case citations
    # of any jurisdiction resolve normally.
    ts = TextStore(tmp_path / "text")
    t = ("Section 5 of the Data Protection Act 2018 applies; see Article 17 of "
         "Regulation (EU) 2016/679, Smith v Jones [2020] EWCA Civ 99 and [2019] IESC 4.")
    _doc(catalogue, ts, "iehc/2024/1", t, source="ie-caselaw")
    extract_document(catalogue, ts, "iehc/2024/1")
    by_method = {}
    for c in catalogue.citations_for("iehc/2024/1"):
        by_method.setdefault(c["method"], []).append(c["candidate_id"])
    assert by_method["uk_act_section"] == [None]          # UK statute name suppressed
    dsts = {e["dst_id"] for e in catalogue.relations_for("iehc/2024/1") if e["dst_id"]}
    assert {"32016R0679", "ewca/civ/2020/99", "iesc/2019/4"} <= dsts


def test_recitals_pinpoint_to_the_instrument():
    by = {c.raw: c for c in extract_citations(
        "See Recital 47 of the GDPR, recital 65 of Regulation (EU) 2016/679, "
        "Recitals 26 and 27 of the GDPR, and recital (11) of the Digital Markets Act.")}
    assert by["Recital 47 of the GDPR"].candidate_id == "32016R0679"
    assert by["Recital 47 of the GDPR"].pinpoint == "Recital 47"
    assert by["recital 65 of Regulation (EU) 2016/679"].pinpoint == "Recital 65"
    assert by["Recitals 26 and 27 of the GDPR"].pinpoint == "Recitals 26 and 27"
    assert by["recital (11) of the Digital Markets Act"].candidate_id == "32022R1925"


def test_bare_recital_carries_forward_to_named_instrument():
    cites = extract_citations("The GDPR is central. Recital 47 explains consent.")
    rec = next(c for c in cites if c.raw == "Recital 47")
    assert rec.candidate_id == "32016R0679" and rec.pinpoint == "Recital 47"
    assert rec.method == "carry_forward"


def test_uk_gdpr_maps_to_the_assimilated_instrument_not_the_eu_original():
    # "the UK GDPR" is the domestic assimilated regulation (eur/2016/679), distinct from
    # the EU original (32016R0679) — and the article/recital must survive.
    by = {c.raw: c for c in extract_citations(
        "Article 20 of the UK GDPR and recital (26) of the UK GDPR apply.")}
    assert by["Article 20 of the UK GDPR"].candidate_id == "european/regulation/2016/0679"
    assert by["Article 20 of the UK GDPR"].pinpoint == "Article 20"
    assert by["recital (26) of the UK GDPR"].candidate_id == "european/regulation/2016/0679"
    assert by["recital (26) of the UK GDPR"].pinpoint == "Recital 26"


def test_digital_regulation_names_resolve_with_subsection_pinpoints():
    by = {c.candidate_id: c for c in extract_citations(
        "Article 8(2) of the DMA and Article 5 of the DSA and the Law Enforcement Directive.")}
    assert by["32022R1925"].pinpoint == "Article 8(2)"   # DMA, subsection kept
    assert by["32022R2065"].pinpoint == "Article 5"      # DSA
    assert "32016L0680" in by                             # LED full name
    # the common word "led" (lower-case) must NOT resolve to the Law Enforcement Directive
    assert all(c.candidate_id != "32016L0680" for c in extract_citations("she led the team"))


def test_eu_guidance_links_eu_law_and_case_law_but_not_domestic_statute(catalogue, tmp_path):
    # An EDPB guidance document links EU legislation (CELEX), CJEU + ECHR case law
    # (ECLI) and English/Irish case-law neutral citations — all unambiguous — but a
    # bare domestic statute NAME is a cross-jurisdiction collision, kept as name-only.
    ts = TextStore(tmp_path / "text")
    t = ("This guidance concerns Article 17 of Regulation (EU) 2016/679; see "
         "ECLI:EU:C:2014:317, Smith v Jones [2020] EWCA Civ 99 and [2019] IESC 4. "
         "Section 5 of the Data Protection Act 2018 is mentioned in passing.")
    _doc(catalogue, ts, "edpb/guidelines-x", t, source="edpb", doc_type=DocType.GUIDANCE)
    extract_document(catalogue, ts, "edpb/guidelines-x")
    by_method = {}
    for c in catalogue.citations_for("edpb/guidelines-x"):
        by_method.setdefault(c["method"], []).append(c["candidate_id"])
    assert by_method["uk_act_section"] == [None]          # domestic statute name suppressed
    dsts = {e["dst_id"] for e in catalogue.relations_for("edpb/guidelines-x") if e["dst_id"]}
    # EU law, CJEU ECLI, and both English + Irish case-law citations DO link
    assert {"32016R0679", "ECLI:EU:C:2014:317", "ewca/civ/2020/99", "iesc/2019/4"} <= dsts


def test_irish_neutral_citations_mint_candidates_and_reporters_stay_maybe():
    by = {c.raw: c for c in extract_citations(
        "see [2008] IEHC 56, [2004] IESC 1, [1999] 1 IR 12 and [2001] 1 ILRM 22")}
    assert by["[2008] IEHC 56"].candidate_id == "iehc/2008/56"
    assert by["[2004] IESC 1"].candidate_id == "iesc/2004/1"
    assert by["[1999] 1 IR 12"].candidate_id is None      # Irish Reports: lookup-only
    assert by["[2001] 1 ILRM 22"].candidate_id is None    # ILRM: lookup-only


def test_commonwealth_neutral_citations_classified_by_jurisdiction():
    # CA/AU/NZ (and NI/Scotland) citations are understood BEFORE any import route
    # exists: candidates mint, and they bucket as pending case-law from their place.
    from raglex.adapters.bailii import external_link
    from raglex.citations.taxonomy import classify_candidate

    by = {c.raw: c for c in extract_citations(
        "see [2020] NZSC 12, 2019 SCC 65, [2020] HCA 5, [2020] NICA 5 and [2021] CSOH 100")}
    assert by["[2020] NZSC 12"].candidate_id == "nzsc/2020/12"
    assert by["2019 SCC 65"].candidate_id == "scc/2019/65"      # Canada is bracketless
    assert by["[2020] HCA 5"].candidate_id == "hca/2020/5"
    assert classify_candidate("nzsc/2020/12").category == "nz-caselaw"
    assert classify_candidate("scc/2019/65").category == "ca-caselaw"
    assert classify_candidate("hca/2020/5").category == "au-caselaw"
    # NI + Scotland are UK case-law with their court sub-type, never "other"
    assert classify_candidate("nica/2020/5").category == "uk-caselaw"
    assert classify_candidate("csoh/2021/100").subtype_label.startswith("Court of Session")
    # and each jurisdiction links to ITS institute, not a BAILII search that can't hit
    assert "canlii" in external_link("scc/2019/65", None)["url"]
    assert "austlii" in external_link("hca/2020/5", None)["url"]
    assert "nzlii" in external_link("nzsc/2020/12", None)["url"]


def test_commonwealth_and_scots_reporters_stay_candidate_less():
    # ordinal Canadian series, Australian reports, and Scots bare-year reports are
    # recognised as report citations (lookup-only) — never fake court slugs
    for t in ["(1990) 70 DLR (4th) 385", "(1976) 60 CCC (2d) 30", "[1992] 175 CLR 1",
              "[1990] 2 SCR 217", "(1985) 17 A Crim R 1", "[1971] NZLR 1041",
              "2012 SC 1", "2011 SLT 651"]:
        cs = extract_citations(t)
        assert cs and all(c.candidate_id is None for c in cs), t
        assert all(c.entity_kind == "case" for c in cs), t

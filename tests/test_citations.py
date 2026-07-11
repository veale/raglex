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

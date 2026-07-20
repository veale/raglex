"""OSCOLA (5th ed.) citation formatting — one source of truth for how a held document
names itself, degrading gracefully as metadata thins out."""

from raglex.citations.oscola import cite


def _italic(out):
    return "".join(p["t"] for p in out["parts"] if p["i"])


def test_uk_case_neutral_citation_from_slug():
    out = cite({"stable_id": "eat/2022/12", "source": "uk-caselaw", "doc_type": "judgment",
                "title": "Guardian News & Media Ltd v Rozanov"})
    assert out["text"] == "Guardian News & Media Ltd v Rozanov [2022] EAT 12"
    # the party name is italicised, the neutral citation is not
    assert _italic(out) == "Guardian News & Media Ltd v Rozanov"


def test_uk_ewhc_chamber_citation():
    out = cite({"stable_id": "ewhc/comm/2015/3076", "source": "uk-caselaw",
                "doc_type": "judgment", "title": "Foo v Bar"})
    assert out["text"] == "Foo v Bar [2015] EWHC 3076 (Comm)"


def test_uk_pre_2001_pseudo_neutral():
    out = cite({"stable_id": "ukhl/1998/1", "source": "uk-hol", "doc_type": "judgment",
                "title": "Kleinwort Benson v Lincoln CC"})
    assert "(pseudo-neutral citation)" in out["text"]


def test_eu_judgment_from_celex():
    out = cite({"stable_id": "ECLI:EU:C:2005:446", "source": "eu-cellar", "ecli": "ECLI:EU:C:2005:446",
                "doc_type": "judgment", "court": "Court of Justice", "title": "Schempp v Finanzamt"},
               {"celex": "62003CJ0403"})
    assert out["text"] == "Case C-403/03 Schempp v Finanzamt EU:C:2005:446"
    assert _italic(out) == "Schempp v Finanzamt"


def test_eu_ag_opinion_tail():
    out = cite({"stable_id": "ECLI:EU:C:2017:329", "source": "eu-cellar", "ecli": "ECLI:EU:C:2017:329",
                "doc_type": "opinion", "court": "Advocate General", "title": None},
               {"celex": "62016CC0189"})
    assert out["text"].startswith("Case C-189/16 EU:C:2017:329")
    assert out["text"].endswith("Opinion of AG")


def test_eu_ag_opinion_boilerplate_title_is_not_treated_as_case_name():
    # 6,966 opinions carry the delivery boilerplate as their title; it must not land
    # in the italic party slot, and the AG's name is lifted out of it for the tail
    out = cite({"stable_id": "ECLI:EU:C:2020:7", "source": "eu-cellar",
                "ecli": "ECLI:EU:C:2020:7", "doc_type": "opinion",
                "court": "Advocate General",
                "title": "Opinion of Advocate General Campos Sánchez-Bordona "
                         "delivered on 15 January 2020"},
               {"celex": "62018CC0520"})
    assert out["text"] == "Case C-520/18 EU:C:2020:7, Opinion of AG Campos Sánchez-Bordona"
    # the boilerplate is not rendered as italic parties
    assert not any(p["i"] and "Advocate General" in p["t"] for p in out["parts"])


def test_eu_opinion_with_real_case_name_keeps_it():
    out = cite({"stable_id": "ECLI:EU:C:2019:9", "source": "eu-cellar",
                "ecli": "ECLI:EU:C:2019:9", "doc_type": "opinion",
                "court": "Advocate General", "title": "La Quadrature du Net and Others"},
               {"celex": "62018CC0511"})
    assert "La Quadrature du Net and Others" in out["text"]
    assert any(p["i"] and "La Quadrature" in p["t"] for p in out["parts"])


def test_echr_with_appno_formation_date():
    out = cite({"stable_id": "echr/001-1", "source": "echr", "doc_type": "judgment",
                "title": "Broniowski v Poland"},
               {"extractedappno": "31443/96", "kpdate": "2004-06-22T00:00:00",
                "doctypebranch": "GRANDCHAMBER"})
    assert out["text"] == "Broniowski v Poland [GC] ECtHR App No 31443/96 (22 June 2004)"


def test_legislation_uses_title_verbatim():
    out = cite({"stable_id": "ukpga/2018/12", "source": "uk-legislation",
                "doc_type": "legislation", "title": "Data Protection Act 2018"})
    assert out["text"] == "Data Protection Act 2018"
    assert _italic(out) == ""  # legislation titles are not italicised


def test_fallback_to_stable_id():
    out = cite({"stable_id": "misc/thing", "source": "other", "doc_type": "commentary", "title": None})
    assert out["text"] == "misc/thing"


# -- neutral-citation jurisdictions beyond the UK ----------------------------

def test_canadian_neutral_citations_are_not_bracketed():
    """Canada writes "2001 SCC 79" bare where the UK writes "[2021] UKSC 12". Routing
    Canadian cases through the UK formatter would bracket every one of them."""
    out = cite({"stable_id": "scc/2001/79", "source": "ca-caselaw",
                "doc_type": "judgment", "title": "Cooper v. Hobart"})
    assert out["text"] == "Cooper v Hobart 2001 SCC 79"
    assert _italic(out) == "Cooper v Hobart"      # OSCOLA italicises the case name only


def test_australian_and_nz_cases_keep_their_brackets():
    assert cite({"stable_id": "hca/2020/1", "source": "au-caselaw", "doc_type": "judgment",
                 "title": "Smith v Jones"})["text"] == "Smith v Jones [2020] HCA 1"
    assert cite({"stable_id": "nzsc/2005/1", "source": "nz-caselaw", "doc_type": "judgment",
                 "title": "Brown v Crown"})["text"] == "Brown v Crown [2005] NZSC 1"


def test_an_unknown_court_code_falls_back_to_the_title():
    """Better a bare title than a confidently wrong citation in an unregistered style."""
    out = cite({"stable_id": "zzzz/2020/1", "source": "xx-caselaw", "doc_type": "judgment",
                "title": "Anonymous v Anonymous"})
    assert out["text"] == "Anonymous v Anonymous"


# -- CJEU titles carrying BAILII's markers -----------------------------------

def test_bailii_document_type_and_database_markers_are_stripped():
    """"(Judgment)", "French Text" and the trailing "[2015] EUECJ T-372/12" are BAILII
    apparatus, not the case name — OSCOLA wants "Case T-372/12 … EU:T:…"."""
    out = cite({"stable_id": "ECLI:EU:T:2018:370", "source": "eu-cellar",
                "doc_type": "judgment",
                "title": "Haverkamp IP v EUIPO - Sissel (Tapis de sol) (Judgment) "
                         "French Text [2018] EUECJ T-521/16"})
    assert out["text"] == "Case T-521/16 Haverkamp IP v EUIPO - Sissel (Tapis de sol) EU:T:2018:370"


def test_the_case_number_is_recovered_from_the_stripped_euecj_tail():
    """The BAILII-archive CJEU documents carry no CELEX and no ECLI, so that discarded
    tail is the only place their case number appears."""
    out = cite({"stable_id": "euecj/2003/t21298", "source": "eu-cellar",
                "doc_type": "judgment",
                "title": "Atlantic Container Line & Ors v Commission (Competition) "
                         "[2003] EUECJ T-212/98"})
    assert out["text"] == "Case T-212/98 Atlantic Container Line & Ors v Commission (Competition)"


def test_a_case_number_already_in_the_title_is_not_printed_twice():
    out = cite({"stable_id": "euecj/2015/t37212", "source": "eu-cellar",
                "doc_type": "judgment",
                "title": "Case T-372/12 El Corte Ingles v OHMI - Apro Tech (APRO) "
                         "(Judgment) [2015] EUECJ T-372/12"})
    assert out["text"] == "Case T-372/12 El Corte Ingles v OHMI - Apro Tech (APRO)"


def test_an_ecli_keyed_stable_id_supplies_the_ecli_when_the_column_is_null():
    """4,180 CJEU rows are keyed by ECLI but never had the ecli column populated;
    without this they cite as though they had no identifier at all."""
    out = cite({"stable_id": "ECLI:EU:C:2020:559", "source": "eu-cellar",
                "doc_type": "judgment", "title": "Schrems"})
    assert "EU:C:2020:559" in out["text"]

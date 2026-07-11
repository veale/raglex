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

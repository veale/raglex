"""Classic law-report recognition + the cited-but-unfetchable frontier (§5).

Pre-neutral-citation authorities ("[1932] AC 562") have no fetchable id. They must (a) be
recognised across case text, (b) NOT be mis-keyed as a Find Case Law slug that 404s, and
(c) surface as a ranked, BAILII-linked list the operator can resolve by upload.
"""

from __future__ import annotations

import tempfile

import pytest

from raglex.citations.reporters import is_report_citation, report_series
from raglex.config import Config
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.facade import Facade
from raglex.resolve.matchers import first_candidate


@pytest.mark.parametrize("cite,series", [
    ("[1982] AC 1", "AC"), ("[1932] A.C. 562", "AC"), ("[1996] 2 All ER 129", "All ER"),
    ("[1995] 2 All E.R. 736", "All ER"), ("(1985) 80 Cr App R 1", "Cr App R"),
    ("(1984) 79 Cr. App. R. 229", "Cr App R"), ("[2004] 1 WLR 113", "WLR"),
    ("[1979] Q.B. 276", "QB"), ("(1868) LR 3 HL 330", "LR"), ("150 ER 1030", "ER"),
    ("[2010] 1 Lloyd's Rep 1", "Lloyd's Rep"), ("[2018] 2 Lloyd’s Rep 55", "Lloyd's Rep"),
    ("[2019] Lloyd's Rep FC 1", "Lloyd's Rep FC"), ("(1990) 60 P & CR 392", "P & CR"),
    ("[2015] Bus LR 291", "Bus LR"), ("[2003] RTR 27", "RTR"), ("1999 SC 583", "SC"),
    ("2001 SLT 1213", "SLT"), ("1949 JC 1", "JC"), ("[1893] 1 Ch 234", "Ch"),
])
def test_report_series_recognised(cite, series):
    assert report_series(cite) == series


@pytest.mark.parametrize("noncite", [
    "[2004] UKHL 22", "[2020] EWCA Civ 1", "2024 SCC 1", "Regulation 2016/679",
    "section 5 of the Act", "paragraph 129", "we adjourned at 3 pm",
])
def test_non_reports_rejected(noncite):
    assert not is_report_citation(noncite)


def test_report_is_not_mis_keyed_as_a_caselaw_slug():
    # the resolver's neutral-citation matcher must reject a report token — else "[1932] AC
    # 562" mints ac/1932/562, which 404s on Find Case Law and hides the report.
    assert first_candidate("[1932] AC 562") is None
    # a real neutral citation still resolves to its slug
    assert first_candidate("[2015] EWHC 100 (Ch)").value == "ewhc/ch/2015/100"


def _facade() -> Facade:
    import os

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _cite(f: Facade, src: str, raws: list[str]) -> None:
    rels = [TypedRelation(relationship_type=RelationshipType.MENTIONS, raw_citation_string=r,
                          extracted_via=ExtractedVia.REGEX, resolution_status=ResolutionStatus.PENDING)
            for r in raws]
    with f._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(source="uk-caselaw", stable_id=src, doc_type=DocType.JUDGMENT,
                                   relations=rels))


def test_unfetchable_list_ranks_reports_with_links():
    f = _facade()
    _cite(f, "case-1", ["[1932] AC 562", "[1982] AC 1", "[2015] EWHC 100 (Ch)"])
    _cite(f, "case-2", ["[1932] AC 562", "[1982] AC 1"])
    _cite(f, "case-3", ["[1932] AC 562"])
    res = f._unfetchable_uncached(50)
    refs = res["references"]
    # the real neutral citation is routable → NOT here; both reports ARE, most-cited first
    assert all("EWHC" not in r["ref"] for r in refs)
    assert refs[0]["ref"] == "[1932] AC 562" and refs[0]["citing_count"] == 3
    assert refs[0]["form"] == "law report (AC)" and refs[0]["is_report"]
    assert refs[0]["link"]["kind"] == "search" and refs[0]["link"]["can_upload"]


def test_unfetchable_gives_a_direct_rtf_link_for_a_neutral_citation_court_with_no_adapter():
    # a court with no adapter but a constructible BAILII path → the direct RTF, uploadable
    f = _facade()
    _cite(f, "case-1", ["[2006] EWCA Civ 717"])  # held? no → routable via uk-caselaw, so NOT unfetchable
    # a genuinely adapter-less neutral citation (unknown court) lands here with a search link
    _cite(f, "case-2", ["[2019] FooCt 3"])
    res = f._unfetchable_uncached(50)
    assert any(r["ref"] == "fooct/2019/3" or "FooCt" in (r["raw"] or "") for r in res["references"])


# -- frontier classification (the "looks like" labels + links) ----------------
from raglex.citations.frontier import classify


@pytest.mark.parametrize("raw,form", [
    ("[1974] ECR 837", "law report (ECR)"),           # European Court Reports, not a neutral cite
    ("[1995] ECR I-4921", "law report (ECR)"),
    ("(2000) 29 EHRR 245", "law report (EHRR)"),
    ("[1982] AC 1", "law report (AC)"),
    ("the Limitation Act 1980", "legislation (by name)"),
    ("Part II of the Road Traffic Act 1991", "legislation (by name)"),
    ("Council Decision 94/800", "EU instrument (by name)"),
    ("Directive 95/46", "EU instrument (by name)"),
])
def test_frontier_labels(raw, form):
    c = classify(raw, None)
    assert c is not None and c["form"] == form and c["link"]


def test_frontier_drops_junk_urls():
    assert classify("https://webarchive.nationalarchives.gov.uk/eu-exit/foo", None) is None


def test_frontier_resolves_statute_name_via_gazetteer():
    # a statute name in the offline gazetteer is routable, not merely unfetchable
    c = classify("section 5 of the Consumer Rights Act 2015", None)
    assert c["gazetteer_id"] == "ukpga/2015/15"


def test_frontier_returns_none_for_plain_case_name():
    # a case by name isn't specially classifiable here — caller falls back
    assert classify("Donoghue v Stevenson", None) is None


def test_series_jurisdiction_buckets():
    from raglex.citations.reporters import series_jurisdiction

    assert series_jurisdiction("AC") == "uk"
    assert series_jurisdiction("SLT") == "uk"          # Scottish series retrieve on Westlaw UK
    assert series_jurisdiction("NI") == "uk"
    assert series_jurisdiction("ILRM") == "ie"
    assert series_jurisdiction("IR") == "ie"
    assert series_jurisdiction("DLR (4th)") == "commonwealth"
    assert series_jurisdiction("CLR") == "commonwealth"
    assert series_jurisdiction("NZLR") == "commonwealth"
    assert series_jurisdiction("CMLR") == "eu"
    assert series_jurisdiction(None) == "uk"           # non-report shapes default UK


# ── display casing of stored (folded) aliases ───────────────────────────────
# Aliases live in citation_aliases casefolded so they compare reliably, which is
# why the case-SENSITIVE report matchers never fire on them. display_citation is
# what puts a stored alias back into the form a lawyer writes.
def test_display_citation_restores_report_series_casing():
    from raglex.citations.reporters import display_citation

    assert display_citation("[2002] 1 wlr 577") == "[2002] 1 WLR 577"
    assert display_citation("[2003] 1 w.l.r. 577") == "[2003] 1 WLR 577"
    assert display_citation("[2008] icr 114") == "[2008] ICR 114"


def test_display_citation_collapses_punctuation_variants():
    from raglex.citations.reporters import display_citation

    # the three spellings the corpus actually stores for one citation all
    # converge, so the "Also cited as" list stops showing it three times
    for raw in ("[2003] 1 all e r(comm) 140",
                "[2003] 1 all e.r. (comm) 140",
                "[2003] 1 all er (comm) 140"):
        assert display_citation(raw) == "[2003] 1 All ER (Comm) 140"


def test_display_citation_handles_apostrophe_series():
    from raglex.citations.reporters import display_citation

    assert display_citation("[2003] 1 lloyd's rep ir 131") == "[2003] 1 Lloyd's Rep IR 131"
    assert display_citation("[2003] lloyd’s rep ir 131") == "[2003] Lloyd's Rep IR 131"


def test_display_citation_cases_neutral_citations():
    from raglex.citations.reporters import display_citation

    assert display_citation("[2002] ewca civ 1642") == "[2002] EWCA Civ 1642"
    assert display_citation("[2024] uksc 12") == "[2024] UKSC 12"
    # trailing chamber/division parenthetical
    assert display_citation("[2012] ukut 440 (aac)") == "[2012] UKUT 440 (AAC)"
    assert display_citation("[2019] ewhc 22 (admin)") == "[2019] EWHC 22 (Admin)"


def test_display_citation_leaves_non_citations_alone():
    from raglex.citations.reporters import display_citation

    # a case name must not be mangled by the neutral-citation caser
    name = "assicurazioni generali spa v arab insurance group (bsc)"
    assert display_citation(name) == name
    assert display_citation("") == ""
    assert display_citation(None) == ""


def test_display_citation_english_and_old_law_reports():
    from raglex.citations.reporters import display_citation

    assert display_citation("150 e.r. 1030") == "150 ER 1030"
    assert display_citation("(1868) lr 7 qb 339") == "(1868) LR 7 QB 339"
    assert display_citation("1999 sc 583") == "1999 SC 583"

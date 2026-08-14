from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from raglex.adapters.be_regulatory import (
    BIPTDecisionsAdapter,
    BIPTJudgmentsAdapter,
    GBAMarketCourtAdapter,
    bipt_listing_links,
    bipt_publication,
    bipt_topic_decisions,
    bipt_total,
    classify_bipt_court,
    market_court_stubs,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.models import DocType, Stub


GBA = """
<div>Resultaten van 1 tot 2 op 99</div>
<article class="media"><h3 class="media-title"><a href="/publications/a.pdf">
Arrest van 3 juni 2026 van het Marktenhof AR/2079. (Beschikbaar in het frans)
</a></h3><div class="media-description">Hof van Beroep - Arrest van 3 juni 2026
(2025/AR/2079)</div></article>
<article class="media"><h3 class="media-title"><a href="/publications/b.pdf">
Tussenarrest van 8 maart 2023 van het Marktenhof (AR/184)</a></h3>
<div class="media-description">Hof van Beroep (2022/AR/184)</div></article>
"""

BIPT_LIST = """
<html><title>641 results found</title><li class="list-group-item">
<a href="/operators/publication/decision-one"><h2>Decision of 27 July 2026</h2>
<time datetime="2026-07-30 00:00">30/07/2026</time></a></li></html>
"""

BIPT_PUBLICATION = """
<h1>Decision of 27 July 2026</h1>
<a href="/file/x/decision-reseau.pdf">Download document "Décision du 27 juillet 2026"</a>
<a href="/file/y/besluit-netwerk.pdf">Download document "Besluit van 27 juli 2026"</a>
<aside class="file-meta"><dl><dt>Publication type</dt><dd>Decision</dd>
<dt>Date</dt><dd>27/07/2026</dd><dt>Publication date</dt><dd>30/07/2026</dd></dl></aside>
"""


def test_sources_are_registered_with_their_legal_types():
    expected = {
        "be-market-court-gba": "caselaw", "be-bipt-judgments": "caselaw",
        "be-bipt-decisions": "administrative", "be-bipt-opinions": "guidance",
    }
    for key, kind in expected.items():
        assert key in ADAPTERS
        assert SOURCE_INFO[key].jurisdiction == "BE"
        assert SOURCE_INFO[key].kind == kind
        assert INCREMENTAL_MODE[key] == "early-stop"


def test_market_court_identity_keeps_same_docket_judgments_distinct():
    rows = market_court_stubs(GBA)
    assert rows[0].stable_id == "be/market-court/gba/2026-06-03-2025-ar-2079"
    assert rows[0].hints["docket"] == "2025/AR/2079"
    assert rows[0].hints["language"] == "fr"
    assert rows[0].hint_date == date(2026, 6, 3)
    assert rows[1].hints["judgment_kind"] == "interim"


def test_bipt_listing_detail_and_language_expressions():
    assert bipt_total(BIPT_LIST) == 641
    links = bipt_listing_links(BIPT_LIST)
    assert links == [("https://www.bipt.be/operators/publication/decision-one",
                      "Decision of 27 July 2026", date(2026, 7, 30))]
    item = bipt_publication(BIPT_PUBLICATION, links[0][0])
    assert item["decision_date"] == date(2026, 7, 27)
    assert item["publication_date"] == date(2026, 7, 30)
    assert [lang for _, lang in item["files"]] == ["fr", "nl"]


def test_dossier_follows_only_decision_children_and_dedupes():
    html = """<a href='/operators/publication/decision-a'>Decision of 1 May 2024</a>
    <a href='/operators/publication/decision-a'>Decision of 1 May 2024</a>
    <a href='/operators/publication/consultation-a'>Consultation about draft decision</a>"""
    assert bipt_topic_decisions(html) == [
        ("https://www.bipt.be/operators/publication/decision-a", "Decision of 1 May 2024")]


def test_court_typology_from_titles():
    assert classify_bipt_court("Judgement of the Market Court")[0] == "be-market-court"
    assert classify_bipt_court("Judgement of the Court of Cassation")[0] == "be-cassation"
    assert classify_bipt_court("Council of State judgement")[0] == "be-rvsce"
    assert classify_bipt_court("Order of Brussels Court of First Instance")[0] == "be-brussels-first-instance"


class _Client:
    def get(self, _url, **_kwargs):
        return SimpleNamespace(content=b"%PDF fake")


def test_judgments_force_bilingual_ocr_but_decisions_do_not(monkeypatch):
    calls = []
    monkeypatch.setattr("raglex.adapters.be_regulatory._forced_bilingual_ocr",
                        lambda blob: (calls.append("forced") or ("Marktenhof arrest " * 20, False, [], "forced")))
    monkeypatch.setattr("raglex.adapters.be_regulatory.text_or_ocr",
                        lambda blob, **kw: (calls.append("normal") or ("BIPT decision " * 20, False, [], "pdf")))
    base = dict(stable_id="be/x/nl", landing_url="https://x", raw_url="https://x.pdf",
                hint_date=date(2026, 1, 1), title="Judgement of the Market Court",
                hints={"language": "nl", "parent_id": "be/x"})
    judgment = BIPTJudgmentsAdapter(client=_Client()).fetch(Stub(**base))
    decision = BIPTDecisionsAdapter(client=_Client()).fetch(Stub(**base))
    assert calls == ["forced", "normal"]
    assert judgment and judgment.doc_type == DocType.JUDGMENT
    assert decision and decision.doc_type == DocType.DECISION


def test_every_resumable_constructor_accepts_a_cursor():
    assert GBAMarketCourtAdapter(client=_Client(), start_offset="50").start_offset == 0
    assert BIPTJudgmentsAdapter(client=_Client(), start_offset="30").start_offset == 20

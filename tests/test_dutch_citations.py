from __future__ import annotations

import zipfile

from raglex.adapters.nl_legislation import NLLegislationAdapter, _parse_sru_page
from raglex.adapters.nl_rechtspraak import NLRechtspraakAdapter, parse_lido_links
from raglex.citations.dutch import law_name_alias
from raglex.citations.extractor import extract_citations


def _nl(text):
    return [c for c in extract_citations(text) if c.method.startswith("nl_")]


def test_dutch_article_and_lid_are_precise():
    cites = _nl("Op grond van art. 8:42, eerste lid, Awb en artikel 6:162 BW.")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("nl:law:algemene wet bestuursrecht", "Artikel 8:42, lid eerste"),
        ("nl:law:burgerlijk wetboek", "Artikel 6:162"),
    ]


def test_dated_juriconnect_does_not_collapse_to_current_work():
    cite = _nl("jci1.3:c:BWBR0002221&hoofdstuk=I&artikel=1&lid=2&z=2015-01-01&g=2015-01-01")[0]
    assert cite.candidate_id == "BWBR0002221@2015-01-01"
    assert cite.pinpoint == "Artikel 1, lid 2 (geldend op 2015-01-01)"


def test_legacy_ljn_gets_resolvable_alias():
    cite = _nl("zie LJN: AV0653, r.o. 4.2")[0]
    assert cite.candidate_id == "nl:ljn:AV0653"
    assert cite.pinpoint == "r.o. 4.2"


def test_dutch_ecli_gets_rechtsoverweging_pinpoint():
    cite = next(c for c in extract_citations("ECLI:NL:HR:2021:656, rov. 3.1.2")
                if c.candidate_id == "ECLI:NL:HR:2021:656")
    assert cite.pinpoint == "r.o. 3.1.2"


def test_civil_code_book_titles_share_collective_alias():
    assert law_name_alias("Burgerlijk Wetboek Boek 6") == "nl:law:burgerlijk wetboek"


def test_sru_page_reports_next_record():
    xml = b'''<searchRetrieveResponse xmlns="http://docs.oasis-open.org/ns/search-ws/sruResponse"
      xmlns:dcterms="http://purl.org/dc/terms/"><records><record><recordData>
      <dcterms:identifier>BWBR0002221</dcterms:identifier><dcterms:title>Voorbeeldwet</dcterms:title>
      <dcterms:modified>2025-01-02</dcterms:modified><geldigheidsdatum>2025-01-01</geldigheidsdatum>
      </recordData></record></records><nextRecordPosition>501</nextRecordPosition></searchRetrieveResponse>'''
    rows, nxt = _parse_sru_page(xml)
    assert rows[0]["identifier"] == "BWBR0002221" and nxt == 501


def test_rechtspraak_bulk_zip_is_discoverable(tmp_path):
    archive = tmp_path / "OpenDataUitspraken.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("2021/ECLI_NL_HR_2021_1.xml", "<x/>")
    stubs = list(NLRechtspraakAdapter(path=str(archive)).discover(None))
    assert len(stubs) == 1 and stubs[0].hints["member"].endswith(".xml")


def test_bwb_bulk_retains_old_versions_and_makes_latest_the_work(tmp_path):
    archive = tmp_path / "bwb.zip"
    template = '''<toestand><meta><identifier>BWBR0002221</identifier>
      <geldigheidsdatum>{date}</geldigheidsdatum></meta><wetgeving>
      <citeertitel>Voorbeeldwet</citeertitel><wettekst><artikel><kop><nr>1</nr></kop>
      <lid><al>Tekst.</al></lid></artikel></wettekst></wetgeving></toestand>'''
    with zipfile.ZipFile(archive, "w") as zf:
        for date in ("2015-01-01", "2020-01-01"):
            zf.writestr(f"BWBR0002221_{date}.xml", template.format(date=date))
    adapter = NLLegislationAdapter(path=str(archive))
    stubs = list(adapter.discover(None))
    assert [s.stable_id for s in stubs] == ["BWBR0002221@2015-01-01", "BWBR0002221"]
    old = adapter.fetch(stubs[0])
    assert old and old.extra["point_in_time"] == "2015-01-01"


def test_lido_outgoing_graph_keeps_historical_statute_version():
    xml = b'''<lido><subject id="http://x/ECLI:NL:HR:2021:656"><uitgaande-links>
      <subject-ref idref="http://x/bwb/BWBR0001830/2637694/2012-07-01/2012-07-01"
       label="Door computer herkende referentie" /></uitgaande-links></subject>
      <subject id="http://x/bwb/BWBR0001830/2637694/2012-07-01/2012-07-01">
       <identifier type="extern">http://wetten.overheid.nl/id/BWBR0001830/2012-07-01/0/Wet/Artikel6</identifier>
      </subject></lido>'''
    rel = parse_lido_links(xml, "ECLI:NL:HR:2021:656")[0]
    assert rel.dst_id == "BWBR0001830@2012-07-01"
    assert rel.dst_anchor == "Artikel 6 (geldend op 2012-07-01)"

from datetime import date

import pytest

from raglex.adapters import be_caselaw as mod
from raglex.adapters.be_caselaw import (
    BelgianConstitutionalCourtAdapter,
    BelgianCouncilOfStateAdapter,
    constitutional_stubs,
    rvs_recent_pages,
    rvs_recent_stubs,
)
from raglex.citations.extractor import extract_citations
from raglex.citations.oscola import cite
from raglex.citations.courts import lookup
from raglex.core.errors import FetchError
from raglex.core.models import Stub


class Response:
    def __init__(self, content: bytes):
        self.content = content


class Client:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        key = (url, tuple(sorted((kwargs.get("params") or {}).items())))
        value = self.responses.get(key, self.responses.get(url))
        if value is None:
            raise AssertionError(f"unexpected request: {key}")
        return Response(value)


RVS_MONTH = b"""
<a href="arr.php?nr=262672&amp;l=fr">262672 (Etrangers) [Ajout\xc3\xa9 le 21/03/2025]</a>
<a href="arr.php?nr=262672&amp;l=fr">262672 (Cassation) [Ajout\xc3\xa9 le 21/03/2025]</a>
<a href="arr.php?nr=262673&amp;l=fr">262673 (March\xc3\xa9s publics) [Ajout\xc3\xa9 le 22/03/2025]</a>
"""

CONST_YEAR = b"""
<div data-testid="judgment-card" class="judgment-card">
  <div class="d-flex justify-space-between align-center w-100 mb-1">
    <span>08/01/2026</span><span>Question pr\xc3\xa9judicielle</span>
  </div>
  <a href="https://fr.const-court.be/1/2026.pdf"></a>
  <h2><a href="/a/1/2026">1/2026</a></h2>
  <div class="mt-2">Loi du 15 d\xc3\xa9cembre 1980 (article 9)</div>
  <div>Num\xc3\xa9ro de r\xc3\xb4le: 8327</div>
</div>
"""


def test_recent_council_register_deduplicates_subject_headings():
    index = '<a href="?lang=fr&amp;page=lastmonth_03">Mars 2025</a>'
    assert rvs_recent_pages(index) == [
        "https://www.raadvst-consetat.be/?lang=fr&page=lastmonth_03"]
    rows = rvs_recent_stubs(RVS_MONTH)
    assert [row.stable_id for row in rows] == ["be/rvsce/262672", "be/rvsce/262673"]
    assert rows[0].hints["added_date"] == "21/03/2025"


def test_council_archive_is_sequential_and_resumes_one_block_early():
    adapter = BelgianCouncilOfStateAdapter(
        client=Client({}), first_number=10000, end_number=10105, start_offset=10100)
    rows = list(adapter.discover(None))
    assert rows[0].stable_id == "be/rvsce/10000"
    assert rows[-1].stable_id == "be/rvsce/10105"
    assert rows[-1].hints["resume_offset"] == 10105


def test_council_watch_mode_uses_the_rolling_register(monkeypatch):
    adapter = BelgianCouncilOfStateAdapter(client=Client({}), watch_mode="true")
    expected = [Stub(stable_id="be/rvsce/262672", landing_url="x")]
    monkeypatch.setattr(adapter, "_recent", lambda: expected)
    assert list(adapter.discover(None, max_pages=None)) == expected


def test_archive_html_is_a_hole_but_advertised_html_is_an_error():
    client = Client({"https://example.test/arr": b"<html>not found</html>"})
    adapter = BelgianCouncilOfStateAdapter(client=client)
    ordinary = Stub("be/rvsce/1", raw_url="https://example.test/arr",
                    hints={"decision_number": 1, "advertised": False})
    advertised = Stub("be/rvsce/1", raw_url="https://example.test/arr",
                      hints={"decision_number": 1, "advertised": True})
    assert adapter.fetch(ordinary) is None
    with pytest.raises(FetchError):
        adapter.fetch(advertised)


def test_council_fetch_uses_ecli_and_own_header_date(monkeypatch):
    text = """CONSEIL D'ETAT  A R R ET
    no 262.672 du 19 mars 2025
    L'arret no 261.462 du 30 septembre 2021 est attaque.
    ECLI:BE:RVSCE:2025:ARR.262.672
    """ + ("corps de la decision " * 20)
    monkeypatch.setattr(mod, "text_or_ocr", lambda *_a, **_k: (text, False, [], "pymupdf"))
    url = "https://example.test/262672"
    adapter = BelgianCouncilOfStateAdapter(client=Client({url: b"%PDF fake"}))
    record = adapter.fetch(Stub(
        "be/rvsce/262672", landing_url=url, raw_url=url,
        hints={"decision_number": 262672, "advertised": True}))
    assert record.stable_id == "ECLI:BE:RVSCE:2025:ARR.262.672"
    assert record.decision_date == date(2025, 3, 19)
    assert record.language == "fr"
    assert record.extra["citation_languages"] == ["fr", "nl"]


def test_constitutional_manifest_supplies_metadata_and_not_pdf_guessing():
    rows = constitutional_stubs(CONST_YEAR, 2026)
    assert len(rows) == 1
    assert rows[0].stable_id == "be/const-court/2026/1"
    assert rows[0].hint_date == date(2026, 1, 8)
    assert rows[0].hints["procedure"] == "Question pr\xe9judicielle"
    assert rows[0].hints["role_number"] == "8327"


def test_constitutional_discovery_reads_the_year_manifest():
    client = Client({(mod.CONST_INDEX, (("year", 2026),)): CONST_YEAR})
    rows = list(BelgianConstitutionalCourtAdapter(
        client=client, start_year=2026, end_year=2026).discover(None))
    assert [row.raw_url for row in rows] == ["https://fr.const-court.be/1/2026.pdf"]
    assert rows[0].hints["resume_offset"] == 0


def test_constitutional_watch_mode_revisits_this_and_last_year():
    current = date.today().year
    previous_html = CONST_YEAR.replace(b"2026", str(current - 1).encode())
    current_html = CONST_YEAR.replace(b"2026", str(current).encode())
    client = Client({
        (mod.CONST_INDEX, (("year", current - 1),)): previous_html,
        (mod.CONST_INDEX, (("year", current),)): current_html,
    })
    rows = list(BelgianConstitutionalCourtAdapter(
        client=client, watch_mode=True).discover(None, max_pages=None))
    assert [row.stable_id for row in rows] == [
        f"be/const-court/{current - 1}/1", f"be/const-court/{current}/1"]


def test_belgian_citations_degrade_without_dangling_punctuation():
    base = {
        "source": "be-rvsce", "stable_id": "ECLI:BE:RVSCE:2025:ARR.262.672",
        "doc_type": "judgment", "court": "be-rvsce",
        "decision_date": "2025-03-19", "ecli": "ECLI:BE:RVSCE:2025:ARR.262.672",
    }
    assert cite(base, {"citation_number": "262.672"})["text"] == (
        "Conseil d’État, arrêt no 262.672 (19 March 2025) "
        "ECLI:BE:RVSCE:2025:ARR.262.672")
    assert cite({**base, "decision_date": None, "ecli": None},
                {"citation_number": "262.672"})["text"] == (
                    "Conseil d’État, arrêt no 262.672")
    assert cite({**base, "decision_date": None}, {})["text"] == (
        "Conseil d’État ECLI:BE:RVSCE:2025:ARR.262.672")


def test_shared_extractor_runs_french_and_dutch_grammars_for_belgian_text():
    found = extract_citations("article VI.110 du CDE; artikel 100, \xa7 1, 9\xb0 WOG")
    assert {citation.method for citation in found} >= {
        "be_fr_law_reference", "nl_law_reference"}


def test_belgian_court_slugs_have_display_names_and_fetch_routes():
    assert lookup("be-rvsce").name == "Council of State (Belgium)"
    assert lookup("be-rvsce").adapter == "be-rvsce"
    assert lookup("be-const-court").jurisdiction == "BE"

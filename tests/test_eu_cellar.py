from __future__ import annotations

import io
import zipfile

from raglex.adapters.eu_cellar import (
    EUCellarAdapter,
    classify_celex,
    extract_formex_text,
    parse_national_judgements,
    unzip_formex,
)
from raglex.core.models import DocType, Record, RelationshipType, Stub


def test_classify_celex_covers_courts_and_instruments():
    # Court of Justice judgment
    assert classify_celex("62022CJ0203") == (DocType.JUDGMENT, "Court of Justice")
    # General Court judgment (T...)
    assert classify_celex("62022TJ0667") == (DocType.JUDGMENT, "General Court")
    # Order
    assert classify_celex("62020CO0123") == (DocType.DECISION, "Court of Justice")
    # Opinion of the Court — e.g. Opinion 1/15 (Canada PNR), descriptor CV
    assert classify_celex("62015CV0001") == (DocType.OPINION, "Court of Justice")
    # AG opinion → classified as opinion, attributed to the Advocate General
    assert classify_celex("62020CC0311") == (DocType.OPINION, "Advocate General")
    # CDM resource-type wins when present
    assert classify_celex("62015CV0001", "OPIN_JUR") == (DocType.OPINION, "Court of Justice")
    assert classify_celex("62022TJ0667", "JUDG") == (DocType.JUDGMENT, "General Court")
from raglex.resolve import Resolver

NJUDG = (
    "<national_judgement><p>*A9* High Court (Irlande), Order of 04/05/2018 (4809 P)</p>"
    "<p>http://www.europeanrights.eu/public/sentenze/Irlanda.pdf</p>"
    "<p>Publication Flash News</p></national_judgement>"
)

FORMEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<JUDGMENT>
  <BIB.JUDGMENT>
    <NO.CELEX>62022CJ0203</NO.CELEX>
    <NO.ECLI>ECLI:EU:C:2025:117</NO.ECLI>
  </BIB.JUDGMENT>
  <CONTENTS.JUDGMENT>
    <GR.SEQ><TITLE><TI><P>Consideration of the questions referred</P></TI></TITLE>
      <NP.ECR IDENTIFIER="NP0001"><NO.P>1</NO.P><TXT>Article 15(1)(h) of Regulation (EU) 2016/679 concerns the right of access.</TXT></NP.ECR>
    </GR.SEQ>
  </CONTENTS.JUDGMENT>
  <JURISDICTION>
    <INTRO>On those grounds, the Court (First Chamber) hereby rules:</INTRO>
    <LIST><ITEM><NP><TXT>The data subject has a right to a copy of personal data.</TXT></NP></ITEM></LIST>
  </JURISDICTION>
</JUDGMENT>
"""


def _zip(xml: bytes, name: str = "ECR_62022CJ0203_EN_01.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, xml)
    return buf.getvalue()


def test_unzip_formex_unpacks_zip_and_passes_raw_xml():
    assert unzip_formex(_zip(FORMEX)) == FORMEX  # zip member extracted
    assert unzip_formex(FORMEX) == FORMEX  # already-raw XML passed through
    assert unzip_formex(b"not a zip or xml") is None


def test_extract_formex_text_prefers_ruling_and_reasoning():
    text = extract_formex_text(FORMEX)
    assert "hereby rules" in text  # JURISDICTION (operative)
    assert "right of access" in text  # CONTENTS.JUDGMENT (reasoning)
    assert "right to a copy of personal data" in text


class FakeClient:
    """Stand-in for RateLimitedClient: serves a canned SPARQL discovery page,
    a canned cited-works result, and a Formex zip — no network."""

    def __init__(self):
        self.formex = _zip(FORMEX)

    def request(self, method, url, *, data=None, headers=None):
        q = data["query"]
        if "case-law_national-judgement" in q:
            rows = [{"njudg": NJUDG, "country": "IRL"}]
        elif "work_cites_work" in q:
            rows = [{"cited_celex": "62014CJ0362", "cited_ecli": "ECLI:EU:C:2015:650"}]
        else:  # discovery
            rows = [{
                "celex": "62022CJ0203",
                "ecli": "ECLI:EU:C:2025:117",
                "date": "2025-02-27",
                "link": "case-law_interpretes_resource_legal",
            }]
        return _JsonResp({"results": {"bindings": [
            {k: {"value": v} for k, v in row.items()} for row in rows
        ]}})

    def get(self, url, *, headers=None):
        return _BytesResp(self.formex)


class _JsonResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _BytesResp:
    def __init__(self, content):
        self.content = content


def test_discover_yields_ecli_stub_with_celex_hint():
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    stubs = list(ad.discover(None))
    assert len(stubs) == 1
    s = stubs[0]
    assert s.stable_id == "ECLI:EU:C:2025:117"  # ECLI primary key
    assert s.court == "Court of Justice"
    assert s.hints["celex"] == "62022CJ0203"
    assert "interpretes" in s.hints["link"]


class PagingClient(FakeClient):
    def request(self, method, url, *, data=None, headers=None):
        query = data["query"]
        if "OFFSET 0" in query:
            rows = [{
                "celex": "62026CJ0123",
                "ecli": "ECLI:EU:C:2026:700",
                "date": "2026-07-24",
                "rtype": "JUDG",
                "title": "Example v Commission",
            }]
        else:
            rows = []
        return _JsonResp({"results": {"bindings": [
            {key: {"value": value} for key, value in row.items()}
            for row in rows
        ]}})


def test_default_discovery_is_incremental_all_case_law():
    ad = EUCellarAdapter(per_page=1, client=PagingClient())
    stubs = list(ad.discover("2026-07-20", max_pages=2))
    assert [stub.stable_id for stub in stubs] == ["ECLI:EU:C:2026:700"]
    assert stubs[0].hints["watermark"] == "2026-07-24"
    rec = ad.fetch(stubs[0])
    # The general currency feed must not manufacture the old targeted-mode edge
    # with a blank legislation destination.
    assert all(relation.dst_id for relation in rec.relations)
    query = ad._enumerate_query("2026-07-20", 100)
    assert 'FILTER(STR(?date) > "2026-07-20")' in query
    assert "OFFSET 100" in query


def test_fetch_builds_legislation_and_citation_edges():
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    stub = list(ad.discover(None))[0]
    rec = ad.fetch(stub)

    assert rec.ecli == "ECLI:EU:C:2025:117"
    assert "hereby rules" in rec.text  # Formex text extracted

    # edge 1: the case INTERPRETS the instrument being followed (typed from the CDM link)
    leg = [r for r in rec.relations if r.dst_id == "32004R0139"]
    assert len(leg) == 1
    assert leg[0].relationship_type == RelationshipType.INTERPRETS

    # edge 2: a mentions edge to a cited case, by ECLI (resolvable)
    cited = [r for r in rec.relations if r.dst_id == "ECLI:EU:C:2015:650"]
    assert len(cited) == 1
    assert cited[0].relationship_type == RelationshipType.MENTIONS


def test_ag_opinion_links_to_its_judgment():
    """An AG opinion (CELEX …CC…) links to its judgment (…CJ…, same case number)."""
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    stub = Stub(stable_id="ECLI:EU:C:2019:1145",
                raw_url="https://publications.europa.eu/resource/celex/62018CC0311",
                hints={"celex": "62018CC0311", "link": "case-law_interpretes_resource_legal"})
    rec = ad.fetch(stub)
    assert rec.doc_type == DocType.OPINION and rec.court == "Advocate General"
    op_edges = [r for r in rec.relations if r.relationship_type == RelationshipType.OPINION_IN]
    assert len(op_edges) == 1 and op_edges[0].dst_id == "62018CJ0311"  # → the judgment


def test_parse_national_judgements_extracts_court_and_url():
    refs = parse_national_judgements([NJUDG])
    assert len(refs) == 1
    assert refs[0].court == "High Court (Irlande)"
    assert "Order of 04/05/2018" in refs[0].reference
    assert refs[0].url.endswith("Irlanda.pdf")  # preserved as a scrape target


def test_fetch_records_preliminary_reference_edge_and_metadata():
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    rec = ad.fetch(list(ad.discover(None))[0])

    pref = [r for r in rec.relations if r.relationship_type == RelationshipType.PRELIMINARY_REFERENCE]
    assert len(pref) == 1
    assert pref[0].dst_id is None  # national case not in corpus → dangling (worklist)
    assert "High Court (Irlande)" in pref[0].raw_citation_string
    assert "Irlanda.pdf" in pref[0].raw_citation_string  # scrape target carried
    assert rec.extra["origin_country"] == "IRL"
    assert rec.extra["referring_courts"] == ["High Court (Irlande)"]


def test_preliminary_reference_surfaces_in_worklist(catalogue):
    """Recorded now, resolved later: the referring national case sits in the
    harvest worklist until a national adapter harvests/scrapes it (§5b, user req)."""
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    rec = ad.fetch(list(ad.discover(None))[0])
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec)
    Resolver(catalogue).run()
    worklist = [r["raw_citation_string"] for r in catalogue.resolution_worklist()]
    assert any("High Court (Irlande)" in w for w in worklist)


def test_cellar_citation_resolves_against_corpus(catalogue):
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    rec = ad.fetch(list(ad.discover(None))[0])

    # harvest the cited case so the edge has a node to resolve to
    target = Record(source="eu-cellar", stable_id="ECLI:EU:C:2015:650",
                    ecli="ECLI:EU:C:2015:650", doc_type=DocType.JUDGMENT,
                    raw_bytes=b"schrems i")
    target.ensure_payload_hash()
    catalogue.upsert_document(target)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec)

    Resolver(catalogue).run()
    edges = {e["dst_id"]: e["resolution_status"] for e in catalogue.relations_for(rec.stable_id)}
    assert edges["ECLI:EU:C:2015:650"] == "resolved"   # cited CJEU case resolved
    assert edges["32004R0139"] == "pending"      # instrument not harvested yet → worklist


# -- older Formex: grounds in GR.SEQ, not NP.ECR (must not come out ruling-only) ----
_OLD_FORMEX = b"""<?xml version="1.0"?>
<JUDGMENT>
 <PARTIES>ZZ v Secretary of State for the Home Department,</PARTIES>
 <NO.CASE>C-300/11</NO.CASE>
 <CONTENTS.JUDGMENT>
  <GR.SEQ><TITLE><TI>Legal context</TI></TITLE>
   <NP><NO.P>1</NO.P><TXT>This request concerns the interpretation of Directive 2004/38/EC.</TXT></NP>
   <NP><NO.P>2</NO.P><TXT>Article 30 governs notification of decisions.</TXT></NP></GR.SEQ>
  <JURISDICTION>On those grounds, the Court hereby rules: Article 30 must be interpreted as follows.</JURISDICTION>
 </CONTENTS.JUDGMENT>
</JUDGMENT>"""


def test_formex_falls_back_to_grseq_grounds_not_ruling_only():
    from raglex.adapters.eu_cellar import extract_formex
    text, segments = extract_formex(_OLD_FORMEX)
    kinds = {s.kind for s in segments}
    # grounds AND ruling, never ruling-only. An old instance's grounds are plain <NP> inside
    # GR.SEQ; the reading-order walk splits them per paragraph and keeps the section heading,
    # where the old flat scan could only take the GR.SEQ whole ("section").
    assert {"paragraph", "heading", "ruling"} <= kinds
    assert "interpretation of Directive 2004/38" in text  # the grounds body is present
    # the operative ruling is appended once, not also swept up as a paragraph
    assert text.count("On those grounds, the Court hereby rules") == 1


def test_formex_case_title_from_parties_and_number():
    from raglex.adapters.eu_cellar import formex_case_title
    assert formex_case_title(_OLD_FORMEX) == "ZZ v Secretary of State for the Home Department"


def test_formex_ag_title_without_parties_element():
    from raglex.adapters.eu_cellar import formex_case_title
    xml = b"""<DOC><NO.CASE>C-340/21</NO.CASE><TITLE>Opinion of Advocate General
      Pitruzzella delivered on 27 April 2023 Case C-340/21 VBvNatsionalna agentsia
      za prihodite (Request for a preliminary ruling)</TITLE></DOC>"""
    assert formex_case_title(xml) == "VB v Natsionalna agentsia za prihodite"


def test_case_display_title_drops_c_t_and_appeal_docket_suffixes():
    from raglex.adapters.eu_cellar import clean_case_display_title
    assert clean_case_display_title("OC (C-479/22P)") == "OC"
    assert clean_case_display_title("EDPS v SRB (C-413/23 P)") == "EDPS v SRB"
    assert clean_case_display_title("Example (T-123/24)") == "Example"
    assert clean_case_display_title("Example (F-12/08)") == "Example"
    assert clean_case_display_title("Alpha (T-1/20), Beta (T-2/20)") == "Alpha, Beta"


# -- joined cases: the judgment lives only under the LEAD case number (§5b) --

def test_resolve_case_celex_joined_case_falls_back_to_lead(monkeypatch):
    """C-48/93 (Factortame) has NO CELEX of its own — the judgment is published under
    the lead case C-46/93. The resolver's second hop follows the lead work's
    cdm:case-law_joins_case_court link back to it."""
    from raglex.adapters import eu_cellar

    def fake_sparql(self, q):
        if "case-law_joins_case_court" in q:
            assert "61993[A-Z][A-Z]0048" in q
            return [{"celex": "61993CJ0046"}]
        return []  # no descriptor exists under the joined number itself

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", fake_sparql)
    assert eu_cellar.resolve_case_celex("61993CJ0048") == "61993CJ0046"


def test_resolve_case_celex_direct_hit_never_hops_to_joined(monkeypatch):
    from raglex.adapters import eu_cellar

    def fake_sparql(self, q):
        assert "case-law_joins_case_court" not in q, "direct hit must not hop"
        return [{"celex": "62016CO0113"}]

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", fake_sparql)
    assert eu_cellar.resolve_case_celex("62016CJ0113") == "62016CO0113"


def test_resolve_case_celex_joined_ranks_judgment_over_order(monkeypatch):
    from raglex.adapters import eu_cellar

    def fake_sparql(self, q):
        if "case-law_joins_case_court" in q:
            return [{"celex": "61993CO0046"}, {"celex": "61993CJ0046"}]
        return []

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", fake_sparql)
    assert eu_cellar.resolve_case_celex("61993CJ0048") == "61993CJ0046"


def test_resolve_case_celex_absent_everywhere_is_none(monkeypatch):
    from raglex.adapters import eu_cellar

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", lambda self, q: [])
    assert eu_cellar.resolve_case_celex("61993CJ9999") is None


def test_resolve_case_celex_transient_failure_raises_not_absent(monkeypatch):
    # A SPARQL transport failure must NOT be read as "case absent" — it raises, so the
    # drain classifies it transient (retry in hours) instead of a 90-day cooldown.
    import pytest

    from raglex.adapters import eu_cellar

    def boom(self, q):
        raise TimeoutError("CELLAR timed out")

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", boom)
    with pytest.raises(eu_cellar.CellarUnavailable):
        eu_cellar.resolve_case_celex("62018CJ0511")


def test_fetch_reference_marks_cellar_outage_transient_not_absent(monkeypatch, tmp_path):
    # end-to-end: a CELLAR outage during a targeted EU fetch → outcome "transient"
    # (hours), never "absent" (90 days) — the poisoning this whole fix prevents.
    import raglex.facade as fmod
    from raglex.adapters.eu_cellar import CellarUnavailable
    from raglex.config import Config

    def raising_builder(cand, **kw):
        raise CellarUnavailable("CELLAR down")

    monkeypatch.setitem(fmod._TARGETED_HARVEST, "eu-cellar", raising_builder)
    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "settings.json",
                 embed_provider="local-hashing", embed_model=None)
    f = fmod.Facade(cfg)
    with f._open() as (cat, rs, ts):
        res = f._fetch_reference(cat, rs, ts, ref="C-511/18", candidate="62018CJ0511")
    assert res["outcome"] == "transient"


# -- legacy single-letter CELEX (the "could not build a fetch" flood) ---------

def test_legacy_single_letter_celex_is_parsed_not_mis_sliced(monkeypatch):
    """"61994J0334" is the LEGACY CELEX form: one descriptor letter, not two. Slicing a
    fixed two characters read the descriptor as "J0" and the case number as "334",
    dropping the leading zero — so the lookup regex could never match and every such
    citation was written off as a genuine absence. The case exists as 61994CJ0334."""
    from raglex.adapters import eu_cellar

    seen: list[str] = []

    def fake_sparql(self, q):
        seen.append(q)
        return [{"celex": "61994CJ0334"}, {"celex": "61994CC0334"}]

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", fake_sparql)
    assert eu_cellar.resolve_case_celex("61994J0334") == "61994CJ0334"
    # the 4-digit case number survives into the query
    assert "0334" in seen[0] and "^61994[A-Z][A-Z]334$" not in seen[0]


def test_legacy_order_prefers_an_order_over_the_judgment(monkeypatch):
    """A legacy letter names the decision TYPE but no court family. A cited order must
    resolve to the order, not to the judgment in the same case."""
    from raglex.adapters import eu_cellar

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql",
                        lambda self, q: [{"celex": "61994CJ0334"}, {"celex": "61994CO0334"}])
    assert eu_cellar.resolve_case_celex("61994O0334") == "61994CO0334"


def test_legacy_judgment_prefers_court_of_justice_then_general_court(monkeypatch):
    from raglex.adapters import eu_cellar

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql",
                        lambda self, q: [{"celex": "61994TJ0334"}])
    # only the General Court judgment exists — still resolves rather than reporting absent
    assert eu_cellar.resolve_case_celex("61994J0334") == "61994TJ0334"


def test_a_malformed_celex_is_rejected_rather_than_queried(monkeypatch):
    """Guard the parse: anything not sector+year / descriptor / 4-digit number should
    not reach CELLAR at all."""
    from raglex.adapters import eu_cellar

    def boom(self, q):
        raise AssertionError("must not query CELLAR for a malformed CELEX")

    monkeypatch.setattr(eu_cellar.EUCellarAdapter, "_sparql", boom)
    assert eu_cellar.resolve_case_celex("61994J334") is None      # 3-digit number
    assert eu_cellar.resolve_case_celex("nonsense") is None


def test_formex_legislation_splits_articles_into_paragraphs():
    """EU legislation in Formex → per-paragraph segments (so 'Article 5(1)' pincites land),
    laid out with indentation, VISA legal-basis + recitals captured; judgments untouched."""
    from raglex.adapters.eu_cellar import extract_formex
    xml = b"""<ACT><PREAMBLE>
      <GR.VISA><VISA>Having regard to Article 16 TFEU,</VISA>
      <VISA>Having regard to the proposal from the Commission,</VISA></GR.VISA>
      <GR.CONSID><CONSID><NO.P>(1)</NO.P><TXT>A recital.</TXT></CONSID></GR.CONSID></PREAMBLE>
      <ENACTING.TERMS>
        <ARTICLE><TI.ART>Article 5</TI.ART><STI.ART>Scope</STI.ART>
          <PARAG><NO.PARAG>1.</NO.PARAG><ALINEA>First paragraph.</ALINEA></PARAG>
          <PARAG><NO.PARAG>2.</NO.PARAG><ALINEA>Second paragraph.</ALINEA></PARAG></ARTICLE>
      </ENACTING.TERMS></ACT>"""
    text, segs = extract_formex(xml)
    labels = [s.label for s in segs]
    kinds = {s.label: s.kind for s in segs}
    assert "Legal basis 1" in labels and kinds["Legal basis 1"] == "visa"
    assert "Recital 1" in labels
    assert "Article 5" in labels                       # whole-article heading resolves
    assert "Article 5(1)" in labels and "Article 5(2)" in labels  # per-paragraph pincites
    # every segment's offsets slice real text, in document order (drift-safe)
    assert all(text[s.char_start:s.char_end].strip() for s in segs)
    assert [s.char_start for s in segs] == sorted(s.char_start for s in segs)
    # judgment Formex still parses as before
    jxml = b"<JUDGMENT><NP.ECR><NO.P>1</NO.P><TXT>Claim.</TXT></NP.ECR><JURISDICTION>Held.</JURISDICTION></JUDGMENT>"
    _jt, js = extract_formex(jxml)
    assert [s.label for s in js] == ["1", "ruling"]


def test_ag_opinion_head_gives_the_citation_its_missing_name():
    """CELLAR carries no Advocate General and these documents arrive titleless, so their
    OSCOLA citation rendered as "…, Opinion of AG" with a hole where the name goes. The
    name is on the face of every Opinion — on the label's line or the next one."""
    from raglex.adapters.eu_cellar import parse_ag_opinion_head
    from raglex.citations.oscola import cite

    assert parse_ag_opinion_head(
        "Provisional text\nOPINION OF ADVOCATE GENERAL\nEMILIOU\ndelivered on 15 May 2025 (\n1\n)"
    ) == {"advocate_general": "Emiliou", "delivered_on": "15 May 2025"}
    # same line, multi-word name, accents preserved
    assert parse_ag_opinion_head(
        "OPINION OF ADVOCATE GENERAL CAMPOS SÁNCHEZ-BORDONA delivered on 3 March 2020 (1)"
    )["advocate_general"] == "Campos Sánchez-Bordona"
    assert parse_ag_opinion_head("VIEW OF ADVOCATE GENERAL\nKOKOTT\ndelivered on 1 April 2011"
                                 )["advocate_general"] == "Kokott"
    # a judgment (or an Opinion OF THE COURT) is not an AG opinion — no data, not a guess
    assert parse_ag_opinion_head("JUDGMENT OF THE COURT (Grand Chamber) 27 February 2025") == {}
    assert parse_ag_opinion_head(None) == {}

    # …and the name reaches the citation
    doc = {"source": "eu-cellar", "doc_type": "opinion", "court": "Advocate General",
           "stable_id": "ECLI:EU:C:2025:362", "ecli": "ECLI:EU:C:2025:362", "title": None}
    assert cite(doc, {"celex": "62023CC0209", "advocate_general": "Emiliou"})["text"] == (
        "Case C-209/23 EU:C:2025:362, Opinion of AG Emiliou")


def test_advocate_general_comes_from_cellar_with_the_page_as_fallback(monkeypatch):
    """The AG is a real CDM relation (``case-law_delivered_by_advocate-general`` → a person
    with an ``agent_name``), so ask CELLAR first. The name printed on the Opinion is the
    fallback for when the endpoint has nothing (older opinions) or is unreachable — and it
    supplies the delivery date either way, which the metadata does not carry."""
    from raglex.adapters.eu_cellar import EUCellarAdapter

    a = EUCellarAdapter()
    text = "OPINION OF ADVOCATE GENERAL\nEMILIOU\ndelivered on 15 May 2025"

    monkeypatch.setattr(a, "advocate_generals", lambda cs: {"62023CC0209": "Emiliou"})
    assert a._ag_meta("62023CC0209", text) == {
        "advocate_general": "Emiliou", "advocate_general_source": "cellar",
        "delivered_on": "15 May 2025"}

    # endpoint silent → the printed heading answers, and says so
    monkeypatch.setattr(a, "advocate_generals", lambda cs: {})
    assert a._ag_meta("62023CC0209", text) == {
        "advocate_general": "Emiliou", "advocate_general_source": "document",
        "delivered_on": "15 May 2025"}

    # neither → no invented name
    assert a._ag_meta("62023CC0209", "JUDGMENT OF THE COURT") == {}


def test_advocate_general_query_compares_celex_as_a_string():
    """The stored CELEX is a typed xsd:string, so a plain-literal VALUES block matches
    nothing on Virtuoso — the batch query must compare with STR(), like the others here."""
    from raglex.adapters.eu_cellar import EUCellarAdapter

    q = EUCellarAdapter()._advocate_general_query(["62023CC0209", "62005CC0166"])
    assert 'FILTER(STR(?c) IN ("62023CC0209", "62005CC0166"))' in q
    assert "case-law_delivered_by_advocate-general" in q and "agent_name" in q


# A CJEU judgment's reasoning as Formex nests it: GR.SEQ sections, each opening with a
# TITLE, paragraphs inside. Trimmed from the real C-203/22 (Dun & Bradstreet) instance.
JUDGMENT_FMX = b"""<JUDGMENT>
  <TITLE><TI>Judgment of the Court (First Chamber) 27 February 2025</TI></TITLE>
  <CONTENTS.JUDGMENT>
    <GR.SEQ>
      <TITLE><TI>Judgment</TI></TITLE>
      <NP.ECR><NO.P>1</NO.P><TXT>This request concerns the interpretation of the GDPR.</TXT></NP.ECR>
    </GR.SEQ>
    <GR.SEQ>
      <TITLE><TI>Legal context</TI></TITLE>
      <GR.SEQ>
        <TITLE><TI>European Union law</TI></TITLE>
        <GR.SEQ>
          <TITLE><TI>The GDPR</TI></TITLE>
          <NP.ECR><NO.P>3</NO.P><TXT>Recitals 4, 11, 58, 63 and 71 of the GDPR state.</TXT></NP.ECR>
        </GR.SEQ>
      </GR.SEQ>
      <GR.SEQ>
        <TITLE><TI>Austrian law</TI></TITLE>
        <NP.ECR><NO.P>15</NO.P><TXT>Paragraph 4(6) of the Datenschutzgesetz provides.</TXT></NP.ECR>
      </GR.SEQ>
    </GR.SEQ>
    <GR.SEQ>
      <TITLE><TI>Consideration of the questions referred</TI></TITLE>
      <GR.SEQ>
        <TITLE><TI>Question 3(b) and (c), Question 4(a) and (b), and Questions 5 and 6</TI></TITLE>
        <NP.ECR><NO.P>67</NO.P><TXT>By Question 3(b) and (c), the referring court asks.</TXT></NP.ECR>
      </GR.SEQ>
    </GR.SEQ>
  </CONTENTS.JUDGMENT>
  <JURISDICTION>On those grounds, the Court hereby rules.</JURISDICTION>
</JUDGMENT>"""


def test_judgment_keeps_its_section_headings_with_their_nesting():
    """EUR-Lex shows a judgment's structure — "Legal context" › "European Union law" › "The
    GDPR", and the question-by-question headings that say what each block of paragraphs is
    answering. Taking only <NP.ECR> dropped every one of them, so the judgment read as an
    unbroken wall of numbered text. Headings are one element type (TITLE); the LEVEL is the
    GR.SEQ nesting depth, not a different tag."""
    from raglex.adapters.eu_cellar import extract_formex

    text, segs = extract_formex(JUDGMENT_FMX)
    heads = [(s.level, s.label) for s in segs if s.kind == "heading"]
    assert heads == [
        (1, "Judgment"),
        (1, "Legal context"),
        (2, "European Union law"),
        (3, "The GDPR"),
        (2, "Austrian law"),
        (1, "Consideration of the questions referred"),
        (2, "Question 3(b) and (c), Question 4(a) and (b), and Questions 5 and 6"),
    ]
    # the document's OWN title block is not a section heading (it sits outside CONTENTS)
    assert not any("Judgment of the Court (First Chamber)" in lbl for _lvl, lbl in heads)
    # headings sit in the text, in reading order, between the paragraphs they introduce
    assert text.index("Austrian law") < text.index("Paragraph 4(6)")
    assert text.index("Question 3(b) and (c), Question") < text.index("By Question 3(b)")
    # paragraphs keep their numbers, the ruling still lands, and every offset is exact
    assert [s.label for s in segs if s.kind == "paragraph"] == ["1", "3", "15", "67"]
    assert any(s.kind == "ruling" for s in segs)
    for s in segs:
        assert text[s.char_start:s.char_end].strip() == text[s.char_start:s.char_end]


def test_a_judgment_without_the_contents_wrapper_still_parses():
    """Older instances have no <CONTENTS.JUDGMENT>: the flat NP.ECR scan is the fallback,
    so nothing that parsed before stops parsing."""
    from raglex.adapters.eu_cellar import extract_formex

    text, segs = extract_formex(
        b"<JUDGMENT><NP.ECR><NO.P>1</NO.P><TXT>Old style paragraph.</TXT></NP.ECR>"
        b"<JURISDICTION>The Court rules.</JURISDICTION></JUDGMENT>")
    assert [s.label for s in segs] == ["1", "ruling"]
    assert "Old style paragraph." in text

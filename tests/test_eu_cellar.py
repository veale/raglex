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
    ad = EUCellarAdapter(client=FakeClient())
    stubs = list(ad.discover(None))
    assert len(stubs) == 1
    s = stubs[0]
    assert s.stable_id == "ECLI:EU:C:2025:117"  # ECLI primary key
    assert s.court == "Court of Justice"
    assert s.hints["celex"] == "62022CJ0203"
    assert "interpretes" in s.hints["link"]


def test_fetch_builds_legislation_and_citation_edges():
    ad = EUCellarAdapter(client=FakeClient())
    stub = list(ad.discover(None))[0]
    rec = ad.fetch(stub)

    assert rec.ecli == "ECLI:EU:C:2025:117"
    assert "hereby rules" in rec.text  # Formex text extracted

    # edge 1: the case INTERPRETS the GDPR (typed from the CDM link property)
    leg = [r for r in rec.relations if r.dst_id == "32016R0679"]
    assert len(leg) == 1
    assert leg[0].relationship_type == RelationshipType.INTERPRETS

    # edge 2: a mentions edge to a cited case, by ECLI (resolvable)
    cited = [r for r in rec.relations if r.dst_id == "ECLI:EU:C:2015:650"]
    assert len(cited) == 1
    assert cited[0].relationship_type == RelationshipType.MENTIONS


def test_ag_opinion_links_to_its_judgment():
    """An AG opinion (CELEX …CC…) links to its judgment (…CJ…, same case number)."""
    ad = EUCellarAdapter(client=FakeClient())
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
    ad = EUCellarAdapter(client=FakeClient())
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
    ad = EUCellarAdapter(client=FakeClient())
    rec = ad.fetch(list(ad.discover(None))[0])
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec)
    Resolver(catalogue).run()
    worklist = [r["raw_citation_string"] for r in catalogue.resolution_worklist()]
    assert any("High Court (Irlande)" in w for w in worklist)


def test_cellar_citation_resolves_against_corpus(catalogue):
    ad = EUCellarAdapter(client=FakeClient())
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
    assert edges["32016R0679"] == "pending"            # GDPR not harvested yet → worklist


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
    assert "section" in kinds and "ruling" in kinds  # grounds AND ruling, not ruling-only
    assert "interpretation of Directive 2004/38" in text  # the grounds body is present


def test_formex_case_title_from_parties_and_number():
    from raglex.adapters.eu_cellar import formex_case_title
    assert formex_case_title(_OLD_FORMEX) == "ZZ v Secretary of State for the Home Department (C-300/11)"

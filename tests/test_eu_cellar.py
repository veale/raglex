from __future__ import annotations

import io
import zipfile

from raglex.adapters.eu_cellar import (
    EUCellarAdapter,
    classify_celex,
    extract_formex,
    extract_formex_text,
    parse_national_judgements,
    pending_formex_title,
    resolve_case_celex,
    unzip_formex,
)
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)


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
    # CA/CN are OJ information notices, never Advocate General opinions.
    assert classify_celex("62024CA0646") == (DocType.NOTE, "Court of Justice")
    assert classify_celex("62024CN0646", "INFO_JUDICIAL") == (DocType.NOTE, "Court of Justice")
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


def test_html_case_title_uses_eurlex_hidden_heading():
    from raglex.adapters.eu_cellar import html_case_title

    html = (b'<html><p id="englishTitle">Judgment of the Court of 19 March 2026.'
            b'#European Commission v Republic of Bulgaria.#Case C-646/24.</p></html>')
    assert html_case_title(html) == "European Commission v Republic of Bulgaria"


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


PENDING_FORMEX = """<?xml version="1.0" encoding="UTF-8"?>
<CJT><TI.CJT><TITLE><TI>
 <P>Action brought on 6 December 2024 – International Electrotechnical Commission and ISO v Commission</P>
 <P>(Case T-631/24)</P>
</TI></TITLE><NO.DOC.C>C/2025/919</NO.DOC.C><LG.PROC>Language of the case: English</LG.PROC>
</TI.CJT><CONTENTS><GR.SEQ><TITLE><TI><P>Form of order sought</P></TI></TITLE>
<P>The applicants claim that the Court should annul the decision.</P></GR.SEQ></CONTENTS></CJT>""".encode()


class PendingClient(FakeClient):
    def __init__(self):
        self.formex = _zip(PENDING_FORMEX, "C_202500919EN.000101.fmx.xml")

    def request(self, method, url, *, data=None, headers=None):
        query = data["query"]
        rows = [] if "OFFSET 100" in query else [{
            "celex": "62024TN0631",
            "date": "2024-12-06",
            "rtype": "INFO_JUDICIAL",
            "procedure": "ANNU",
            "dossier": "http://publications.europa.eu/resource/case/T-631%2F24",
        }]
        return _JsonResp({"results": {"bindings": [
            {key: {"value": value} for key, value in row.items()} for row in rows
        ]}})


def test_pending_t_notice_is_retrieved_named_tagged_and_aliased():
    ad = EUCellarAdapter(pending_cases=True, client=PendingClient())
    stub = list(ad.discover(None, max_pages=1))[0]
    rec = ad.fetch(stub)

    assert rec.stable_id == "62024TN0631"
    assert rec.court == "General Court" and rec.doc_type == DocType.NOTE
    # Docketed like every decided case: "Pending: X v Y" alone gives a reader following
    # a case no way to recognise it.
    assert rec.title == ("Pending: International Electrotechnical Commission and ISO "
                         "v Commission (T-631/24)")
    assert rec.topic_tags == ["Pending", "Action brought"]
    assert rec.extra["pending_procedure"] == "ANNU"
    assert rec.extra["aliases"] == ["62024TJ0631"]


#: A real C-series preliminary-reference notice: the referring court's name contains a
#: dash, the docket parenthetical carries the Court's short name for the case, and a
#: footnote sits inside the first question's sentence.
PENDING_REFERENCE_FORMEX = """<?xml version="1.0" encoding="UTF-8"?>
<CJT><TI.CJT><TITLE><TI>
 <P>Request for a preliminary ruling from the Okrazhen sad – Razgrad (Bulgaria) lodged on
 6 May 2026 – Rayonen sad – Tutrakan v O.K.M.</P>
 <P>(Case C-448/26, Rayonen sad – Tutrakan)</P>
</TI></TITLE><LG.PROC>Language of the case: Bulgarian</LG.PROC></TI.CJT>
<CONTENTS>
<GR.SEQ LEVEL="1"><TITLE><TI><P>Referring court</P></TI></TITLE>
<P>Okrazhen sad – Razgrad</P></GR.SEQ>
<GR.SEQ LEVEL="1"><TITLE><TI><P>Parties to the main proceedings</P></TI></TITLE>
<P><HT TYPE="ITALIC">Appellant:</HT> Rayonen sad Tutrakan</P>
<P><HT TYPE="ITALIC">Respondent:</HT> O.K.M.</P></GR.SEQ>
<GR.SEQ LEVEL="1"><TITLE><TI><P>Questions referred</P></TI></TITLE>
<LIST TYPE="ARAB">
<ITEM><NP><NO.P>1.</NO.P><TXT>Must Article 5 of Directive 2008/115/EC<NOTE TYPE="FOOTNOTE"><P>OJ 2008 L 348, p. 98.</P></NOTE> be interpreted as precluding national legislation?</TXT></NP></ITEM>
<ITEM><NP><NO.P>2.</NO.P><TXT>If the answer to Question 1 is in the negative, does Article 19(2) of the Charter preclude removal?</TXT></NP></ITEM>
</LIST></GR.SEQ></CONTENTS></CJT>""".encode()


def test_notice_title_survives_a_dash_inside_the_referring_courts_name():
    # The party split anchors on the LODGING DATE, not on "some dash": a greedy match
    # took the last one and titled this case "Tutrakan)".
    assert pending_formex_title(PENDING_REFERENCE_FORMEX) == \
        "Rayonen sad – Tutrakan v O.K.M (C-448/26)"


def test_notice_is_parsed_as_a_form_not_as_a_wall_of_text():
    text, segments = extract_formex(PENDING_REFERENCE_FORMEX)
    labels = [s.label for s in segments]
    # Each named section is a heading, each party its own line, each question its own
    # segment labelled with the number the Court itself will use.
    assert "Referring court" in labels and "Parties to the main proceedings" in labels
    assert "Question 1" in labels and "Question 2" in labels
    assert [s.kind for s in segments if s.label == "Referring court"] == ["heading"]
    parties = [text[s.char_start:s.char_end] for s in segments
               if s.label.startswith("Parties to the main proceedings —")]
    assert parties == ["Appellant: Rayonen sad Tutrakan", "Respondent: O.K.M."]
    q1 = next(text[s.char_start:s.char_end] for s in segments if s.label == "Question 1")
    # The footnote is lifted OUT of the sentence it annotated — inline, it spliced a
    # whole OJ citation into the middle of the question.
    assert "Directive 2008/115/EC [1] be interpreted" in q1
    assert q1.endswith("[1] OJ 2008 L 348, p. 98.")


def test_pending_query_uses_dossier_for_c_and_t_resolution():
    query = EUCellarAdapter(pending_cases=True)._pending_query("2026-07-20", 100)
    assert "work_part_of_dossier" in query
    assert "[CT]N" in query and "[CT][JO]" in query
    assert 'FILTER(STR(?date) > "2026-07-20")' in query
    # Resolving decisions deliberately have no date cutoff: this is what catches a
    # late English rendition of an older French-only decision.
    assert query.count('FILTER(STR(?date) > "2026-07-20")') == 1
    assert "ORDER BY ?phase DESC(?date)" in query


def test_modern_case_resolution_never_crosses_court_family(monkeypatch):
    class SameNumberClient:
        def request(self, method, url, *, data=None, headers=None):
            return _JsonResp({"results": {"bindings": [
                {"celex": {"value": "62024CC0631"}},
                {"celex": {"value": "62024TN0631"}},
            ]}})

    # T-631/24 is not the unrelated C-631/24 merely because the latter already has
    # an Advocate General's opinion.  Notices are not decisions, so this stays pending.
    assert resolve_case_celex(
        "62024TJ0631", client=SameNumberClient()
    ) is None


def test_full_english_decision_retires_but_does_not_delete_pending_notice(catalogue):
    pending = Record(source="eu-cellar", stable_id="62024TN0631", doc_type=DocType.NOTE,
                     title="Pending: IEC and ISO v Commission", raw_bytes=b"pending",
                     text="application", source_language="en", extra={"pending": True})
    pending.ensure_payload_hash()
    final = Record(source="eu-cellar", stable_id="ECLI:EU:T:2026:1",
                   ecli="ECLI:EU:T:2026:1", doc_type=DocType.JUDGMENT,
                   title="IEC and ISO v Commission", raw_bytes=b"full English judgment",
                   text="full reasons", source_language="en")
    final.ensure_payload_hash()
    catalogue.upsert_document(pending)
    catalogue.upsert_document(final)

    assert catalogue.retire_pending_eu_notice(pending.stable_id, final.stable_id)
    assert catalogue.get_document(pending.stable_id) is not None
    assert catalogue.get_document(pending.stable_id)["search_excluded"] == 1
    assert catalogue.document_meta(pending.stable_id)["resolved_by"] == final.stable_id
    assert any(r["relationship_type"] == "supersedes"
               and r["dst_id"] == pending.stable_id
               for r in catalogue.relations_for(final.stable_id))


def _notice(stable_id: str, celex: str | None = None, **extra) -> Record:
    rec = Record(source="eu-cellar", stable_id=stable_id, doc_type=DocType.NOTE,
                 title=f"Pending: {stable_id}", raw_bytes=b"pending", text="application",
                 source_language="en",
                 extra={"pending": True, "celex": celex or stable_id,
                        "aliases": [stable_id[:6] + "J" + stable_id[7:]], **extra})
    rec.ensure_payload_hash()
    return rec


def test_an_ag_opinion_never_retires_the_pending_notice(catalogue):
    """The Opinion is filed months before judgment and decides nothing. Suppressing the
    notice on it would hide a live case behind a document that does not answer it."""
    notice = _notice("62024CN0801")
    opinion = Record(source="eu-cellar", stable_id="ECLI:EU:C:2026:17",
                     ecli="ECLI:EU:C:2026:17", doc_type=DocType.OPINION,
                     title="Opinion of AG Medina", raw_bytes=b"opinion", text="opinion",
                     source_language="en", extra={"celex": "62024CC0801"})
    opinion.ensure_payload_hash()
    catalogue.upsert_document(notice)
    catalogue.upsert_document(opinion)

    assert catalogue.retire_pending_eu_notice("62024CN0801", "ECLI:EU:C:2026:17") is False
    assert catalogue.get_document("62024CN0801")["search_excluded"] == 0
    assert catalogue.document_meta("62024CN0801")["pending"] is True


def test_retirement_survives_the_next_harvest_of_the_same_notice(catalogue):
    """The pending feed re-enumerates a notice forever. Taking the incoming record's
    (unset) search_excluded verbatim un-hid 67 notices on the live corpus."""
    notice = _notice("62024TN0631")
    final = Record(source="eu-cellar", stable_id="ECLI:EU:T:2026:1",
                   ecli="ECLI:EU:T:2026:1", doc_type=DocType.JUDGMENT, title="IEC v Commission",
                   raw_bytes=b"judgment", text="reasons", source_language="en",
                   extra={"celex": "62024TJ0631"})
    final.ensure_payload_hash()
    catalogue.upsert_document(notice)
    catalogue.upsert_document(final)
    assert catalogue.retire_pending_eu_notice("62024TN0631", "ECLI:EU:T:2026:1")

    catalogue.upsert_document(_notice("62024TN0631"))   # the next daily pass
    assert catalogue.get_document("62024TN0631")["search_excluded"] == 1


def test_retirement_hands_the_judgment_celex_back_to_the_judgment(catalogue):
    """While pending, the notice holds the judgment's CELEX alias in trust — which is
    why an AG Opinion's opinion_in edge landed on it. Retirement must hand it back."""
    catalogue.upsert_document(_notice("62024CN0801"))
    catalogue.put_alias("62024cj0801", "62024CN0801", source="adapter-alias")
    judgment = Record(source="eu-cellar", stable_id="ECLI:EU:C:2026:472",
                      ecli="ECLI:EU:C:2026:472", doc_type=DocType.JUDGMENT,
                      title="NSD v Council", raw_bytes=b"judgment", text="reasons",
                      source_language="en", extra={"celex": "62024CJ0801"})
    judgment.ensure_payload_hash()
    catalogue.upsert_document(judgment)
    catalogue.add_relations("ECLI:EU:C:2026:17", [TypedRelation(
        relationship_type=RelationshipType.OPINION_IN, raw_citation_string="62024CJ0801",
        dst_id="62024CN0801", extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.RESOLVED)])

    assert catalogue.retire_pending_eu_notice("62024CN0801", "ECLI:EU:C:2026:472")
    assert catalogue.find_document_id("62024CJ0801") == "ECLI:EU:C:2026:472"
    opinion_edge = catalogue.relations_for("ECLI:EU:C:2026:17")[0]
    assert opinion_edge["dst_id"] == "ECLI:EU:C:2026:472"


def test_sweep_retires_notices_the_feed_never_paired(catalogue):
    """The order-independent sweep: a judgment harvested by the ordinary CJEU feed left
    its notice reading "Pending:" indefinitely (220 of them, live)."""
    catalogue.upsert_document(_notice("62024CN0801"))
    catalogue.upsert_document(_notice("62025CN0100"))          # still genuinely pending
    for stable_id, celex, doc_type in (
        ("ECLI:EU:C:2026:472", "62024CJ0801", DocType.JUDGMENT),
        ("ECLI:EU:C:2026:17", "62025CC0100", DocType.OPINION),  # an Opinion resolves nothing
    ):
        rec = Record(source="eu-cellar", stable_id=stable_id, ecli=stable_id,
                     doc_type=doc_type, title=stable_id, raw_bytes=stable_id.encode(),
                     text="text", source_language="en", extra={"celex": celex})
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec)

    pairs = catalogue.resolved_pending_eu_notices()
    assert pairs == [("62024CN0801", "ECLI:EU:C:2026:472")]
    assert catalogue.retire_pending_eu_notice(*pairs[0])
    assert catalogue.get_document("62025CN0100")["search_excluded"] == 0


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


def test_oj_result_notice_is_not_linked_as_an_ag_opinion():
    ad = EUCellarAdapter(legislation_celex="32004R0139", client=FakeClient())
    stub = Stub(stable_id="62024CA0646",
                raw_url="https://publications.europa.eu/resource/celex/62024CA0646",
                hints={"celex": "62024CA0646", "rtype": "INFO_JUDICIAL"})
    rec = ad.fetch(stub)
    assert rec.doc_type == DocType.NOTE and rec.court == "Court of Justice"
    assert not any(r.relationship_type == RelationshipType.OPINION_IN for r in rec.relations)


def test_french_fallback_appends_labelled_english_oj_ruling(monkeypatch):
    ad = EUCellarAdapter(client=FakeClient())
    french = "ARRÊT DE LA COUR\nDans l’affaire C-646/24, la Commission européenne.\nPar ces motifs."
    notice = ("Operative part of the judgment\nThe Court:\n1. Declares the failure.\n"
              "2. Orders payment.\nELI: http://example.test/oj")

    def rendition(_url, _celex, language):
        if language == "en":
            return b"<html>French passthrough</html>", "html", french, []
        return "<html>Texte français</html>".encode(), "html", french, []

    monkeypatch.setattr(ad, "_rendition", rendition)
    monkeypatch.setattr(ad, "_fetch_eurlex_html", lambda celex, language: notice.encode())
    monkeypatch.setattr(ad, "_html_to_text", lambda raw: raw.decode())

    stub = Stub(stable_id="ECLI:EU:C:2026:221", hint_date=None,
                raw_url="https://publications.europa.eu/resource/celex/62024CJ0646",
                hints={"celex": "62024CJ0646"})
    monkeypatch.setattr(ad, "_sparql", lambda _query: [])
    rec = ad.fetch(stub)
    assert rec.source_language == "fr"
    assert "English Official Journal notice — operative part" in rec.text
    assert "The Court:\n1. Declares" in rec.text
    assert "ELI:" not in rec.extra["english_oj_operative_part"]
    assert rec.extra["english_oj_notice_celex"] == "62024CA0646"


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
      </ENACTING.TERMS>
      <ANNEX><TITLE>ANNEX I</TITLE><P>Commercial practices which are in all
      circumstances considered unfair.</P><ITEM>1. Claiming to be a signatory.</ITEM></ANNEX>
      </ACT>"""
    text, segs = extract_formex(xml)
    labels = [s.label for s in segs]
    kinds = {s.label: s.kind for s in segs}
    assert "Legal basis 1" in labels and kinds["Legal basis 1"] == "visa"
    assert "Recital 1" in labels
    assert "Article 5" in labels                       # whole-article heading resolves
    assert "Article 5(1)" in labels and "Article 5(2)" in labels  # per-paragraph pincites
    assert "ANNEX I" in labels and kinds["ANNEX I"] == "annex"
    assert "Commercial practices" in text and "Claiming to be a signatory" in text
    # every segment's offsets slice real text, in document order (drift-safe)
    assert all(text[s.char_start:s.char_end].strip() for s in segs)
    assert [s.char_start for s in segs] == sorted(s.char_start for s in segs)
    # judgment Formex still parses as before
    jxml = b"<JUDGMENT><NP.ECR><NO.P>1</NO.P><TXT>Claim.</TXT></NP.ECR><JURISDICTION>Held.</JURISDICTION></JUDGMENT>"
    _jt, js = extract_formex(jxml)
    assert [s.label for s in js] == ["1", "ruling"]


def test_formex_legislation_combines_split_zip_members():
    """The UCPD package stores Annex I outside the largest XML member."""
    import io
    import zipfile
    from raglex.formats.formex import parse_formex_legislation

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("01.xml", """
          <ACT><ENACTING.TERMS><ARTICLE><TI.ART>Article 1</TI.ART>
          <P>Purpose.</P></ARTICLE></ENACTING.TERMS></ACT>""")
        z.writestr("02.xml", """
          <ACT><ANNEX><TITLE>ANNEX I</TITLE>
          <P>Commercial practices always considered unfair.</P></ANNEX></ACT>""")
        z.writestr("notice.doc.xml", "<DOC><TITLE>notice</TITLE></DOC>")
    parsed = parse_formex_legislation(buf.getvalue())
    assert [segment.label for segment in parsed.segments] == ["Article 1", "ANNEX I"]
    assert "Commercial practices always considered unfair" in parsed.text


def test_formex_consolidation_preserves_cons_annexes():
    """Sector-0 Formex spells annexes CONS.ANNEX rather than ANNEX."""
    from raglex.formats.formex import parse_formex_legislation

    parsed = parse_formex_legislation(b"""
      <CONS.ACT><CONS.DOC><ENACTING.TERMS>
        <ARTICLE><TI.ART>Article 1</TI.ART><P>Purpose.</P></ARTICLE>
      </ENACTING.TERMS>
      <CONS.ANNEX><TITLE><TI><P>ANNEX I</P></TI>
        <STI><P>BLACKLIST</P></STI></TITLE>
        <P>A practice always considered unfair.</P></CONS.ANNEX>
      </CONS.DOC></CONS.ACT>""")
    assert [(s.label, s.kind) for s in parsed.segments] == [
        ("Article 1", "article"), ("ANNEX I BLACKLIST", "annex"),
    ]
    assert "A practice always considered unfair" in parsed.text


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


def test_a_judgment_reparsed_through_the_format_registry_is_not_read_as_an_act():
    """The Formex parser is registered for every Formex instance, and CJEU CASE LAW is
    Formex too. Run against a judgment, the legislation reader recognises only the recitals
    the judgment QUOTES and discards the reasoning: a live re-parse cut Dun & Bradstreet
    (C-203/22) from 57,012 characters to 3,822 — six "recital" segments and no judgment.
    A judgment must reach the case-law reader, which is what the adapter uses at harvest."""
    from raglex.formats import parse

    pd = parse("formex-legislation", JUDGMENT_FMX)
    kinds = {s.kind for s in pd.segments}
    assert "recital" not in kinds
    assert {"paragraph", "heading", "ruling"} <= kinds
    assert "By Question 3(b) and (c), the referring court asks." in (pd.text or "")
    # …and an ACT still parses as an act
    act = parse("formex-legislation", b"""<ACT><TITLE><TI>Regulation</TI></TITLE>
      <PREAMBLE><GR.CONSID><CONSID><NP><NO.P>(1)</NO.P><TXT>Whereas this.</TXT></NP></CONSID></GR.CONSID></PREAMBLE>
      <ENACTING.TERMS><ARTICLE><TI.ART>Article 1</TI.ART><PARAG><NO.PARAG>1</NO.PARAG>
      <ALINEA>This Regulation applies.</ALINEA></PARAG></ARTICLE></ENACTING.TERMS></ACT>""")
    assert any(s.kind == "article" for s in act.segments)
    assert "This Regulation applies." in (act.text or "")


def test_unzip_picks_the_document_not_the_oj_masthead():
    """CELLAR ships the OJ issue's contents wrapper alongside the notice itself, and it
    sorts first as often as not. Taking member[0] stored the masthead as 993 notices'
    text — they had no parties to read a case name from, so they were titled
    "Pending: Case T-8/24" and their questions were an OJ front page."""
    import io as _io
    import zipfile as _zip

    wrapper = (b'<?xml version="1.0" encoding="UTF-8"?>\n<PUBLICATION><OJ><BIB.OJ>'
               b"<COLL>C</COLL></BIB.OJ>"
               b'<ITEM.PUB DOC.INSTANCE="C_202401706EN.doc.fmx.xml"/></OJ></PUBLICATION>')
    document = (b'<?xml version="1.0" encoding="UTF-8"?>\n<CJT><TI.CJT><TITLE><TI>'
                b"<P>Action brought on 8 January 2024 - Alpha v Commission</P>"
                b"<P>(Case T-8/24)</P></TI></TITLE></TI.CJT></CJT>")
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("C_202401706EN.xml", wrapper)          # sorts first
        zf.writestr("C_202401706EN.doc.fmx.xml", document)
    picked = unzip_formex(buf.getvalue())
    assert picked is not None and b"<CJT>" in picked
    assert pending_formex_title(picked) == "Alpha v Commission (T-8/24)"

    # …and an archive that names no instance still yields the document beside the wrapper
    buf2 = _io.BytesIO()
    with _zip.ZipFile(buf2, "w") as zf:
        zf.writestr("aaa_index.xml", b'<?xml version="1.0"?>\n<PUBLICATION></PUBLICATION>')
        zf.writestr("zzz_item.xml", document)
    assert b"<CJT>" in (unzip_formex(buf2.getvalue()) or b"")


def test_unzip_skips_the_bibliographic_manifest_too():
    """The real archive has TWO wrappers, and the second one is the trap.

      C_202402318EN.toc.fmx.xml     <PUBLICATION>  masthead
      C_202402318EN.doc.fmx.xml     <DOC><BIB.DOC> manifest — REF.PHYS points on
      C_202402318EN.000101.fmx.xml  <CJT>          the notice

    Preferring the ".doc." member — "the document instance" — swapped the masthead for
    the manifest, whose text is its own field values ("20240315034 483657 2024 2318 T
    ELI:…"), so the repair pass re-titled 944 notices "Pending: Case T-48/24" a second
    time. The root element says what a file is; the filename doesn't.
    """
    import io as _io
    import zipfile as _zip

    toc = (b'<?xml version="1.0" encoding="UTF-8"?>\n<PUBLICATION><OJ>'
           b'<ITEM.PUB DOC.INSTANCE="C_202402318EN.doc.fmx.xml"/></OJ></PUBLICATION>')
    manifest = (b'<?xml version="1.0" encoding="UTF-8"?>\n<DOC><BIB.DOC>'
                b"<PROD.ID>20240315034</PROD.ID><AUTHOR>T</AUTHOR></BIB.DOC>"
                b'<FMX><DOC.MAIN.PUB NO.SEQ="0001">'
                b'<REF.PHYS FILE="C_202402318EN.000101.fmx.xml" TYPE="DOC.XML"/>'
                b"</DOC.MAIN.PUB></FMX></DOC>")
    notice = (b'<?xml version="1.0" encoding="UTF-8"?>\n<CJT><TI.CJT><TITLE><TI>'
              b"<P>Action brought on 30 January 2024 - CE v EIB</P>"
              b"<P>(Case T-48/24)</P></TI></TITLE></TI.CJT></CJT>")
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("C_202402318EN.toc.fmx.xml", toc)
        zf.writestr("C_202402318EN.doc.fmx.xml", manifest)
        zf.writestr("C_202402318EN.000101.fmx.xml", notice)
    picked = unzip_formex(buf.getvalue())
    assert picked is not None and b"<CJT>" in picked
    assert b"BIB.DOC" not in picked
    assert pending_formex_title(picked) == "CE v EIB (T-48/24)"


def test_oj_summary_notice_title_is_the_parties_not_the_endnote():
    """An OJ judgment-summary notice puts its parties BEFORE the docket:

        Judgment of the Court (Second Chamber) of 13 December 2007 —
        Commission of the European Communities v Ireland (Case C-418/04) OJ C 6, 8.1.2005

    The AG-opinion caption rule takes what follows the case number, which here is the
    endnote, so these were titled ") OJ C 6, 8.1.2005" — and two of them, whose endnote
    sat elsewhere, were titled nothing at all. Latent until the wrapper repair started
    re-fetching notices whose stored raw had been the OJ masthead.
    """
    notice = (
        '<?xml version="1.0" encoding="UTF-8"?><CJT NNC="YES"><TI.CJT><TITLE><TI>'
        "<P>Judgment of the Court (Second Chamber) of "
        '<DATE ISO="20071213">13 December 2007</DATE> — Commission of the European '
        "Communities v Ireland</P><P>(Case C-418/04)"
        '<NOTE NOTE.ID="E0001"><P><REF.DOC.OJ COLL="C">OJ C 6, 8.1.2005</REF.DOC.OJ>.'
        "</P></NOTE></P></TI></TITLE></TI.CJT></CJT>"
    ).encode()
    from raglex.adapters.eu_cellar import formex_case_title

    assert formex_case_title(notice) == "Commission of the European Communities v Ireland"


def test_ag_opinion_caption_still_reads_the_other_way_round():
    # The pattern the OJ rule must not displace: parties AFTER the case number.
    opinion = (
        '<?xml version="1.0" encoding="UTF-8"?><OPI><TITLE><TI>'
        "<P>Case C-340/21 VB v Natsionalna agentsia za prihodite (Request for a "
        "preliminary ruling)</P></TI></TITLE></OPI>"
    ).encode()
    from raglex.adapters.eu_cellar import formex_case_title

    assert formex_case_title(opinion) == "VB v Natsionalna agentsia za prihodite"


def test_referring_court_parenthetical_is_not_the_party_separator():
    """"…(Bundesgerichtshof – Germany) — Peek & Cloppenburg KG v Cassina SpA".

    The heading rule stops at the FIRST dash, which here sits inside the referring
    court's own parenthetical, so the capture opened mid-parenthetical and the case
    was titled "Germany) — Peek & Cloppenburg KG v Cassina SpA". Same trap
    _PENDING_HEAD_RE anchors on a date to avoid.
    """
    from raglex.adapters.eu_cellar import formex_case_title

    notice = (
        '<?xml version="1.0" encoding="UTF-8"?><CJT><TI.CJT><TITLE><TI><P>'
        "Judgment of the Court (Fourth Chamber) of 17 April 2008 (reference for a "
        "preliminary ruling from the Bundesgerichtshof – Germany) — Peek &amp; "
        "Cloppenburg KG v Cassina SpA</P><P>(Case C-456/06)</P>"
        "</TI></TITLE></TI.CJT></CJT>"
    ).encode()
    assert formex_case_title(notice) == "Peek & Cloppenburg KG v Cassina SpA"

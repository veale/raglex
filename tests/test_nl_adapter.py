from __future__ import annotations

from datetime import date

from raglex.adapters.nl_rechtspraak import parse_content, parse_index
from raglex.core.models import RelationshipType
from raglex.resolve import Resolver

INDEX = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="text">Rechtspraak Open Data (Uitspraken)</title>
  <subtitle type="text">Aantal gevonden ECLI's: 936765</subtitle>
  <entry>
    <id>ECLI:NL:HR:2021:1303</id>
    <title type="text">ECLI:NL:HR:2021:1303, Hoge Raad, 17-09-2021, 21/00400</title>
    <updated>2021-09-17T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://uitspraken.rechtspraak.nl/details?id=ECLI:NL:HR:2021:1303"/>
  </entry>
</feed>
"""

CONTENT = b"""<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dcterms="http://purl.org/dc/terms/"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ecli="https://e-justice.europa.eu/ecli"
         xmlns:psi="http://psi.rechtspraak.nl/">
  <rdf:Description>
    <dcterms:identifier>ECLI:NL:HR:2021:1303</dcterms:identifier>
    <dcterms:creator rdfs:label="Instantie">Hoge Raad</dcterms:creator>
    <dcterms:date rdfs:label="Uitspraakdatum">2021-09-17</dcterms:date>
    <dcterms:title>ECLI:NL:HR:2021:1303 Hoge Raad</dcterms:title>
    <dcterms:subject rdfs:label="Rechtsgebied">Bestuursrecht; Belastingrecht</dcterms:subject>
    <dcterms:relation rdfs:label="Formele relatie"
        ecli:resourceIdentifier="ECLI:NL:GHSHE:2020:4113"
        psi:gevolg="http://psi.rechtspraak.nl/gevolg#bekrachtiging/bevestiging">In cassatie op : ECLI:NL:GHSHE:2020:4113, Bekrachtiging/bevestiging</dcterms:relation>
  </rdf:Description>
  <uitspraak>
    <p>Het beroep betreft de verwerking van persoonsgegevens onder de AVG.</p>
  </uitspraak>
</rdf:RDF>
"""


def test_parse_index_yields_ecli_stubs():
    page = parse_index(INDEX)
    assert page.count == 1
    stub = page.stubs[0]
    assert stub.stable_id == "ECLI:NL:HR:2021:1303"
    assert stub.court == "Hoge Raad"
    assert stub.hint_date == date(2021, 9, 17)
    assert stub.raw_url.endswith("?id=ECLI:NL:HR:2021:1303")


def test_parse_content_extracts_metadata_typed_edge_and_body():
    parsed = parse_content(CONTENT)
    assert parsed.ecli == "ECLI:NL:HR:2021:1303"
    assert parsed.court == "Hoge Raad"
    assert parsed.decision_date == date(2021, 9, 17)
    assert parsed.rechtsgebied == "Bestuursrecht; Belastingrecht"
    assert "persoonsgegevens" in parsed.text
    # the <p> in <uitspraak> becomes a structural segment (§6b)
    assert parsed.segments and parsed.text[parsed.segments[0].char_start:parsed.segments[0].char_end].strip()

    assert len(parsed.relations) == 1
    rel = parsed.relations[0]
    # gevolg 'bekrachtiging/bevestiging' (affirmed) maps to a typed treatment edge
    assert rel.relationship_type == RelationshipType.APPLIES
    assert rel.dst_id == "ECLI:NL:GHSHE:2020:4113"
    assert "Bekrachtiging" in rel.raw_citation_string


def test_unknown_gevolg_falls_back_to_mentions():
    xml = CONTENT.replace(b"gevolg#bekrachtiging/bevestiging", b"gevolg#onbekend")
    rel = parse_content(xml).relations[0]
    assert rel.relationship_type == RelationshipType.MENTIONS


def test_cross_jurisdiction_resolution_uk_and_nl(catalogue):
    """The whole point of the ECLI spine: NL formal relations resolve once the
    target is in the corpus, in the same catalogue as UK docs (§1.1, §5b)."""
    from raglex.core.models import DocType, Record

    # the NL target (lower court) and a UK doc coexist in one catalogue
    for sid in ("ECLI:NL:GHSHE:2020:4113", "ukftt/grc/2026/904"):
        r = Record(source="x", stable_id=sid, ecli=sid if sid.startswith("ECLI") else None,
                   doc_type=DocType.JUDGMENT, raw_bytes=sid.encode())
        r.ensure_payload_hash()
        catalogue.upsert_document(r)

    # the citing NL decision with its typed formal-relation edge
    rec = parse_content(CONTENT)
    nl = Record(
        source="nl-rechtspraak",
        stable_id=rec.ecli,
        ecli=rec.ecli,
        doc_type=DocType.JUDGMENT,
        raw_bytes=CONTENT,
        relations=rec.relations,
    )
    nl.ensure_payload_hash()
    catalogue.upsert_document(nl)

    stats = Resolver(catalogue).run()
    assert stats.resolved == 1
    edge = catalogue.relations_for("ECLI:NL:HR:2021:1303")[0]
    assert edge["resolution_status"] == "resolved"
    assert edge["dst_id"] == "ECLI:NL:GHSHE:2020:4113"
    assert edge["relationship_type"] == "applies"

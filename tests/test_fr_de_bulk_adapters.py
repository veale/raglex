from __future__ import annotations

from datetime import date
from pathlib import Path

from raglex.adapters.de_gii import DeGiiAdapter, _slug
from raglex.adapters.de_rii import DeRiiAdapter
from raglex.adapters.fr_dila import FrDilaAdapter, _display_date
from raglex.core.models import DocType, RelationshipType
from raglex.formats.gii_xml import parse_gii
from raglex.formats.rii_xml import parse_rii
from raglex.formats.dila_xml import parse_dila_article, parse_dila_juri
from xml.etree import ElementTree as ET

REFS = Path(__file__).resolve().parent.parent / "raglex design docs" / "raglex-refs"
GII_ARCHIVE = REFS / "de-legacy" / "gii-archive" / "gesetze"


# -- Germany: gii legislation (real data) -----------------------------------
def test_gii_parser_real_law():
    d = parse_gii((GII_ARCHIVE / "zappro" / "zappro.xml").read_bytes())
    assert d.title == "Approbationsordnung für Zahnärzte und Zahnärztinnen"
    assert d.metadata["jurabk"] == "ZApprO"
    assert d.decision_date == date(2019, 7, 8)
    # §§ become citable segments
    assert any(s.label.startswith("§ 1 ") for s in d.segments)
    assert len(d.segments) > 50


def test_de_gii_local_discover_and_fetch():
    adapter = DeGiiAdapter(path=str(GII_ARCHIVE), ids=["ZApprO"])
    stubs = list(adapter.discover(None))
    assert stubs and stubs[0].stable_id == "de/gesetz/zappro"
    rec = adapter.fetch(stubs[0])
    assert rec.doc_type == DocType.LEGISLATION
    assert rec.extra["jurabk"] == "ZApprO"
    assert rec.text and rec.segments


def test_de_gii_slug():
    assert _slug("SGB V") == "de/gesetz/sgbv"


# -- Germany: rii case law (constructed fixture, juris rii DTD shape) --------
RII = """<?xml version="1.0" encoding="UTF-8"?>
<dokumente>
 <dokument doknr="KVRE123">
  <gertyp>BGH</gertyp>
  <spruchkoerper>VI. Zivilsenat</spruchkoerper>
  <ecli>ECLI:DE:BGH:2021:120521UVIZR100.20.0</ecli>
  <entsch-datum>2021-05-12</entsch-datum>
  <aktenzeichen>VI ZR 100/20</aktenzeichen>
  <doktyp>Urteil</doktyp>
  <norm>§ 823 Abs. 1 BGB</norm>
  <vorinstanz>vorgehend OLG München, Az. 1 U 2/20</vorinstanz>
  <identifier>https://www.rechtsprechung-im-internet.de/example</identifier>
  <publisher>BMJV</publisher>
  <titelzeile>Schadensersatz nach Datenschutzverstoß</titelzeile>
  <leitsatz><Content><P>Der Leitsatz.</P></Content></leitsatz>
  <tenor><Content><P>Die Revision wird zurueckgewiesen.</P></Content></tenor>
  <tatbestand><Content><P>Der Klaeger verlangt Schadensersatz.</P></Content></tatbestand>
  <entscheidungsgruende><Content><P>Die Revision ist unbegruendet.</P></Content></entscheidungsgruende>
 </dokument>
</dokumente>""".encode("utf-8")


def test_rii_parser_zones_and_ecli():
    d = parse_rii(RII)
    assert d.metadata["ecli"] == "ECLI:DE:BGH:2021:120521UVIZR100.20.0"
    assert d.metadata["court"] == "Bundesgerichtshof"
    assert d.metadata["court_code"] == "BGH"
    assert d.metadata["court_body"] == "VI. Zivilsenat"
    assert d.decision_date == date(2021, 5, 12)
    assert [s.label for s in d.segments] == ["Normen", "Vorinstanz", "Leitsatz", "Tenor", "Tatbestand", "Entscheidungsgründe"]
    assert d.metadata["identifier"].endswith("/example")


def test_de_rii_local_fetch(tmp_path):
    f = tmp_path / "bgh.xml"
    f.write_bytes(RII)
    adapter = DeRiiAdapter(path=str(tmp_path))
    stubs = list(adapter.discover(None))
    rec = adapter.fetch(stubs[0])
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.ecli == "ECLI:DE:BGH:2021:120521UVIZR100.20.0"
    assert rec.court == "Bundesgerichtshof" and rec.text
    assert rec.extra["court_code"] == "BGH"
    assert rec.extra["court_body"] == "VI. Zivilsenat"
    assert rec.extra["norms"] == "§ 823 Abs. 1 BGB"
    assert rec.landing_url.endswith("/example")


# -- France: DILA (constructed fixtures, DTD shapes) ------------------------
DILA_JURI = """<?xml version="1.0" encoding="UTF-8"?>
<TEXTE_JURI_JUDI>
 <META><META_COMMUN><ID>JURITEXT000012345</ID><NATURE>ARRET</NATURE></META_COMMUN>
  <META_SPEC><META_JURI>
    <TITRE>Cour de cassation, civile, Chambre civile 1</TITRE>
    <DATE_DEC>2021-05-12</DATE_DEC>
    <JURIDICTION>Cour de cassation</JURIDICTION>
    <NUMERO>21-00400</NUMERO>
    <ECLI>ECLI:FR:CCASS:2021:C100400</ECLI>
    <SOLUTION>Cassation</SOLUTION>
  </META_JURI></META_SPEC>
 </META>
 <TEXTE><BLOC_TEXTUEL><CONTENU><p>Sur le moyen unique, la Cour casse.</p></CONTENU></BLOC_TEXTUEL></TEXTE>
 <LIENS><LIEN id="LEGIARTI000032041571" nature="CITATION">article 1240 du code civil</LIEN></LIENS>
</TEXTE_JURI_JUDI>""".encode("utf-8")

DILA_ARTICLE = """<?xml version="1.0" encoding="UTF-8"?>
<ARTICLE>
 <META><META_COMMUN><ID>LEGIARTI000032041571</ID></META_COMMUN>
  <META_SPEC><META_ARTICLE><NUM>1240</NUM><ETAT>VIGUEUR</ETAT>
   <DATE_DEBUT>2016-10-01</DATE_DEBUT><DATE_FIN>2999-01-01</DATE_FIN></META_ARTICLE></META_SPEC>
 </META>
 <CONTEXTE><TEXTE cid="JORFTEXT000000000001" nature="LOI" num="2016-999"
   date_signature="2016-09-30" date_publi="2016-10-01">
   <TITRE_TXT c_titre_court="Code civil" id_txt="LEGITEXT000006070721">Code civil relatif aux obligations</TITRE_TXT>
  </TEXTE></CONTEXTE>
 <BLOC_TEXTUEL><CONTENU><p>Tout fait quelconque de l'homme...</p></CONTENU></BLOC_TEXTUEL>
 <LIENS><LIEN id="LEGIARTI000000000002" cidtexte="JORFTEXT000000000002"
   typelien="MODIFICATION" num="2">Loi antérieure - art. 2 (M)</LIEN></LIENS>
</ARTICLE>""".encode("utf-8")


def test_dila_juri_parse():
    j = parse_dila_juri(ET.fromstring(DILA_JURI))
    assert j.ecli == "ECLI:FR:CCASS:2021:C100400"
    assert j.jurisdiction == "Cour de cassation"
    assert j.date == date(2021, 5, 12)
    assert "casse" in j.text
    assert j.relations and j.relations[0].raw_citation_string.startswith("article 1240")


def test_dila_article_parse():
    a = parse_dila_article(ET.fromstring(DILA_ARTICLE))
    assert a.art_id == "LEGIARTI000032041571"
    assert a.num == "1240"
    assert a.etat == "VIGUEUR"
    assert a.date_debut == date(2016, 10, 1)
    assert a.code_cid == "LEGITEXT000006070721"
    assert a.code_title == "Code civil"
    assert a.full_title == "Code civil relatif aux obligations"
    assert a.jorf_cid == "JORFTEXT000000000001"
    assert a.text_number == "2016-999"
    assert {r.dst_id for r in a.relations} == {
        "LEGIARTI000000000002", "JORFTEXT000000000002"}


def test_dila_future_validity_sentinels_are_not_display_dates():
    assert _display_date(date(2999, 1, 1)) is None
    assert _display_date(date(2020, 1, 1)) == date(2020, 1, 1)


def test_fr_dila_fetch_juri_and_article(tmp_path):
    (tmp_path / "JURITEXT000000000001.xml").write_bytes(DILA_JURI)
    (tmp_path / "index.xml").write_bytes(b"<INDEX/>")
    cass = FrDilaAdapter(path=str(tmp_path), fond="CASS")
    stubs = list(cass.discover(None))
    assert [s.stable_id for s in stubs] == ["JURITEXT000000000001"]
    rec = cass.fetch(stubs[0])
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.stable_id == "ECLI:FR:CCASS:2021:C100400"
    assert rec.relations[0].relationship_type == RelationshipType.MENTIONS

    legi_dir = tmp_path / "legi"
    legi_dir.mkdir()
    (legi_dir / "LEGIARTI000032041571.xml").write_bytes(DILA_ARTICLE)
    # Real snapshots also contain many ELI/text metadata XML files.  LEGI discovery
    # must not spend the bulk run feeding those irrelevant files to the parser.
    (legi_dir / "versions.xml").write_bytes(b"<VERSIONS/>")
    legi = FrDilaAdapter(path=str(legi_dir), fond="LEGI")
    legi_stubs = list(legi.discover(None))
    assert [s.stable_id for s in legi_stubs] == ["LEGIARTI000032041571"]
    rec2 = legi.fetch(legi_stubs[0])
    assert rec2.doc_type == DocType.LEGISLATION
    assert rec2.stable_id == "LEGIARTI000032041571"
    assert "Code civil" in rec2.title


def test_bulk_sources_registered():
    from raglex.adapters.registry import ADAPTERS, source_catalog
    for k in ("de-gii", "de-rii", "fr-dila", "fr-dila-legi", "fr-dila-cnil"):
        assert k in ADAPTERS
    cat = {r["key"]: r for r in source_catalog()}
    assert cat["de-gii"]["jurisdiction"] == "DE"
    assert cat["fr-dila"]["kind"] == "caselaw"
    assert cat["fr-dila-cnil"]["kind"] == "administrative"


# -- bulk harvest recovery ---------------------------------------------------

def test_de_rii_toc_discovery_survives_without_local_path(monkeypatch):
    """The ToC-diff network path previously died with a NameError (`_compact_date`
    was never imported) on its FIRST item — the only no-download way to run de-rii
    was entirely unusable."""
    toc = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
      <gericht>BGH</gericht><entsch-datum>20210512</entsch-datum>
      <aktenzeichen>VI ZR 100/20</aktenzeichen>
      <link>https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/KORE123</link>
      <modified>2021-05-13</modified>
    </item></channel></rss>"""

    class FakeResp:
        status_code = 200
        content = toc

    adapter = DeRiiAdapter()
    monkeypatch.setattr(adapter, "_client", type("C", (), {
        "get": lambda self, url, **kw: FakeResp()})())
    stubs = list(adapter.discover(None))
    assert len(stubs) == 1
    assert stubs[0].stable_id == "KORE123"
    from datetime import date
    assert stubs[0].hint_date == date(2021, 5, 12)


def test_bulk_harvest_recovers_stored_but_unextracted_backlog(tmp_path):
    """An ECLI-keyed bulk source stores under the ECLI resolved at fetch, so the
    pipeline's per-stub held check (keyed on the FILE name) never matches on restart —
    the durable-backlog rebuild must pick those documents up for extraction anyway."""
    from raglex.config import Config
    from raglex.core.models import Record
    from raglex.facade import Facade

    data_dir = tmp_path / "data"
    f = Facade(Config(
        data_dir=data_dir, catalogue_path=data_dir / "cat.sqlite",
        raw_dir=data_dir / "raw", text_dir=data_dir / "text",
        settings_path=data_dir / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))
    # simulate an interrupted earlier run: document stored, never extracted
    with f._open() as (cat, _rs, ts):
        rec = Record(source="fr-dila", stable_id="ECLI:FR:CCASS:2021:C100400",
                     ecli="ECLI:FR:CCASS:2021:C100400", doc_type=DocType.JUDGMENT,
                     title="Cass civ 1", text="Sur le moyen unique, la Cour casse.")
        rec.ensure_payload_hash()
        text_path = str(ts.put(rec.payload_hash, rec.text))
        cat.upsert_document(rec, raw_path=None, text_path=text_path)
        assert cat.get_document(rec.stable_id)["last_extracted_at"] is None
    # a fresh harvest over an EMPTY corpus dir discovers nothing new, but the
    # bulk-source backlog rebuild must still finish the stored document
    empty = tmp_path / "empty"
    empty.mkdir()
    f.harvest("fr-dila", backfill=True, max_pages=None,
              options={"path": str(empty), "fond": "CASS"})
    with f._open() as (cat, _rs, _ts):
        assert cat.get_document("ECLI:FR:CCASS:2021:C100400")["last_extracted_at"]


def test_de_rii_network_stub_id_matches_the_stored_id():
    """The pipeline skips an already-held document by ID before downloading it, and a
    rii decision is stored under the doknr — the local-clone path uses the zip's stem.
    Yielding "jb-JURE100054597.zip" from the feed matched nothing held, so every pass
    re-fetched and re-parsed all 83,465 decisions only to drop them on the payload hash."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <link>https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-JURE100054597.zip</link>
        <entsch-datum>20100108</entsch-datum>
        <gericht>BGH</gericht><aktenzeichen>VI ZR 100/20</aktenzeichen>
      </item>
    </channel></rss>"""

    class _Resp:
        content = feed
        status_code = 200

    class _HTTP:
        def get(self, url, **kw):
            return _Resp()

    stubs = list(DeRiiAdapter(client=_HTTP()).discover(None))
    assert [s.stable_id for s in stubs] == ["jb-JURE100054597"]
    assert stubs[0].hints["url"].endswith("jb-JURE100054597.zip")   # the fetch still gets the zip


def test_de_rii_cursor_is_the_toc_modified_stamp_not_the_decision_date():
    """discover() filters on the ToC's <modified>, so the cursor it advances has to be
    the same clock. It rode on the decision date instead — and rii publishes decisions
    weeks to months after they are decided, so the two never lined up."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <link>https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-JURE100054597.zip</link>
        <entsch-datum>20150108</entsch-datum>
        <gericht>BGH</gericht><aktenzeichen>VI ZR 100/20</aktenzeichen>
        <modified>2026-08-07T21:08:51.192Z</modified>
      </item>
    </channel></rss>"""

    class _Resp:
        content = feed
        status_code = 200

    class _HTTP:
        def get(self, url, **kw):
            return _Resp()

    stub = list(DeRiiAdapter(client=_HTTP()).discover(None))[0]
    assert stub.hints["watermark"] == "2026-08-07T21:08:51.192Z"
    # a 2015 decision re-published today is exactly what the modified filter is for
    assert stub.hint_date.isoformat() == "2015-01-08"
    assert list(DeRiiAdapter(client=_HTTP()).discover("2026-08-08T00:00:00Z")) == []


def test_de_gii_incremental_only_yields_laws_the_server_says_moved():
    """The gii ToC carries a title and a link and nothing else, so each law's HTTP
    Last-Modified is the only change signal for the ~6,130 federal statutes. Without it
    a routine run downloaded, unzipped and XML-parsed every one of them — the whole BGB
    included — purely to discard it on the payload hash."""
    from raglex.adapters.de_gii import DeGiiAdapter

    toc = b"""<?xml version="1.0"?><rss><channel>
      <item><title>BGB</title><link>http://www.gesetze-im-internet.de/bgb/xml.zip</link></item>
      <item><title>BDSG</title><link>http://www.gesetze-im-internet.de/bdsg_2018/xml.zip</link></item>
    </channel></rss>"""
    last_modified = {
        "http://www.gesetze-im-internet.de/bgb/xml.zip": "Wed, 05 Aug 2026 10:00:00 GMT",
        "http://www.gesetze-im-internet.de/bdsg_2018/xml.zip": "Mon, 13 Jul 2026 19:55:13 GMT",
    }

    class _Resp:
        def __init__(self, content=b"", headers=None):
            self.status_code, self.content = 200, content
            self.headers = headers or {}

    class _HTTP:
        def get(self, url, **kw):
            return _Resp(toc)

        def request(self, method, url, **kw):
            return _Resp(headers={"last-modified": last_modified[url]})

    seed = list(DeGiiAdapter(client=_HTTP()).discover(None))
    assert [s.stable_id for s in seed] == ["de/gii/bgb", "de/gii/bdsg_2018"]

    incremental = list(DeGiiAdapter(client=_HTTP()).discover("2026-08-01T00:00:00Z"))
    assert [s.stable_id for s in incremental] == ["de/gii/bgb"]
    assert incremental[0].hints["watermark"] == "2026-08-05T10:00:00Z"
    # a consolidated statute is amended in place under one id, so the pipeline has to be
    # told the held copy is superseded or it will (rightly) skip it
    assert incremental[0].hints["revision"] is True

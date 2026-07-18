"""Regulatory-guidance groundwork (§1.9/§5c/§6b): the layout-aware PDF extractor,
numbered-paragraph segments for guidance imports, the raw-original serving endpoint,
and the citation scanner behind the PDF viewer's linkified text layer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raglex.config import Config
from raglex.facade import Facade
from raglex.web import create_app

fitz = pytest.importorskip("fitz")  # PyMuPDF — used both to BUILD test PDFs and to parse


def _guidance_pdf() -> bytes:
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 90), "Guidelines 05/2020 on consent under Regulation 2016/679")
    y = 130
    for n in range(1, 6):
        p1.insert_text((72, y), f"{n}. Paragraph {n} discusses Article 7 of Regulation (EU) 2016/679.")
        y += 40
    p2 = doc.new_page()
    y = 100
    for n in range(6, 10):
        p2.insert_text((72, y), f"{n}. Further analysis; see Case C-673/17.")
        y += 40
    return doc.tobytes()


def test_structured_pdf_extractor_wins_and_records_pages():
    from raglex.extraction import extract_bytes

    ex = extract_bytes(_guidance_pdf(), ext="pdf")
    assert ex.engine == "pymupdf"
    assert not ex.needs_ocr
    assert [p for p, _s, _e in ex.page_spans] == [1, 2]
    assert ex.text.startswith("Guidelines 05/2020")


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    ))


def test_guidance_import_gets_numbered_para_segments(facade):
    r = facade.import_bytes(data=_guidance_pdf(), filename="edpb-05-2020.pdf",
                            doc_type="guidance", title="EDPB Guidelines 05/2020")
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document(r["stable_id"])
        labels = [s.label for s in ts.get_segments(doc["payload_hash"])]
    # the citable unit is the numbered paragraph, not the page
    assert labels[:3] == ["para 1", "para 2", "para 3"] and "para 9" in labels


def test_commentary_import_keeps_page_segments(facade):
    r = facade.import_bytes(data=_guidance_pdf(), filename="c.pdf", doc_type="commentary")
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document(r["stable_id"])
        kinds = {s.kind for s in ts.get_segments(doc["payload_hash"])}
    assert kinds == {"page"}


def test_raw_endpoint_serves_original_and_scan_resolves(facade, tmp_path):
    r = facade.import_bytes(data=_guidance_pdf(), filename="g.pdf", doc_type="guidance")
    sid = r["stable_id"]
    client = TestClient(create_app(facade.config))

    resp = client.get(f"/documents/{sid}/raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")

    missing = client.get("/documents/nope/raw")
    assert missing.status_code == 404

    # the PDF viewer's text-layer scanner: grammar + resolution state per span
    scan = client.post("/citations/scan", json={
        "text": "see Article 7 of Regulation (EU) 2016/679 and Case C-673/17"}).json()
    by = {c["candidate_id"]: c for c in scan["citations"]}
    assert by["32016R0679"]["pinpoint"] == "Article 7"
    assert by["62017CJ0673"]["state"] == "pending"  # recognised, not yet held


def test_raw_html_is_served_sandboxed(facade):
    r = facade.import_bytes(data=b"<html><body><script>alert(1)</script>hi</body></html>",
                            filename="page.html", doc_type="commentary")
    client = TestClient(create_app(facade.config))
    resp = client.get(f"/documents/{r['stable_id']}/raw")
    assert resp.status_code == 200
    assert "sandbox" in resp.headers.get("content-security-policy", "")


def test_document_body_reports_raw_ext(facade):
    r = facade.import_bytes(data=_guidance_pdf(), filename="g.pdf", doc_type="guidance")
    assert facade.document_body(r["stable_id"])["raw_ext"] == "pdf"


def test_classifier_fields_carry_rule_and_evidence():
    from raglex.citations.guidance_class import classify_guidance, merge_rules

    got = classify_guidance(
        title="Guidelines 05/2020 on consent under Regulation 2016/679",
        url="https://edpb.europa.eu/x.pdf",
        text="EUROPEAN DATA PROTECTION BOARD\nVersion 1.1\nAdopted on 4 May 2020",
        rules=merge_rules(None))
    assert got["issuer"]["value"] == "edpb"
    assert "domain:edpb.europa.eu" in got["issuer"]["rule"]      # the working is shown
    assert got["number"]["value"] == "Guidelines 05/2020"
    assert got["version"]["value"] == "1.1"
    assert got["status"]["value"] == "adopted"
    assert got["adopted_date"]["value"] == "2020-05-04"
    assert "guidelines 5/2020" in got["aliases"]                 # unpadded form citers use
    # WP29 papers: the body's own name ("WP29") must never read as a paper number
    wp = classify_guidance(title="Opinion on X (WP248 rev.01)", rules=merge_rules(None))
    assert wp["number"]["value"] == "WP248 rev.01"
    assert "number" not in classify_guidance(title="the WP29 view", rules=merge_rules(None))


def test_rules_overlay_merges_by_code_and_persists(facade):
    r1 = facade.guidance_rules()
    assert any(i["code"] == "edpb" for i in r1["issuers"])
    facade.update_guidance_rules({"issuers": [
        {"code": "cnil", "label": "CNIL", "domains": ["cnil.fr"], "boilerplate": [],
         "default_regime": "32016R0679"},
        {"code": "edpb", "label": "EDPB (renamed)", "domains": ["edpb.europa.eu"],
         "boilerplate": ["european data protection board"]},
    ], "collections": {"RAGX999": {"doc_type": "guidance", "issuer": "edpb"}}})
    r2 = facade.guidance_rules()
    codes = {i["code"]: i for i in r2["issuers"]}
    assert "cnil" in codes                       # user row appended
    assert codes["edpb"]["label"] == "EDPB (renamed)"  # user row replaces the default
    assert r2["collections"]["RAGX999"]["doc_type"] == "guidance"


def test_classify_apply_regime_from_dominant_citation_and_manual_wins(facade):
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((72, 80), "EUROPEAN DATA PROTECTION BOARD")
    pg.insert_text((72, 110), "Guidelines 07/2020 on controllers")
    y = 150
    for n in range(1, 7):
        pg.insert_text((72, y), f"{n}. Article 4 of Regulation (EU) 2016/679 applies.")
        y += 30
    r = facade.import_bytes(data=doc.tobytes(), filename="g.pdf", doc_type="guidance",
                            title="Guidelines 07/2020 on controllers")
    sid = r["stable_id"]
    with facade._open() as (cat, _rs, ts):
        res = facade._classify_guidance_into(cat, ts, sid)
        assert res["fields"]["regime"]["value"] == "32016R0679"
        assert res["fields"]["regime"]["rule"] == "dominant-citation"  # beats issuer default
        assert cat.get_alias("guidelines 07/2020") == sid
        assert any(e["relationship_type"] == "interprets" and e["dst_id"] == "32016R0679"
                   for e in cat.relations_for(sid))
    # a human's correction survives a re-classify (method 'manual' is never overwritten)
    facade.set_guidance_field(stable_id=sid, field="issuer", value="cnil")
    facade.reclassify_guidance()
    with facade._open() as (cat, _rs, _ts):
        g = cat.document_meta(sid)["guidance"]
        assert g["issuer"] == {"value": "cnil", "method": "manual", "rule": "user-edit",
                               "evidence": ""}
        assert g["number"]["method"] == "rule"   # rule fields refreshed as normal


def test_zotero_status_derives_library_id_from_key(facade):
    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class FakeHttp:
        def get(self, url, headers=None, params=None):
            if url.endswith("/keys/current"):
                return FakeResp({"userID": 4242, "username": "michael", "access": {}})
            return FakeResp([{"data": {"key": "COLL1", "name": "RAGlex intake",
                                       "parentCollection": False}}])

    facade.update_settings({"ZOTERO_API_KEY": "k-test"})
    st = facade.zotero_status(http=FakeHttp())
    assert st["connected"] and st["username"] == "michael"
    assert st["library_id"] == "4242"            # derived from the key, never typed
    assert st["collections"][0]["name"] == "RAGlex intake"
    # and it persisted, so the next call needs no re-derivation
    assert facade.settings.resolve("ZOTERO_LIBRARY_ID") == "4242"


def test_zotero_collection_scope_and_doctype_override(tmp_path):
    from raglex.core.models import DocType
    from raglex.imports.zotero import ZoteroImporter
    from raglex.storage import Catalogue, RawStore, TextStore

    cat = Catalogue(":memory:")
    rs, ts = RawStore(tmp_path / "raw"), TextStore(tmp_path / "text")
    seen: list[str] = []

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class FakeHttp:
        def get(self, url, headers=None, params=None):
            seen.append(url)
            return FakeResp([{"data": {"key": "GUID0001", "itemType": "report",
                                       "title": "Guidelines 05/2020 on consent",
                                       "abstractNote": "", "creators": [], "date": "2020"}}])

    importer = ZoteroImporter(FakeHttp(), "12345", "key", "users")
    ids = importer.import_into(cat, rs, ts, limit=10, collection="RAGX999",
                               doc_type=DocType.GUIDANCE)
    assert ids == ["zotero:GUID0001"]
    assert "/collections/RAGX999/items" in seen[0]  # scoped to the intake collection
    assert cat.get_document("zotero:GUID0001")["doc_type"] == "guidance"  # override wins
    cat.close()

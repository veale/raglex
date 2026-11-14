import json

import httpx
import pytest

from raglex.adapters.dk_domsdatabasen import DomsdatabasenAdapter
from raglex.adapters.registry import source_catalog, INCREMENTAL_MODE
from raglex.core.errors import FetchError
from raglex.core.models import DocType


def case(n, *, revoked=False):
    return {'id': str(n), 'headline': 'En dansk sag', 'officeName': 'Højesteret',
            'courtCaseNumber': f'BS-{n}/2026-HJR',
            'documents': [{'id': str(n * 2), 'displayTitle': 'Dom',
                           'documentType': {'enumValue': 1}, 'ecli': f'ECLI:DK:HJR:2026:{n}',
                           'verdictDateTime': '2026-11-06T11:00:00',
                           'contentHtml': '<p>Domstolens afgørelse om erstatning.</p>',
                           'revokedDateTime': '2026-11-13' if revoked else None},
                          {'id': str(n * 2 + 1), 'displayTitle': 'Domsresumé',
                           'documentType': {'enumValue': 4},
                           'contentHtml': '<p>Et kort resumé.</p>'}]}


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, json={'tokenString': 'test-token'}, request=httpx.Request(method, url))

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, json=self.pages.get(kwargs['params']['sideNr'], []),
                              request=httpx.Request('GET', url))


def adapter(pages, **kwargs):
    return DomsdatabasenAdapter(client=Client(pages), email='test@example.org', password='secret', **kwargs)


def test_all_documents_survive_short_pages_and_remain_distinct():
    a = adapter({1: [case(1)], 2: [case(2)]})
    stubs = list(a.discover(None))
    assert len(stubs) == 4
    records = [a.fetch(s) for s in stubs]
    assert records[0].doc_type == DocType.JUDGMENT
    assert records[1].doc_type == DocType.COMMENTARY
    assert records[0].stable_id != records[1].stable_id
    assert records[0].ecli in records[0].extra['aliases']
    assert not records[1].extra['aliases']
    assert 'secret' not in records[0].raw_bytes.decode()
    assert [s.hints['resume_offset'] for s in stubs] == [0, 0, 25, 25]


def test_resume_backs_off_a_case_page_and_does_not_count_siblings():
    a = adapter({2: [case(26)]}, start_offset='60')
    stubs = list(a.discover(None))
    assert len(stubs) == 2
    assert a._client.calls[1][1]['params']['sideNr'] == 2
    assert all(s.hints['resume_offset'] == 25 for s in stubs)


def test_change_feed_uses_inclusive_publication_date_not_judgment_date():
    a = adapter({1: [case(1)]})
    stubs = list(a.discover('2026-11-12T10:00:00'))
    url, kwargs = a._client.calls[1]
    assert url.endswith('/sagsaendringer')
    assert kwargs['params']['fraDato'] == '2026-11-12'
    assert stubs[0].hints['watermark'] >= '2026-11-12'


def test_error_envelopes_and_repeated_pages_raise():
    with pytest.raises(FetchError):
        list(adapter({1: {'Error': 'bad parameter'}}).discover(None))
    with pytest.raises(FetchError):
        list(adapter({1: [case(1)], 2: [case(1)]}).discover(None))


def test_withdrawal_never_returns_its_body():
    a = adapter({1: [case(1, revoked=True)]})
    stub = next(a.discover('2026-11-12'))
    assert stub.hints['upstream_withdrawn']
    with pytest.raises(FetchError) as err:
        a.fetch(stub)
    assert err.value.status_code == 410


def test_missing_credentials_fail_visibly(monkeypatch):
    monkeypatch.delenv('DOMSDATABASEN_EMAIL', raising=False)
    monkeypatch.delenv('DOMSDATABASEN_PASSWORD', raising=False)
    with pytest.raises(FetchError):
        list(DomsdatabasenAdapter(client=Client({})).discover(None))


def test_catalog_and_incremental_contract():
    row = next(r for r in source_catalog() if r['key'] == 'dk-domsdatabasen')
    assert row['jurisdiction'] == 'DK'
    assert INCREMENTAL_MODE['dk-domsdatabasen'] == 'server'


def test_independent_inventory_reports_missing_documents(monkeypatch):
    from raglex import danish_inventory
    from tests.test_mcp_lookup import _facade, _doc
    f = _facade()
    _doc(f, 'dk/domsdatabasen/1/2', 'Held judgment.', 'Held')
    with f._open() as (cat, _, _):
        cat.conn.execute("UPDATE documents SET source=?, meta_json=? WHERE stable_id=?",
                         ('dk-domsdatabasen', json.dumps({'case_id': '1'}), 'dk/domsdatabasen/1/2'))
        cat.conn.commit()
    class PublicClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, *a, **kw):
            return httpx.Response(200, json={'totalCount': 1, 'cases': [case(1)]})
    monkeypatch.setattr(danish_inventory, 'RateLimitedClient', PublicClient)
    progress = []
    result = danish_inventory.audit_inventory(f, on_progress=lambda **p: progress.append(p))
    assert result['public_cases'] == 1
    assert result['public_documents'] == 2
    assert result['missing_documents'] == ['dk/domsdatabasen/1/3']
    assert result['complete'] is False
    assert progress[0]['stage'] == 'reading independent Danish portal inventory'


def test_case_tombstone_withdraws_held_siblings_and_walk_continues():
    from tests.test_mcp_lookup import _facade, _doc
    from raglex.pipeline import Pipeline
    f = _facade()
    ids = ['dk/domsdatabasen/1/2', 'dk/domsdatabasen/1/3', 'dk/domsdatabasen/10/2']
    for ident in ids:
        _doc(f, ident, 'Held text.', 'Held')
    withdrawn = {'id': '1', 'revokedDateTime': '2026-11-13', 'documents': []}
    a = adapter({1: [withdrawn, case(2)]})
    stubs = list(a.discover(None))
    assert len(stubs) == 3 and stubs[0].hints['withdrawn_prefix'] == 'dk/domsdatabasen/1/'
    with f._open() as (cat, rs, ts):
        cat.conn.execute("UPDATE documents SET source='dk-domsdatabasen'")
        cat.conn.commit()
        stats = Pipeline(cat, rs, textstore=ts).run(a)
        assert cat.get_document(ids[0])['upstream_status'] == 'withdrawn'
        assert cat.get_document(ids[1])['upstream_status'] == 'withdrawn'
        assert cat.get_document(ids[2])['upstream_status'] != 'withdrawn'
        assert any('withdrawn case dk/domsdatabasen/1: 2' in n for n in stats.notes)
        assert cat.get_document('dk/domsdatabasen/2/4') is not None


def test_scanned_pdf_uses_danish_ocr_and_does_not_publish_truncated_scans(monkeypatch):
    import base64
    fitz = pytest.importorskip("fitz")
    from raglex.extraction import Extracted
    from raglex.extraction import ocr
    from raglex.adapters import dk_domsdatabasen as module
    pdf = fitz.open();pdf.new_page();data=pdf.tobytes();pdf.close()
    row = case(1);row['documents'] = [row['documents'][0]]
    row['documents'][0].update(contentHtml='',contentPdf=base64.b64encode(data).decode())
    monkeypatch.setattr(module,'extract_bytes',lambda *a,**k: Extracted(text='',needs_ocr=True,engine='pdf',engine_version='1'))
    calls=[]
    def recognize(data,**kwargs):
        calls.append(kwargs)
        return 'Domstolens begrundelse for afgørelsen. '*12
    monkeypatch.setattr(ocr,'ocr_pdf',recognize)
    a=adapter({1:[row]});record=a.fetch(next(a.discover(None)))
    assert calls == [{'language':'dan+eng','max_pages':80}]
    assert record.extra['extraction_engine']=='tesseract-dan'
    assert 'Domstolens' in record.text
    big=fitz.open()
    for _ in range(81):big.new_page()
    row['documents'][0]['contentPdf']=base64.b64encode(big.tobytes()).decode();big.close()
    a=adapter({1:[row]})
    with pytest.raises(FetchError,match='dedicated OCR required'):
        a.fetch(next(a.discover(None)))


def test_inventory_count_mismatch_remains_visible_at_advertised_last_page(monkeypatch):
    from raglex import danish_inventory
    from tests.test_mcp_lookup import _facade
    class PublicClient:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def request(self,*a,**kw):
            assert kw['params']['page']==1
            return httpx.Response(200,json={'totalCount':2,'pageCount':1,'cases':[case(1)]})
    monkeypatch.setattr(danish_inventory,'RateLimitedClient',PublicClient)
    result=danish_inventory.audit_inventory(_facade())
    assert result['public_cases']==1 and result['unaccounted_publisher_results']==1
    assert not result['complete'] and not result['inventory_complete']


def test_verified_backfill_enables_watch_only_after_inventory_reconciles(monkeypatch):
    from raglex import danish_inventory
    from raglex.jobs import _harvest_source
    class Facade:
        def __init__(self):self.created=False
        def harvest(self,**kwargs):
            assert 'verify_inventory' not in kwargs
            assert kwargs['force_full'] is True
            return {'stored':37,'errors':0}
        def list_watches(self):return []
        def create_watch(self,**kwargs):
            self.created=True
            assert kwargs['spec']['source']=='dk-domsdatabasen'
            return {'watch_id':1}
    f=Facade()
    monkeypatch.setattr(danish_inventory,'audit_inventory',lambda *a,**k:{'complete':False})
    result=_harvest_source(f,{'source':'dk-domsdatabasen','verify_inventory':True},lambda **p:None,lambda:False)
    assert 'error' in result and not f.created
    monkeypatch.setattr(danish_inventory,'audit_inventory',lambda *a,**k:{'complete':True})
    result=_harvest_source(f,{'source':'dk-domsdatabasen','verify_inventory':True},lambda **p:None,lambda:False)
    assert f.created and result['inventory']['complete']


def test_public_recovery_validates_identity_and_retains_pdf_fallback(monkeypatch):
    import base64
    class PublicClient:
        def get(self, url, **kwargs):
            if '/get/' in url:
                payload = case(1)
                return httpx.Response(200,json=payload,request=httpx.Request('GET',url))
            if '/download/' in url:
                return httpx.Response(200,content=b'%PDF-official-scan',request=httpx.Request('GET',url))
            return httpx.Response(200,json={'contentHtml':'<p>Official public body</p>'},
                                  request=httpx.Request('GET',url))
    a=DomsdatabasenAdapter(client=PublicClient(),case_ids='1',max_ocr_pages='500')
    stubs=list(a.discover(None))
    assert len(stubs)==2
    assert all(s.hints['watermark'] is None for s in stubs)
    assert base64.b64decode(stubs[0].hints['payload']['document']['contentPdf']).startswith(b'%PDF-')
    record=a.fetch(stubs[0])
    assert record.extra['format']=='domsdatabasen-public-recovery'
    assert record.text=='Official public body'
    assert a.max_ocr_pages==500
    with pytest.raises(FetchError,match='identity mismatch'):
        list(DomsdatabasenAdapter(client=PublicClient(),case_ids='2').discover(None))
    with pytest.raises(ValueError):DomsdatabasenAdapter(case_ids='../bad')
    with pytest.raises(ValueError):DomsdatabasenAdapter(max_ocr_pages=501)


def test_kapi_image_only_html_downloads_official_pdf_for_ocr(monkeypatch):
    fitz=pytest.importorskip('fitz')
    from raglex.adapters import dk_domsdatabasen as module
    from raglex.extraction import Extracted,ocr
    pdf=fitz.open();pdf.new_page();blob=pdf.tobytes();pdf.close()
    class FallbackClient(Client):
        def get(self,url,**kwargs):
            if '/document/download/content/' in url:
                return httpx.Response(200,content=blob,request=httpx.Request('GET',url))
            return super().get(url,**kwargs)
    row=case(1);row['documents']=row['documents'][:1]
    row['documents'][0]['contentHtml']='<html><body><img src="page.png"></body></html>'
    monkeypatch.setattr(module,'extract_bytes',lambda *a,**k:Extracted(text='',needs_ocr=True,engine='pdf',engine_version='1'))
    monkeypatch.setattr(ocr,'ocr_pdf',lambda *a,**k:'Domstolens begrundelse. '*20)
    a=DomsdatabasenAdapter(client=FallbackClient({1:[row]}),email='test',password='test')
    record=a.fetch(next(a.discover(None)))
    assert record.extra['extraction_engine']=='tesseract-dan'
    assert 'contentPdf' in json.loads(record.raw_bytes)['document']


def test_selected_case_recovery_automatically_audits_without_enabling_watch(monkeypatch):
    from raglex import danish_inventory
    from raglex.jobs import _harvest_source
    class Facade:
        def harvest(self,**kwargs):return {'stored':2,'errors':0}
        def list_watches(self):raise AssertionError('selected recovery must not enable watch')
    monkeypatch.setattr(danish_inventory,'audit_inventory',lambda *a,**k:{'complete':True})
    result=_harvest_source(Facade(),{'source':'dk-domsdatabasen','options':{'case_ids':'1'}},
                           lambda **p:None,lambda:False)
    assert result['inventory']['complete']

from raglex.core.models import Record, DocType, ExtractedVia
from raglex.storage import TextStore
from raglex.fulltext.index import build


def seed(cat, ts, sid, source='test'):
    text='Substantive text with an identifiable holding. '+sid
    rec=Record(source=source,stable_id=sid,title=sid,doc_type=DocType.JUDGMENT,
               text=text,raw_bytes=text.encode(),extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    cat.upsert_document(rec,text_path=str(ts.put(rec.payload_hash,text)))


def test_bounded_restart_advances_past_completed_documents(catalogue,tmp_path,monkeypatch):
    ts=TextStore(tmp_path/'text')
    for sid in ['a','b','c']:seed(catalogue,ts,sid)
    def forbidden(*args,**kwargs):raise AssertionError('corpus-wide index-state scan')
    monkeypatch.setattr(catalogue,'fts_indexed_ids',forbidden)
    monkeypatch.setattr(catalogue,'fts_body_indexed_ids',forbidden)
    events=[]
    first=build(catalogue,ts,sources=['test'],limit=2,on_progress=lambda **p:events.append(p))
    second=build(catalogue,ts,sources=['test'],limit=2)
    assert first['indexed']==2 and not first['complete']
    assert second['indexed']==1 and second['complete']
    assert events[0]['stage']=='selecting free-text index batch'
    assert 'total' not in events[0]


def test_heading_upgrade_does_not_rewrite_body_and_cancellation_is_visible(catalogue,tmp_path,monkeypatch):
    ts=TextStore(tmp_path/'text');seed(catalogue,ts,'a')
    catalogue.put_doc_fts('a','Already indexed authoritative body')
    catalogue.conn.execute('DELETE FROM doc_headings WHERE doc_id=?',('a',));catalogue.commit()
    monkeypatch.setattr(catalogue,'put_doc_fts',lambda *a,**k:(_ for _ in ()).throw(AssertionError('rewrote body')))
    upgraded=build(catalogue,ts,sources=['test'])
    assert upgraded['indexed']==1 and upgraded['headings_only']==1
    cancelled=build(catalogue,ts,sources=['test'],cancel_check=lambda:True)
    assert cancelled['cancelled'] and not cancelled['complete']


def test_single_failed_document_does_not_erase_other_committed_indexes(catalogue,tmp_path,monkeypatch):
    ts=TextStore(tmp_path/'text')
    for sid in ['a','b','c']:seed(catalogue,ts,sid)
    original=catalogue.put_doc_fts
    def fail_middle(sid,*args,**kwargs):
        if sid=='b':
            original(sid,*args,**kwargs)
            raise ValueError('bad document')
        return original(sid,*args,**kwargs)
    monkeypatch.setattr(catalogue,'put_doc_fts',fail_middle)
    result=build(catalogue,ts,sources=['test'])
    assert result['indexed']==2 and result['errors']==1 and not result['complete']
    assert catalogue.fts_body_indexed_ids()=={'a','c'}

import pytest
from raglex.case_admin import preview, run_action
from raglex.core.models import DocType, Record, TypedRelation, RelationshipType, ResolutionStatus
from tests.test_mcp_lookup import _facade, _doc

ID='ewhc/kb/2026/2776'
CITE='[2026] EWHC 2776 (KB)'


def test_preview_resolves_citation_to_exact_case_and_rejects_legislation():
    f=_facade();_doc(f,ID,'Original judgment.','A v B')
    match=preview(f,CITE)
    assert match['stable_id']==ID and match['can_purge'] and match['can_refetch']
    with f._open() as (cat,_,_ts):
        cat.upsert_document(Record(source='uk-legislation',stable_id='ukpga/2018/12',doc_type=DocType.LEGISLATION))
    assert 'error' in preview(f,'ukpga/2018/12')


def test_purge_removes_case_state_preserves_incoming_evidence_and_allows_reimport():
    f=_facade();_doc(f,ID,'Original judgment.','A v B');_doc(f,'other/1','A citing judgment.','Other')
    with f._open() as (cat,_,_ts):
        cat.put_alias('an old report citation',ID)
        cat.add_relation('other/1',TypedRelation(RelationshipType.MENTIONS,dst_id=ID,
                         raw_citation_string=CITE,resolution_status=ResolutionStatus.RESOLVED))
        cat.conn.execute('INSERT INTO document_versions (stable_id,version,archived_at) VALUES (?,1,?)',(ID,'2026-11-13'))
        cat.conn.execute('INSERT INTO doc_fts (doc_id,part,indexed_at) VALUES (?,0,?)',(ID,'2026-11-13'))
        cat.conn.execute('INSERT INTO doc_headings (doc_id,heading_no,label) VALUES (?,0,?)',(ID,'A v B'))
        cat.conn.commit()
    progress=[]
    result=run_action(f,citation=CITE,expected_id=ID,action='purge',on_progress=lambda **p:progress.append(p))
    assert result['purged'] and result['future_harvest_allowed']
    with f._open() as (cat,_,_ts):
        assert cat.get_document(ID) is None
        assert cat.get_alias('an old report citation') is None
        for table,col in [('document_versions','stable_id'),('doc_fts','doc_id'),('doc_headings','doc_id')]:
            assert cat.conn.execute(f'SELECT count(*) AS n FROM {table} WHERE {col}=?',(ID,)).fetchone()['n']==0
        edge=cat.relations_for('other/1')[0]
        assert edge['dst_id'] is None and edge['resolution_status']=='pending'
        assert edge['raw_citation_string']==CITE
    _doc(f,ID,'Republished judgment.','A v B')
    assert f.get_document(ID)['document']['stable_id']==ID
    assert progress[-1]['done']==1


def test_changed_match_is_never_purged():
    f=_facade();_doc(f,ID,'Original.','A v B')
    with pytest.raises(ValueError,match='different case'):
        run_action(f,citation=CITE,expected_id='wrong/id',action='purge')
    assert preview(f,CITE)['held']


def test_force_refetch_bypasses_held_dedup_and_refreshes_same_bytes_projection(monkeypatch):
    from raglex import facade as fm
    f=_facade();_doc(f,ID,'Old parsed text.','A v B')
    class Adapter:
        def discover(self,*a,**kw):yield object()
        def fetch(self,stub):
            return Record(source='uk-caselaw',stable_id=ID,doc_type=DocType.JUDGMENT,
                          title='Corrected title',raw_bytes=b'Old parsed text.',raw_ext='xml',
                          text='Freshly parsed publisher judgment.')
    monkeypatch.setitem(fm._TARGETED_HARVEST,'uk-caselaw',lambda _:Adapter())
    assert run_action(f,citation=CITE,expected_id=ID,action='refetch')['refetched']
    assert f.document_body(ID)['text']=='Freshly parsed publisher judgment.'


def test_failed_refetch_keeps_the_held_copy(monkeypatch):
    from raglex import facade as fm
    f=_facade();_doc(f,ID,'Original judgment.','A v B')
    class Adapter:
        def discover(self,*a,**kw):yield object()
        def fetch(self,stub):return None
    monkeypatch.setitem(fm._TARGETED_HARVEST,'uk-caselaw',lambda _:Adapter())
    with pytest.raises(ValueError,match='held copy was retained'):
        run_action(f,citation=CITE,expected_id=ID,action='refetch')
    assert f.document_body(ID)['text']=='Original judgment.'

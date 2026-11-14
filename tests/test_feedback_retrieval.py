"""Regressions reported through the live UI and MCP review queue."""
import pytest

from raglex.core.models import DocType, Record, Segment
from raglex.facade import _match_segment
from tests.test_mcp_lookup import _facade, _doc


@pytest.mark.parametrize('phrase', [
    'incomplete or inaccurate information',
    'Suppliers of substances and mixtures shall ensure',
    'does not prevent the competent supervisory authority',
    'section 4(2) and/or Article 8',
])
def test_literal_search_accepts_drafting_words(phrase):
    f = _facade()
    _doc(f, 'test/1', phrase, 'Test')
    for query in (phrase, f'"{phrase}"'):
        result = f.search_within_document('test/1', query)
        assert result['matched'] and result['total'] == 1


@pytest.mark.parametrize('label', ['36', '[36]', 'para 36', 'paragraph 36'])
def test_paragraph_pinpoints_match_para_labels(label):
    segs = [Segment('para 360', 0, 10), Segment('para 36', 10, 20)]
    assert _match_segment(segs, label) == 1


def test_unsegmented_body_does_not_claim_a_missing_article():
    f = _facade()
    _doc(f, 'test/1', 'An unsegmented landing page.', 'Test')
    result = f.get_provision('test/1', label='Article 34')
    assert result['error'] == 'no matching segment'
    assert 'segments' not in result


def test_unsegmented_offset_read_is_bounded():
    f = _facade()
    body = 'word ' * 20000
    _doc(f, 'test/1', body, 'Test')
    result = f.get_provision('test/1', char_start=50000)
    seg = result['segments'][0]
    assert seg['char_start'] <= 50000 < seg['char_end']
    assert len(seg['text']) <= 800


@pytest.mark.parametrize('segmented', [False, True])
def test_body_window_also_windows_line_metadata(segmented):
    f = _facade()
    text = '\n'.join(f'({n}) Provision text.' for n in range(1, 5000))
    with f._open() as (cat, _, ts):
        rec = Record(source='eu-legislation', stable_id='test/1',
                     doc_type=DocType.LEGISLATION, text=text, raw_bytes=text.encode())
        rec.ensure_payload_hash()
        segs = [Segment('Article 1', 0, len(text), kind='article')] if segmented else []
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))
        ts.put_segments(rec.payload_hash, segs)
    result = f.document_body('test/1', offset=10000, limit=1800)
    lines = result['segments'][0]['lines'] if segmented else result['lines']
    assert lines and len(lines) < 150
    assert all(line['end'] > 10000 and line['start'] < 11800 for line in lines)


def test_paragraph_range_from_mcp_list_returns_both_paragraphs():
    from raglex.mcp_server import _pinpoint_arg
    f = _facade()
    text = 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.'
    _doc(f, 'test/1', text, 'Test')
    with f._open() as (cat, _, ts):
        rec = cat.get_document('test/1')
        ts.put_segments(rec['payload_hash'], [Segment('para 44', 0, 16),
                        Segment('para 45', 18, 35), Segment('para 46', 37, len(text))])
    label = _pinpoint_arg(['para 44', 'para 45'])
    assert label == '[44]-[45]'
    result = f.get_provision('test/1', label=label, context=0)
    assert [s['label'] for s in result['segments']] == ['para 44', 'para 45']
    assert all(s['focus'] for s in result['segments'])


def test_eurlex_error_page_cannot_be_parsed_as_legislation():
    from raglex.formats.eurlex_html import parse_eurlex_html
    from raglex.core.errors import FetchError
    with pytest.raises(FetchError):
        parse_eurlex_html(b'<html><body>The requested document does not exist.</body></html>')


@pytest.mark.parametrize('html', [
    b'<html><div id="errorDocumentView">The request could not be processed, please try again later.</div></html>',
    b'<html><body>JavaScript is disabled. We need to verify that you are not a robot.</body></html>',
])
def test_eurlex_challenges_and_transient_error_pages_are_not_law(html):
    from raglex.formats.eurlex_html import parse_eurlex_html
    from raglex.adapters.eu_cellar import EUCellarAdapter
    from raglex.core.errors import FetchError
    with pytest.raises(FetchError):parse_eurlex_html(html)
    assert EUCellarAdapter._html_to_text(html) is None


def test_compact_citer_pages_bound_large_provision_lists_without_changing_cached_rows(monkeypatch):
    import json
    f=_facade()
    rows=[{'stable_id':f'case/{i}','date':'2026-11-13','court':'Court',
           'cites_provisions':['Article 35']*1000,'snippet':{'where':'para 1','text':'x'*320}}
          for i in range(75)]
    monkeypatch.setattr(f,'_resolve_held_id',lambda _:('32016R0679','32016R0679'))
    monkeypatch.setattr(f,'canonical_read_target',lambda _: {'stable_id':'32016R0679'})
    monkeypatch.setattr(f,'_citing_documents',lambda *a: {'total':190,'results':rows,'has_more':True})
    monkeypatch.setattr(f,'_cached',lambda key,ttl,build:build())
    result=f.citing_documents('GDPR',anchor='Article 35',limit=75,compact=True)
    assert result['total']==190 and result['has_more']
    assert len(result['results'])==75
    assert len(json.dumps(result))<25000
    assert 'cites_provisions' not in result['results'][0]
    assert len(rows[0]['cites_provisions'])==1000


def test_held_browser_challenge_is_not_served_as_a_cjeu_passage():
    f=_facade()
    text="JavaScript is disabled. We need to verify that you're not a robot."
    _doc(f,'ECLI:EU:C:1989:331',text,'A case')
    with f._open() as (cat,_,_ts):
        cat.conn.execute("UPDATE documents SET source='eu-cellar' WHERE stable_id=?",('ECLI:EU:C:1989:331',))
        cat.conn.commit()
    assert f.document_body('ECLI:EU:C:1989:331')['text'] is None
    assert 'body unavailable' in f.get_provision('ECLI:EU:C:1989:331',label='para 31')['error']


def test_explicit_index_build_does_not_wait_for_whole_corpus_coverage(monkeypatch):
    f=_facade()
    def expensive():raise AssertionError('coverage aggregate must not run to start this job')
    monkeypatch.setattr(f,'freetext_scope',expensive)
    result=f.build_freetext_index(sources=['uk-cpr'])
    assert result['scanned']==0


def test_flagged_document_rescan_resolves_only_reviewed_documents(monkeypatch):
    from tests.test_mcp_lookup import _facade, _doc
    from raglex.resolve import Resolver
    f = _facade()
    _doc(f, 'ewhc/kb/2026/2776', 'Applying Article 17 of Regulation (EU) 2016/679.', 'Held')
    with f._open() as (cat, _, _):
        cat.conn.execute("UPDATE documents SET has_text=1 WHERE stable_id=?", ('ewhc/kb/2026/2776',))
        cat.conn.commit()
    def global_resolve(*args, **kwargs):
        raise AssertionError('bounded repair attempted whole-corpus resolution')
    monkeypatch.setattr(Resolver, 'run', global_resolve)
    result = f.apply_rules(document_ids=['ewhc/kb/2026/2776'])
    assert result['documents'] == 1


def test_danish_source_grammar_rescan_does_not_resolve_whole_corpus(monkeypatch):
    from tests.test_mcp_lookup import _facade, _doc
    from raglex.resolve import Resolver
    f=_facade()
    _doc(f,'dk/domsdatabasen/1/2','forordning (EU) 2024/1689. Artikel 5, stk. 1.','Danish')
    with f._open() as (cat,_,__):
        cat.conn.execute("UPDATE documents SET source='dk-domsdatabasen', has_text=1 WHERE stable_id='dk/domsdatabasen/1/2'")
        cat.conn.commit()
    monkeypatch.setattr(Resolver,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('unbounded resolution')))
    result=f.apply_rules(source='dk-domsdatabasen')
    assert result['documents']==1 and result['citations']>=2


def test_resumed_source_scan_still_resolves_previously_extracted_documents(monkeypatch):
    from tests.test_mcp_lookup import _facade, _doc
    from raglex.resolve import Resolver, ResolveStats
    f = _facade()
    sid = 'dk/domsdatabasen/1/2'
    _doc(f, sid, 'forordning (EU) 2024/1689.', 'Danish')
    with f._open() as (cat, _, __):
        cat.conn.execute("UPDATE documents SET source='dk-domsdatabasen', has_text=1 WHERE stable_id=?", (sid,))
        cat.conn.commit()
    seen=[]
    monkeypatch.setattr(Resolver,'run_for_documents',lambda self,ids,**kwargs: (seen.extend(ids) or ResolveStats()))
    first=f.apply_rules(source='dk-domsdatabasen',run_id='resume-test')
    seen.clear()
    second=f.apply_rules(source='dk-domsdatabasen',run_id='resume-test')
    assert first['documents']==1 and second['documents']==0
    assert seen==[sid]

from tests.test_mcp_lookup import _facade


def test_explicit_takedown_stops_body_raw_pinpoint_and_literal_search_but_keeps_archive():
    f = _facade()
    sid = f.import_bytes(data=b'<p>Judgment secret operative text.</p>', filename='judgment.html',
                         doc_type='judgment')['stable_id']
    original = f.document_raw(sid)
    assert original is not None
    result = f.withdraw_document_publication(stable_id=sid, reason='Publisher takedown request, verified upstream 404')
    assert result['archive_retained']
    assert f.document_body(sid)['text'] is None
    assert f.document_raw(sid) is None
    assert 'withdrawn' in f.get_provision(sid, label='para 1')['error']
    assert 'withdrawn' in f.search_within_document(sid, 'operative text')['error']
    assert f.get_document(sid)['publication_withdrawn'] is True
    from pathlib import Path
    assert Path(original['path']).exists()
    with f._open() as (cat, _, ts):
        row = cat.get_document(sid)
        assert row['search_excluded'] == 1 and row['upstream_status'] == 'withdrawn'
        assert 'secret operative' in ts.get(row['payload_hash'])
        assert cat.conn.execute('SELECT count(*) AS n FROM doc_fts WHERE doc_id=?',(sid,)).fetchone()['n'] == 0
    # Re-importing the same source bytes cannot undo the explicit takedown.
    f.import_bytes(data=b'<p>Judgment secret operative text.</p>', filename='judgment.html',
                   doc_type='judgment')
    assert f.document_body(sid)['text'] is None
    assert f.document_raw(sid) is None

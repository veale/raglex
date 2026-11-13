from tests.test_mcp_lookup import _facade


def test_response_is_retained_when_issue_stays_open_then_resolves():
    f = _facade()
    fid = f.submit_feedback(kind='bug', message='Missing article', metadata={'about': 'lookup'})['feedback_id']
    f.resolve_feedback(feedback_id=fid, status='open', response='Parser fixed; stored-text repair pending.')
    row = f.list_feedback()[0]
    assert row['status'] == 'open'
    assert row['metadata']['about'] == 'lookup'
    assert row['metadata']['responses'][0]['message'].startswith('Parser fixed')
    f.resolve_feedback(feedback_id=fid, response='Verified the repaired article.')
    assert len(f.list_feedback(status='resolved')[0]['metadata']['responses']) == 2

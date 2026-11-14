from datetime import datetime, timezone
import httpx
import pytest
from raglex import acquisition_window as window
from raglex.acquisition_window import AcquisitionWindowClosed
from raglex.core.http import RateLimitedClient


@pytest.mark.parametrize('stamp,allowed',[
 ('2026-01-01T23:59:59+00:00',False),('2026-01-02T00:00:00+00:00',True),
 ('2026-01-02T04:59:59+00:00',True),('2026-01-02T05:00:00+00:00',False),
 ('2026-07-01T22:59:59+00:00',False),('2026-07-01T23:00:00+00:00',True),
 ('2026-07-02T03:59:59+00:00',True),('2026-07-02T04:00:00+00:00',False),
 # The spring gap and both autumn 02:30 occurrences stay inside the window.
 ('2026-03-29T00:30:00+00:00',True),('2026-03-29T01:30:00+00:00',True),
 ('2026-10-25T00:30:00+00:00',True),('2026-10-25T01:30:00+00:00',True),
])
def test_denmark_window_uses_local_time_and_dst(stamp,allowed):
 assert window.danish_window_open(datetime.fromisoformat(stamp)) is allowed


def test_all_publisher_routes_are_blocked_outside_window(monkeypatch):
 monkeypatch.setattr(window,'danish_window_open',lambda:False)
 calls=[]
 client=httpx.Client(transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(200)))
 with RateLimitedClient('any-source',client=client,min_interval=0) as c:
  for route in ['kapi/v2/autoriser','kapi/v2/sager','api/Case/advanced','api/Case/document/download/content/1']:
   with pytest.raises(AcquisitionWindowClosed):c.request('POST','https://domsdatabasen.dk/webapi/'+route)
  assert c.get('https://example.org/').status_code==200
 assert len(calls)==1


def test_retry_crossing_six_am_does_not_send_again(monkeypatch):
 allowed=[True];monkeypatch.setattr(window,'danish_window_open',lambda:allowed[0])
 calls=[]
 def transport(r):
  calls.append(r);allowed[0]=False
  return httpx.Response(503)
 client=httpx.Client(transport=httpx.MockTransport(transport))
 with RateLimitedClient('dk-domsdatabasen',client=client,min_interval=0,sleep=lambda n:None) as c:
  with pytest.raises(AcquisitionWindowClosed):c.get('https://domsdatabasen.dk/webapi/kapi/v2/sager')
 assert len(calls)==1


def test_queued_danish_job_waits_without_blocking_other_sources(monkeypatch):
 from tests.test_jobs import _facade
 from raglex.jobs import JobManager
 import threading
 monkeypatch.setattr(window,'danish_window_open',lambda:False)
 launched=[];monkeypatch.setattr(threading.Thread,'start',lambda self:launched.append(self))
 f=_facade();m=JobManager(f)
 job=m.start('harvest-source','Danish',{'source':'dk-domsdatabasen'})
 assert job['queued']
 assert m.promote_queued()==[]
 other=m.start('harvest-source','Other',{'source':'uk-cpr'})
 assert 'queued' not in other
 monkeypatch.setattr(window,'danish_window_open',lambda:True)
 assert m.promote_queued()==[job['job_id']]


def test_window_close_requeues_same_job_and_restores_checkpoint(monkeypatch):
 from tests.test_jobs import _facade
 from raglex.jobs import JobManager,RUNNERS
 monkeypatch.setattr(window,'danish_window_open',lambda:True)
 f=_facade();m=JobManager(f)
 def runner(f,p,progress,cancel):
  progress(stage='harvesting Danish',done=251,_checkpoint={'source':'dk-domsdatabasen','resume_offset':250})
  monkeypatch.setattr(window,'danish_window_open',lambda:False)
  raise AcquisitionWindowClosed('wait until 01:00 Europe/Copenhagen')
 monkeypatch.setitem(RUNNERS,'harvest-source',runner)
 with f._open() as (cat,_,__):cat.create_job('night','harvest-source','Danish',{'source':'dk-domsdatabasen'})
 m._worker('night','harvest-source',{'source':'dk-domsdatabasen'})
 import json
 with f._open() as (cat,_,__):
  row=cat.get_job('night');p=json.loads(row['params_json'])
  assert row['status']=='queued' and row['finished_at'] is None
  assert p['options']['start_offset']==250 and p['resume_unfinished']
 assert m.promote_queued()==[]


def test_full_acquisition_is_scheduled_once_per_danish_night(monkeypatch):
 from tests.test_jobs import _facade
 from raglex.jobs import JobManager
 from raglex.danish_acquisition import schedule_acquisition
 monkeypatch.setenv('DOMSDATABASEN_EMAIL','test@example.org')
 monkeypatch.setenv('DOMSDATABASEN_PASSWORD','not-a-real-password')
 monkeypatch.setattr(window,'danish_window_open',lambda:True)
 f=_facade();m=JobManager(f)
 night=datetime(2026,11,15,0,1,tzinfo=timezone.utc)
 day=datetime(2026,11,15,12,tzinfo=timezone.utc)
 assert schedule_acquisition(f,m,now=day)['waiting_for_window']
 first=schedule_acquisition(f,m,now=night)
 assert first['queued']
 second=schedule_acquisition(f,m,now=night)
 assert second['job_id']==first['job_id']
 import json
 with f._open() as (cat,_,__):
  p=json.loads(cat.get_job(first['job_id'])['params_json'])
 assert p['backfill'] and p['max_pages'] is None and p['verify_inventory']


def test_danish_watch_and_inventory_jobs_are_held_outside_window(monkeypatch):
 from tests.test_jobs import _facade
 from raglex.jobs import JobManager
 monkeypatch.setattr(window,'danish_window_open',lambda:False)
 f=_facade();m=JobManager(f)
 w=f.create_watch(name='Danish',spec={'source':'dk-domsdatabasen'},cadence_minutes=1440)
 assert m.start('run-watch','Danish watch',{'watch_id':w['watch_id']})['queued']
 assert m.start('audit-domsdatabasen','Danish audit',{})['queued']
 assert m.promote_queued()==[]


def test_fetch_window_exit_propagates_without_advancing_watermark():
 from tests.test_jobs import _facade
 from raglex.core.models import Stub
 from raglex.pipeline import Pipeline
 class Adapter:
  source='dk-domsdatabasen'
  def discover(self,*a,**k):yield Stub('dk/domsdatabasen/1/2',hints={'watermark':'2026-11-15'})
  def fetch(self,stub):raise AcquisitionWindowClosed('wait')
 f=_facade()
 with f._open() as (cat,raw,text):
  with pytest.raises(AcquisitionWindowClosed):Pipeline(cat,raw,textstore=text).run(Adapter())
  assert cat.get_watermark('dk-domsdatabasen') is None


def test_redirect_into_danish_publisher_cannot_bypass_window(monkeypatch):
 monkeypatch.setattr(window,'danish_window_open',lambda:False)
 calls=[]
 def transport(r):
  calls.append(r)
  return httpx.Response(302,headers={'Location':'https://domsdatabasen.dk/webapi/kapi/v2/sager'})
 client=httpx.Client(transport=httpx.MockTransport(transport),follow_redirects=True)
 with RateLimitedClient('any-source',client=client,min_interval=0) as c:
  with pytest.raises(AcquisitionWindowClosed):c.get('https://example.org/redirect')
 assert len(calls)==1

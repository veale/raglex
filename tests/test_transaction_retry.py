from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from raglex.storage._postgres import PgConnShim

class Retryable(Exception):sqlstate='40P01'
class Raw:
 def __init__(self):self.info=SimpleNamespace(transaction_status=0);self.values=[];self.rollbacks=0
 @contextmanager
 def transaction(self):
  old=list(self.values);self.info.transaction_status=2
  try:yield
  except Exception:
   self.values=old;self.rollbacks+=1;raise
  finally:self.info.transaction_status=0


def test_retry_replays_entire_transaction_after_rollback():
 raw=Raw();conn=PgConnShim(raw);attempts=[]
 def work():
  raw.values.append('first');attempts.append(1)
  if len(attempts)==1:raise Retryable('deadlock')
  raw.values.append('second');return 2
 assert conn.run_transaction(work,sleep=lambda _:None)==2
 assert raw.values==['first','second'] and raw.rollbacks==1


def test_nested_owner_and_non_retryable_failures_are_not_replayed():
 raw=Raw();conn=PgConnShim(raw);calls=[]
 def work():calls.append(1);raise Retryable()
 raw.info.transaction_status=2
 with pytest.raises(Retryable):conn.run_transaction(work,sleep=lambda _:None)
 assert len(calls)==1
 raw.info.transaction_status=0
 with pytest.raises(ValueError):conn.run_transaction(lambda:(_ for _ in ()).throw(ValueError('bad data')))


def test_retry_limit_does_not_claim_success():
 raw=Raw();conn=PgConnShim(raw);calls=[]
 def work():calls.append(1);raise Retryable()
 with pytest.raises(Retryable):conn.run_transaction(work,sleep=lambda _:None)
 assert len(calls)==3 and raw.rollbacks==3

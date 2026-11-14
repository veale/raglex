"""Optional integration test: only a unique temporary PostgreSQL schema is changed."""
import os
import pytest

@pytest.mark.skipif(not os.environ.get("RAGLEX_TEST_POSTGRES_DSN"), reason="requires PostgreSQL test DSN")
def test_watch_and_index_build_replace_one_document_atomically():
    import uuid, threading, time, concurrent.futures
    import psycopg
    from psycopg import sql
    from raglex.storage._postgres import PgConnShim
    from raglex.storage.catalogue import Catalogue
    dsn=os.environ["RAGLEX_TEST_POSTGRES_DSN"];schema='raglex_fts_race_'+uuid.uuid4().hex
    admin=psycopg.connect(dsn,autocommit=True);deleted=threading.Event()
    try:
     admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
     for statement in ["CREATE TABLE documents(stable_id text primary key)","INSERT INTO documents VALUES ('case/1')","CREATE TABLE doc_fts(doc_id text,part integer,char_start integer,char_end integer,words integer,tsv tsvector,indexed_at text,primary key(doc_id,part))","CREATE TABLE doc_headings(doc_id text,heading_no integer,label text,char_start integer,tsv tsvector,primary key(doc_id,heading_no))"]:
      admin.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)));admin.execute(statement)
     def worker(first):
      raw=psycopg.connect(dsn,autocommit=True);raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)));raw.execute("SET statement_timeout='10s'")
      class HookShim(PgConnShim):
       def execute(self,statement,params=None):
        result=super().execute(statement,params)
        if first and statement.startswith('DELETE FROM doc_fts'):
         deleted.set();time.sleep(.2)
        return result
      cat=Catalogue.__new__(Catalogue);cat.backend='postgres';cat.conn=HookShim(raw)
      try:
       if not first:assert deleted.wait(5)
       name='First' if first else 'Second'
       return cat.put_doc_fts('case/1',name+' authoritative body',headings=[(name+' heading',0)])
      finally:raw.close()
     with concurrent.futures.ThreadPoolExecutor(2) as pool:
      a=pool.submit(worker,True);b=pool.submit(worker,False);assert a.result()==1 and b.result()==1
     rows=admin.execute('SELECT doc_id,part,tsv::text FROM doc_fts').fetchall();headings=admin.execute('SELECT label FROM doc_headings').fetchall()
     assert len(rows)==1 and 'second' in rows[0][2] and headings==[('Second heading',)],(rows,headings)

    finally:
     admin.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)));admin.close()

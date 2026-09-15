import sqlite3
import pytest
from autobot import agent_market_queue as queue


def test_busy_first_open_releases_handle_and_next_request_keeps_one_job(tmp_path,monkeypatch):
    connect=sqlite3.connect
    attempts=[];closed=[]
    class Busy(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql=='PRAGMA journal_mode = WAL':
                error=sqlite3.OperationalError('database is locked');error.sqlite_errorcode=sqlite3.SQLITE_BUSY
                raise error
            return super().execute(sql,*args,**kwargs)
        def close(self):
            closed.append(True);return super().close()
    def open_database(*args,**kwargs):
        attempts.append(True)
        return connect(*args,**kwargs,**({'factory':Busy} if len(attempts)==1 else {}))
    monkeypatch.setattr(queue.sqlite3,'connect',open_database)
    path=tmp_path/'queue.sqlite3'
    jobs=[{'position_key':'one','name':'Concrete','job_mode':'web'}]
    queue.enqueue_jobs('tender',jobs,path=path)
    before=queue.list_jobs('tender',path=path)
    queue.enqueue_jobs('tender',jobs,path=path)
    assert len(before)==1 and queue.list_jobs('tender',path=path)[0]['id']==before[0]['id']
    assert closed==[True]
    with connect(path) as con:assert con.execute('PRAGMA journal_mode').fetchone()[0]=='wal'


def test_broken_database_is_not_retried_or_replaced(tmp_path,monkeypatch):
    path=tmp_path/'broken.sqlite3';original=b'not a sqlite database';path.write_bytes(original)
    monkeypatch.setattr(queue.time,'sleep',lambda *_:pytest.fail('Unexpected retry of corrupt data'))
    with pytest.raises(sqlite3.DatabaseError):queue._connect(path)
    assert path.read_bytes()==original


def test_permanently_busy_initialization_stops_at_the_deadline(tmp_path,monkeypatch):
    closed=[]
    class Busy:
        def execute(self,*args):
            error=sqlite3.OperationalError('database is locked');error.sqlite_errorcode=sqlite3.SQLITE_BUSY
            raise error
        def close(self):closed.append(True)
    monkeypatch.setattr(queue.sqlite3,'connect',lambda *a,**k:Busy())
    clock=iter((0,0,21))
    monkeypatch.setattr(queue.time,'monotonic',lambda:next(clock))
    monkeypatch.setattr(queue.time,'sleep',lambda *_:pytest.fail('Retry past deadline'))
    with pytest.raises(sqlite3.OperationalError):queue._connect(tmp_path/'locked.sqlite3')
    assert closed==[True]

"""Durable, resumable catalogue walks, separate from tender-position jobs."""
import time
import uuid

from autobot import supplier_catalog_store as store


def enqueue(source_id, *, page_budget=160, path=None):
    src=store.source(source_id,path)
    if not src or not src['enabled']:
        raise ValueError('Для этого источника ещё не настроен импорт каталога')
    budget=max(1,min(500,int(page_budget)))
    now=time.time()
    with store.connect(path,write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        active=con.execute("SELECT * FROM supplier_catalog_jobs WHERE source_id=? AND status IN ('queued','running','paused')",(source_id,)).fetchone()
        if active:
            if active['status']=='paused':
                con.execute("UPDATE supplier_catalog_jobs SET status='queued',page_budget=?,cancel_requested=0,error='',updated_at=? WHERE id=?",(budget,now,active['id']))
            return active['id']
        job_id=uuid.uuid4().hex
        con.execute('''INSERT INTO supplier_catalog_jobs
            (id,source_id,status,created_at,updated_at,page_budget) VALUES (?,?,'queued',?,?,?)''',
            (job_id,source_id,now,now,budget))
        con.execute('INSERT INTO supplier_catalog_frontier(job_id,url,kind,label) VALUES (?,?,?,?)',
                    (job_id,src['url'],'catalog',src['name']))
    return job_id


def claim(*, path=None, lease_seconds=90):
    now=time.time()
    with store.connect(path,write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE supplier_catalog_jobs SET status='canceled',owner='',updated_at=? WHERE status='running' AND cancel_requested=1 AND heartbeat_at<?",(now,now-lease_seconds))
        row=con.execute('''SELECT * FROM supplier_catalog_jobs WHERE cancel_requested=0 AND
            (status='queued' OR (status='running' AND heartbeat_at<?)) ORDER BY created_at LIMIT 1''',
            (now-lease_seconds,)).fetchone()
        if row is None:
            return None
        owner=uuid.uuid4().hex
        con.execute("UPDATE supplier_catalog_jobs SET status='running',owner=?,heartbeat_at=?,updated_at=? WHERE id=?",(owner,now,now,row['id']))
        con.execute("UPDATE supplier_catalog_frontier SET state='queued' WHERE job_id=? AND state='running'",(row['id'],))
    return dict(row,status='running',owner=owner)


def owns(job_id,owner,*,path=None):
    with store.connect(path) as con:
        return bool(con.execute("SELECT 1 FROM supplier_catalog_jobs WHERE id=? AND owner=? AND status='running' AND cancel_requested=0",(job_id,owner)).fetchone())


def next_page(job_id,owner,*,path=None):
    with store.connect(path,write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        current=con.execute("SELECT 1 FROM supplier_catalog_jobs WHERE id=? AND owner=? AND status='running' AND cancel_requested=0",(job_id,owner)).fetchone()
        if not current:
            return None
        row=con.execute("SELECT * FROM supplier_catalog_frontier WHERE job_id=? AND state='queued' ORDER BY CASE kind WHEN 'context' THEN 0 WHEN 'catalog' THEN 1 ELSE 2 END,url LIMIT 1",(job_id,)).fetchone()
        if row is None:
            return None
        con.execute("UPDATE supplier_catalog_frontier SET state='running',attempts=attempts+1 WHERE job_id=? AND url=?",(job_id,row['url']))
        con.execute('UPDATE supplier_catalog_jobs SET heartbeat_at=?,updated_at=? WHERE id=?',(time.time(),time.time(),job_id))
    return dict(row)


def complete_page(job_id,owner,page,links,*,error='',path=None):
    with store.connect(path,write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute("SELECT 1 FROM supplier_catalog_jobs WHERE id=? AND owner=? AND status='running' AND cancel_requested=0",(job_id,owner)).fetchone():
            return False
        con.execute('UPDATE supplier_catalog_frontier SET state=?,error=? WHERE job_id=? AND url=?',
                    ('error' if error else 'done',store.clean(error)[:700],job_id,page['url']))
        for link in links[:500]:
            con.execute('INSERT OR IGNORE INTO supplier_catalog_frontier(job_id,url,kind,label) VALUES (?,?,?,?)',
                        (job_id,link['url'],link['kind'],store.clean(link.get('label'))[:1600]))
        con.execute('UPDATE supplier_catalog_jobs SET heartbeat_at=?,updated_at=? WHERE id=?',(time.time(),time.time(),job_id))
    return True


def finish(job_id,owner,*,error='',path=None):
    with store.connect(path,write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        job=con.execute('SELECT * FROM supplier_catalog_jobs WHERE id=? AND owner=?',(job_id,owner)).fetchone()
        if not job:
            return
        states=dict(con.execute('SELECT state,count(*) FROM supplier_catalog_frontier WHERE job_id=? GROUP BY state',(job_id,)).fetchall())
        status='canceled' if job['cancel_requested'] else 'paused' if states.get('queued') or states.get('running') else 'partial' if states.get('error') else 'completed'
        if error and status=='completed': status='partial'
        con.execute("UPDATE supplier_catalog_frontier SET state='queued' WHERE job_id=? AND state='running'",(job_id,))
        con.execute('UPDATE supplier_catalog_jobs SET status=?,owner=\'\',updated_at=?,error=? WHERE id=?',
                    (status,time.time(),store.clean(error)[:700],job_id))


def cancel(job_id,*,path=None):
    with store.connect(path,write=True) as con:
        changed=con.execute("UPDATE supplier_catalog_jobs SET cancel_requested=1,status=CASE WHEN status IN ('queued','paused') THEN 'canceled' ELSE status END,updated_at=? WHERE id=? AND status IN ('queued','running','paused')",(time.time(),job_id)).rowcount
    return bool(changed)


def list_jobs(*,limit=20,path=None):
    if not store.ready(path): return []
    with store.connect(path) as con:
        rows=con.execute('''SELECT j.id,j.source_id,j.status,j.created_at,j.updated_at,j.page_budget,j.error,j.cancel_requested,
            s.name,count(f.url) AS discovered,
            sum(CASE WHEN f.state='done' THEN 1 ELSE 0 END) AS processed,
            sum(CASE WHEN f.state='error' THEN 1 ELSE 0 END) AS errors
            FROM supplier_catalog_jobs j JOIN supplier_catalog_sources s ON s.id=j.source_id
            LEFT JOIN supplier_catalog_frontier f ON f.job_id=j.id GROUP BY j.id ORDER BY j.created_at DESC LIMIT ?''',(max(1,min(50,limit)),)).fetchall()
    return [dict(row) for row in rows]

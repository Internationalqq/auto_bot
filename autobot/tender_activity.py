"""Last deliberate user action. Polling and worker updates never touch this DB."""
from contextlib import closing
import re
import sqlite3
import time
from autobot.paths import DATA_DIR

DB_PATH = DATA_DIR / 'tender_activity.sqlite3'
LABELS = {'open':'Открывали тендер', 'suppliers':'Работали с поставщиками',
          'analysis':'Запускали анализ', 'documents':'Работали с документами'}


def record(tid, action):
    if not re.fullmatch(r'\d{8,25}',tid) or action not in LABELS: raise ValueError('Некорректное действие')
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(DB_PATH,timeout=10)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS tender_activity (
            tender_id TEXT PRIMARY KEY, action TEXT NOT NULL, acted_at REAL NOT NULL)''')
        db.execute('INSERT OR REPLACE INTO tender_activity VALUES (?,?,?)',(tid,action,time.time()))


def listing():
    result = {}
    # Existing explicit search/send requests give old tenders a useful place
    # before their first visit after this release. Never use worker updated_at.
    old = DATA_DIR / 'buyer_outbox.sqlite3'
    if old.is_file():
        with closing(sqlite3.connect(old.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('buyer_campaigns','buyer_search_runs'):
                if table not in tables: continue
                for tid,stamp in db.execute('SELECT tender_id,MAX(created_at) FROM '+table+' GROUP BY tender_id'):
                    if stamp > result.get(tid,{}).get('acted_at',0): result[tid]={'action':'suppliers','acted_at':stamp}
    if DB_PATH.is_file():
        with closing(sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='tender_activity'").fetchone():
                for tid,action,stamp in db.execute('SELECT * FROM tender_activity'):
                    if action in LABELS and stamp > result.get(tid,{}).get('acted_at',0): result[tid]={'action':action,'acted_at':stamp}
    return {tid:row | {'label':LABELS[row['action']]} for tid,row in result.items()}

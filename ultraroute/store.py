import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.path = root / 'ultraroute.db'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'disconnected', created INTEGER NOT NULL,
                    last_error TEXT, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE, created INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    account_id TEXT PRIMARY KEY, browser_id TEXT NOT NULL,
                    browser_name TEXT NOT NULL, kind TEXT NOT NULL, command TEXT NOT NULL,
                    profile TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, provider TEXT, account_id TEXT,
                    status TEXT NOT NULL, duration REAL, created INTEGER NOT NULL
                );
            ''')
            # Browser state cannot be assumed live after restart.
            db.execute("UPDATE accounts SET status='disconnected'")
            db.execute("UPDATE accounts SET status='ready' WHERE id IN (SELECT account_id FROM browser_sessions WHERE approved=1)")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def accounts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM accounts ORDER BY created, id')]

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            return dict(row) if row else None

    def add_account(self, provider, name):
        aid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO accounts(id,provider,name,created) VALUES(?,?,?,?)',
                       (aid, provider, name, int(time.time())))
        return self.account(aid)

    def status(self, aid, status, error=None):
        with self.connect() as db:
            db.execute('UPDATE accounts SET status=?,last_error=? WHERE id=?', (status, error, aid))

    def remove(self, aid):
        with self.connect() as db:
            db.execute('DELETE FROM browser_sessions WHERE account_id=?', (aid,))
            db.execute('DELETE FROM accounts WHERE id=?', (aid,))

    def keys(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,prefix,created FROM api_keys ORDER BY created DESC')]

    def create_key(self, name):
        token = 'ur_' + secrets.token_urlsafe(32)
        kid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO api_keys VALUES(?,?,?,?,?)',
                       (kid, name, token[:10], hashlib.sha256(token.encode()).hexdigest(), int(time.time())))
        return {'id': kid, 'key': token, 'name': name}

    def valid_key(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            return db.execute('SELECT 1 FROM api_keys WHERE digest=?', (digest,)).fetchone() is not None

    def revoke(self, kid):
        with self.connect() as db:
            db.execute('DELETE FROM api_keys WHERE id=?', (kid,))

    def record(self, provider, aid, status, duration):
        with self.connect() as db:
            db.execute('INSERT INTO requests(provider,account_id,status,duration,created) VALUES(?,?,?,?,?)',
                       (provider, aid, status, duration, int(time.time())))
            db.execute('DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 500)')

    def stats(self):
        with self.connect() as db:
            return dict(db.execute("SELECT count(*) total, COALESCE(sum(status='ok'),0) successful, COALESCE(avg(duration),0) avg_seconds FROM requests").fetchone())

    def browser_session(self, aid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM browser_sessions WHERE account_id=?', (aid,)).fetchone()
            return dict(row) if row else None

    def save_browser(self, aid, browser, profile):
        import json
        with self.connect() as db:
            db.execute("""INSERT INTO browser_sessions(account_id,browser_id,browser_name,kind,command,profile)
                VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
                browser_id=excluded.browser_id,browser_name=excluded.browser_name,
                kind=excluded.kind,command=excluded.command,profile=excluded.profile""",
                (aid, browser.id, browser.name, browser.kind, json.dumps(browser.command), str(profile)))

    def approve_browser(self, aid, approved=True):
        with self.connect() as db:
            db.execute('UPDATE browser_sessions SET approved=? WHERE account_id=?', (int(approved), aid))

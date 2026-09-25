from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class BurstStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS pending(scope TEXT NOT NULL, ref TEXT NOT NULL, bot_scope TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,ref))")
        return db

    def save(self, scope, ref, bot_scope, data):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("DELETE FROM pending WHERE scope=? AND ref=?", [(scope, str(value)) for value in data.get("refs", []) if str(value) != str(ref)])
            db.execute("INSERT INTO pending VALUES (?,?,?,?,?) ON CONFLICT(scope,ref) DO UPDATE SET data=excluded.data",
                       (scope, ref, bot_scope, json.dumps(data, ensure_ascii=False, default=str), time.time()))

    def remove(self, scope, refs):
        with closing(self._connect()) as db, db:
            db.executemany("DELETE FROM pending WHERE scope=? AND ref=?", [(scope, str(ref)) for ref in refs])

    def recover(self, bot_scope, before):
        if not self.path.exists():
            return []
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM pending WHERE bot_scope=? AND created<? ORDER BY created", (bot_scope, before))]

    def clear_scope(self, scope):
        if not self.path.exists():
            return
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM pending WHERE scope=?", (scope,))

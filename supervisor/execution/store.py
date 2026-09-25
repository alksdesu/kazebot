from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class ConflictError(ValueError):
    pass


class PlanStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
                    status TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, plan_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    plan_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (plan_id, revision)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, plan: dict, request_key: str = "") -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                prior = db.execute("SELECT plan_id FROM requests WHERE key=?", (request_key,)).fetchone()
                if prior:
                    return json.loads(db.execute("SELECT body FROM plans WHERE id=?", (prior[0],)).fetchone()[0])
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?)", (
                plan["id"], plan["scope"], plan["owner"], plan["status"], json.dumps(plan, ensure_ascii=False),
            ))
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (plan["id"], plan["revision"], json.dumps(plan, ensure_ascii=False)))
            if request_key:
                db.execute("INSERT INTO requests VALUES (?,?)", (request_key, plan["id"]))
        return plan

    def get(self, plan_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT body FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError("计划不存在")
        return json.loads(row[0])

    def list(self, scope: str = "", active: bool = False, *, owner: str = "", bot_scope: str = "", notifications: bool = False) -> list[dict]:
        query, args, filters = "SELECT body FROM plans", [], []
        if scope:
            filters.append("scope=?")
            args.append(scope)
        if owner:
            filters.append("owner=?")
            args.append(owner)
        if bot_scope:
            filters.append("json_extract(body, '$.bot_scope')=?")
            args.append(bot_scope)
        if active:
            filters.append("status IN ('queued','running','cancelling')")
        if notifications:
            filters.append("json_extract(body, '$.notification_status')='pending'")
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY rowid DESC"
        if not active and not notifications:
            query += " LIMIT 500"
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(query, args)]

    def mutate(self, plan_id: str, change: Callable[[dict], Any], *, archive: bool = False) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError("计划不存在")
            plan = json.loads(row[0])
            change(plan)
            plan["version"] += 1
            body = json.dumps(plan, ensure_ascii=False)
            db.execute("UPDATE plans SET status=?,body=? WHERE id=?", (plan["status"], body, plan_id))
            if archive:
                db.execute("INSERT INTO revisions VALUES (?,?,?)", (plan_id, plan["revision"], body))
        return plan

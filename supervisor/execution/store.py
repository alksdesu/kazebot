from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from engine.conversation_migration import (
    ConversationRekey,
    RekeyCount,
    rekey_sqlite_documents,
    rekey_sqlite_table,
    sqlite_rekey_transaction,
)


class ConflictError(ValueError):
    pass


def plans_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / "data/execution/execution.sqlite3"


def rekey_plans(path: Path, rekey: ConversationRekey, *, apply: bool) -> RekeyCount:
    """执行计划跟着会话走：在新会话里接着执行，执行身份和任务上下文里的会话键、渠道、账号一起换，历史版本同样。"""
    if not rekey or not Path(path).exists():
        return RekeyCount()
    with sqlite_rekey_transaction(Path(path), apply=apply) as db:
        return (
            rekey_sqlite_table(db, "plans", rekey, document_column="body", rewrite_document=_rekey_plan_body)
            + rekey_sqlite_documents(db, "revisions", rekey, document_column="body", rewrite_document=_rekey_plan_body)
        )


def _rekey_plan_body(raw: str, rekey: ConversationRekey, old: str, new: str) -> str:
    plan = json.loads(raw)
    rekey.rewrite(plan, old, new, keys=("scope",), channels=("channel",), bot_scopes=("bot_scope",))
    binding = plan.get("actor_binding")
    if isinstance(binding, dict):
        # 到点执行时按它重建执行身份，漏换的话身份还挂在旧会话上，群管理这类角色判定就对不上。
        rekey.rewrite(binding, old, new, keys=("scope",), channels=("channel",), bot_scopes=("bot_scope",))
    context = plan.get("task_context")
    if isinstance(context, dict):
        rekey.rewrite(
            context, old, new,
            keys=("conversation_key", "route_conversation_key", "parent_conversation_key"), channels=("channel",),
        )
        auth = context.get("platform_auth")
        if isinstance(auth, dict):
            rekey.rewrite(auth, old, new, keys=("group_scope",), channels=("platform",), bot_scopes=("bot_scope",))
    return json.dumps(plan, ensure_ascii=False)


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

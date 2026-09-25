from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from supervisor.feature_auth import FeatureActor

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReminderConflict(ValueError):
    pass


class ReminderService:
    def __init__(self, state: Any):
        self.state = state
        self.path = Path(state.workspace_root) / "data/reminders/reminders.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config_path = Path(state.workspace_root) / "config/reminders.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        self.max_open = max(1, min(int((config or {}).get("max_open_per_owner", 50)), 500))
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
                    request_key TEXT UNIQUE, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminder_actions (
                    key TEXT PRIMARY KEY, reminder_id TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminder_message_bindings (
                    scope TEXT NOT NULL, bot_scope TEXT NOT NULL, message_id TEXT NOT NULL,
                    reminder_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    delivery_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, bot_scope, message_id)
                );
            """)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reminders", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    stop = close

    def _loop(self) -> None:
        while not self._stop.wait(1):
            try:
                self.tick()
            except Exception:
                log.exception("reminder tick failed")

    def list(self, actor: FeatureActor) -> list[dict]:
        filters, args = [], []
        if actor.scope:
            filters.append("scope=?")
            args.append(actor.scope)
        if not actor.is_admin:
            filters.extend(("owner=?", "json_extract(body, '$.bot_scope')=?"))
            args.extend((actor.owner, actor.bot_scope))
        query = "SELECT body FROM reminders"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY (json_extract(body, '$.status')='open') DESC, rowid DESC LIMIT 500"
        with self.connection() as db:
            rows = db.execute(query, args).fetchall()
        records = [json.loads(row[0]) for row in rows]
        return [record for record in records if (not actor.scope or record["scope"] == actor.scope) and (
            actor.is_admin or record["owner"] == actor.owner and record["bot_scope"] == actor.bot_scope and record["scope"] == actor.scope
        )]

    def get(self, actor: FeatureActor, reminder_id: str) -> dict:
        with self.connection() as db:
            record = self._load(db, reminder_id)
        actor.require_owner(record["owner"], record["scope"])
        if not actor.is_admin and actor.bot_scope != record["bot_scope"]:
            raise PermissionError("此提醒属于其他机器人")
        return record

    @staticmethod
    def _load(db, reminder_id: str) -> dict:
        row = db.execute("SELECT body FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if row is None:
            raise KeyError("提醒不存在")
        return json.loads(row[0])

    @staticmethod
    def _save(db, record: dict) -> None:
        db.execute("UPDATE reminders SET body=? WHERE id=?", (json.dumps(record, ensure_ascii=False), record["id"]))

    def create(self, actor: FeatureActor, text: str, due_at: str, timezone_name: str = "Asia/Shanghai") -> dict:
        if not actor.scope or not text.strip() or len(text) > 4000:
            raise ValueError("请选择会话并填写不超过 4000 字的提醒内容")
        try:
            zone = ZoneInfo(timezone_name)
        except KeyError as exc:
            raise ValueError("无效时区，请使用 Asia/Shanghai 等时区名称") from exc
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=zone)
        due = due.astimezone(timezone.utc)
        if due <= utcnow() or due > utcnow() + timedelta(days=366 * 5):
            raise ValueError("提醒时间必须在未来五年内")
        record = {
            "id": "R" + uuid.uuid4().hex[:12], "scope": actor.scope, "owner": actor.owner,
            "bot_scope": actor.bot_scope, "channel": actor.channel, "text": text.strip(),
            "owner_label": f"QQ {actor.user_id}" if actor.user_id else "控制台管理员" if actor.is_admin else actor.owner,
            "timezone": timezone_name, "due_at": due.isoformat(), "revision": 1,
            "status": "open", "delivery_status": "scheduled", "delivery_id": "",
            "created_at": utcnow().isoformat(), "history": [],
        }
        content_hash = hashlib.sha256(f"{record['text']}:{record['due_at']}".encode()).hexdigest()
        key = f"{actor.bot_scope}:{actor.scope}:{actor.owner}:{actor.message_id}:{'' if actor.interactive else content_hash}" if actor.message_id else None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                previous = db.execute("SELECT body FROM reminders WHERE request_key=?", (key,)).fetchone()
                if previous:
                    return json.loads(previous[0])
            count = db.execute("SELECT COUNT(*) FROM reminders WHERE owner=? AND json_extract(body, '$.bot_scope')=? AND json_extract(body, '$.status')='open'", (actor.owner, actor.bot_scope)).fetchone()[0]
            if not actor.is_admin and count >= self.max_open:
                raise ValueError(f"最多同时保留 {self.max_open} 条未完成提醒")
            db.execute("INSERT INTO reminders VALUES (?,?,?,?,?)", (record["id"], actor.scope, actor.owner, key, json.dumps(record, ensure_ascii=False)))
        return record

    def act(self, actor: FeatureActor, reminder_id: str, action: str, revision: int, *, minutes: int = 10, action_id: str = "") -> dict:
        self.get(actor, reminder_id)
        if action not in {"complete", "cancel", "snooze"}:
            raise ValueError("不支持的提醒操作")
        if action == "snooze" and not 1 <= minutes <= 525600:
            raise ValueError("延后时长应在 1 分钟到一年之间")
        key = f"{actor.owner}:{actor.message_id or action_id}:{reminder_id}:{action}" if actor.message_id or action_id else ""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                prior = db.execute("SELECT result FROM reminder_actions WHERE key=?", (key,)).fetchone()
                if prior:
                    return json.loads(prior[0])
            record = self._load(db, reminder_id)
            if record["revision"] != revision:
                raise ReminderConflict("提醒已更新，请使用最新卡片")
            if record["status"] != "open":
                if (action == "complete" and record["status"] == "completed") or (action == "cancel" and record["status"] == "cancelled"):
                    return record
                raise ReminderConflict("该提醒已经结束")
            record["history"].append({"action": action, "actor": actor.owner, "at": utcnow().isoformat(), "revision": revision})
            record["revision"] += 1
            if action == "snooze":
                record.update(due_at=(utcnow() + timedelta(minutes=minutes)).isoformat(), delivery_status="scheduled", delivery_id="")
            else:
                record["status"] = "completed" if action == "complete" else "cancelled"
            self._save(db, record)
            if key:
                db.execute("INSERT INTO reminder_actions VALUES (?,?,?)", (key, reminder_id, json.dumps(record, ensure_ascii=False)))
        return record

    def delivery_status(self, delivery_id: str) -> dict:
        parts = delivery_id.split(":")
        if len(parts) != 3 or parts[0] != "reminder":
            return {"deliver": False, "reason": "unknown_delivery"}
        with self.connection() as db:
            try:
                record = self._load(db, parts[1])
            except KeyError:
                return {"deliver": False, "reason": "missing"}
        current = record["status"] == "open" and str(record["revision"]) == parts[2] and record["delivery_id"] == delivery_id
        reason = "superseded" if not current else "already_delivered" if record["delivery_status"] == "delivered" else "outcome_unknown" if record["delivery_status"] == "outcome_unknown" else "current"
        return {"deliver": reason == "current", "reminder_id": record["id"], "scope": record["scope"], "revision": record["revision"], "reason": reason}

    def receipt(self, delivery_id: str, status: str, *, message_ids: list[str] | None = None) -> dict:
        if status not in {"delivered", "failed", "outcome_unknown"}:
            raise ValueError("不支持的投递状态")
        identity = self.delivery_status(delivery_id)
        if not identity["deliver"] and identity.get("reason") != "already_delivered" and not (identity.get("reason") == "outcome_unknown" and status == "delivered"):
            return {"accepted": False, "reason": "superseded"}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, identity["reminder_id"])
            if record["status"] != "open" or record["delivery_id"] != delivery_id:
                return {"accepted": False, "reason": "superseded"}
            if record["delivery_status"] != "delivered":
                record.update(delivery_status=status, delivered_at=utcnow().isoformat() if status == "delivered" else "", platform_message_ids=message_ids or [])
                self._save(db, record)
        return {"accepted": True, "reminder": record}

    def bind_delivery_message(self, delivery_id: str, scope: str, bot_scope: str, message_id: str) -> dict:
        parts = delivery_id.split(":")
        if len(parts) != 3 or parts[0] != "reminder" or not parts[2].isdigit():
            raise ValueError("无效提醒投递编号")
        if not scope or not bot_scope or not message_id or message_id.startswith(("idempotent:", "uploaded:")):
            raise ValueError("需要真实平台消息编号及投递会话")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, parts[1])
            if record["scope"] != scope or (record["channel"].startswith("qq_") and record["bot_scope"] != bot_scope):
                raise PermissionError("提醒投递目标不匹配")
            if record["status"] != "open" or record["delivery_id"] != delivery_id or record["revision"] != int(parts[2]):
                return {"accepted": False, "reason": "superseded"}
            prior = db.execute("SELECT reminder_id,revision,delivery_id FROM reminder_message_bindings WHERE scope=? AND bot_scope=? AND message_id=?", (scope, bot_scope, message_id)).fetchone()
            if prior:
                if tuple(prior) != (record["id"], record["revision"], delivery_id):
                    raise ReminderConflict("平台消息已绑定到其他提醒，不能覆盖")
                return {"accepted": True}
            db.execute("INSERT INTO reminder_message_bindings VALUES (?,?,?,?,?,?,?)", (scope, bot_scope, message_id, record["id"], record["revision"], delivery_id, utcnow().isoformat()))
        return {"accepted": True}

    def reply_context(self, actor: FeatureActor, message_id: str) -> dict:
        actor.require_interaction()
        if not actor.scope or not actor.bot_scope or not message_id:
            return {"matched": False}
        with self.connection() as db:
            row = db.execute("SELECT reminder_id,revision FROM reminder_message_bindings WHERE scope=? AND bot_scope=? AND message_id=?", (actor.scope, actor.bot_scope, message_id)).fetchone()
            if row is None:
                return {"matched": False}
            record = self._load(db, row["reminder_id"])
        if record["owner"] != actor.owner:
            return {"matched": True, "authorized": False}
        return {"matched": True, "authorized": True, "reminder_id": record["id"], "revision": row["revision"], "current": record["status"] == "open" and record["revision"] == row["revision"]}

    def act_on_reply(self, actor: FeatureActor, message_id: str, action: str, *, minutes: int = 10) -> dict:
        context = self.reply_context(actor, message_id)
        if not context["matched"]:
            raise ValueError("请引用机器人发出的真实提醒消息，不能猜测提醒对象")
        if not context.get("authorized"):
            raise PermissionError("只能通过引用操作本人创建的提醒")
        return self.act(actor, context["reminder_id"], action, context["revision"], minutes=minutes)

    def tick(self, now: datetime | None = None) -> None:
        current = now or utcnow()
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            with self.connection() as db:
                records = [json.loads(row[0]) for row in db.execute("SELECT body FROM reminders")]
            for initial in records:
                if initial["status"] != "open" or initial["delivery_status"] not in {"scheduled", "sending"} or datetime.fromisoformat(initial["due_at"]) > current:
                    continue
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    record = self._load(db, initial["id"])
                    if record["revision"] != initial["revision"] or record["status"] != "open":
                        continue
                    if record["delivery_status"] == "sending" and record.get("lease_until") and datetime.fromisoformat(record["lease_until"]) > current:
                        continue
                    if record["delivery_status"] not in {"scheduled", "sending"}:
                        continue
                    delivery_id = f"reminder:{record['id']}:{record['revision']}"
                    record.update(delivery_id=delivery_id, delivery_status="sending", lease_until=(current + timedelta(seconds=30)).isoformat())
                    self._save(db, record)
                session_id = self.state.get_or_create_session(channel=record["channel"], conversation_key=record["scope"])
                if not self.state.eventlog.find_delivery_event(delivery_id, event_type="outbound_message"):
                    text = f"提醒 {record['id']}（{record.get('owner_label', '创建者本人')}）：{record['text']}\n引用此消息回复“已完成”“十分钟后”或“取消提醒”。\n也可使用 /提醒完成 {record['id']} 或 /提醒延后 {record['id']} 10分钟。"
                    event = self.state.eventlog.append(session_id=session_id, component="reminders", type_="outbound_message", payload={
                        "text": text, "attachments": [], "conversation_key": record["scope"],
                        "delivery_id": delivery_id, "reminder_id": record["id"], "reminder_revision": record["revision"],
                    })
                    self.state.record_outbound_message_event(event)
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    latest = self._load(db, record["id"])
                    if latest["revision"] == record["revision"] and latest["delivery_status"] == "sending":
                        latest["delivery_status"] = "queued"
                        self._save(db, latest)
        finally:
            self._tick_lock.release()

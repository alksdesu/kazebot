from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS = {
    "response_policy_enabled": False,
    "topic_enabled": False,
    "merge_window_sec": 0.0,
    "merge_max_wait_sec": 4.0,
    "reply_budget_per_minute": 6,
    "welcome_enabled": False,
}


class CommunityService:
    def __init__(self, workspace_root: Path):
        self.path = Path(workspace_root) / "data" / "community.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    scope TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}',
                    guide TEXT NOT NULL DEFAULT '{}', quiet TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activities_scope ON activities(scope, updated);
                CREATE TABLE IF NOT EXISTS operations (
                    scope TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(scope, key)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, activity_id TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL, due REAL NOT NULL, expires REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', message_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    scope TEXT NOT NULL, ref TEXT NOT NULL, owner TEXT NOT NULL, label TEXT NOT NULL,
                    text TEXT NOT NULL, reply_ref TEXT NOT NULL, topic TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(scope, ref)
                );
                CREATE INDEX IF NOT EXISTS messages_topic ON messages(scope, topic, created);
                CREATE TABLE IF NOT EXISTS decisions (
                    scope TEXT NOT NULL, ref TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(scope, ref)
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _db(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=15.0)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _scope(actor) -> str:
        scope = str(actor.scope or "").strip()
        if not scope:
            raise ValueError("请先选择一个群或会话")
        actor.require_scope(scope)
        return scope

    @staticmethod
    def _dump(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _text(value, limit: int = 4000) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            raise ValueError(f"内容不能超过 {limit} 字")
        return text

    def state(self, actor) -> dict[str, Any]:
        scope = self._scope(actor)
        with self._db() as db:
            row = db.execute("SELECT * FROM groups WHERE scope=?", (scope,)).fetchone()
        settings = dict(DEFAULT_SETTINGS)
        guide, quiet, revision = {}, {}, 0
        if row:
            settings.update(json.loads(row["settings"]))
            guide, quiet, revision = json.loads(row["guide"]), json.loads(row["quiet"]), row["revision"]
        if quiet and float(quiet.get("expires_at", 0)) <= time.time():
            quiet = {}
        return {"scope": scope, "settings": settings, "guide": guide, "quiet": quiet, "revision": revision}

    def clear_context(self, scope: str) -> None:
        with self._db(True) as db:
            db.execute("DELETE FROM messages WHERE scope=?", (scope,))
            db.execute("DELETE FROM decisions WHERE scope=?", (scope,))

    def update_settings(self, actor, changes: dict) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        parsed = {}
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"未知对话设置：{key}")
            if isinstance(DEFAULT_SETTINGS[key], bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} 必须是开关")
                parsed[key] = value
            elif key == "reply_budget_per_minute":
                if type(value) is not int or not 1 <= value <= 60:
                    raise ValueError("每分钟回复预算应为 1～60")
                parsed[key] = int(value)
            else:
                number = float(value)
                if not 0 <= number <= 10:
                    raise ValueError("合并等待时间应为 0～10 秒")
                parsed[key] = number
        with self._db(True) as db:
            db.execute("INSERT OR IGNORE INTO groups(scope) VALUES (?)", (scope,))
            current = json.loads(db.execute("SELECT settings FROM groups WHERE scope=?", (scope,)).fetchone()[0])
            current.update(parsed)
            if current.get("merge_max_wait_sec", 4) < current.get("merge_window_sec", 0):
                raise ValueError("最长等待时间不能小于短句等待窗口")
            db.execute("UPDATE groups SET settings=?, revision=revision+1 WHERE scope=?", (self._dump(current), scope))
        return self.state(actor)

    def update_guide(self, actor, body: dict) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        guide = {
            "rules": self._text(body.get("rules"), 12000),
            "resources": self._text(body.get("resources"), 12000),
            "faq": self._text(body.get("faq"), 12000),
            "welcome": self._text(body.get("welcome"), 1000),
            "updated_at": time.time(), "updated_by": actor.owner,
        }
        with self._db(True) as db:
            db.execute("INSERT OR IGNORE INTO groups(scope) VALUES (?)", (scope,))
            db.execute("UPDATE groups SET guide=?,revision=revision+1 WHERE scope=?", (self._dump(guide), scope))
        return self.state(actor)

    def set_quiet(self, actor, mode: str, duration_sec: float = 1800) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        if mode not in {"off", "listen", "silent"}:
            raise ValueError("安静模式必须是 off、listen 或 silent")
        duration = float(duration_sec)
        if mode != "off" and not 1 <= duration <= 604800:
            raise ValueError("安静时长应在 1 秒到 7 天之间")
        quiet = {} if mode == "off" else {
            "mode": mode, "expires_at": time.time() + duration,
            "changed_by": actor.owner, "created_at": time.time(),
        }
        with self._db(True) as db:
            db.execute("INSERT OR IGNORE INTO groups(scope) VALUES (?)", (scope,))
            db.execute("UPDATE groups SET quiet=?,revision=revision+1 WHERE scope=?", (self._dump(quiet), scope))
        return self.state(actor)

    def activities(self, actor) -> list[dict]:
        scope = self._scope(actor)
        with self._db() as db:
            rows = db.execute("SELECT data FROM activities WHERE scope=? ORDER BY updated DESC LIMIT 100", (scope,))
            return [self._public_activity(json.loads(row[0]), actor.owner) for row in rows]

    @staticmethod
    def _public_activity(data: dict, owner: str) -> dict:
        result = {key: value for key, value in data.items() if key not in {"votes", "participants"}}
        result["counts"] = [sum(index in values for values in data.get("votes", {}).values()) for index in range(len(data.get("options", [])))]
        participants = list(data.get("participants", {}).values())
        result["participants"] = [{"name": item["name"], "status": item["status"]} for item in participants]
        result["mine"] = data.get("participants", {}).get(owner)
        result["my_vote"] = data.get("votes", {}).get(owner, [])
        result["remaining"] = max(0, data.get("capacity", 0) - sum(item["status"] in {"joined", "confirmed"} for item in participants))
        return result

    def create_activity(self, actor, body: dict) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        kind = body.get("kind", "event")
        title = self._text(body.get("title"), 200)
        if kind not in {"poll", "event"} or not title:
            raise ValueError("需要活动类型和标题")
        raw_options = body.get("options", [])
        if not isinstance(raw_options, list):
            raise ValueError("投票选项必须是列表")
        options = [self._text(option, 100) for option in raw_options]
        if kind == "poll" and (not 2 <= len(options) <= 20 or len(set(options)) != len(options) or not all(options)):
            raise ValueError("投票需有 2～20 个不同且非空的选项")
        capacity = body.get("capacity", 20)
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError("活动名额应为 1～10000")
        multiple = body.get("multiple", False)
        if not isinstance(multiple, bool):
            raise ValueError("多选必须是开关")
        deadline = float(body.get("deadline") or 0)
        reminder_at = float(body.get("reminder_at") or 0)
        if not math.isfinite(deadline) or not math.isfinite(reminder_at):
            raise ValueError("截止和提醒时间必须是有效时间")
        if deadline and deadline <= time.time():
            raise ValueError("截止时间必须在未来")
        if reminder_at and (reminder_at <= time.time() or (deadline and reminder_at >= deadline)):
            raise ValueError("确认提醒应在现在之后、截止时间之前")
        key = f"create:{actor.message_id}" if actor.message_id else ""
        with self._db(True) as db:
            previous = db.execute("SELECT result FROM operations WHERE scope=? AND key=?", (scope, key)).fetchone() if key else None
            if previous:
                return json.loads(previous[0])
            identity = ("P" if kind == "poll" else "A") + uuid.uuid4().hex[:8]
            data = {"id": identity, "kind": kind, "title": title, "description": self._text(body.get("description")),
                    "options": options, "multiple": multiple, "capacity": capacity,
                    "deadline": deadline, "reminder_at": reminder_at, "status": "open", "revision": 1,
                    "creator": actor.owner, "votes": {}, "participants": {}, "created_at": time.time()}
            db.execute("INSERT INTO activities VALUES (?,?,?,?)", (identity, scope, self._dump(data), time.time()))
            if reminder_at:
                db.execute("INSERT INTO notifications(id,scope,activity_id,text,due,expires) VALUES (?,?,?,?,?,?)",
                           (f"activity:{identity}:confirm", scope, identity, "", reminder_at, deadline or reminder_at + 86400))
            result = self._public_activity(data, actor.owner)
            if key:
                db.execute("INSERT INTO operations VALUES (?,?,?,?)", (scope, key, self._dump(result), time.time()))
        return result

    def activity_action(self, actor, identity: str, action: str, body: dict) -> dict:
        scope = self._scope(actor)
        if action in {"close", "cancel", "remind"}:
            actor.require_manager(scope)
        operation_key = f"activity:{identity}:{actor.owner}:{actor.message_id}:{action}" if actor.message_id and action != "view" else ""
        with self._db(True) as db:
            previous = db.execute("SELECT result FROM operations WHERE scope=? AND key=?", (scope, operation_key)).fetchone() if operation_key else None
            if previous:
                return json.loads(previous[0])
            row = db.execute("SELECT data FROM activities WHERE id=? AND scope=?", (identity, scope)).fetchone()
            if row is None:
                raise ValueError("本群没有这个活动")
            data = json.loads(row[0])
            if action == "view":
                return self._public_activity(data, actor.owner)
            if data["status"] != "open":
                raise ValueError("活动已经结束或取消")
            if data["deadline"] and time.time() >= data["deadline"] and action not in {"close", "cancel"}:
                raise ValueError("活动已超过截止时间")
            if action in {"close", "cancel"}:
                data["status"] = "closed" if action == "close" else "cancelled"
                db.execute("UPDATE notifications SET status='cancelled' WHERE activity_id=? AND status='pending'", (identity,))
            elif action == "vote":
                if data["kind"] != "poll":
                    raise ValueError("这不是投票")
                raw_choices = body.get("choices", [])
                if not isinstance(raw_choices, list) or any(type(value) is not int for value in raw_choices):
                    raise ValueError("投票选项必须是整数列表")
                choices = sorted(set(raw_choices))
                if not choices or any(value < 0 or value >= len(data["options"]) for value in choices):
                    raise ValueError("请选择有效的投票选项")
                if not data["multiple"] and len(choices) != 1:
                    raise ValueError("本投票只能选一项")
                data["votes"][actor.owner] = choices
            elif action in {"join", "leave", "confirm"}:
                if data["kind"] != "event":
                    raise ValueError("这不是报名活动")
                participants = data["participants"]
                current = participants.get(actor.owner)
                if action == "join" and (not current or current["status"] == "left"):
                    occupied = sum(item["status"] in {"joined", "confirmed"} for item in participants.values())
                    participants[actor.owner] = {"name": self._text(body.get("display_name"), 80) or f"成员{str(actor.owner)[-4:]}",
                                                 "status": "joined" if occupied < data["capacity"] else "waiting", "joined_at": time.time()}
                elif action == "leave" and current:
                    current["status"] = "left"
                    occupied = sum(item["status"] in {"joined", "confirmed"} for item in participants.values())
                    waiting = sorted((item for item in participants.values() if item["status"] == "waiting"), key=lambda item: item["joined_at"])
                    if waiting and occupied < data["capacity"]:
                        waiting[0]["status"] = "joined"
                elif action == "confirm":
                    if not current or current["status"] not in {"joined", "confirmed"}:
                        raise ValueError("请先报名并获得名额，再确认参加")
                    current["status"] = "confirmed"
                elif action == "leave":
                    raise ValueError("你还没有报名")
            elif action == "remind":
                key = f"activity:{identity}:manual:{actor.message_id or int(time.time() // 60)}"
                db.execute("INSERT OR IGNORE INTO notifications(id,scope,activity_id,text,due,expires) VALUES (?,?,?,?,?,?)",
                           (key, scope, identity, "", time.time(), data["deadline"] or time.time() + 3600))
            else:
                raise ValueError("不支持的活动操作")
            data["revision"] += 1
            db.execute("UPDATE activities SET data=?,updated=? WHERE id=?", (self._dump(data), time.time(), identity))
            result = self._public_activity(data, actor.owner)
            if operation_key:
                db.execute("INSERT INTO operations VALUES (?,?,?,?)", (scope, operation_key, self._dump(result), time.time()))
            return result

    def notice(self, actor, body: dict) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        state = self.state(actor)
        if not state["settings"]["welcome_enabled"] or body.get("notice_type") != "group_increase":
            return {"queued": False}
        event_time = float(body.get("timestamp") or time.time())
        if abs(time.time() - event_time) > 120:
            return {"queued": False}
        member = str(body.get("member_id") or "")
        if not member or member == str(actor.bot_scope):
            return {"queued": False}
        name = self._text(body.get("display_name"), 80) or "新朋友"
        template = state["guide"].get("welcome") or "欢迎加入！发送 /群规、/群资料 或 /常见问题 可以查看本群指引。"
        identity = f"welcome:{member}:{int(event_time // 120)}"
        with self._db(True) as db:
            db.execute("INSERT OR IGNORE INTO notifications(id,scope,text,due,expires) VALUES (?,?,?,?,?)",
                       (scope + ":" + identity, scope, f"{name}，{template}", event_time + 3, event_time + 120))
        return {"queued": True}

    def notifications(self, actor) -> list[dict]:
        scope = self._scope(actor)
        actor.require_manager(scope)
        now = time.time()
        state = self.state(actor)
        with self._db(True) as db:
            db.execute("UPDATE notifications SET status='expired' WHERE scope=? AND status='pending' AND expires<=?", (scope, now))
            if not state["settings"]["welcome_enabled"]:
                db.execute("UPDATE notifications SET status='cancelled' WHERE scope=? AND activity_id='' AND status='pending'", (scope,))
            if state["quiet"]:
                return []
            rows = db.execute("SELECT * FROM notifications WHERE scope=? AND status='pending' AND due<=? ORDER BY due LIMIT 10", (scope, now)).fetchall()
            results = []
            for row in rows:
                text = row["text"]
                if row["activity_id"]:
                    activity = db.execute("SELECT data FROM activities WHERE id=? AND scope=?", (row["activity_id"], scope)).fetchone()
                    data = json.loads(activity[0]) if activity else {}
                    pending = [item for item in data.get("participants", {}).values() if item["status"] == "joined"]
                    if data.get("status") != "open" or not pending:
                        db.execute("UPDATE notifications SET status='cancelled' WHERE id=?", (row["id"],))
                        continue
                    text = f"{data['title']}（{data['id']}）还有 {len(pending)} 人未确认参加：" + "、".join(item["name"] for item in pending[:15]) + f"。请发送 /确认 {data['id']}。"
                results.append({"id": row["id"], "text": text, "expires_at": row["expires"], "attachments": []})
            return results

    def acknowledge_notification(self, actor, identity: str, message_id: str) -> dict:
        scope = self._scope(actor)
        actor.require_manager(scope)
        if not message_id:
            raise ValueError("发送确认缺少平台消息 ID")
        with self._db(True) as db:
            db.execute("UPDATE notifications SET status='sent',message_id=? WHERE id=? AND scope=?", (message_id, identity, scope))
        return {"ok": True}

    def ingest(self, actor, body: dict) -> dict:
        scope = self._scope(actor)
        actor.require_interaction()
        state = self.state(actor)
        if not state["settings"]["topic_enabled"]:
            return {"topic_id": "", "context": []}
        ref = str(body.get("message_id") or actor.message_id or "")
        if not ref:
            raise ValueError("话题消息需要真实消息标识")
        reply_ref = str(body.get("reply_ref") or "")
        bound_topic = self._text(body.get("topic_id"), 100) if actor.is_admin else ""
        now = time.time()
        with self._db(True) as db:
            existing = db.execute("SELECT topic FROM messages WHERE scope=? AND ref=?", (scope, ref)).fetchone()
            parent = db.execute("SELECT topic FROM messages WHERE scope=? AND ref=?", (scope, reply_ref)).fetchone() if reply_ref else None
            recent = db.execute("SELECT topic FROM messages WHERE scope=? AND owner=? AND created>? ORDER BY created DESC LIMIT 1", (scope, actor.owner, now - 180)).fetchone()
            topic = existing[0] if existing else bound_topic or (parent[0] if parent else recent[0] if recent and not reply_ref else uuid.uuid4().hex[:12])
            text = self._text(body.get("text"), 20000)
            label = self._text(body.get("sender_name"), 100) or "成员"
            db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?)", (scope, ref, actor.owner, label, text, reply_ref, topic, now))
            db.execute("DELETE FROM messages WHERE scope=? AND created<?", (scope, now - 21600))
            db.execute("DELETE FROM messages WHERE scope=? AND ref NOT IN (SELECT ref FROM messages WHERE scope=? ORDER BY created DESC LIMIT 500)", (scope, scope))
            rows = db.execute("SELECT ref,label,text,reply_ref FROM messages WHERE scope=? AND topic=? ORDER BY created DESC LIMIT 12", (scope, topic)).fetchall()
        return {"topic_id": topic, "context": [dict(row) for row in reversed(rows)], "reply_ref": ref}

    def record_decision(self, actor, body: dict) -> dict:
        scope = self._scope(actor)
        action = str(body.get("action") or "ignore")
        if action not in {"ignore", "react", "short_reply", "task", "reply"}:
            raise ValueError("无效回应动作")
        ref = str(actor.message_id or body.get("message_id") or "")
        decision = {"action": action, "reason": self._text(body.get("reason"), 200),
                    "topic_id": str(body.get("topic_id") or ""), "message_id": ref}
        with self._db(True) as db:
            db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)", (scope, ref, self._dump(decision), time.time()))
            db.execute("DELETE FROM decisions WHERE scope=? AND ref NOT IN (SELECT ref FROM decisions WHERE scope=? ORDER BY created DESC LIMIT 100)", (scope, scope))
        return decision

    def decisions(self, actor) -> list[dict]:
        scope = self._scope(actor)
        actor.require_manager(scope)
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM decisions WHERE scope=? ORDER BY created DESC LIMIT 100", (scope,))]

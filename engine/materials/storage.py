from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


class MaterialError(ValueError):
    def __init__(self, message: str, code: str = "invalid_input", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def value(actor: Any, name: str, default: Any = "") -> Any:
    return actor.get(name, default) if isinstance(actor, dict) else getattr(actor, name, default)


def identity(actor: Any) -> tuple[str, str, str]:
    scope, owner, bot = (str(value(actor, key) or "").strip() for key in ("scope", "owner", "bot_scope"))
    if not scope or not owner:
        raise MaterialError("请选择已授权的会话范围。", "scope_required", 403)
    return scope, owner, bot


def require_row(actor: Any, row: dict, *, edit: bool = False) -> None:
    scope, owner, bot = identity(actor)
    if row["scope"] != scope or row["bot_scope"] != bot:
        raise MaterialError("找不到当前会话有权访问的资料。", "not_found", 404)
    if edit and row["owner"] != owner and not bool(value(actor, "is_admin", False)):
        raise MaterialError("只有创建者或管理员可以修改这份资料。", "forbidden", 403)


def bounded_text(raw: Any, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(raw, str):
        raise MaterialError(f"{name} 必须是文字。")
    text = raw.strip()
    if required and not text:
        raise MaterialError(f"{name} 不能为空。")
    if len(text) > limit:
        raise MaterialError(f"{name} 超过 {limit} 字限制。", "limit_exceeded", 413)
    return text


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class MaterialStore:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "data" / "materials"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                owner TEXT NOT NULL, bot_scope TEXT NOT NULL, created REAL NOT NULL,
                data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS objects_scope ON objects(kind,bot_scope,scope,created);
            CREATE UNIQUE INDEX IF NOT EXISTS image_generation_identity
                ON objects(scope,bot_scope,owner,json_extract(data,'$.metadata.generation_key'))
                WHERE kind='version' AND json_extract(data,'$.metadata.generation_key') IS NOT NULL;
            CREATE TABLE IF NOT EXISTS journal(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, bot_scope TEXT NOT NULL,
                owner TEXT NOT NULL, message_id TEXT NOT NULL, timestamp REAL NOT NULL,
                text TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE(bot_scope,scope,message_id));
            CREATE INDEX IF NOT EXISTS journal_time ON journal(bot_scope,scope,timestamp);
            CREATE TABLE IF NOT EXISTS settings(
                bot_scope TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(bot_scope,scope));
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def create(self, kind: str, actor: Any, data: dict, object_id: str = "") -> dict:
        scope, owner, bot = identity(actor)
        record = {"id": object_id or new_id(kind[0] + "_"), "kind": kind, "scope": scope,
                  "owner": owner, "bot_scope": bot, "created": time.time(), **data}
        with self.lock:
            self.db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)", (
                record["id"], kind, scope, owner, bot, record["created"], dump(record),
            ))
        return record

    def get(self, object_id: str, actor: Any, *, kind: str = "", edit: bool = False) -> dict:
        with self.lock:
            raw = self.db.execute("SELECT data FROM objects WHERE id=?", (object_id,)).fetchone()
        if raw is None:
            raise MaterialError("资料不存在或已删除。", "not_found", 404)
        row = json.loads(raw[0])
        require_row(actor, row, edit=edit)
        if kind and row["kind"] != kind:
            raise MaterialError("资料类型不匹配。")
        return row

    def update(self, record: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE objects SET data=? WHERE id=?", (dump(record), record["id"]))

    def list(self, kind: str, actor: Any, limit: int = 100) -> list[dict]:
        scope, _, bot = identity(actor)
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM objects WHERE kind=? AND bot_scope=? AND scope=? ORDER BY created DESC LIMIT ?",
                (kind, bot, scope, min(max(limit, 1), 200)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def blob(self, content: bytes, suffix: str) -> tuple[str, str]:
        if len(content) > MAX_FILE_BYTES:
            raise MaterialError("文件超过 50 MB。", "limit_exceeded", 413)
        digest = hashlib.sha256(content).hexdigest()
        suffix = suffix.lower() if suffix.lower() in {
            ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".tsv",
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".json",
        } else ".bin"
        path = self.root / "blobs" / digest[:2] / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path.relative_to(self.root).as_posix(), digest

    def path(self, relative: str, digest: str = "") -> Path:
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise MaterialError("资料文件不存在。", "file_missing", 404)
        if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise MaterialError("资料文件校验失败，请重新上传。", "integrity_error", 409)
        return path

    def settings(self, actor: Any) -> dict:
        scope, _, bot = identity(actor)
        with self.lock:
            row = self.db.execute("SELECT data FROM settings WHERE bot_scope=? AND scope=?", (bot, scope)).fetchone()
        return {"journal_enabled": False, "journal_retention_days": 30, **(json.loads(row[0]) if row else {})}

    def save_settings(self, actor: Any, data: dict) -> dict:
        if not bool(value(actor, "is_admin", False)) and value(actor, "role") not in {"owner", "admin", "group_admin"}:
            raise MaterialError("只有管理员可以更改资料记录范围。", "forbidden", 403)
        config = self.settings(actor)
        if "journal_enabled" in data:
            if type(data["journal_enabled"]) is not bool:
                raise MaterialError("journal_enabled 必须为布尔值。")
            config["journal_enabled"] = data["journal_enabled"]
        if "journal_retention_days" in data:
            days = data["journal_retention_days"]
            if type(days) is not int or not 1 <= days <= 365:
                raise MaterialError("聊天记录保留天数须在 1–365 之间。")
            config["journal_retention_days"] = days
        scope, _, bot = identity(actor)
        with self.lock:
            self.db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(bot_scope,scope) DO UPDATE SET data=excluded.data", (bot, scope, dump(config)))
        return config

    def close(self) -> None:
        self.db.close()

"""表情包库。

内容哈希做主键，一张表用 state 表达待审/在库/已弃。多实例共享同一个库文件，
所以走 WAL + BEGIN IMMEDIATE 而不是进程内锁。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

STATE_PENDING = "pending"
STATE_LIBRARY = "library"
STATE_DISCARDED = "discarded"
STATES = (STATE_PENDING, STATE_LIBRARY, STATE_DISCARDED)

CAPTION_PENDING = "pending"
CAPTION_RUNNING = "running"
CAPTION_DONE = "done"
CAPTION_FAILED = "failed"
CAPTION_STATES = (CAPTION_PENDING, CAPTION_RUNNING, CAPTION_DONE, CAPTION_FAILED)

SOURCE_GROUP = "group"
SOURCE_UPLOAD = "upload"
SOURCE_IMPORT = "import"
SOURCE_IMAGEBED = "imagebed"

# 改这个数会让全库重新打标，烧的是真钱。只有 prompt 语义真的变了才 +1。
CAPTION_PROMPT_VERSION = 1

# 名字要能被模型照抄回来，所以剔掉标记语法会用到的字符。
_NAME_STRIP = re.compile(r"[\[\]:：,，\s]+")
_NAME_MAX = 24


@dataclass(frozen=True)
class Sticker:
    sha256: str
    name: str
    rel_path: str
    source: str
    state: str
    tags: list[str] = field(default_factory=list)
    auto_tags: list[str] = field(default_factory=list)
    manual_tags: list[str] = field(default_factory=list)
    manual_override: bool = False
    caption_state: str = CAPTION_PENDING
    caption_error: str = ""
    caption_prompt_version: int = 0
    width: int = 0
    height: int = 0
    animated: bool = False
    fmt: str = ""
    size: int = 0
    from_group: str = ""
    from_user: str = ""
    origin: str = ""
    sent_count: int = 0
    last_sent_at: int = 0
    created_at: int = 0
    updated_at: int = 0

    @property
    def usable(self) -> bool:
        """能不能进检索。没打标的图对模型来说是不可描述的，给了也选不出来。"""
        return self.state == STATE_LIBRARY and self.caption_state == CAPTION_DONE


def normalize_name(wanted: Any, *, digest: str = "") -> str:
    """名字要能被模型照抄进 [表情:名称]，带方括号或冒号就把标记本身破坏了。"""
    base = _NAME_STRIP.sub("", str(wanted or "").strip())[:_NAME_MAX]
    return base or f"表情{str(digest)[:6]}"


def _loads(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _row_to_sticker(row: sqlite3.Row) -> Sticker:
    return Sticker(
        sha256=str(row["sha256"]),
        name=str(row["name"]),
        rel_path=str(row["rel_path"]),
        source=str(row["source"]),
        state=str(row["state"]),
        tags=_loads(row["tags"]),
        auto_tags=_loads(row["auto_tags"]),
        manual_tags=_loads(row["manual_tags"]),
        manual_override=bool(row["manual_override"]),
        caption_state=str(row["caption_state"]),
        caption_error=str(row["caption_error"] or ""),
        caption_prompt_version=int(row["caption_prompt_version"] or 0),
        width=int(row["width"] or 0),
        height=int(row["height"] or 0),
        animated=bool(row["animated"]),
        fmt=str(row["fmt"] or ""),
        size=int(row["size"] or 0),
        from_group=str(row["from_group"] or ""),
        from_user=str(row["from_user"] or ""),
        origin=str(row["origin"] or ""),
        sent_count=int(row["sent_count"] or 0),
        last_sent_at=int(row["last_sent_at"] or 0),
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
    )


def merge_tags(auto: Sequence[str], manual: Sequence[str], *, override: bool) -> list[str]:
    """检索用的合并标签。override 时完全丢掉自动标签，人工说了算。"""
    merged: list[str] = []
    for item in (list(manual) if override else [*manual, *auto]):
        text = str(item or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


class StickerStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 收集 worker 跑在 to_thread 的线程池里，而控制台和聊天命令从事件循环线程进来。
        # 放开线程检查后连接不再自带互斥，全部访问必须走 _lock。
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA synchronous=FULL")
        # 多个实例共读共写这一个文件，WAL 让读不阻塞写。
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        try:
            self._verify()
            self._create_schema()
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _verify(self) -> None:
        rows = self._db.execute("PRAGMA quick_check").fetchall()
        if not rows or any(str(row[0]).lower() != "ok" for row in rows):
            raise sqlite3.DatabaseError(f"表情包库自检未过: {[str(r[0]) for r in rows]}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchone()

    def _create_schema(self) -> None:
        exists = self._one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stickers'"
        )
        if exists:
            return
        with self._tx() as db:
            db.execute(
                f"""CREATE TABLE stickers (
                    sha256 TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN {STATES}),
                    tags TEXT NOT NULL DEFAULT '[]',
                    auto_tags TEXT NOT NULL DEFAULT '[]',
                    manual_tags TEXT NOT NULL DEFAULT '[]',
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    caption_state TEXT NOT NULL DEFAULT '{CAPTION_PENDING}'
                        CHECK(caption_state IN {CAPTION_STATES}),
                    caption_error TEXT NOT NULL DEFAULT '',
                    caption_prompt_version INTEGER NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    animated INTEGER NOT NULL DEFAULT 0,
                    fmt TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    from_group TEXT NOT NULL DEFAULT '',
                    from_user TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL DEFAULT '',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    last_sent_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
            # 模型靠名字点图，重名就会点歪。
            db.execute("CREATE UNIQUE INDEX stickers_name ON stickers(name) WHERE state='library'")
            db.execute("CREATE INDEX stickers_state ON stickers(state,caption_state)")
            db.execute("CREATE INDEX stickers_created ON stickers(state,created_at)")
            db.execute(
                """CREATE TABLE sent_log (
                    conversation_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY (conversation_key, sha256)
                )"""
            )
            db.execute("CREATE INDEX sent_log_at ON sent_log(conversation_key,sent_at)")
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta(key,value) VALUES('schema','1')")

    # ── 去重 ──

    def state_of(self, sha256: str) -> str:
        """热路径上唯一要做的事：这张图见过没有。"""
        row = self._one("SELECT state FROM stickers WHERE sha256=?", (str(sha256),))
        return str(row[0]) if row else ""

    def known(self, sha256: str) -> bool:
        return bool(self.state_of(sha256))

    # ── 写入 ──

    def _unique_name(self, db: sqlite3.Connection, wanted: str, sha256: str) -> str:
        base = normalize_name(wanted, digest=sha256)
        candidate = base
        for suffix in range(2, 100):
            row = db.execute(
                "SELECT 1 FROM stickers WHERE name=? AND sha256<>?", (candidate, sha256)
            ).fetchone()
            if not row:
                return candidate
            candidate = f"{base}{suffix}"
        return f"{base}{sha256[:6]}"

    def add(
        self,
        *,
        sha256: str,
        rel_path: str,
        source: str,
        state: str = STATE_PENDING,
        name: str = "",
        width: int = 0,
        height: int = 0,
        animated: bool = False,
        fmt: str = "",
        size: int = 0,
        from_group: str = "",
        from_user: str = "",
        origin: str = "",
    ) -> Sticker | None:
        """登记一张图。已存在（含已弃）时返回 None，调用方据此删掉刚落盘的副本。"""
        now = int(time.time())
        with self._tx() as db:
            if db.execute("SELECT 1 FROM stickers WHERE sha256=?", (sha256,)).fetchone():
                return None
            final_name = self._unique_name(db, name or fmt or "表情", sha256)
            db.execute(
                """INSERT INTO stickers
                (sha256,name,rel_path,source,state,width,height,animated,fmt,size,
                 from_group,from_user,origin,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sha256, final_name, rel_path, source, state, int(width), int(height),
                 1 if animated else 0, fmt, int(size), from_group, from_user, origin, now, now),
            )
        return self.get(sha256)

    def get(self, sha256: str) -> Sticker | None:
        row = self._one("SELECT * FROM stickers WHERE sha256=?", (str(sha256),))
        return _row_to_sticker(row) if row else None

    def by_name(self, name: str) -> Sticker | None:
        """按名字取在库的图。模型点名走这里。"""
        row = self._one(
            "SELECT * FROM stickers WHERE name=? AND state=?",
            (str(name or "").strip(), STATE_LIBRARY),
        )
        return _row_to_sticker(row) if row else None

    def accept(self, sha256: str, *, rel_path: str = "") -> bool:
        """待审转在库。rel_path 非空表示文件同时搬了家。"""
        now = int(time.time())
        with self._tx() as db:
            row = db.execute(
                "SELECT name FROM stickers WHERE sha256=? AND state=?", (sha256, STATE_PENDING)
            ).fetchone()
            if not row:
                return False
            # 唯一索引只覆盖在库项，转正这一刻才可能撞名。
            name = self._unique_name(db, str(row[0]), sha256)
            if rel_path:
                db.execute(
                    "UPDATE stickers SET state=?,name=?,rel_path=?,updated_at=? WHERE sha256=?",
                    (STATE_LIBRARY, name, rel_path, now, sha256),
                )
            else:
                db.execute(
                    "UPDATE stickers SET state=?,name=?,updated_at=? WHERE sha256=?",
                    (STATE_LIBRARY, name, now, sha256),
                )
        return True

    def discard(self, sha256: str) -> bool:
        """丢弃并记住。留着哈希是为了下次别再收同一张。"""
        with self._tx() as db:
            cursor = db.execute(
                """UPDATE stickers SET state=?,rel_path='',tags='[]',auto_tags='[]',
                   manual_tags='[]',updated_at=? WHERE sha256=? AND state<>?""",
                (STATE_DISCARDED, int(time.time()), sha256, STATE_DISCARDED),
            )
            return cursor.rowcount > 0

    def forget(self, sha256: str) -> bool:
        """彻底删记录。之后这张图可以重新被收。"""
        with self._tx() as db:
            db.execute("DELETE FROM sent_log WHERE sha256=?", (sha256,))
            return db.execute("DELETE FROM stickers WHERE sha256=?", (sha256,)).rowcount > 0

    def rename(self, sha256: str, name: str) -> str:
        with self._tx() as db:
            final = self._unique_name(db, name, sha256)
            db.execute(
                "UPDATE stickers SET name=?,updated_at=? WHERE sha256=?",
                (final, int(time.time()), sha256),
            )
        return final

    # ── 标签 ──

    def set_auto_tags(self, sha256: str, tags: Sequence[str], *, version: int) -> None:
        with self._tx() as db:
            row = db.execute(
                "SELECT manual_tags,manual_override FROM stickers WHERE sha256=?", (sha256,)
            ).fetchone()
            if not row:
                return
            manual = _loads(row["manual_tags"])
            override = bool(row["manual_override"])
            db.execute(
                """UPDATE stickers SET auto_tags=?,tags=?,caption_state=?,caption_error='',
                   caption_prompt_version=?,updated_at=? WHERE sha256=?""",
                (json.dumps(list(tags), ensure_ascii=False),
                 json.dumps(merge_tags(tags, manual, override=override), ensure_ascii=False),
                 CAPTION_DONE, int(version), int(time.time()), sha256),
            )

    def set_manual_tags(
        self, sha256: str, tags: Sequence[str], *, override: bool | None = None,
    ) -> None:
        with self._tx() as db:
            row = db.execute(
                "SELECT auto_tags,manual_override FROM stickers WHERE sha256=?", (sha256,)
            ).fetchone()
            if not row:
                return
            flag = bool(row["manual_override"]) if override is None else bool(override)
            auto = _loads(row["auto_tags"])
            db.execute(
                """UPDATE stickers SET manual_tags=?,manual_override=?,tags=?,updated_at=?
                   WHERE sha256=?""",
                (json.dumps(list(tags), ensure_ascii=False), 1 if flag else 0,
                 json.dumps(merge_tags(auto, tags, override=flag), ensure_ascii=False),
                 int(time.time()), sha256),
            )

    def mark_caption(self, sha256: str, state: str, *, error: str = "") -> None:
        if state not in CAPTION_STATES:
            raise ValueError(f"未知打标状态: {state}")
        with self._tx() as db:
            db.execute(
                "UPDATE stickers SET caption_state=?,caption_error=?,updated_at=? WHERE sha256=?",
                (state, str(error)[:500], int(time.time()), sha256),
            )

    def reset_captions(self, *, only_failed: bool = False) -> int:
        """重打标。prompt 语义变了或想换模型重跑时用。"""
        clause = f"AND caption_state='{CAPTION_FAILED}'" if only_failed else ""
        with self._tx() as db:
            return db.execute(
                f"""UPDATE stickers SET caption_state=?,caption_error='',updated_at=?
                    WHERE state<>? {clause}""",
                (CAPTION_PENDING, int(time.time()), STATE_DISCARDED),
            ).rowcount

    def due_for_caption(self, limit: int = 20, *, version: int = CAPTION_PROMPT_VERSION) -> list[Sticker]:
        """待打标的活。版本落后的也算 —— prompt 变了旧标签就不可比。"""
        rows = self._all(
            """SELECT * FROM stickers WHERE state<>? AND (
                   caption_state=? OR (caption_state=? AND caption_prompt_version<?)
               ) ORDER BY created_at LIMIT ?""",
            (STATE_DISCARDED, CAPTION_PENDING, CAPTION_DONE, int(version), int(limit)),
        )
        return [_row_to_sticker(row) for row in rows]

    # ── 查询 ──

    def all_usable(self) -> list[Sticker]:
        """可参与检索的全部图。"""
        rows = self._all(
            "SELECT * FROM stickers WHERE state=? AND caption_state=?",
            (STATE_LIBRARY, CAPTION_DONE),
        )
        return [_row_to_sticker(row) for row in rows]

    def browse(
        self,
        *,
        state: str = "",
        limit: int = 100,
        offset: int = 0,
        search: str = "",
    ) -> list[Sticker]:
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if search:
            clauses.append("(name LIKE ? OR tags LIKE ?)")
            params.extend([f"%{search}%"] * 2)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        rows = self._all(
            f"SELECT * FROM stickers {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
        )
        return [_row_to_sticker(row) for row in rows]

    def counts(self) -> dict[str, int]:
        out = {state: 0 for state in STATES}
        out.update({
            str(row[0]): int(row[1])
            for row in self._all("SELECT state,COUNT(*) FROM stickers GROUP BY state")
        })
        out["usable"] = int(self._one(
            "SELECT COUNT(*) FROM stickers WHERE state=? AND caption_state=?",
            (STATE_LIBRARY, CAPTION_DONE),
        )[0])
        out["awaiting_caption"] = int(self._one(
            "SELECT COUNT(*) FROM stickers WHERE state<>? AND caption_state IN (?,?)",
            (STATE_DISCARDED, CAPTION_PENDING, CAPTION_RUNNING),
        )[0])
        return out

    # ── 已发历史 ──

    def record_sent(self, conversation_key: str, sha256: str) -> None:
        now = int(time.time())
        with self._tx() as db:
            db.execute(
                """INSERT INTO sent_log(conversation_key,sha256,sent_at) VALUES(?,?,?)
                   ON CONFLICT(conversation_key,sha256) DO UPDATE SET sent_at=excluded.sent_at""",
                (str(conversation_key), str(sha256), now),
            )
            db.execute(
                "UPDATE stickers SET sent_count=sent_count+1,last_sent_at=? WHERE sha256=?",
                (now, str(sha256)),
            )

    def recently_sent(self, conversation_key: str, *, within_sec: int) -> set[str]:
        """这个会话最近发过哪些。同一张图连着刷是最容易被看出是机器的行为。"""
        # 严格大于：窗口传 0 就该是"什么都不算最近"，而不是把刚发的那条也算进来。
        rows = self._all(
            "SELECT sha256 FROM sent_log WHERE conversation_key=? AND sent_at>?",
            (str(conversation_key), int(time.time()) - max(int(within_sec), 0)),
        )
        return {str(row[0]) for row in rows}

    def prune_sent_log(self, *, older_than_sec: int) -> int:
        with self._tx() as db:
            return db.execute(
                "DELETE FROM sent_log WHERE sent_at<=?",
                (int(time.time()) - max(int(older_than_sec), 0),),
            ).rowcount

    # ── 配额 ──

    def expired_pending(self, *, ttl_sec: int) -> list[Sticker]:
        # 小于等于：TTL 传 0 意为立即过期，用于"清空待审池"。
        rows = self._all(
            "SELECT * FROM stickers WHERE state=? AND created_at<=?",
            (STATE_PENDING, int(time.time()) - max(int(ttl_sec), 0)),
        )
        return [_row_to_sticker(row) for row in rows]

    def overflow(self, state: str, *, keep: int) -> list[Sticker]:
        """超出配额的部分，最旧的先出。调用方负责删文件。"""
        rows = self._all(
            "SELECT * FROM stickers WHERE state=? ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (state, max(int(keep), 0)),
        )
        return [_row_to_sticker(row) for row in rows]

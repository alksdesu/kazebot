"""TaskRecord 的用量汇总：按会话、节点、模型或渠道算提示缓存的命中率，基线和当前值并排给出。

同一段用量会在几份 transcript 里各出现一次（轮摘要回写、入口分支合并、父子会话各写一份），只算一次。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

import config_layout
from engine.memory_namespace import conversation_namespace
from engine.task_record import TaskRecord, parse_record_timestamp

from .admin_api import verify_admin_token
from .conversation_labels import (
    account_scope,
    describe_namespaces,
    is_internal_task_key,
    is_runtime_copy_session,
    load_sessions,
)

log = logging.getLogger(__name__)

GROUP_BY_DIMENSIONS = ("session", "node", "model", "channel")
BASELINE_LEGACY = "legacy"
BASELINE_BEFORE = "before"
CURRENT = "current"
BASELINE = "baseline"

# 副本之间会变的只有这些：轮摘要回写改 summary（老记录连带 outcome、version），分支合并和父子两份改 session_id，compressed 是事后打的标
_COPY_ONLY_FIELDS = frozenset({"session_id", "summary", "outcome", "version", "compressed"})
_KEY_BYTES = 16
_BRANCH_PREFIX = "branch_"

Stamp = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class UsageSegment:
    """transcript 里一条记录的用量部分。key 相同的是同一段用量的几份副本。"""

    key: bytes
    task_id: str
    session_id: str
    child_session_id: str
    node_id: str
    completed_at: float | None
    model: str
    provider: str
    channel: str
    prompt_tokens: int
    cache_read_tokens: int | None


@dataclass(frozen=True)
class ParsedTranscript:
    stamp: Stamp
    segments: tuple[UsageSegment, ...]
    bad_lines: int


@dataclass
class ScanStats:
    files: int = 0
    reparsed: int = 0
    bad_lines: int = 0
    records: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"files": self.files, "reparsed": self.reparsed, "bad_lines": self.bad_lines, "records": self.records}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _shared(value: Any) -> str:
    # 节点、模型、会话名在几十万条记录里反复出现，同一个字符串只留一份
    return sys.intern(_text(value))


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _copy_key(record: TaskRecord) -> bytes:
    identity = {key: value for key, value in record.to_dict().items() if key not in _COPY_ONLY_FIELDS}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=_KEY_BYTES).digest()


def segment_of(data: Any) -> UsageSegment | None:
    """一行 transcript 解析出来的对象 → 用量段。不是一条像样的 TaskRecord 就是 None。"""
    if not isinstance(data, dict):
        return None
    record = TaskRecord.from_dict(data)
    task_id = _text(record.task_id)
    if not task_id:
        return None
    completed_at: float | None = None
    if record.completed_at:
        moment = parse_record_timestamp(record.completed_at)
        if moment is None:
            return None
        completed_at = moment.timestamp()
    usage = record.token_usage if isinstance(record.token_usage, dict) else {}
    return UsageSegment(
        key=_copy_key(record),
        task_id=task_id,
        session_id=_shared(record.session_id),
        child_session_id=_shared(record.child_session_id),
        node_id=_shared(record.node_id),
        completed_at=completed_at,
        model=_shared(record.model),
        provider=_shared(record.provider),
        channel=_shared(record.channel),
        prompt_tokens=_token_count(usage.get("prompt_tokens")) or 0,
        cache_read_tokens=_token_count(usage.get("cache_read_tokens")),
    )


def parse_transcript_file(path: Path, stamp: Stamp) -> ParsedTranscript | None:
    """逐行流式读一份 transcript，坏行跳过并计数。整份读不了是 None。"""
    segments: list[UsageSegment] = []
    bad_lines = 0
    try:
        with path.open("rb") as stream:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                try:
                    segment = segment_of(json.loads(line))
                except Exception:
                    # 一行写坏了不能让整份、更不能让整个汇总读不出来
                    segment = None
                if segment is None:
                    bad_lines += 1
                else:
                    segments.append(segment)
    except OSError as exc:
        log.warning("读不了 %s，这一份不计入用量汇总：%s", path.name, exc)
        return None
    return ParsedTranscript(stamp=stamp, segments=tuple(segments), bad_lines=bad_lines)


class TranscriptUsageIndex:
    """data/transcripts/ 下全部转写的用量段。每份按 file_stamp 缓存，没变的不重读。"""

    def __init__(self, transcript_dir: Path) -> None:
        self._dir = Path(transcript_dir)
        self._parsed: dict[str, ParsedTranscript] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[list[UsageSegment], ScanStats]:
        with self._lock:
            return self._refresh_locked()

    def _transcript_paths(self) -> list[Path]:
        try:
            return sorted(self._dir.glob("*.jsonl"))
        except OSError as exc:
            log.warning("列不出 %s，用量汇总按没有转写算：%s", self._dir, exc)
            return []

    def _refresh_locked(self) -> tuple[list[UsageSegment], ScanStats]:
        stats = ScanStats()
        segments: list[UsageSegment] = []
        kept: dict[str, ParsedTranscript] = {}
        for path in self._transcript_paths():
            stamp = config_layout.file_stamp(path)
            if stamp is None:
                continue
            parsed = self._parsed.get(path.name)
            if parsed is None or parsed.stamp != stamp:
                # 指纹在读之前取：读的同时有人追加，下次指纹对不上会再读一遍
                parsed = parse_transcript_file(path, stamp)
                if parsed is None:
                    continue
                stats.reparsed += 1
            kept[path.name] = parsed
            stats.files += 1
            stats.bad_lines += parsed.bad_lines
            stats.records += len(parsed.segments)
            segments.extend(parsed.segments)
        # 清理掉的会话连缓存一起丢，不然删了的文件还一直占着内存
        self._parsed = kept
        return segments, stats


@dataclass(frozen=True)
class CacheQuery:
    group_by: str = "session"
    since: datetime | None = None
    until: datetime | None = None
    baseline_before: datetime | None = None

    @property
    def baseline_mode(self) -> str:
        return BASELINE_BEFORE if self.baseline_before is not None else BASELINE_LEGACY


def _query_time(text: str, name: str) -> datetime | None:
    if not text or not text.strip():
        return None
    moment = parse_record_timestamp(text)
    if moment is None:
        raise ValueError(f"{name} 要写成 ISO 8601 时间，收到的是 {text!r}")
    return moment


def parse_query(
    *, group_by: str = "session", since: str = "", until: str = "", baseline_before: str = "",
) -> CacheQuery:
    dimension = (group_by or "session").strip()
    if dimension not in GROUP_BY_DIMENSIONS:
        raise ValueError(f"group_by 只能是 {'、'.join(GROUP_BY_DIMENSIONS)} 之一，收到的是 {group_by!r}")
    start = _query_time(since, "since")
    end = _query_time(until, "until")
    before = _query_time(baseline_before, "baseline_before")
    if start is None and before is not None:
        # 截止时间之前的当基线，当前值没给起点就从截止时间算起，两段不重叠
        start = before
    if start is not None and end is not None and end <= start:
        raise ValueError("until 要晚于 since")
    return CacheQuery(group_by=dimension, since=start, until=end, baseline_before=before)


@dataclass
class _Tally:
    tasks: set[str] = field(default_factory=set)
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    no_data_tasks: set[str] = field(default_factory=set)
    no_data_prompt_tokens: int = 0

    def add(self, segment: UsageSegment, hit: int | None) -> None:
        if hit is None:
            self.no_data_tasks.add(segment.task_id)
            self.no_data_prompt_tokens += segment.prompt_tokens
            return
        self.tasks.add(segment.task_id)
        self.prompt_tokens += segment.prompt_tokens
        # 中转报的命中量偶尔比输入还多，按输入封顶，比例才不会超过 100%
        self.cache_read_tokens += min(hit, segment.prompt_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": len(self.tasks),
            "prompt_tokens": self.prompt_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "miss_tokens": self.prompt_tokens - self.cache_read_tokens,
            "hit_rate": self.cache_read_tokens / self.prompt_tokens if self.prompt_tokens else None,
            "no_data_tasks": len(self.no_data_tasks),
            "no_data_prompt_tokens": self.no_data_prompt_tokens,
        }


def _volume(stats: Mapping[str, Any]) -> int:
    return int(stats["prompt_tokens"]) + int(stats["no_data_prompt_tokens"])


@dataclass
class _Copies:
    segment: UsageSegment
    sessions: set[str]
    # 轮摘要副本和原记录在同一份 transcript 里，session_id 相同，份数不能拿 sessions 的大小算
    count: int = 1


def _merge_copies(segments: Iterable[UsageSegment]) -> list[_Copies]:
    merged: dict[bytes, _Copies] = {}
    for segment in segments:
        found = merged.get(segment.key)
        if found is None:
            merged[segment.key] = _Copies(segment, {segment.session_id})
        else:
            found.sessions.add(segment.session_id)
            found.count += 1
    return list(merged.values())


def session_parents(sessions: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """sessions.json 里子会话、入口分支 → 父会话。"""
    parents: dict[str, str] = {}
    for session_id, entry in sessions.items():
        parent = _text(entry.get("parent_session_id")).strip() if isinstance(entry, Mapping) else ""
        if parent:
            parents[str(session_id)] = parent
    return parents


class _SessionTree:
    def __init__(self, parents: Mapping[str, str]) -> None:
        self._parents = parents

    def parent(self, session_id: str) -> str:
        return self._parents.get(session_id, "")

    def root(self, session_id: str) -> str:
        current, seen = session_id, {session_id}
        while True:
            parent = self._parents.get(current, "")
            if not parent or parent in seen:
                return current
            seen.add(parent)
            current = parent


def _user_copy(sessions: Iterable[str]) -> str:
    """几份副本里记在用户会话名下的那一份：入口分支合并过去的、子任务写给父会话的。"""
    return next((session_id for session_id in sorted(sessions) if not is_runtime_copy_session(session_id)), "")


def _run_session(copies: _Copies, tree: _SessionTree) -> str:
    """这一段实际跑在哪个会话：子任务是它的子会话；入口分支只是一次 inbound 的副本，算回父会话。"""
    if copies.segment.child_session_id:
        return copies.segment.child_session_id
    home = _user_copy(copies.sessions) or sorted(copies.sessions)[0]
    if home.startswith(_BRANCH_PREFIX):
        return tree.parent(home) or home
    return home


def _conversation(copies: _Copies, run_session: str, tree: _SessionTree) -> str:
    """按会话汇总时归到哪个用户会话。"""
    root = tree.root(run_session)
    if not is_runtime_copy_session(root):
        return root
    # 子会话清掉后 sessions.json 里断了链，写给父会话的那份副本还认得出归属
    return _user_copy(copies.sessions) or root


def _sides(
    completed_at: float | None, *, since: float | None, until: float | None, before: float | None,
) -> tuple[str, ...]:
    if completed_at is None:
        # 没有完成时间的是升级前的老记录：默认整体当基线，时间段筛不到它们
        return (BASELINE,) if before is None else ()
    sides: list[str] = []
    if before is not None and completed_at < before:
        sides.append(BASELINE)
    if (since is None or completed_at >= since) and (until is None or completed_at < until):
        sides.append(CURRENT)
    return tuple(sides)


def _timestamp(moment: datetime | None) -> float | None:
    return moment.timestamp() if moment is not None else None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _group_key(dimension: str, copies: _Copies, run_session: str, tree: _SessionTree) -> str:
    segment = copies.segment
    if dimension == "session":
        return _conversation(copies, run_session, tree)
    if dimension == "node":
        return segment.node_id
    if dimension == "model":
        return segment.model
    return segment.channel


def build_cache_report(
    segments: Iterable[UsageSegment], query: CacheQuery, *, parents: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """命中率 = 命中 token / 输入 token。报不出缓存的「无数据」不进分子分母，单独计数。

    老记录命中为 0 时不写 cache_read_tokens：它实际跑的会话里有记录报过这一项（新记录报的 0 也算）就按 0 算，
    从没报过的算无数据。新记录报了就照记，缺了是渠道没报。
    """
    tree = _SessionTree(parents or {})
    merged = _merge_copies(segments)
    since, until = _timestamp(query.since), _timestamp(query.until)
    before = _timestamp(query.baseline_before)

    placed: list[tuple[_Copies, str]] = []
    reporting_sessions: set[str] = set()
    without_usage = 0
    for copies in merged:
        run_session = _run_session(copies, tree)
        segment = copies.segment
        # 新记录也作数：Gemini 没命中时老记录缺这一项，升级后同一会话报出 0 才知道那是 0 而不是不报
        if segment.cache_read_tokens is not None:
            reporting_sessions.add(run_session)
        if segment.prompt_tokens <= 0:
            without_usage += 1
            continue
        placed.append((copies, run_session))

    groups: dict[str, dict[str, _Tally]] = {}
    totals = {CURRENT: _Tally(), BASELINE: _Tally()}
    for copies, run_session in placed:
        segment = copies.segment
        sides = _sides(segment.completed_at, since=since, until=until, before=before)
        if not sides:
            continue
        hit = segment.cache_read_tokens
        if hit is None and segment.completed_at is None and run_session in reporting_sessions:
            hit = 0
        key = _group_key(query.group_by, copies, run_session, tree)
        tallies = groups.setdefault(key, {CURRENT: _Tally(), BASELINE: _Tally()})
        for side in sides:
            tallies[side].add(segment, hit)
            totals[side].add(segment, hit)

    rows = [
        {"key": key, CURRENT: tallies[CURRENT].to_dict(), BASELINE: tallies[BASELINE].to_dict()}
        for key, tallies in groups.items()
    ]
    rows.sort(key=lambda row: (-_volume(row[CURRENT]), -_volume(row[BASELINE]), row["key"]))
    return {
        "group_by": query.group_by,
        "window": {"since": _iso(query.since), "until": _iso(query.until)},
        "baseline": {"mode": query.baseline_mode, "before": _iso(query.baseline_before)},
        "totals": {side: tally.to_dict() for side, tally in totals.items()},
        "rows": rows,
        "scan": {
            "segments": len(merged),
            "duplicates": sum(copies.count for copies in merged) - len(merged),
            "without_usage": without_usage,
        },
    }


def describe_sessions(
    workspace_root: Path, session_ids: Iterable[str], sessions: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """会话行的归属：和控制台会话清单同一套，前端拿它起名字。"""
    labels: Mapping[str, dict[str, Any]] = {}
    accounts = None
    try:
        labels = describe_namespaces(workspace_root)
        accounts = account_scope(workspace_root)
    except Exception:
        # 认不出归属不该拖垮整张表：行照出，名字退回会话键
        log.warning("会话归属读取失败，用量表只按会话键起名", exc_info=True)
    described: dict[str, dict[str, Any]] = {}
    for session_id in session_ids:
        entry = sessions.get(session_id)
        conversation_key = _text(entry.get("conversation_key")).strip() if isinstance(entry, Mapping) else ""
        described[session_id] = {
            "conversation_key": conversation_key,
            "owner": labels.get(conversation_namespace(conversation_key)) if conversation_key else None,
            "current_account": accounts.is_current(conversation_key) if accounts is not None else True,
            "internal": is_internal_task_key(conversation_key),
        }
    return described


def create_usage_router(workspace_root: Path) -> APIRouter:
    """挂在 /v1/admin/usage 下，要管理令牌。"""
    root = Path(workspace_root)
    index = TranscriptUsageIndex(root / "data" / "transcripts")
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    @router.get("/cache")
    def cache_usage(
        group_by: str = Query("session"),
        since: str = Query(""),
        until: str = Query(""),
        baseline_before: str = Query(""),
    ) -> dict[str, Any]:
        try:
            query = parse_query(group_by=group_by, since=since, until=until, baseline_before=baseline_before)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        segments, scan = index.snapshot()
        sessions = load_sessions(root)
        report = build_cache_report(segments, query, parents=session_parents(sessions))
        report["scan"] = {**scan.to_dict(), **report["scan"]}
        if query.group_by == "session":
            described = describe_sessions(root, (row["key"] for row in report["rows"]), sessions)
            for row in report["rows"]:
                row["session"] = described[row["key"]]
        return report

    return router

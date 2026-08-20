from __future__ import annotations

import hashlib
import logging
import os as _os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_int
from engine.memory_subjects import (
    is_enrolled as _subject_is_enrolled,
    resolve_load_subjects,
    subject_namespace,
)
from engine.node import Node
from toolbox._common import request_guard, resolve_under_allowed_roots
from toolbox.builtins import SKILL_NAME_RE
from toolbox.context import ToolContext

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.eventlog_rotation import eventlog_file_lock
from engine.memory_hit_cache import record_hits

logger = logging.getLogger(__name__)


# Why: skill and memory injection now share one before_prompt_build handler.
# How: declare one metadata entry with the higher previous priority. Purpose:
# preserve hook discovery while removing the old two-step injector chain.
PLUGIN_META = {
    "handler_class": "KnowledgeInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 50,
    # Why: the six skill and memory CRUD tools now belong to the knowledge
    # plugin rather than toolbox.registry.py. How: the concrete declarations are
    # attached after their functions are defined below. Purpose: keep one source
    # of truth for knowledge behavior and tool registration metadata.
    "tools": [],
}



# ---------------------------------------------------------------------------
#  Keyword matching
# ---------------------------------------------------------------------------

_REGEX_KEYWORD_RE = re.compile(r"^/(.+)/([ism]*)$", re.DOTALL)
# 纯 ASCII 的短词按子串匹配等于通配符：`ai` 命中 said / mail / again，
# `qq`、`id` 同理。CJK 没有词边界可用，只能保子串。
_BOUNDARY_MAX_LEN = 3


def compile_keyword(kw: str) -> re.Pattern[str] | str:
    """Compile a skill or memory keyword entry."""
    # Why: skill and memory keyword matching now lives inside the knowledge
    # plugin instead of the old standalone matcher file. How: keep the exact legacy
    # /pattern/flags parsing and lower-case substring fallback. Purpose: preserve
    # activation behavior while removing the standalone matcher module.
    kw = (kw or "").strip()
    if not kw:
        return ""

    # 只有整体形如 /pattern/flags 才当正则。原来的判据是「以 / 开头且后面还有 /」，
    # flags 从任意尾串里瞎抠：`/api/config` 会变成 re.compile("api", IGNORECASE)
    # ——因为 config 里有个 i。写路径当关键词的人拿到的是完全不同的匹配集合。
    match = _REGEX_KEYWORD_RE.match(kw)
    if match is not None:
        pattern, flags_str = match.group(1), match.group(2)
        flags = 0
        if "i" in flags_str:
            flags |= re.IGNORECASE
        if "s" in flags_str:
            flags |= re.DOTALL
        if "m" in flags_str:
            flags |= re.MULTILINE
        try:
            return re.compile(pattern, flags)
        except re.error as regex_error:
            # 原来这里是静默 pass，用户写错正则只会得到「莫名匹配不上」。
            logger.warning("keyword %r is not a valid regex, matching it literally: %s", kw, regex_error)
            return kw.lower()
    elif kw.startswith("/") and kw.rfind("/") > 0:
        logger.info("keyword %r is now matched literally, not as a regex", kw)

    lowered = kw.lower()
    if len(lowered) <= _BOUNDARY_MAX_LEN and lowered.isascii():
        # 不能用 \b —— CJK 也算 \w，`\bai\b` 在「他说ai很好」里不成立，会反过来漏掉。
        return re.compile(
            rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", re.IGNORECASE,
        )
    return lowered


def match_keywords(compiled: list[re.Pattern[str] | str], text: str) -> bool:
    """Return True when any compiled keyword matches text."""
    # Why: both skill and memory entries use the same activation semantics. How:
    # run regex entries against original text and literal entries against lowered
    # text, matching the old helper exactly. Purpose: prevent behavior drift while
    # the duplicated matcher files are removed.
    if not compiled or not text:
        return False
    text_lower = text.lower()
    for kw in compiled:
        if not kw:
            continue
        if isinstance(kw, re.Pattern):
            if kw.search(text):
                return True
        elif kw in text_lower:
            return True
    return False


def build_scan_text(
    instruction_text: str,
    history: list[dict[str, Any]] | None,
    scan_depth: int,
) -> str:
    """Build the text scanned for keyword activation."""
    # Why: skill and memory scan-depth handling must stay identical. How: always
    # include the current instruction and append the last scan_depth conversation
    # rounds using the legacy user-message boundary rule. Purpose: keep keyword
    # activation scope unchanged after the files are merged.
    parts: list[str] = [instruction_text or ""]
    if history and scan_depth > 0:
        # Why: the old implementation defined a round as a user message that
        # starts after a non-user message. How: walk backward until enough round
        # starts are found. Purpose: preserve keyword activation scope exactly.
        round_starts: list[int] = []
        for i in range(len(history) - 1, -1, -1):
            role = history[i].get("role", "")
            if role != "user":
                continue
            if i == 0 or history[i - 1].get("role", "") != "user":
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break

        if round_starts:
            start_idx = round_starts[-1]
            for msg in history[start_idx:]:
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Skill frontmatter, catalog loading, and legacy skill builder
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md content."""
    # Why: skill CRUD and skill injection now share this module after deleting
    # the old separate skill runtime file. How: keep the same frontmatter delimiter
    # and YAML fallback behavior. Purpose: preserve SKILL.md compatibility during
    # the move.
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 5:]
    try:
        meta = yaml.safe_load(head) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


class _SkillCache:
    """Per-workspace skill catalog cache keyed on file mtimes."""

    # Why: skill catalog scans can happen several times in one prompt build. How:
    # keep the old short time-based cache in the merged plugin. Purpose: avoid
    # extra filesystem reads without changing cache lifetime.
    _lock = threading.Lock()
    _entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _mtimes: dict[str, dict[str, float]] = {}
    _TTL = 2.0

    @classmethod
    def get(cls, workspace_root: Path) -> list[dict[str, Any]] | None:
        key = str(workspace_root)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]]) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)


def load_skill_catalog(workspace_root: Path, *, _use_cache: bool = True) -> list[dict[str, Any]]:
    """Scan ``skills/*/SKILL.md`` and return metadata + body for each skill."""
    # Why: skill scanning is now owned by the knowledge plugin. How: move the
    # former skill catalog loader here byte-for-byte except for local matcher
    # references. Purpose: keep skill injection output stable while removing the
    # old runtime module.
    if _use_cache:
        cached = _SkillCache.get(workspace_root)
        if cached is not None:
            return cached

    skills_dir = workspace_root / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta, body = parse_skill_frontmatter(text)
            name = str(meta.get("name") or skill_md.parent.name).strip() or skill_md.parent.name
            description = str(meta.get("description") or "").strip()
            enabled = bool(meta.get("enabled", True))

            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"

            raw_kw = meta.get("keywords")
            keywords: list[str] = []
            if isinstance(raw_kw, list):
                keywords = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            elif isinstance(raw_kw, str) and raw_kw.strip():
                keywords = [raw_kw.strip()]

            order = 0
            if isinstance(meta.get("order"), (int, float)):
                order = int(meta["order"])
            priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                priority = int(meta["priority"])
            scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                scan_depth = max(0, int(meta["scan_depth"]))

            raw_node_ids = meta.get("node_ids")
            node_ids: list[str] = []
            if isinstance(raw_node_ids, list):
                node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
            elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                node_ids = [raw_node_ids.strip()]

            items.append({
                "name": name,
                "description": description,
                "enabled": enabled,
                "path": skill_md.relative_to(workspace_root).as_posix(),
                "strategy": strategy,
                "keywords": keywords,
                "compiled_keywords": [compile_keyword(k) for k in keywords],
                "order": order,
                "priority": priority,
                "scan_depth": scan_depth,
                "body": body.strip(),
                "node_ids": node_ids,
            })
        except Exception as load_error:
            logger.warning("skill %s skipped, unreadable: %s", skill_md.parent.name, load_error)
            continue
    _SkillCache.put(workspace_root, items)
    return items


def build_skill_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    max_budget_chars: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for skill injection through the unified pipeline."""
    # Why: legacy callers need a compatible skill message builder after the old
    # runtime module is removed. How: delegate to the unified knowledge pipeline
    # with memory disabled. Purpose: preserve the public behavior while keeping
    # one injection implementation.
    skill_static, skill_dynamic, _memory_static, _memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_skill_entries(load_skill_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode="none",
        memory_allow=None,
        skill_max_budget_chars=max_budget_chars,
        memory_max_budget_chars=0,
        knowledge_max_budget_chars=0,
    )
    return skill_static, skill_dynamic


# ---------------------------------------------------------------------------
#  Memory catalog loading and legacy memory builder
# ---------------------------------------------------------------------------


class _MemoryCache:
    """Per-workspace memory catalog cache keyed on time."""

    # Why: memory catalog scans are now in this plugin, but prompt builds still
    # need the same short cache. How: preserve the old cache API including
    # invalidate(). Purpose: keep memory extraction and CRUD invalidation working.
    # [2026-05-28] cache key 从 str(workspace_root) 改为 (str(workspace_root), memory_book)。
    # 为什么：记忆 namespace 隔离后，不同 memory_book 的 catalog 互不相同。
    # 怎么改：所有方法增加 keyword-only memory_book 参数，cache key 改为元组。
    # 目的：不同 namespace 各自独立缓存，互不污染。
    _lock = threading.Lock()
    _entries: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
    _TTL = 2.0

    @classmethod
    def get(cls, workspace_root: Path, *, memory_book: str = "") -> list[dict[str, Any]] | None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]], *, memory_book: str = "") -> None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)

    @classmethod
    def invalidate(cls, workspace_root: Path, *, memory_book: str = "") -> None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            cls._entries.pop(key, None)


def memory_dir(workspace_root: Path, memory_book: str = "") -> Path:
    """Return the memory storage directory path.

    [2026-05-28] 增加 memory_book 参数支持 namespace 隔离。
    为什么：持久化子节点的记忆应隔离到独立子目录，避免互相污染。
    怎么改：memory_book 非空时返回 data/memory/{memory_book}/，否则返回 data/memory/。
    目的：save_memory 和 load_memory_catalog 共用此路径计算。
    """
    base = workspace_root / "data" / "memory"
    if memory_book:
        return base / memory_book
    return base



_MEMORY_NAMESPACE_RE = re.compile(r"^conv_[0-9a-f]{24}$")

# 记忆条目缺 scan_depth 时的扫描轮数。写入侧和载入侧共用同一个值，否则手工写的
# YAML 会退回「只看当前这一句」，而那正是召回率最大的那个洞。
_DEFAULT_SCAN_DEPTH = 2


def _conversation_memory_namespace(conversation_key: Any) -> str:
    """Return a stable per-conversation memory namespace without leaking raw ids."""
    raw = str(conversation_key or "").strip()
    if not raw:
        return ""
    # 已经是 namespace 就原样返回：dream 需要以目标会话的 namespace 为 conversation_key
    # 建任务，才能让 save_memory/delete_memory 落到那个会话的目录。真实 key 一律形如
    # <channel>:<id>，必带冒号，不会命中这里。
    if _MEMORY_NAMESPACE_RE.match(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"conv_{digest}"


def memory_owner_conversation_key(conversation_key: Any, routed_key: Any = "", parent_key: Any = "") -> str:
    """长期记忆归属哪个会话。

    dispatch 出去的子节点跑在 `agent:<节点>:<父key>` 会话里，fresh/fork 每次还额外拼一个
    新 uuid —— 按运行期 key 派生 namespace，子节点就是每次读一个空目录、写进一个用一次
    就废弃的目录，那些目录还会无人清理地堆下去。长期记忆本来跨会话，归发起它的会话。
    route_conversation_key 沿 dispatch 链恒为根会话的 key，多层委派也不会跑偏。
    """
    own = str(conversation_key or "").strip()
    if not own.startswith("agent:"):
        return own
    return str(routed_key or "").strip() or str(parent_key or "").strip() or own


def _context_memory_namespace(node: Node | None = None, task_context: dict[str, Any] | None = None) -> str:
    """Resolve memory namespace: explicit node memory_book wins; otherwise the owning conversation."""
    extra = getattr(node, "extra", {}) if node is not None else {}
    explicit = str((extra or {}).get("memory_book") or "").strip()
    if explicit:
        return explicit
    ctx = task_context if isinstance(task_context, dict) else {}
    return _conversation_memory_namespace(memory_owner_conversation_key(
        ctx.get("conversation_key"),
        ctx.get("route_conversation_key"),
        ctx.get("parent_conversation_key"),
    ))


def _tool_memory_namespaces(ctx: ToolContext, subject: str = "") -> list[str]:
    """Namespaces a memory tool may address, most specific first.

    三个工具（save / list / delete）此前各写一份推导，优先级还不一样：save 认 subject，
    另两个不认，于是按人档案写得进删不掉。这里定死顺序 —— 已建档的 subject > 节点
    显式 memory_book > 会话派生 namespace。[0] 是新建时落哪，其余只用于定位已有条目。
    """
    namespaces: list[str] = []
    text = str(subject or "").strip()
    if text and _subject_is_enrolled(ctx.workspace_root, text):
        ns = subject_namespace(text)
        if ns:
            namespaces.append(ns)
    node_extra = getattr(ctx, "_node_extra", None) or {}
    explicit = str(node_extra.get("memory_book") or "").strip()
    # 写入侧必须和注入侧算出同一个 namespace，否则子节点写进的目录下一轮读不到。
    namespaces.append(explicit or _conversation_memory_namespace(memory_owner_conversation_key(
        getattr(ctx, "conversation_key", ""),
        getattr(ctx, "_route_conversation_key", ""),
        getattr(ctx, "_parent_conversation_key", ""),
    )))
    # 本轮被注入档案的人也要能被寻址：模型看得见那些条目，却在不重复给 subject 时
    # 更新不到它们，那次更新会落进会话 namespace 再被读侧去重吃掉。
    for hinted in resolve_load_subjects(ctx.workspace_root, getattr(ctx, "_memory_subjects", None)):
        ns = subject_namespace(hinted)
        if ns and ns not in namespaces:
            namespaces.append(ns)
    return namespaces


def _locate_memory_entry(
    workspace_root: Path, namespaces: list[str], book: str, mid: str,
) -> tuple[str, Path | None, dict[str, Any] | None, int]:
    """Find an existing (book, id) entry across candidate namespaces.

    subject 一旦建档，同一个 id 会既在会话 namespace 又在档案 namespace 出现两份，
    注入时同一条被渲染两遍。定位到就在原处更新，namespace 只在新建时才由 subject 决定。
    """
    for namespace in namespaces:
        path = memory_dir(workspace_root, namespace) / f"{book}.yaml"
        if not path.exists():
            continue
        data = _load_book(path)
        for index, entry in enumerate(data.get("entries", [])):
            if isinstance(entry, dict) and str(entry.get("id") or "").strip() == mid:
                return namespace, path, data, index
    return "", None, None, -1


_AUTO_SOURCE = "auto"


def _memory_source() -> str:
    """工具写的一律 auto。人只能从控制台写，那条路径由 memory_admin 打 manual。

    曾按节点名前缀判定，于是 qq.orchestrator 写的全成了 manual —— bot 自己记的
    东西自己删不掉、dream 也清不掉，只能靠新增一条「上面那条错了」打补丁。
    """
    return _AUTO_SOURCE


def _is_tool_writable(entry: dict[str, Any]) -> bool:
    """工具只动得了自己写的。空 source 按手工保护：早期 save_memory 不写这个字段。"""
    return str(entry.get("source") or "").strip() == _AUTO_SOURCE


def load_memory_catalog(
    workspace_root: Path,
    *,
    memory_book: str = "",
    _use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Scan memory yaml files and return parsed entries.

    [2026-05-28] 增加 memory_book 参数支持 namespace 隔离。
    为什么：持久化子节点的记忆应存储在独立子目录，与主节点互不干扰。
    怎么改：memory_book 非空时扫 data/memory/{memory_book}/*.yaml，
    否则扫 data/memory/*.yaml（现有行为，不递归）。
    缓存 key 用 (workspace_root, memory_book) 元组区分。
    """
    if _use_cache:
        cached = _MemoryCache.get(workspace_root, memory_book=memory_book)
        if cached is not None:
            return cached

    mem_dir = memory_dir(workspace_root, memory_book)
    if not mem_dir.exists() or not mem_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            text = yaml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                continue
            book = str(data.get("book") or yaml_path.stem).strip()
            entries = data.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                if not entry.get("enabled", True):
                    continue

                content = str(entry.get("content") or "").strip()
                if not content:
                    continue

                raw_kw = entry.get("keywords")
                keywords: list[str] = []
                if isinstance(raw_kw, list):
                    keywords = [
                        str(k).strip()
                        for k in raw_kw
                        if isinstance(k, str) and str(k).strip()
                    ]
                elif isinstance(raw_kw, str) and raw_kw.strip():
                    keywords = [raw_kw.strip()]

                constant = bool(entry.get("constant", False))
                priority = 0
                if isinstance(entry.get("priority"), (int, float)):
                    priority = int(entry["priority"])
                scan_depth = _DEFAULT_SCAN_DEPTH
                if isinstance(entry.get("scan_depth"), (int, float)):
                    scan_depth = max(0, int(entry["scan_depth"]))

                # Why: old memory books accepted node_ids as either a list or a
                # comma-separated string. How: keep the same coercion. Purpose:
                # node-scoped memories continue to load without migration.
                raw_node_ids = entry.get("node_ids")
                node_ids: list[str] = []
                if isinstance(raw_node_ids, list):
                    node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
                elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                    node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

                source = str(entry.get("source", "")).strip()
                created_at = str(entry.get("created_at", "")).strip()
                last_hit_at = str(entry.get("last_hit_at", "")).strip()

                items.append({
                    "book": book,
                    "id": eid,
                    "keywords": keywords,
                    "compiled_keywords": [compile_keyword(k) for k in keywords],
                    "content": content,
                    "constant": constant,
                    "priority": priority,
                    "scan_depth": scan_depth,
                    "node_ids": node_ids,
                    "source": source,
                    "created_at": created_at,
                    "last_hit_at": last_hit_at,
                })
        except Exception as load_error:
            # 一个 YAML 语法错误原来会让整本记忆凭空消失，零日志零告警 —— 手工编辑
            # 记忆本时打错一个冒号，那一整本就此不再被注入，而且无从察觉。
            # 用 warning 而非 error：编辑期间每轮 prompt build 都会经过这里。
            logger.warning("memory book %s skipped, unreadable: %s", yaml_path.name, load_error)
            continue

    _MemoryCache.put(workspace_root, items, memory_book=memory_book)
    return items


def build_memory_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    max_budget_chars: int = 0,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for memory injection through the unified pipeline."""
    # Why: legacy callers still need build_memory_messages after the old memory
    # module is deleted. How: delegate to the unified knowledge pipeline with skills
    # disabled. Purpose: keep public memory message behavior compatible.
    _skill_static, _skill_dynamic, memory_static, memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_memory_entries(load_memory_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode="none",
        skill_allow=None,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
        skill_max_budget_chars=0,
        memory_max_budget_chars=max_budget_chars,
        knowledge_max_budget_chars=0,
    )
    return memory_static, memory_dynamic

def _short_text(s: str, max_chars: int = 240) -> str:
    """Return the same short skill description used by the legacy index."""
    # Why: skill INDEX rendering moved into this module. How: keep the old
    # truncation helper byte-compatible. Purpose: avoid changing INDEX text.
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def _as_string_list(value: Any) -> list[str]:
    """Normalize loose catalog values into a list of non-empty strings."""
    # Why: catalogs are loaded from YAML/frontmatter and may contain either a
    # scalar or a list. How: mirror the existing loader coercion rules. Purpose:
    # let the unified pipeline accept both raw and already-normalized catalogs.
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_int(value: Any, default: int = 0) -> int:
    """Return integer metadata while preserving legacy numeric-only coercion."""
    # Why: priority, order, and scan_depth were accepted only when YAML parsed
    # them as numbers. How: keep that rule here instead of parsing arbitrary
    # strings. Purpose: prevent a subtle behavior change during normalization.
    if isinstance(value, (int, float)):
        return int(value)
    return default


def normalize_skill_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map skill catalog entries to the unified knowledge-entry dictionary."""
    # Why: skill and memory now share filtering, matching, budgeting, and
    # rendering orchestration. How: convert skill-specific fields into the
    # common Entry shape while preserving skill-only render metadata. Purpose:
    # make global skill/memory budget selection possible without merging storage.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        keywords = _as_string_list(raw.get("keywords"))
        raw_strategy = str(raw.get("strategy") or "normal").strip().lower()
        if raw_strategy == "constant":
            strategy = "constant"
        elif keywords:
            strategy = "keyword"
        else:
            strategy = "index"

        name = str(raw.get("name") or raw.get("id") or "").strip()
        if not name:
            continue

        entries.append({
            "id": name,
            "kind": "skill",
            "content": str(raw.get("body") or raw.get("content") or ""),
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": _as_int(raw.get("order"), 0),
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), 0)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": str(raw.get("description") or ""),
            "path": str(raw.get("path") or ""),
            "book": "",
            "source": "",
            "created_at": "",
            "last_hit_at": "",
        })
    return entries


def normalize_memory_entries(
    catalog: list[dict[str, Any]], *, always_active: bool = False, namespace: str = "",
) -> list[dict[str, Any]]:
    """Map memory catalog entries to the unified knowledge-entry dictionary.

    *always_active* 用于按人加载的档案：那批条目的召回理由是「这轮提到了这个人」，
    不该再要求关键词命中，也不该因为没写关键词就被丢掉。
    """
    # Why: memory injection should use the same pipeline as skills, but memory
    # has no discovery INDEX. How: map constant memories to constant entries,
    # keyword memories to keyword entries, and skip keywordless non-constant
    # memories. Purpose: preserve existing memory visibility exactly.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        memory_id = str(raw.get("id") or "").strip()
        content = str(raw.get("content") or "").strip()
        if not memory_id or not content:
            continue

        keywords = _as_string_list(raw.get("keywords"))
        if bool(raw.get("constant", False)):
            strategy = "constant"
        elif always_active:
            strategy = "subject"
        elif keywords:
            strategy = "keyword"
        else:
            continue

        entries.append({
            "id": memory_id,
            "kind": "memory",
            "content": content,
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": 0,
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), _DEFAULT_SCAN_DEPTH)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": "",
            "path": "",
            "book": str(raw.get("book") or ""),
            "namespace": namespace,
            "source": str(raw.get("source") or ""),
            "subject": str(raw.get("subject") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "last_hit_at": str(raw.get("last_hit_at") or ""),
        })
    return entries


def _allow_set(allow: list[str] | None) -> set[str] | None:
    """None = 调用方没声明限制；空 list = 声明了一个空白名单（因此全禁）。"""
    return set(allow) if allow is not None else None


def _filter_entries(
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply node visibility and node-level skill/memory access rules.

    `_allow_set` 的 None 与空集必须分开：memory 侧原来用 `if memory_allow` 判定，
    而空 list 是 falsy —— 于是 `memories: {mode: allowlist}` 不写 allow 反而全部放行，
    和 `skills:` 完全相同的配置形状语义相反。作者以为写了 allowlist 是收紧，实际是敞开。
    """
    # Why: both old builders performed the same node_id and allowlist filtering
    # in separate files. How: branch only on kind-specific access semantics.
    # Purpose: give the unified budget stage one already-authorized entry list.
    filtered: list[dict[str, Any]] = []
    skill_allow_set = _allow_set(skill_allow)
    memory_allow_set = _allow_set(memory_allow)

    for entry in entries:
        node_ids = entry.get("node_ids") or []
        if node_id and node_ids and node_id not in node_ids:
            continue

        if entry.get("kind") == "skill":
            if skill_mode == "none":
                continue
            if skill_mode == "allowlist" and skill_allow_set is not None and entry.get("id") not in skill_allow_set:
                continue
        elif entry.get("kind") == "memory":
            if memory_mode == "none":
                continue
            if memory_mode == "allowlist" and memory_allow_set is not None and entry.get("book") not in memory_allow_set:
                continue
        else:
            continue
        filtered.append(entry)
    return filtered


def _new_buckets() -> dict[str, list[dict[str, Any]]]:
    """Create render buckets shared by budget and rendering stages."""
    # Why: prompt layout still needs separate skill/static, skill/dynamic,
    # memory/static, and memory/dynamic messages. How: keep explicit buckets
    # after unified matching. Purpose: unify selection without changing labels.
    return {
        "skill_constant": [],
        "skill_active": [],
        "skill_index": [],
        "memory_constant": [],
        "memory_active": [],
    }


def _classify_and_match_entries(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify entries and run keyword activation for keyword entries."""
    # Why: the old builders both used constant/keyword/index phases. How: run
    # the same phases over unified entries and route results back to render
    # buckets by kind. Purpose: make later budgeting global while preserving
    # memory's lack of an INDEX block.
    buckets = _new_buckets()
    matched_memories: list[dict[str, Any]] = []

    for entry in entries:
        kind = entry.get("kind")
        strategy = entry.get("strategy")
        if strategy == "constant":
            if kind == "skill":
                buckets["skill_constant"].append(entry)
            elif kind == "memory":
                buckets["memory_constant"].append(entry)
            continue

        # 按人加载的档案：召回理由是「这轮提到了这个人」，无需再命中关键词。
        if strategy == "subject" and kind == "memory":
            buckets["memory_active"].append(entry)
            matched_memories.append(entry)
            continue

        if strategy == "keyword":
            scan_text = build_scan_text(instruction_text, history, int(entry.get("scan_depth") or 0))
            if match_keywords(list(entry.get("compiled_keywords") or []), scan_text):
                if kind == "skill":
                    buckets["skill_active"].append(entry)
                elif kind == "memory":
                    buckets["memory_active"].append(entry)
                    matched_memories.append(entry)
            elif kind == "skill":
                buckets["skill_index"].append(entry)
            continue

        if strategy == "index" and kind == "skill":
            buckets["skill_index"].append(entry)

    if matched_memories:
        record_hits(workspace_root, matched_memories)

    return buckets


def _sort_render_buckets(buckets: dict[str, list[dict[str, Any]]]) -> None:
    """Sort buckets exactly as the legacy renderers did before budgeting."""
    # Why: prompt cache stability depends on deterministic ordering. How: sort
    # skills by (order, id) and memories by id, matching the old builders.
    # Purpose: keep render order stable after moving logic into one module.
    for key in ("skill_constant", "skill_active", "skill_index"):
        buckets[key].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    for key in ("memory_constant", "memory_active"):
        buckets[key].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_skill_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply the legacy skill-only budget to skill injectable buckets."""
    # Why: knowledge.max_budget_chars=0 must use the old independent skill
    # budget. How: select constant and active skills by priority and append
    # rejected skills to INDEX. Purpose: preserve both injected content and
    # discovery behavior for existing configs.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    for entry in buckets["skill_constant"]:
        all_injectable.append(("skill_constant", entry))
    for entry in buckets["skill_active"]:
        all_injectable.append(("skill_active", entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept_constant: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            if origin == "skill_constant":
                kept_constant.append(entry)
            else:
                kept_active.append(entry)
        else:
            buckets["skill_index"].append(entry)

    buckets["skill_constant"] = kept_constant
    buckets["skill_active"] = kept_active
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))


# constant 吃光预算时仍留给召回条目的最低额度。没有这条，一条超长的常驻记忆
# 就能让「@ 某人加载他的档案」整个功能失效，而外部只看到 bot 不记得人。
_MEMORY_ACTIVE_FLOOR_RATIO = 0.25


def _subject_scores(subjects: list[str] | None) -> dict[str, int]:
    """本轮 subject 的排名换成分数，发起人（下标 0）最高。

    collect_subjects 把发起人排在最前，理由就是「预算裁剪时他的档案最该留下」——
    不把这个次序带到排序里，那句承诺就是空的。
    """
    items = [str(alias or "").strip() for alias in (subjects or [])]
    return {alias: len(items) - idx for idx, alias in enumerate(items) if alias}


def _memory_budget_rank(
    entry: dict[str, Any], subject_scores: dict[str, int] | None = None,
) -> tuple[int, int, str]:
    """Rank memories for budget survival: priority, then this turn's subject, then freshness."""
    # 同优先级下先看本轮涉及谁，再按新旧：按人加载会一次调出一个人的全部档案，
    # 光靠 priority 分不出高下，而越近写下的越可能是当前有效的说法。
    freshness = str(entry.get("updated_at") or entry.get("created_at") or "")
    subject = str(entry.get("subject") or "").strip()
    return (
        int(entry.get("priority") or 0),
        (subject_scores or {}).get(subject, 0),
        freshness,
    )


def _apply_memory_budget(
    buckets: dict[str, list[dict[str, Any]]],
    max_budget_chars: int,
    subjects: list[str] | None = None,
) -> None:
    """Apply the legacy memory-only budget to memory injectable buckets."""
    # Why: the default path must keep memory's old independent budget. How:
    # select constant and active memories by priority and drop rejected memories
    # because memories never had an INDEX. Purpose: preserve existing memory
    # prompt output when no global knowledge budget is configured.
    if max_budget_chars <= 0:
        return

    # constant 条目的工具描述写的是「总是注入」，让它们参与淘汰等于说话不算数，
    # 所以先无条件收下，剩下的预算才给按关键词/按人召回的条目分。
    kept_constant = list(buckets["memory_constant"])
    constant_chars = sum(len(str(entry.get("content") or "")) for entry in kept_constant)
    active_budget = max(
        max_budget_chars - constant_chars,
        int(max_budget_chars * _MEMORY_ACTIVE_FLOOR_RATIO),
    )
    if constant_chars > max_budget_chars:
        # 配置问题而不是运行时波动：常驻记忆总量本身就超了预算，注入量注定越界。
        logger.warning(
            "memory budget: constant entries alone need %d chars of a %d budget; "
            "recalled entries keep a %d char floor",
            constant_chars, max_budget_chars, active_budget,
        )

    scores = _subject_scores(subjects)
    kept_active: list[dict[str, Any]] = []
    used = 0
    dropped = 0
    # 按优先级降序、装不下即停：旧实现是 first-fit（跳过装不下的继续往后找），
    # 于是一条高优先级的长记忆会被丢掉，换三条无关紧要的短记忆进来。
    for entry in sorted(
        buckets["memory_active"], key=lambda e: _memory_budget_rank(e, scores), reverse=True,
    ):
        body_len = len(str(entry.get("content") or ""))
        if used + body_len > active_budget:
            dropped = len(buckets["memory_active"]) - len(kept_active)
            break
        used += body_len
        kept_active.append(entry)

    if dropped:
        # 记忆没有 INDEX 兜底，被丢掉就是彻底隐形，至少让日志能对上账。
        logger.info(
            "memory budget dropped %d of %d active entries (budget=%d chars)",
            dropped, len(buckets["memory_active"]), active_budget,
        )

    buckets["memory_constant"] = kept_constant
    buckets["memory_active"] = kept_active
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_global_budget(
    buckets: dict[str, list[dict[str, Any]]],
    max_budget_chars: int,
    subjects: list[str] | None = None,
) -> None:
    """Apply one priority-sorted budget pool across skills and memories."""
    # Why: Phase 3 requires high-priority memories and skills to compete in one
    # pool. How: rank all injectable entries by priority while remembering their
    # render bucket, then restore kept entries to their original labels. Purpose:
    # change budget selection without changing final tag names or prompt layout.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    # Why: equal-priority ties need deterministic behavior. How: start from the
    # prompt render order before the stable priority sort. Purpose: avoid random
    # output while still making priority the only cross-kind ranking key.
    for bucket_name in ("skill_constant", "memory_constant", "skill_active", "memory_active"):
        for entry in buckets[bucket_name]:
            all_injectable.append((bucket_name, entry))
    scores = _subject_scores(subjects)
    # constant 排在同优先级的 active 之前，再按本轮涉及谁、最后按新旧 —— 与独立
    # memory 预算用同一套判据，两条路径不能对「谁该留」给出不同答案。
    all_injectable.sort(
        key=lambda item: (
            int(item[1].get("priority") or 0),
            1 if item[0].endswith("_constant") else 0,
            *_memory_budget_rank(item[1], scores)[1:],
        ),
        reverse=True,
    )

    kept: dict[str, list[dict[str, Any]]] = {
        "skill_constant": [],
        "skill_active": [],
        "memory_constant": [],
        "memory_active": [],
    }
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            kept[origin].append(entry)
        elif origin.startswith("skill_"):
            # Why: over-budget skill bodies used to remain discoverable through
            # SKILLS:INDEX. How: append rejected injectable skills to the index
            # bucket. Purpose: global budgeting does not hide skill metadata.
            buckets["skill_index"].append(entry)

    for bucket_name, entries in kept.items():
        buckets[bucket_name] = entries
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _render_skill_messages(
    constant_skills: list[dict[str, Any]],
    dynamic_skills: list[dict[str, Any]],
    index_only_skills: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render skill buckets using the legacy SKILLS tags."""
    # Why: callers and tests depend on exact SKILLS tag names and section
    # structure. How: copy the old renderer's joins and header strings while
    # reading unified entry fields. Purpose: make the refactor output-compatible.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_skills:
        parts: list[str] = ["[SKILLS:CONSTANT]"]
        for entry in constant_skills:
            parts.append(f"\n## Skill: {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/SKILLS:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_skills:
        dynamic_parts.append("[SKILLS:ACTIVE]")
        for entry in dynamic_skills:
            dynamic_parts.append(f"\n## Skill: {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/SKILLS:ACTIVE]")

    if index_only_skills:
        if dynamic_parts:
            dynamic_parts.append("")
        dynamic_parts.append("[SKILLS:INDEX]")
        dynamic_parts.append(
            "以下 skill 未被激活。如果当前任务需要，可通过 read_file 读取对应 path 的全文。"
        )
        for entry in index_only_skills:
            dynamic_parts.append(f"- name: {entry['id']}")
            dynamic_parts.append(f"  description: {_short_text(str(entry.get('description') or ''))}")
            dynamic_parts.append(f"  path: {entry.get('path') or ''}")
        dynamic_parts.append("[/SKILLS:INDEX]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def _render_memory_messages(
    constant_entries: list[dict[str, Any]],
    dynamic_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render memory buckets using the legacy MEMORY tags."""
    # Why: memories did not have an INDEX block and used different section
    # headers from skills. How: keep the old MEMORY renderer's exact tag names
    # and joins. Purpose: preserve prompt text for existing memory users.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_entries:
        parts: list[str] = ["[MEMORY:CONSTANT]"]
        for entry in constant_entries:
            parts.append(f"\n## {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/MEMORY:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_entries:
        dynamic_parts.append("[MEMORY:ACTIVE]")
        for entry in dynamic_entries:
            dynamic_parts.append(f"\n## {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/MEMORY:ACTIVE]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def build_knowledge_messages(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
    skill_max_budget_chars: int = 0,
    memory_max_budget_chars: int = 0,
    knowledge_max_budget_chars: int = 0,
    memory_subjects: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Build skill and memory messages from already-normalized entries."""
    # Why: build_knowledge_context and legacy wrappers need the same pipeline.
    # How: filter, classify, match, budget, and render unified entries in one
    # helper. Purpose: avoid reintroducing divergent skill and memory behavior.
    filtered_entries = _filter_entries(
        entries,
        node_id=node_id,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
    )
    if not filtered_entries:
        return [], [], [], []

    buckets = _classify_and_match_entries(
        workspace_root,
        filtered_entries,
        instruction_text=instruction_text,
        history=history,
    )
    _sort_render_buckets(buckets)

    if knowledge_max_budget_chars > 0:
        _apply_global_budget(buckets, knowledge_max_budget_chars, memory_subjects)
    else:
        _apply_skill_budget(buckets, skill_max_budget_chars)
        _apply_memory_budget(buckets, memory_max_budget_chars, memory_subjects)

    skill_static, skill_dynamic = _render_skill_messages(
        buckets["skill_constant"],
        buckets["skill_active"],
        buckets["skill_index"],
    )
    memory_static, memory_dynamic = _render_memory_messages(
        buckets["memory_constant"],
        buckets["memory_active"],
    )
    return skill_static, skill_dynamic, memory_static, memory_dynamic


def _turn_memory_subjects(
    workspace_root: Path, task_context: dict[str, Any] | None,
) -> list[str]:
    """本轮涉及的、有档案的人，保持适配层给出的顺序（发起人在最前）。"""
    ctx = task_context if isinstance(task_context, dict) else {}
    hints = ctx.get("memory_hints")
    requested = hints.get("subjects") if isinstance(hints, dict) else None
    return resolve_load_subjects(workspace_root, requested)


def _load_subject_memory_entries(
    workspace_root: Path, task_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Load the full memory archive of everyone this turn involves.

    「@ 了某人」本身就是意图信号，所以这些条目不再走关键词匹配 —— 关键词只能命中
    当前这一句字面出现的词，而问「他电话多少」时那个人的名字并不在句子里。
    档案跨会话共享，因此只对名册里的人生效（被提到但没主动找过 bot 的人没有档案）。
    """
    subjects = _turn_memory_subjects(workspace_root, task_context)
    if not subjects:
        return []

    entries: list[dict[str, Any]] = []
    for subject in subjects:
        namespace = subject_namespace(subject)
        if not namespace:
            continue
        loaded = normalize_memory_entries(
            load_memory_catalog(workspace_root, memory_book=namespace),
            always_active=True,
            namespace=namespace,
        )
        for entry in loaded:
            entry["subject"] = subject
        entries.extend(loaded)
    return entries


def _merge_subject_archives(
    conversation_entries: list[dict[str, Any]], subject_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge per-person archives into conversation memory, one copy per (book, id).

    建档前写在会话 namespace、建档后写在档案 namespace 的同一条记忆会被渲染两遍。
    冲突时留档案那份 —— 它才是跨会话共享、被 dream 整理过的那条。id 的唯一性是
    per-book 的，所以去重键必须带上 book。
    """
    archived = {
        (str(entry.get("book") or ""), str(entry.get("id") or ""))
        for entry in subject_entries
    }
    merged = [
        entry for entry in conversation_entries
        if (str(entry.get("book") or ""), str(entry.get("id") or "")) not in archived
    ]
    merged.extend(subject_entries)
    return merged


def build_knowledge_context(
    workspace_root: Path,
    node: Node,
    instruction_text: str,
    history: list[dict],
    runtime_cfg: dict,
    task_context: dict[str, Any] | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return ``(skill_static, skill_dynamic, memory_static, memory_dynamic)``.

    Why: inference and preempt paths must not know how skill and memory builders
    are wired. How: load both catalogs, normalize them into one Entry shape, and
    run one filtering/matching/budget/render pipeline. Purpose: support global
    skill/memory budgeting while keeping the public context boundary stable.
    """
    safe_runtime_cfg = runtime_cfg or {}

    # Why: the knowledge plugin now owns both storage loaders and the unified
    # injection pipeline. How: load skill and memory catalogs locally, normalize
    # them into one Entry shape, and render through build_knowledge_messages.
    # Purpose: keep prompt injection behavior stable while deleting the old files.
    entries = normalize_skill_entries(load_skill_catalog(workspace_root))
    # [2026-06-17] conversation_key 级 memory 隔离：默认每个外部会话读取自己的
    # data/memory/conv_<hash>/ 命名空间；节点显式配置 memory_book 时仍优先使用
    # 该节点命名空间。目的：避免私聊/群聊/不同群之间的长期记忆串用和隐私泄露。
    _mb = _context_memory_namespace(node, task_context)
    entries.extend(_merge_subject_archives(
        normalize_memory_entries(load_memory_catalog(workspace_root, memory_book=_mb), namespace=_mb),
        _load_subject_memory_entries(workspace_root, task_context),
    ))

    return build_knowledge_messages(
        workspace_root,
        entries,
        node_id=node.id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
        memory_mode=node.memory_access.mode,
        memory_allow=node.memory_access.allow,
        skill_max_budget_chars=get_int(safe_runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
        memory_max_budget_chars=get_int(safe_runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
        knowledge_max_budget_chars=get_int(safe_runtime_cfg, "knowledge.max_budget_chars", 0, min_value=0),
        memory_subjects=_turn_memory_subjects(workspace_root, task_context),
    )



# ---------------------------------------------------------------------------
#  Skill CRUD tools
# ---------------------------------------------------------------------------


def _tool_ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: knowledge tools return paths, ids, and catalog lists
    # that should live under data while data.result remains readable. How: use one
    # helper for skill and memory success payloads. Purpose: keep plugin-owned tools
    # aligned with the ok/data/error response schema.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _tool_err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: knowledge-tool validation and guard failures should
    # expose data.result. How: wrap error text and mirror optional fields under data
    # and top level. Purpose: keep failures readable and migration-compatible.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


async def create_or_update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a skill under skills/<name>/SKILL.md."""
    # Why: skill management tools now live beside skill loading and injection.
    # How: move the old skill-tool implementation here and keep the
    # async tool signature unchanged. Purpose: let PLUGIN_META register the tool
    # without hard-coding it in toolbox.registry.py.
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    content = args.get("content")
    enabled = bool(args.get("enabled", True))

    strategy = str(args.get("strategy", "")).strip().lower() or None
    raw_keywords = args.get("keywords")
    keywords: list[str] | None = None
    if isinstance(raw_keywords, list):
        keywords = [str(k).strip() for k in raw_keywords if isinstance(k, str) and str(k).strip()]

    order: int | None = None
    if args.get("order") is not None:
        try:
            order = int(args["order"])
        except (TypeError, ValueError):
            pass
    priority: int | None = None
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass
    scan_depth: int | None = None
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    if not name:
        return _tool_err("empty skill name")
    if not SKILL_NAME_RE.fullmatch(name):
        return _tool_err("invalid skill name: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed")

    path = f"skills/{name}/SKILL.md"
    if not isinstance(content, str) or not content.strip():
        meta: dict[str, Any] = {
            "name": name,
            "description": description,
            "enabled": enabled,
        }
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        body = description or f"Skill {name}"
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"
    else:
        meta, body = parse_skill_frontmatter(content)
        if not isinstance(meta, dict):
            meta = {}
        meta["name"] = name
        if description:
            meta["description"] = description
        elif not isinstance(meta.get("description"), str):
            meta["description"] = ""
        meta["enabled"] = enabled
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"

    # Why: write_file already centralizes policy approval and path checks. How:
    # import it lazily from toolbox.builtins after moving this function out of that
    # package. Purpose: preserve the guarded write behavior without a module cycle.
    from toolbox.builtins.write_file import write_file

    res = await write_file({"path": path, "content": content}, ctx)
    if not res.get("ok"):
        return res
    return _tool_ok(f"Skill written: {path}", path=path, name=name, enabled=enabled)


async def list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List local skills under skills/*/SKILL.md."""
    # Why: the listing tool was moved with the skill parser. How: keep the same
    # filesystem scan and metadata coercion. Purpose: preserve tool output shape.
    skills_dir = ctx.workspace_root / "skills"
    if not skills_dir.exists():
        return _tool_ok("0 skills", skills=[])

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            rel = skill_md.relative_to(ctx.workspace_root).as_posix()
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = parse_skill_frontmatter(text)
            if not isinstance(meta, dict):
                meta = {}
            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"
            raw_kw = meta.get("keywords")
            kw_list: list[str] = []
            if isinstance(raw_kw, list):
                kw_list = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            item_order = 0
            if isinstance(meta.get("order"), (int, float)):
                item_order = int(meta["order"])
            item_priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                item_priority = int(meta["priority"])
            item_scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                item_scan_depth = max(0, int(meta["scan_depth"]))
            items.append(
                {
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "strategy": strategy,
                    "keywords": kw_list,
                    "order": item_order,
                    "priority": item_priority,
                    "scan_depth": item_scan_depth,
                    "path": rel,
                }
            )
        except Exception as e:
            items.append({"name": skill_md.parent.name, "path": skill_md.relative_to(ctx.workspace_root).as_posix(), "error": str(e)})

    return _tool_ok(f"{len(items)} skills", skills=items)


async def delete_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a skill directory under skills/<name>/."""
    # Why: delete_skill is now plugin-owned like the other knowledge tools. How:
    # keep the old guard and shutil.rmtree behavior. Purpose: preserve approval
    # and deletion semantics while removing the old skill-tool file.
    name = str(args.get("name", "")).strip()
    if not name:
        return _tool_err("empty skill name")
    if not SKILL_NAME_RE.fullmatch(name):
        return _tool_err("invalid skill name")

    skill_dir = resolve_under_allowed_roots(ctx.workspace_root, f"skills/{name}")
    if not skill_dir.exists():
        return _tool_err(f"skill not found: {name}")
    if not skill_dir.is_dir():
        return _tool_err(f"not a skill directory: {name}")

    _op, err = await request_guard(ctx, "write_file", {"path": f"skills/{name}/SKILL.md", "delete": True})
    if err is not None:
        return _tool_err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    shutil.rmtree(skill_dir)
    return _tool_ok(f"Skill deleted: {name}", deleted=True, name=name)


# ---------------------------------------------------------------------------
#  Memory CRUD tools
# ---------------------------------------------------------------------------

_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_BOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _load_book(path: Path) -> dict[str, Any]:
    """Load a memory book yaml. Returns default structure if missing."""
    # Why: memory CRUD moved into the injection plugin with the cache. How: keep
    # the same tolerant YAML load and default book structure. Purpose: avoid
    # changing how malformed or missing memory books are handled.
    if not path.exists():
        return {"book": path.stem, "entries": []}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception:
        return {"book": path.stem, "entries": []}
    if not isinstance(data, dict):
        return {"book": path.stem, "entries": []}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _save_book(path: Path, data: dict[str, Any]) -> None:
    """Write a memory book yaml back to disk, atomically."""
    # 直接 write_text 写一半就崩，留下的半截 YAML 会被 _load_book 静默当成空本 ——
    # 整本记忆无声消失。先写同目录 tmp 再 replace，读者只会看到旧本或新本。
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    tmp = path.with_name(f"{path.name}.{_os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _os.replace(tmp, path)


def _memory_write_lock(workspace_root: Path):
    """save/delete 的读-改-写临界区锁。

    两个 engine worker 各跑一次记忆提取就能撞上：都读到同一份 entries，各自加一条，
    后写的把前一条整段覆盖。粒度取整个 memory 根目录 —— 具体落哪本要等寻址完才知道，
    按 book 加锁等于先探测再加锁，反倒多开一个竞态窗口。
    """
    lock_path = workspace_root / "data" / "memory" / ".write"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return eventlog_file_lock(lock_path)


def _invalidate_cache(workspace_root: Path, *, memory_book: str = "") -> None:
    """Clear the memory cache so the next prompt build picks up changes.

    [2026-05-28] 增加 memory_book 参数，与 _MemoryCache.invalidate 对齐。
    为什么：namespace 隔离后 cache key 包含 memory_book，invalidate 也需指定。
    目的：精确清除对应 namespace 的缓存。
    """
    try:
        _MemoryCache.invalidate(workspace_root, memory_book=memory_book)
    except Exception:
        pass


async def save_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a memory entry in a book."""
    # Why: memory management tools now live beside memory loading and injection.
    # How: move the old memory-tool implementation here and keep the
    # async tool signature unchanged. Purpose: let PLUGIN_META register the tool
    # without hard-coding it in toolbox.registry.py.
    mid = str(args.get("id") or "").strip()
    if not mid:
        return _tool_err("empty memory id")
    if not _MEMORY_ID_RE.fullmatch(mid):
        return _tool_err("invalid id: only [A-Za-z0-9][A-Za-z0-9_.-]{0,127} allowed")

    # [2026-05-27] memory_book namespace 支持：当节点 yaml 配置了 memory_book 时，
    # 没有显式指定 book 的 save_memory 调用将使用节点配置的默认 book 名称。
    # 为什么：持久节点的记忆应隔离到独立的 namespace，避免互相污染。
    # 怎么改：从 ToolContext._node_extra dict 零 IO 读取 memory_book，
    #   extra 由 Node.load_node 在加载 yaml 时一次性收集，通过 ai_step 传入。
    # 目的：插件层零 IO 拿到业务配置，引擎核心不知道具体字段的存在。
    _user_book = str(args.get("book") or "").strip()
    if _user_book:
        book = _user_book
    else:
        # 从 Node.extra dict 零 IO 读取 memory_book
        _extra = getattr(ctx, "_node_extra", None) or {}
        book = str(_extra.get("memory_book") or "").strip() or "default"
    if not _BOOK_NAME_RE.fullmatch(book):
        return _tool_err("invalid book name")

    content = str(args.get("content") or "").strip()
    if not content:
        return _tool_err("empty content")

    raw_keywords = args.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        keywords = [
            str(k).strip()
            for k in raw_keywords
            if isinstance(k, str) and str(k).strip()
        ]
    elif isinstance(raw_keywords, str) and raw_keywords.strip():
        keywords = [raw_keywords.strip()]

    constant = bool(args.get("constant", False))
    enabled = bool(args.get("enabled", True))

    # Why: existing memory tools accepted node_ids as a list or comma-separated
    # string. How: preserve both forms. Purpose: node-scoped memory entries keep
    # their tool API compatibility.
    raw_node_ids = args.get("node_ids")
    node_ids: list[str] = []
    if isinstance(raw_node_ids, list):
        node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
    elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
        node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

    priority = 0
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass

    # 默认 0 等于「只看当前这一句」：以「张三」为关键词的记忆在下一句「他电话多少」
    # 上不会激活。提取器的提示词从不提这个参数，所以默认值就是实际值。
    scan_depth = _DEFAULT_SCAN_DEPTH
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    # subject 指定这条记忆「关于谁」，落到跨会话的 user_<alias>/ 档案。名册之外的人
    # 一律退回会话 namespace：被别人提到但从未主动找过 bot 的人不建档，那条信息
    # 归说话人所在会话，而不是凭空给他开一份跨群档案。
    subject = str(args.get("subject") or "").strip()
    if subject and not _subject_is_enrolled(ctx.workspace_root, subject):
        subject = ""

    _namespaces = _tool_memory_namespaces(ctx, subject)
    # 寻址、改、写必须在同一把锁里：读完再等锁，拿到锁时 data 已经是别人改过之前的快照。
    with _memory_write_lock(ctx.workspace_root):
        return _save_memory_locked(
            ctx, args, mid, book, content, keywords, constant, enabled,
            node_ids, priority, scan_depth, subject, _namespaces,
        )


def _save_memory_locked(
    ctx: ToolContext,
    args: dict[str, Any],
    mid: str,
    book: str,
    content: str,
    keywords: list[str],
    constant: bool,
    enabled: bool,
    node_ids: list[str],
    priority: int,
    scan_depth: int,
    subject: str,
    _namespaces: list[str],
) -> dict[str, Any]:
    """save_memory 的读-改-写主体，调用方必须已持有 _memory_write_lock。"""
    _ns_memory_book, book_path, data, found_index = _locate_memory_entry(
        ctx.workspace_root, _namespaces, book, mid,
    )
    if not _ns_memory_book:
        _ns_memory_book = _namespaces[0]
        book_path = memory_dir(ctx.workspace_root, _ns_memory_book) / f"{book}.yaml"
        data = _load_book(book_path)
    data.setdefault("book", book)

    # [AutoC 2026-05-31] Why: save_memory 之前不写 created_at 和 source，
    # 导致 dream 的过期/清理逻辑无法判断条目年龄和来源。
    # How: 新建时写入 created_at + source；更新时保留原 created_at，刷新 updated_at。
    # Purpose: dream Phase 4 过期判断和 source 保护逻辑能正常工作。
    from datetime import datetime, timezone
    _now_iso = datetime.now(timezone.utc).isoformat()

    new_entry: dict[str, Any] = {
        "id": mid,
        "content": content,
        "keywords": keywords,
        "constant": constant,
        "enabled": enabled,
        "priority": priority,
        "scan_depth": scan_depth,
    }
    # node_ids 以前被解析完就丢掉，工具写不进去、手工写的又会被下一次自动提取整条
    # 覆盖掉。这里连同 subject 一起落盘，空值不写，避免给每条记忆塞两个空字段。
    if node_ids:
        new_entry["node_ids"] = node_ids
    if subject:
        new_entry["subject"] = subject

    entries = data["entries"]
    found = found_index >= 0
    if found:
        old = entries[found_index]
        # 手工记忆对工具只读。挡在这里而不只挡 delete：整条覆盖 content、或者一个
        # enabled=false，效果和删掉没区别，守卫不对称就等于没有守卫。
        if not _is_tool_writable(old):
            return _tool_err(f"cannot modify manually authored memory: {mid}")
        # constant 卸不掉：卸得掉就能先解锁再 delete，绕开那边的 constant 守卫。
        if bool(old.get("constant", False)) and "constant" in args and not constant:
            return _tool_err(f"cannot clear constant flag: {mid}")
        new_entry["created_at"] = str(old.get("created_at") or "").strip() or _now_iso
        new_entry["source"] = _AUTO_SOURCE
        new_entry["updated_at"] = _now_iso
        # 整条替换会抹掉旧字段。判据必须是「args 里有没有这个键」—— keywords/constant
        # 这些在 new_entry 里总带默认值，按 "not in new_entry" 判等于永远不继承，
        # 于是模型只想改 content 就把关键词洗成空，条目下一轮起永久不可召回。
        for carried in ("keywords", "constant", "enabled", "priority", "scan_depth", "node_ids", "subject"):
            if carried not in args and carried in old:
                new_entry[carried] = old[carried]
    else:
        new_entry["created_at"] = _now_iso
        new_entry["updated_at"] = _now_iso
        new_entry["source"] = _memory_source()

    # 三者皆无的条目在注入侧被直接 continue 掉，而工具此前照样返回成功 ——
    # 等于允许静默写一条永远不会被读到的记忆。
    if not new_entry.get("keywords") and not new_entry.get("constant") and not new_entry.get("subject"):
        return _tool_err(
            f"memory {book}/{mid} would never be recalled: give it keywords, "
            "or constant=true, or a subject"
        )

    if found:
        entries[found_index] = new_entry
    else:
        entries.append(new_entry)

    _save_book(book_path, data)
    # invalidate 时也传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_ns_memory_book)
    return _tool_ok(f"Memory {'updated' if found else 'saved'}: {book}/{mid}", book=book, id=mid, updated=found)


async def list_memories(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List memory entries, optionally filtered by book."""
    # Why: list_memories is now plugin-owned like the memory catalog. How: keep
    # the old book scan and preview fields. Purpose: preserve the tool response.
    # [2026-06-17] namespace 隔离：节点显式 memory_book 优先；否则按当前
    # conversation_key 扫描会话专属 memory 目录。
    book_filter = str(args.get("book") or "").strip() or None
    # 给了 subject 就列那个人的跨会话档案，否则列本会话的 —— 不认 subject 的话，
    # 按人档案里的条目模型压根看不见，也就无从知道该拿什么 id 去删。
    _ns_memory_book = _tool_memory_namespaces(ctx, str(args.get("subject") or ""))[0]
    mem_dir = memory_dir(ctx.workspace_root, _ns_memory_book)
    if not mem_dir.exists():
        return _tool_ok("0 memories", entries=[])

    result: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            data = _load_book(yaml_path)
            bname = str(data.get("book") or yaml_path.stem).strip()
            if book_filter and bname != book_filter:
                continue
            for e in data.get("entries", []):
                if not isinstance(e, dict):
                    continue
                result.append({
                    "book": bname,
                    "id": str(e.get("id") or ""),
                    "content": str(e.get("content") or "")[:200],
                    "keywords": e.get("keywords", []),
                    "constant": bool(e.get("constant", False)),
                    "enabled": bool(e.get("enabled", True)),
                    "priority": int(e.get("priority") or 0),
                    "scan_depth": int(e.get("scan_depth") or 0),
                    "node_ids": e.get("node_ids", []),
                    "subject": str(e.get("subject") or ""),
                })
        except Exception as read_error:
            logger.warning("memory book %s skipped while listing: %s", yaml_path.name, read_error)
            continue

    return _tool_ok(f"{len(result)} memories", entries=result)


async def delete_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a memory entry from a book."""
    # Why: delete_memory moved into the knowledge plugin with save_memory. How:
    # keep the old constant-memory protection and empty-book removal behavior.
    # Purpose: avoid any tool-level behavior change during registration refactor.
    mid = str(args.get("id") or "").strip()
    if not mid:
        return _tool_err("empty memory id")

    book = str(args.get("book") or "default").strip()
    # 跟 save_memory 走同一套寻址：给了 subject 就先在那个人的档案里找。此前只认会话
    # namespace，于是 data/memory/user_<别名>/ 下的条目写得进去却删不掉。
    _namespaces = _tool_memory_namespaces(ctx, str(args.get("subject") or ""))
    with _memory_write_lock(ctx.workspace_root):
        return _delete_memory_locked(ctx, book, mid, _namespaces)


def _delete_memory_locked(
    ctx: ToolContext, book: str, mid: str, _namespaces: list[str],
) -> dict[str, Any]:
    """delete_memory 的读-改-写主体，调用方必须已持有 _memory_write_lock。"""
    _ns_memory_book, book_path, data, found_index = _locate_memory_entry(
        ctx.workspace_root, _namespaces, book, mid,
    )
    if not _ns_memory_book:
        probe = memory_dir(ctx.workspace_root, _namespaces[0]) / f"{book}.yaml"
        if not probe.exists():
            return _tool_err(f"book not found: {book}")
        return _tool_err(f"memory not found: {mid}")

    entries = data.get("entries", [])

    # Why: constant memories are treated as protected baseline context. How: keep
    # the old refusal before filtering entries. Purpose: prevent accidental removal
    # of always-injected memory through the tool API.
    target = entries[found_index]
    if bool(target.get("constant", False)):
        return _tool_err(f"cannot delete constant memory: {mid}")

    # 手工记忆只能由人删。与 save_memory 共用一个判据，避免两套标准。
    if not _is_tool_writable(target):
        return _tool_err(f"cannot delete manually authored memory: {mid}")

    new_entries = [
        e for e in entries
        if not (isinstance(e, dict) and str(e.get("id") or "").strip() == mid)
    ]

    data["entries"] = new_entries
    if new_entries:
        _save_book(book_path, data)
    else:
        try:
            book_path.unlink()
        except Exception:
            pass

    # [2026-05-28] invalidate 时传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_ns_memory_book)
    return _tool_ok(f"Memory deleted: {book}/{mid}", book=book, id=mid, deleted=True)


# ---------------------------------------------------------------------------
#  PLUGIN_META tool declarations
# ---------------------------------------------------------------------------

# Why: toolbox.registry.py no longer owns these knowledge tool specs. How: attach
# exact copied descriptions and input schemas to PLUGIN_META after the functions
# exist. Purpose: let engine.builtin.loader register plugin-owned builtin tools.
PLUGIN_META["tools"] = [
    {
        "name": "create_or_update_skill",
        "description": "Create or update a skill under skills/<name>/SKILL.md.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "content": {"type": "string", "description": "full SKILL.md content (optional; frontmatter will be normalized)"},
                "enabled": {"type": "boolean"},
                "strategy": {"type": "string", "description": "constant (always injected) or normal (keyword-triggered); default normal", "enum": ["constant", "normal"]},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "activation keywords; supports /regex/flags syntax"},
                "order": {"type": "integer", "description": "injection order within the same block; higher values are placed later (closer to conversation)"},
                "priority": {"type": "integer", "description": "budget priority; higher values are kept first when token budget is exceeded"},
                "scan_depth": {"type": "integer", "description": "number of recent conversation rounds to scan for keyword matching; 0 = current message only"},
            },
            "required": ["name"],
        },
        "func": create_or_update_skill,
    },
    {
        "name": "list_skills",
        "description": "List local skills under skills/*/SKILL.md.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "func": list_skills,
    },
    {
        "name": "delete_skill",
        "description": "Delete a skill directory under skills/<name>/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
        "func": delete_skill,
    },
    {
        "name": "save_memory",
        "description": "Save or update a memory entry in a book. "
        "Use this when you learn something worth remembering across conversations: "
        "user preferences, corrections, project context, external resource pointers, "
        "or character profiles in group chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique entry id (e.g. user_zhangsan, rule_no_mock)."},
                "book": {"type": "string", "description": "Book name (file grouping). Default 'default'. Use e.g. 'people' for character profiles, 'rules' for behavioral rules."},
                "content": {"type": "string", "description": "Memory content text."},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Activation keywords. Supports /regex/flags. When any keyword matches user input, this memory is injected into context.",
                },
                "constant": {"type": "boolean", "description": "If true, always injected regardless of keywords. Default false."},
                "enabled": {"type": "boolean", "description": "Whether this entry is active. Default true."},
                "priority": {"type": "integer", "description": "Budget priority; higher = kept first when budget exceeded."},
                "scan_depth": {"type": "integer", "description": "Number of recent conversation rounds to scan for keywords. Default 2. 0 = current message only."},
                "subject": {
                    "type": "string",
                    "description": "The anonymized alias (e.g. UserA) this memory is ABOUT. "
                    "Set it when the content describes a specific person, so the entry joins that "
                    "person's cross-conversation profile and loads whenever they are mentioned. "
                    "Only aliases who have talked to the bot directly are accepted; anything else "
                    "falls back to this conversation's own memory.",
                },
                "node_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict this entry to these node ids. Omit to make it visible to every node.",
                },
            },
            "required": ["id", "content"],
        },
        "func": save_memory,
    },
    {
        "name": "list_memories",
        "description": "List memory entries, optionally filtered by book name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "book": {"type": "string", "description": "Filter by book name. Omit to list all."},
                "subject": {
                    "type": "string",
                    "description": "List one person's cross-conversation profile instead of this "
                    "conversation's own memory. Pass the anonymized alias (e.g. UserA).",
                },
            },
            "required": [],
        },
        "func": list_memories,
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory entry from a book.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory entry id to delete."},
                "book": {"type": "string", "description": "Book name. Default 'default'."},
                "subject": {
                    "type": "string",
                    "description": "Delete from that person's cross-conversation profile. Pass the "
                    "same alias the entry was saved with; list_memories reports it per entry.",
                },
            },
            "required": ["id"],
        },
        "func": delete_memory,
    },
]

class KnowledgeInjector:
    """Glue handler for skill and memory prompt injection."""

    name = "knowledge_inject"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Build skill and memory messages, then optionally rebuild the prompt.

        Why: the previous separate skill and memory prompt hooks duplicated glue
        and filtered conversation history separately. How: filter history once, call
        the unified knowledge builder with either independent or global budgets,
        and store the same ctx.extra keys as before. Purpose: unify ownership
        without changing prompt content, order, labels, or downstream contracts.
        """
        if ctx.node is None or ctx.rctx is None:
            return None

        from engine.inference.message_assembly import _conversational_history

        runtime_cfg = ctx.extra.get("runtime_cfg") or {}
        instruction_text = str(ctx.extra.get("instruction_text") or "")
        history = _conversational_history(ctx.extra.get("history") or [])

        # Why: this hook and the inference/preempt paths must share one builder
        # boundary. How: delegate to build_knowledge_context after filtering the
        # same conversation history as before. Purpose: remove duplicated builder
        # calls without changing ctx.extra keys or prompt layout.
        skill_static, skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
            ctx.rctx.workspace_root,
            ctx.node,
            instruction_text,
            history,
            runtime_cfg,
            task_context=getattr(ctx.rctx, "task_context", {}) or {},
        )

        ctx.extra["skill_static_messages"] = skill_static
        ctx.extra["skill_dynamic_messages"] = skill_dynamic
        ctx.extra["memory_static_messages"] = memory_static
        ctx.extra["memory_dynamic_messages"] = memory_dynamic

        if ctx.extra.get("apply_injection"):
            _rebuild_prompt_messages(ctx)
            return hook_result(modified=True)
        return hook_result(modified=bool(skill_static or skill_dynamic or memory_static or memory_dynamic))


def _rebuild_prompt_messages(ctx: Any) -> None:
    """Rebuild ctx.messages with all prompt injections currently in ctx.extra.

    Why: the previous prompt rebuild helper was shared by both knowledge paths and
    had to survive the merge. How: keep the same assemble_messages_with_injections
    call in the unified module and read the unchanged ctx.extra key names.
    Purpose: preserve the existing prompt layout while removing the old modules.
    """
    from engine.inference.message_assembly import assemble_messages_with_injections

    rebuilt, is_block_mode = assemble_messages_with_injections(
        workspace_root=ctx.rctx.workspace_root,
        system_prompt=list(ctx.extra.get("system_prompt") or []),
        history=list(ctx.extra.get("history") or []),
        instruction=str(ctx.extra.get("instruction_text") or ""),
        attachments=ctx.extra.get("attachments"),
        skill_static=list(ctx.extra.get("skill_static_messages") or []),
        skill_dynamic=list(ctx.extra.get("skill_dynamic_messages") or []),
        memory_static=list(ctx.extra.get("memory_static_messages") or []),
        memory_dynamic=list(ctx.extra.get("memory_dynamic_messages") or []),
    )
    ctx.messages[:] = rebuilt
    ctx.extra["is_block_mode"] = is_block_mode

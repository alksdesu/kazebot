"""把 conv_<摘要> 这类目录名还原成「哪个群 / 谁的私聊」。

摘要是 HMAC，反不回去，只能拿已知会话正推一遍再比对。各平台适配器落的映射文件
就是已知会话的全部来源，文件名由平台描述声明，supervisor 只读不写。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platforms.descriptor import (
    CHANNEL_GROUP,
    CONVERSATION_KINDS,
    STATE_ALIASES,
    STATE_ROUTES,
    PlatformDescriptor,
)
from platforms.ids import split_identity_key
from platforms.live import read_live_state
from platforms.registry import (
    all_platforms,
    conversation_channel,
    platform_state_path,
    stable_key_pattern,
)

# 两种 memory namespace 形态：会话记忆按摘要建目录，人物档案按别名
# （别名规则见 engine.memory_subjects.SUBJECT_RE）。
_CONV_NAMESPACE = r"conv_[0-9a-f]{24}"
_SUBJECT_NAMESPACE = r"user_[A-Za-z][A-Za-z0-9_]{0,63}"
_NAMESPACE_RE = re.compile(rf"^{_CONV_NAMESPACE}$")
_TASK_KEY_RE = re.compile(rf"^(?:{_CONV_NAMESPACE}|{_SUBJECT_NAMESPACE})$")
_SESSIONS = "sessions.json"
_ROSTER = "memory_subjects.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def memory_namespace(conversation_key: str) -> str:
    """与 knowledge_inject._conversation_memory_namespace 同一个算法。"""
    raw = str(conversation_key or "").strip()
    if not raw:
        return ""
    if _NAMESPACE_RE.match(raw):
        return raw
    return "conv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def is_internal_task_key(conversation_key: str) -> bool:
    """这条会话键是内部任务借 memory namespace 建的，不是真人对话。

    dream 拿 namespace 当 conversation_key 建整理任务（save_memory 靠它推目录），
    supervisor 于是给它开一条 session。会话整理用 conv_<摘要>，人物档案整理用
    user_<别名>；真人会话键永远带平台通道前缀，两种都长不成。
    """
    return bool(_TASK_KEY_RE.match(str(conversation_key or "").strip()))


def is_runtime_copy_session(session_id: str) -> bool:
    """入口分支与子代理副本：一次 inbound 的执行态，不是用户的会话。

    入口分支从父会话 fork 全部历史，conversation_key 也照抄，于是在列表里变成
    一条与真实会话同名、大小也几乎相同的第二行。它 merge 完就删，点进去往往
    已经没了，显示成「共 0 条」。
    """
    return str(session_id or "").strip().startswith(("branch_", "child_"))


@dataclass(frozen=True)
class AccountScope:
    """各平台当前登录账号名下的会话键。

    摘要带账号作用域，换号后同一个群另起一条，两个号的会话会并排堆在列表里。
    密钥只有 bot 进程有，这里只消费它算好的集合；平台没公布过时无从判断，一律算当前。
    """

    keys: Mapping[str, frozenset[str] | None]

    def is_current(self, conversation_key: str) -> bool:
        found = conversation_channel(conversation_key)
        if found is None or found[1].kind not in CONVERSATION_KINDS:
            return True
        keys = self.keys.get(found[0].id)
        return keys is None or conversation_key in keys


def account_scope(workspace_root: Path) -> AccountScope:
    keys: dict[str, frozenset[str] | None] = {}
    for platform in all_platforms():
        published = read_live_state(platform, workspace_root).get("scope_conversation_keys")
        keys[platform.id] = (
            frozenset(str(key) for key in published if str(key or "").strip())
            if isinstance(published, list) else None
        )
    return AccountScope(keys=keys)


class _DisplayNames:
    """真实群名与昵称。群名只有 bot 进程拿得到，靠它公布的 live state 转交。"""

    def __init__(self, workspace_root: Path, platform: PlatformDescriptor) -> None:
        root = Path(workspace_root)
        groups = read_live_state(platform, root).get("group_names")
        self._groups = groups if isinstance(groups, dict) else {}
        roster = _read_json(root / "data" / _ROSTER).get("subjects")
        self._users = roster if isinstance(roster, dict) else {}

    def group(self, real_id: str) -> str:
        name = self._groups.get(str(real_id))
        return name.strip() if isinstance(name, str) else ""

    def user(self, alias: str) -> str:
        entry = self._users.get(str(alias)) if alias else None
        name = entry.get("display_name") if isinstance(entry, dict) else None
        return name.strip() if isinstance(name, str) else ""


def _alias_index(platform: PlatformDescriptor, bucket: Any) -> dict[str, str]:
    """别名表一个桶里原始 ID → 别名。

    按账号分 ID 的平台，键前面带着机器人账号；同一串 ID 在几个号名下各有别名时认不出是哪个，干脆不给。
    """
    if not isinstance(bucket, dict):
        return {}
    found: dict[str, set[str]] = {}
    for key, entry in bucket.items():
        alias = str(entry.get("alias") or "").strip() if isinstance(entry, dict) else ""
        if alias:
            found.setdefault(split_identity_key(platform, str(key))[1], set()).add(alias)
    return {raw: next(iter(aliases)) for raw, aliases in found.items() if len(aliases) == 1}


@dataclass
class _PlatformLabels:
    platform: PlatformDescriptor
    real_map: dict[str, Any]
    user_aliases: dict[str, str]
    group_aliases: dict[str, str]
    names: _DisplayNames

    @classmethod
    def load(cls, workspace_root: Path, platform: PlatformDescriptor) -> "_PlatformLabels":
        real_map = _read_json(platform_state_path(platform, STATE_ROUTES, workspace_root)).get("real_conversation_keys")
        anon = _read_json(platform_state_path(platform, STATE_ALIASES, workspace_root))
        return cls(
            platform=platform,
            real_map=real_map if isinstance(real_map, dict) else {},
            user_aliases=_alias_index(platform, anon.get("users")),
            group_aliases=_alias_index(platform, anon.get("groups")),
            names=_DisplayNames(workspace_root, platform),
        )

    def label(self, stable: str) -> dict[str, Any]:
        found = conversation_channel(stable)
        kind = "group" if found is not None and found[1].kind == CHANNEL_GROUP else "private"
        raw_key = self.real_map.get(stable)
        real_key = raw_key if isinstance(raw_key, str) else ""
        real_id = real_key.split(":", 1)[1] if ":" in real_key else ""
        alias = (self.group_aliases if kind == "group" else self.user_aliases).get(real_id, "")
        # 别名是给模型脱敏用的，管理员看的应该是真名；拿不到真名才退回别名。
        display = self.names.group(real_id) if kind == "group" else self.names.user(alias)
        fallback = alias or (
            (f"群 {real_id}" if kind == "group" else f"私聊 {real_id}") if real_id
            else ("群聊（名称暂不可用）" if kind == "group" else "私聊（名称暂不可用）")
        )
        return {
            "kind": kind,
            "conversation_key": stable,
            "real_id": real_id,
            "alias": alias,
            "label": display or fallback,
            "name_available": bool(display),
        }


def subject_label(workspace_root: Path, alias: str) -> str:
    """人物档案该显示的名字。没记下昵称就退回别名。"""
    roster = _read_json(Path(workspace_root) / "data" / _ROSTER).get("subjects")
    entry = roster.get(str(alias)) if isinstance(roster, dict) and alias else None
    name = entry.get("display_name") if isinstance(entry, dict) else None
    return (name.strip() if isinstance(name, str) else "") or str(alias or "")


def load_sessions(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """session_id -> 条目。直接读盘而不是读内存表，child 与入口分支才不会漏。"""
    raw = _read_json(Path(workspace_root) / "data" / _SESSIONS)
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _candidate_keys(workspace_root: Path, platforms: Iterable[_PlatformLabels]) -> set[str]:
    """所有可能算出过记忆目录的会话键。

    child / 入口分支不进 conversation_map，但它们照样会写记忆目录，所以从
    sessions.json 全量取，而不是只看适配器那份映射。
    """
    keys: set[str] = {str(key) for labels in platforms for key in labels.real_map}
    for row in load_sessions(workspace_root).values():
        key = str(row.get("conversation_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def describe_namespaces(
    workspace_root: Path, *, conversation_keys: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """namespace -> 归属描述。认不出来的目录不会出现在结果里。"""
    root = Path(workspace_root)
    loaded = {platform.id: _PlatformLabels.load(root, platform) for platform in all_platforms()}
    stable_re = stable_key_pattern()

    described: dict[str, dict[str, Any]] = {}
    keys = _candidate_keys(root, loaded.values()) | {
        key for key in conversation_keys if isinstance(key, str) and key
    }
    for key in keys:
        namespace = memory_namespace(key)
        if not namespace:
            continue
        found = conversation_channel(key)
        if found is not None and found[1].kind in CONVERSATION_KINDS:
            described[namespace] = loaded[found[0].id].label(key)
            continue
        # 子代理的 key 形如 agent:<节点>:<通道>:<摘要>:<uuid>，把中间那段捞出来，
        # 它写的记忆才不至于在页面上变成一个无主目录。
        match = stable_re.search(key) if stable_re is not None else None
        parent_found = conversation_channel(match.group(0)) if match else None
        if parent_found is None:
            continue
        parent = loaded[parent_found[0].id].label(match.group(0))
        described[namespace] = {
            **parent,
            "kind": "agent",
            "conversation_key": key,
            "label": parent["label"] + " · 子代理",
        }
    return described

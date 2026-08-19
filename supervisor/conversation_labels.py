"""把 conv_<摘要> 这类目录名还原成「哪个群 / 谁的私聊」。

摘要是 HMAC，反不回去，只能拿已知会话正推一遍再比对。适配器落的那两份
映射文件就是已知会话的全部来源，supervisor 只读不写。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_NAMESPACE_RE = re.compile(r"^conv_[0-9a-f]{24}$")
_STABLE_KEY_RE = re.compile(r"qq_(?:group|private):[0-9a-f]{24}")
_ROUTE_STATE = "onebot_plugin_state.json"
_ANON_MAP = "onebot_anon_map.json"
_SESSIONS = "sessions.json"
_LIVE_STATE = "qq_live_state.json"
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


def _label_for_stable(
    stable: str, real_map: dict[str, Any], anon: dict[str, Any], names: "_DisplayNames",
) -> dict[str, Any]:
    real_key = str(real_map.get(stable) or "")
    kind = "group" if stable.startswith("qq_group:") else "private"
    real_id = real_key.split(":", 1)[1] if ":" in real_key else ""
    bucket = anon.get("groups" if kind == "group" else "users") or {}
    alias = str((bucket.get(real_id) or {}).get("alias") or "") if isinstance(bucket, dict) else ""
    # 别名是给模型脱敏用的，管理员看的应该是真名；拿不到真名才退回别名。
    display = names.group(real_id) if kind == "group" else names.user(alias)
    fallback = alias or (f"群 {real_id}" if kind == "group" else f"私聊 {real_id}") or stable
    return {
        "kind": kind,
        "conversation_key": stable,
        "real_id": real_id,
        "alias": alias,
        "label": display or fallback,
    }


class _DisplayNames:
    """真实群名与昵称。群名只有 bot 进程拿得到，靠它公布的 live state 转交。"""

    def __init__(self, workspace_root: Path) -> None:
        root = Path(workspace_root)
        live = _read_json(root / "data" / _LIVE_STATE)
        groups = live.get("group_names")
        self._groups = groups if isinstance(groups, dict) else {}
        roster = _read_json(root / "data" / _ROSTER).get("subjects")
        self._users = roster if isinstance(roster, dict) else {}

    def group(self, real_id: str) -> str:
        return str(self._groups.get(str(real_id)) or "").strip()

    def user(self, alias: str) -> str:
        entry = self._users.get(str(alias)) if alias else None
        return str((entry or {}).get("display_name") or "").strip() if isinstance(entry, dict) else ""


def subject_label(workspace_root: Path, alias: str) -> str:
    """人物档案该显示的名字。没记下昵称就退回别名。"""
    return _DisplayNames(workspace_root).user(alias) or str(alias or "")


def load_sessions(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """session_id -> 条目。直接读盘而不是读内存表，child 与入口分支才不会漏。"""
    raw = _read_json(Path(workspace_root) / "data" / _SESSIONS)
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _candidate_keys(workspace_root: Path, real_map: dict[str, Any]) -> set[str]:
    """所有可能算出过记忆目录的会话键。

    child / 入口分支不进 conversation_map，但它们照样会写记忆目录，所以从
    sessions.json 全量取，而不是只看适配器那份映射。
    """
    keys: set[str] = {str(key) for key in real_map}
    for row in load_sessions(workspace_root).values():
        key = str(row.get("conversation_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def describe_namespaces(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """namespace -> 归属描述。认不出来的目录不会出现在结果里。"""
    root = Path(workspace_root)
    real_map = _read_json(root / "data" / _ROUTE_STATE).get("real_conversation_keys") or {}
    real_map = real_map if isinstance(real_map, dict) else {}
    anon = _read_json(root / "data" / _ANON_MAP)
    names = _DisplayNames(root)

    described: dict[str, dict[str, Any]] = {}
    for key in _candidate_keys(root, real_map):
        namespace = memory_namespace(key)
        if not namespace:
            continue
        if key.startswith(("qq_group:", "qq_private:")):
            described[namespace] = _label_for_stable(key, real_map, anon, names)
            continue
        # 子代理的 key 形如 agent:<节点>:qq_group:<摘要>:<uuid>，把中间那段捞出来，
        # 它写的记忆才不至于在页面上变成一个无主目录。
        found = _STABLE_KEY_RE.search(key)
        if not found:
            continue
        parent = _label_for_stable(found.group(0), real_map, anon, names)
        described[namespace] = {
            **parent,
            "kind": "agent",
            "conversation_key": key,
            "label": parent["label"] + " · 子代理",
        }
    return described

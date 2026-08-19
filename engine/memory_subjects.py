"""按人记忆的建档名册：只有主动找过 bot 的人才拥有记忆档案。

适配层（bot.py 进程）在有人主动 @ / 回复 / 私聊时登记，engine worker 读它来判断
某个 subject 能不能落到 data/memory/user_<alias>/。名册与匿名别名同层，不含真实
QQ 号，因此可以安全地被 engine 读到。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROSTER_FILENAME = "memory_subjects.json"
# 别名由适配层的匿名化计数器产生（UserA / UserAF / GroupB…）；限定字符集，避免
# subject 被拼进目录名时越出 data/memory/。
SUBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MEMORY_NAMESPACE_PREFIX = "user_"

_write_lock = threading.Lock()


def roster_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / "data" / _ROSTER_FILENAME


def is_valid_subject(subject: Any) -> bool:
    return bool(SUBJECT_RE.match(str(subject or "").strip()))


def subject_namespace(subject: str) -> str:
    """Return the memory directory name for one person, or "" when invalid."""
    text = str(subject or "").strip()
    if not is_valid_subject(text):
        return ""
    return f"{_MEMORY_NAMESPACE_PREFIX}{text}"


def load_roster(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Return the enrolled subjects, or an empty mapping when nobody qualifies yet."""
    path = roster_path(workspace_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as read_error:
        # 名册损坏时按「没人建档」处理：宁可不加载记忆，也不要凭一份坏数据乱写档案。
        log.warning("memory subject roster unreadable: %s", read_error)
        return {}
    subjects = data.get("subjects") if isinstance(data, dict) else None
    if not isinstance(subjects, dict):
        return {}
    return {
        str(key): value for key, value in subjects.items()
        if is_valid_subject(key) and isinstance(value, dict)
    }


def enrolled_subjects(workspace_root: Path) -> set[str]:
    return set(load_roster(workspace_root))


def is_enrolled(workspace_root: Path, subject: Any) -> bool:
    text = str(subject or "").strip()
    return is_valid_subject(text) and text in enrolled_subjects(workspace_root)


_DISPLAY_NAME_MAX_LEN = 64


def _clean_display_name(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    # 名片能塞换行和控制字符，落进名册后会污染后续的字面匹配。
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()[:_DISPLAY_NAME_MAX_LEN]


def subject_display_names(workspace_root: Path) -> dict[str, str]:
    """已建档者最近一次露面时用的显示名。同名者一并剔除。

    重名时无法判断文本里说的是谁，认错人会把别人的档案念出来，比认不出更糟。
    """
    names: dict[str, str] = {}
    for alias, entry in load_roster(workspace_root).items():
        display = _clean_display_name(entry.get("display_name"))
        if display:
            names[alias] = display
    duplicated = {
        name for name in names.values()
        if sum(1 for other in names.values() if other == name) > 1
    }
    return {alias: name for alias, name in names.items() if name not in duplicated}


def record_interaction(
    workspace_root: Path, subject: str, *, now: str = "", display_name: Any = "",
) -> bool:
    """Enroll *subject* on a direct interaction and return whether the file changed.

    只在本人主动找 bot 时调用 —— bot 因关键词或随机回复插话不算，否则群一活跃
    所有人迟早都会被建档，「只有交互过的人才有档案」这条门槛就废了。
    """
    text = str(subject or "").strip()
    if not is_valid_subject(text):
        return False
    stamp = now or datetime.now(timezone.utc).isoformat()
    display = _clean_display_name(display_name)
    path = roster_path(workspace_root)
    with _write_lock:
        roster = load_roster(workspace_root)
        entry = roster.get(text)
        if entry is None:
            roster[text] = {"first_seen": stamp, "last_seen": stamp, "interactions": 1}
            entry = roster[text]
        else:
            entry["last_seen"] = stamp
            entry["interactions"] = int(entry.get("interactions") or 0) + 1
        # 改了名就跟着改：认人靠的是他现在用的名字，不是第一次见到的那个。
        if display:
            entry["display_name"] = display
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"subjects": roster}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as write_error:
            log.warning("memory subject roster write failed: %s", write_error)
            return False
    return True


def resolve_load_subjects(workspace_root: Path, requested: Any) -> list[str]:
    """Filter requested subjects down to the ones that actually have a档案.

    被提到但从未主动找过 bot 的人不建档，所以也没有档案可加载 —— 这一步就是那条
    门槛在读取侧的体现。顺序去重，保持适配层给出的优先次序。
    """
    if not isinstance(requested, (list, tuple, set)):
        return []
    enrolled = enrolled_subjects(workspace_root)
    seen: set[str] = set()
    result: list[str] = []
    for item in requested:
        text = str(item or "").strip()
        if not is_valid_subject(text) or text in seen or text not in enrolled:
            continue
        seen.add(text)
        result.append(text)
    return result

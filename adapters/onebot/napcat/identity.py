"""发送者身份：Profile、显示名、匿名成员。"""
from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any, Dict, List
from ..config import CLONOTH_WORKSPACE
from ..yaml_loader import load_yaml

from . import anonymize, common, permissions, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _sanitize_name(name: str, max_len: int = 32) -> str:
    """清洗成员名称，避免换行和结构符号破坏输入格式。"""
    name = (name or "").replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")


def _load_qq_user_profiles(path_text: str) -> Dict[str, Dict[str, Any]]:
    """从 JSON/YAML 加载 QQ 用户称呼 Profile；仅用于展示和提示，不授予权限。"""
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path(CLONOTH_WORKSPACE) / path).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix.lower() == ".json" else load_yaml(raw)
    except FileNotFoundError:
        return {}
    except Exception:
        common.logger.warning("failed to load QQ user profiles: %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    users = data.get("users") if isinstance(data.get("users"), dict) else data
    profiles: Dict[str, Dict[str, Any]] = {}
    for raw_uid, raw_profile in users.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_profile, dict):
            continue
        profile = {
            "display_name": str(raw_profile.get("display_name") or raw_profile.get("name") or "").strip(),
            "address_as": str(raw_profile.get("address_as") or raw_profile.get("call_as") or "").strip(),
            "title": str(raw_profile.get("title") or raw_profile.get("role") or "").strip(),
            "note": str(raw_profile.get("note") or raw_profile.get("style_note") or "").strip(),
        }
        profiles[uid] = {k: v for k, v in profile.items() if v}
    return profiles


def _qq_user_profile(user_id: Any) -> Dict[str, Any]:
    raw = str(user_id or "").strip()
    return dict(state._adapter.qq_user_profiles.get(raw) or {})


def _qq_profile_display_name(user_id: Any) -> str:
    name = str(_qq_user_profile(user_id).get("display_name") or "").strip()
    return _sanitize_name(name) if name else ""


def _sender_display_name(sender: Any, fallback_user_id: Any = "") -> str:
    """优先使用 QQ Profile 显示名，其次使用群名片/昵称，最后回退到匿名别名。

    2026-07-09 修改原因：取消了 `_anonymize_text_for_ai` 的泛数字兜底后，本函数不能再
    直接回退到裸 QQ 号，否则无名片/无昵称的发送者真实 QQ 号会泄露给模型。
    无可用名称时回退到稳定匿名别名（并登记映射）。
    """
    profile_name = _qq_profile_display_name(fallback_user_id)
    if profile_name:
        return profile_name
    card = getattr(sender, "card", "") or ""
    nickname = getattr(sender, "nickname", "") or ""
    if card or nickname:
        return _sanitize_name(card or nickname)
    return anonymize._anonymize_user_id(fallback_user_id) if str(fallback_user_id or "").strip() else "未知成员"


# QQ 群匿名消息的发送者占位号，全群匿名成员共用；不能当成一个真实用户登记。
_ANONYMOUS_PLACEHOLDER_QQ_IDS = frozenset({"80000000"})


_ANONYMOUS_UNKNOWN_ALIAS = "AnonUnknown"


_ANONYMOUS_DISPLAY_NAME = "匿名成员"


# 匿名 id 由平台按天轮换，落盘只会撑大映射表且反解无意义，因此这份表只存在于进程内。
_ANON_SENDER_MAX_ENTRIES = 4096


def _anonymous_field(anonymous: Any, key: str) -> str:
    if isinstance(anonymous, dict):
        return str(anonymous.get(key) or "").strip()
    return str(getattr(anonymous, key, "") or "").strip()


def _anonymous_identity(event: Any) -> str:
    """群匿名消息的身份键；非匿名消息返回空串。"""
    anonymous = getattr(event, "anonymous", None)
    if anonymous is None:
        return ""
    ident = (
        _anonymous_field(anonymous, "id")
        or _anonymous_field(anonymous, "flag")
        or _anonymous_field(anonymous, "name")
    )
    group = str(getattr(event, "group_id", "") or "")
    return f"anon:{group}:{ident}" if ident else f"anon:{group}:unknown"


def _anonymize_anonymous_sender(event: Any) -> str:
    """给匿名发送者分配进程内别名；不同匿名成员必须拿到不同别名。"""
    key = _anonymous_identity(event)
    if not key:
        return ""
    alias = state._adapter.anon_senders.get(key)
    if alias is None:
        alias = anonymize._alias_from_index("Anon", state._adapter.anon_sender_next)
        state._adapter.anon_sender_next += 1
        state._adapter.anon_senders[key] = alias
        while len(state._adapter.anon_senders) > _ANON_SENDER_MAX_ENTRIES:
            state._adapter.anon_senders.popitem(last=False)
    else:
        state._adapter.anon_senders.move_to_end(key)
    return alias


def _event_user_alias(event: Any) -> str:
    """模型可见的发送者稳定标识。"""
    return _anonymize_anonymous_sender(event) or anonymize._anonymize_user_id(getattr(event, "user_id", ""))


def _event_display_name(event: Any) -> str:
    """模型可见的发送者显示名。匿名昵称是发送者自选的，用它等于让人随手冒充别人。"""
    if _anonymous_identity(event):
        return _ANONYMOUS_DISPLAY_NAME
    return _sender_display_name(getattr(event, "sender", None), getattr(event, "user_id", ""))


def _event_sender_key(event: Any) -> str:
    """“同一发送者”判定用的键（最近图片绑定），匿名成员之间不能互相命中。"""
    return _anonymous_identity(event) or str(getattr(event, "user_id", "") or "")


def _user_identity_lines(event: Any, display_name: str) -> List[str]:
    """生成模型可见的用户身份/称呼提示；权限仍由代码侧 platform_auth 控制。"""
    anonymous = bool(_anonymous_identity(event))
    user_id = "" if anonymous else getattr(event, "user_id", "")
    profile = _qq_user_profile(user_id)
    stable_user = _event_user_alias(event)
    lines = [
        f"稳定用户标识: {stable_user}",
        f"显示名: {_sanitize_name(display_name)}",
        # 标识排在显示名前面、历史里又每行都跟着，模型很容易拿它当称呼；而 @ 标记确实要用它，不能一概禁。
        f"称呼规则: 说到这个人时用显示名，{stable_user} 只在 [at:{stable_user}] 里用，不要写进正文",
    ]
    address_as = str(profile.get("address_as") or "").strip()
    if address_as:
        lines.append(f"称呼要求: 请称呼该用户为「{_sanitize_name(address_as)}」")
    title = str(profile.get("title") or "").strip()
    if title:
        lines.append(f"身份标签: {_sanitize_name(title)}")
    note = str(profile.get("note") or "").strip()
    if note:
        lines.append(f"称呼/语气备注: {_sanitize_name(note, max_len=80)}")
    lines.append(f"Clonoth 管理员: {'是' if (not anonymous and permissions._is_admin_user(user_id)) else '否'}")
    lines.append("权限说明: 管理员权限仅由系统配置判定，以上称呼配置不授予任何权限。")
    if anonymous:
        lines.append("身份说明: 群匿名消息，发送者身份无法核验")
    return lines

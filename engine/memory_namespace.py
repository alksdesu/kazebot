"""data/memory 下的目录名：会话记忆是 conv_<稳定键摘要>，人物档案是 user_<别名>，节点自带的记忆库用 memory_book 原名。

engine、supervisor、适配器和离线迁移脚本都按这里算、按这里认，别处不再各抄一份。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

CONVERSATION_PREFIX = "conv_"
SUBJECT_PREFIX = "user_"
# 内部任务点名要读写哪个目录时用的会话键前缀；conv_/user_ 目录名本身就能当键，用不着它。
NAMESPACE_KEY_PREFIX = "memory:"
# 控制台 URL 里没法放空串，用它指代根目录；真有这么个目录就会和根目录混成一个。
ROOT_NAMESPACE_SENTINEL = "__root__"
_AGENT_KEY_PREFIX = "agent:"

# 别名由适配层的匿名化计数器产生（UserA / UserAF / GroupB…）；限定字符集，拼进目录名时越不出 data/memory/。
_SUBJECT = r"[A-Za-z][A-Za-z0-9_]{0,63}"
_CONVERSATION_NAMESPACE = rf"{CONVERSATION_PREFIX}[0-9a-f]{{24}}"
_SUBJECT_RE = re.compile(_SUBJECT)
_MEMORY_NAMESPACE_RE = re.compile(rf"(?:{_CONVERSATION_NAMESPACE}|{SUBJECT_PREFIX}{_SUBJECT})")

# 节点 id 的上限；create_agent 不给 memory_book 时就拿节点 id 当目录名。
_CUSTOM_NAMESPACE_MAX_LEN = 64
# 两种路径分隔符，加上 Windows 文件名里放不下的字符。
_FORBIDDEN_NAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})
_BOOK_SUFFIX = ".yaml"


class InvalidMemoryNamespace(ValueError):
    """这个名字当不了 data/memory 下的目录。"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_valid_subject(subject: Any) -> bool:
    return _SUBJECT_RE.fullmatch(_text(subject)) is not None


def is_memory_namespace(value: Any) -> bool:
    """是不是自动生成的两种目录名之一。真人会话键都带平台通道前缀和冒号，长不成这样。"""
    return _MEMORY_NAMESPACE_RE.fullmatch(_text(value)) is not None


def namespace_error(value: Any) -> str:
    """value 当不了 data/memory 下的一级目录名时说出原因，当得了给空串。

    自动生成的两种目录名之外，节点在 memory_book 里自己起的名字也算；起名只需不越出
    data/memory、不撞上记忆库自己的文件，中文名照样能用。
    """
    text = "" if value is None else str(value)
    if not text.strip():
        return "empty name"
    if _MEMORY_NAMESPACE_RE.fullmatch(text):
        return ""
    # 前半截像不算数：conv_/user_ 开头却不是那两种目录，控制台会把它认成别人的会话或档案。
    if text.startswith((CONVERSATION_PREFIX, SUBJECT_PREFIX)):
        return (
            f"names starting with {CONVERSATION_PREFIX} or {SUBJECT_PREFIX} are reserved "
            "for conversation memory and person profiles"
        )
    if text == ROOT_NAMESPACE_SENTINEL:
        return "reserved: the console uses it to address the memory root"
    if len(text) > _CUSTOM_NAMESPACE_MAX_LEN:
        return f"longer than {_CUSTOM_NAMESPACE_MAX_LEN} characters"
    if text != text.strip():
        return "leading or trailing whitespace"
    if text.startswith("."):
        return "names starting with a dot are reserved for the memory store's own files"
    if any(ch in _FORBIDDEN_NAME_CHARS or not ch.isprintable() for ch in text):
        return "contains a path separator or a character file names cannot hold"
    # Windows 会吃掉结尾的点，a. 和 a 落进同一个目录。
    if text.endswith("."):
        return "ends with a dot"
    # 根目录按 *.yaml 认历史记忆本，同名目录会被当成一本读不出来的书。
    if text.lower().endswith(_BOOK_SUFFIX):
        return f"ends with {_BOOK_SUFFIX}, the memory book file suffix"
    if text.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
        return "a reserved device name on Windows"
    return ""


def is_valid_namespace(value: Any) -> bool:
    return not namespace_error(value)


def memory_book_error(value: Any) -> str:
    """新建节点时给的 memory_book 能不能用。

    在 namespace_error 之上还不许直接占用会话记忆或人物档案的目录：那等于让一个子节点
    读写某个群的记忆、某个人的档案。
    """
    reason = namespace_error(value)
    if reason:
        return reason
    if is_memory_namespace(value):
        return "already names a conversation's memory or a person's profile"
    return ""


def conversation_namespace(conversation_key: Any) -> str:
    """这个会话的记忆落在哪个目录，键为空时给空串。"""
    raw = _text(conversation_key)
    if not raw:
        return ""
    # 已经是目录名就原样返回：dream 拿要整理的目录名当 conversation_key 建任务，会话记忆和人物档案都是，
    # save_memory/delete_memory 才落回那个目录。
    if _MEMORY_NAMESPACE_RE.fullmatch(raw):
        return raw
    named = raw[len(NAMESPACE_KEY_PREFIX):] if raw.startswith(NAMESPACE_KEY_PREFIX) else ""
    if named and is_valid_namespace(named):
        return named
    return CONVERSATION_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def namespace_conversation_key(namespace: Any) -> str:
    """内部任务要让记忆工具读写 namespace 时给的会话键，conversation_namespace 能原样算回来。"""
    text = _text(namespace)
    if not text or is_memory_namespace(text):
        return text
    reason = namespace_error(text)
    if reason:
        raise InvalidMemoryNamespace(f"invalid memory namespace {text!r}: {reason}")
    return NAMESPACE_KEY_PREFIX + text


def is_namespace_conversation_key(value: Any) -> bool:
    """namespace_conversation_key 给出的那种键：内部任务借来点名目录的，不是真人对话。"""
    text = _text(value)
    if is_memory_namespace(text):
        return True
    return text.startswith(NAMESPACE_KEY_PREFIX) and is_valid_namespace(text[len(NAMESPACE_KEY_PREFIX):])


def memory_owner_conversation_key(conversation_key: Any, routed_key: Any = "", parent_key: Any = "") -> str:
    """长期记忆归属哪个会话。

    dispatch 出去的子节点跑在 `agent:<节点>:<父key>` 会话里，fresh/fork 每次还额外拼一个
    新 uuid —— 按运行期 key 派生 namespace，子节点就是每次读一个空目录、写进一个用一次
    就废弃的目录，那些目录还会无人清理地堆下去。长期记忆本来跨会话，归发起它的会话。
    route_conversation_key 沿 dispatch 链恒为根会话的 key，多层委派也不会跑偏。
    """
    own = _text(conversation_key)
    if not own.startswith(_AGENT_KEY_PREFIX):
        return own
    return _text(routed_key) or _text(parent_key) or own


def node_memory_namespace(
    memory_book: Any, conversation_key: Any, routed_key: Any = "", parent_key: Any = "",
) -> str:
    """节点在这个会话里读写哪个 namespace：节点自带的 memory_book 优先，否则归属会话。

    注入、三个记忆工具、自动提取都从这里取答案，算岔一处就是写得进、读不到。
    """
    explicit = _text(memory_book)
    if explicit:
        reason = namespace_error(explicit)
        if reason:
            raise InvalidMemoryNamespace(f"invalid memory_book {explicit!r}: {reason}")
        return explicit
    return conversation_namespace(memory_owner_conversation_key(conversation_key, routed_key, parent_key))


def task_memory_namespace(memory_book: Any, task_context: Any) -> str:
    """node_memory_namespace 的任务版，三个会话键都从 task_context 取。"""
    ctx = task_context if isinstance(task_context, dict) else {}
    return node_memory_namespace(
        memory_book,
        ctx.get("conversation_key"),
        ctx.get("route_conversation_key"),
        ctx.get("parent_conversation_key"),
    )


def subject_namespace(subject: Any) -> str:
    """一个人的档案目录名，别名不合规时给空串。"""
    text = _text(subject)
    return SUBJECT_PREFIX + text if is_valid_subject(text) else ""


def subject_from_namespace(namespace: Any) -> str:
    """subject_namespace 反过来：档案目录名还原成别名，其余给空串。"""
    text = _text(namespace)
    if not text.startswith(SUBJECT_PREFIX):
        return ""
    subject = text[len(SUBJECT_PREFIX):]
    return subject if _SUBJECT_RE.fullmatch(subject) else ""


def memory_root(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "data" / "memory"


def _resolves_inside(root: Path, directory: Path) -> bool:
    # 名字合规还不够：namespace 目录若是指向别处的链接，读写就跟着出了 data/memory。
    return directory.resolve().parent == root.resolve()


def memory_dir(workspace_root: Path | str, namespace: Any = "") -> Path:
    """namespace 的目录；空串是 data/memory 本身，放着没有归属的历史记忆本。"""
    root = memory_root(workspace_root)
    if not namespace:
        return root
    reason = namespace_error(namespace)
    if reason:
        raise InvalidMemoryNamespace(f"invalid memory namespace {namespace!r}: {reason}")
    directory = root / str(namespace)
    if not _resolves_inside(root, directory):
        raise InvalidMemoryNamespace(f"memory namespace {namespace!r} leads outside {root}")
    return directory


def namespace_dirs(workspace_root: Path | str) -> list[Path]:
    """data/memory 下实际存在的各个 namespace 目录，名字合不合规都列出来。

    以点开头的是记忆库自己的锁目录；链接到 data/memory 外面的不算。
    """
    root = memory_root(workspace_root)
    if not root.is_dir():
        return []
    return [
        child for child in sorted(root.iterdir())
        if not child.name.startswith(".") and child.is_dir() and _resolves_inside(root, child)
    ]

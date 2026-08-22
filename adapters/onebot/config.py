"""Clonoth OneBot 11 适配器配置。

敏感/实例特定的参数通过环境变量注入，避免硬编码。改一次就要重启进程的键留在这里；
运营期要频繁调的键在 live_config.py，读 config/qq.yaml 并即时生效。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from workspace import resolve_workspace_root


def _env_bool(name: str, default: bool) -> bool:
    """解析布尔环境变量，兼容 onebot11_adapter.py 的 ONEBOT_* 配置风格。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = float(str(raw).strip()) if raw is not None else float(default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_first(*names: str, default: str = "") -> str:
    """按优先级读取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


# QQ 号/群号只收 ASCII 十进制正整数。上界是 JS 安全整数：控制台把整张名单经 JSON
# 往返写回 yaml，超过这个数的号会被静默改写成另一个号。
QQ_ID_PATTERN = r"^[0-9]+$"
QQ_ID_MIN = 1
QQ_ID_MAX = 9007199254740991
_QQ_ID_RE = re.compile(QQ_ID_PATTERN)


def parse_qq_id(raw: Any) -> int | None:
    """把一个名单项转成 QQ 号/群号；不合法返回 None。"""
    token = str(raw if raw is not None else "").strip()
    if not _QQ_ID_RE.match(token):
        return None
    value = int(token)
    return value if QQ_ID_MIN <= value <= QQ_ID_MAX else None


def _is_number_attempt(token: str) -> bool:
    """int() 收得下就说明写的人是想写个号码，只是这个号码不可用。"""
    try:
        int(token)
    except ValueError:
        return False
    return True


def parse_qq_id_item(raw: Any, on_reject: Callable[[Any], None] | None = None) -> int | None:
    """名单单项的唯一入口。yaml 原生列表和 env 逗号串都走这里，两条路的值域才不会各说一套。"""
    token = str(raw if raw is not None else "").strip()
    value = parse_qq_id(token)
    # 说明性占位符文本是文档里的默认值，为它告警等于每次加载都喊一次狼来了。
    if value is None and on_reject is not None and _is_number_attempt(token):
        on_reject(raw)
    return value


def parse_qq_id_list(raw: str, on_reject: Callable[[Any], None] | None = None) -> list[int]:
    """解析逗号分隔 QQ 号/群号列表；忽略占位符和非法项。"""
    result: list[int] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        value = parse_qq_id_item(token, on_reject)
        if value is None:
            continue
        if value not in result:
            result.append(value)
    return result


def parse_prefix_list(raw: str) -> tuple[str, ...]:
    """解析半角逗号分隔的触发前缀；去空白、去重并保持配置顺序。"""
    result: list[str] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if token and token not in result:
            result.append(token)
    return tuple(result)


def parse_path_list(raw: str) -> tuple[str, ...]:
    """解析逗号分隔的绝对目录列表；去空白去重，保持配置顺序。"""
    result: list[str] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if token and token not in result:
            result.append(token)
    return tuple(result)


# Clonoth Supervisor API 地址
CLONOTH_BASE_URL = _env_first("CLONOTH_BASE_URL", "CLONOTH_SUPERVISOR_URL", default="http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）。
# 与 supervisor/engine 共用同一个解析函数：各算各的时 `~` 和相对路径会被展开成两个根，
# 附件、记忆、admin token 于是静默落到两个地方。
CLONOTH_WORKSPACE = str(resolve_workspace_root(Path(__file__).resolve().parents[2]))

# 入口节点 ID。默认使用 QQ 综合入口，兼顾联网搜索、调度、重启和取消任务。
# 如需搜索-only 安全窄入口，可显式设置 CLONOTH_ENTRY_NODE=qq.web_search。
ENTRY_NODE_ID = _env_first("CLONOTH_ENTRY_NODE", "ONEBOT_ENTRY_NODE_ID", default="qq.orchestrator")

# QQ 收藏表情 AI 可见名称文件路径。默认使用该文件给 AI 注入可用表情名。
CUSTOM_FACE_NAMES_PATH = _env_first(
    "ONEBOT_CUSTOM_FACE_NAMES_PATH",
    "CLONOTH_QQ_CUSTOM_FACES_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.txt"),
)
CUSTOM_FACE_METADATA_PATH = _env_first(
    "ONEBOT_CUSTOM_FACE_METADATA_PATH",
    "CLONOTH_QQ_CUSTOM_FACES_METADATA_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.json"),
)
# 旧 bqbs.txt 顺序别名文件路径（可选）。默认不使用；仅配置 env 时参与兼容匹配/同步。
BQBS_PATH = _env_first("ONEBOT_CUSTOM_EMOJI_INDEX_PATH", "CLONOTH_BQBS_PATH", default="")

# QQ 用户身份/称呼配置文件路径（可选，支持 JSON/YAML）。只用于模型可见称呼，不授予权限。
# 默认读取 Clonoth 工作区 config/qq_user_profiles.yaml；文件不存在时静默跳过。
USER_PROFILES_PATH = _env_first(
    "CLONOTH_QQ_USER_PROFILES_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_user_profiles.yaml"),
)

# 群聊触发模式：mention_only（默认，只 @Bot）、prefix（@Bot 或前缀）、all（所有消息）。
# 选哪一种在 live_config，这里只是取值域。
GROUP_TRIGGER_MENTION_MODES = frozenset({"mention_only", "at", "at_only"})
GROUP_TRIGGER_PREFIX_MODES = frozenset({"prefix", "prefix_or_mention", "mention_or_prefix"})
GROUP_TRIGGER_ALL_MODES = frozenset({"all", "always"})
GROUP_TRIGGER_MODES = GROUP_TRIGGER_MENTION_MODES | GROUP_TRIGGER_PREFIX_MODES | GROUP_TRIGGER_ALL_MODES

# QQ 图片/多模态输入配置。
IMAGE_DOWNLOAD_TIMEOUT = _env_float("ONEBOT_IMAGE_DOWNLOAD_TIMEOUT", 15.0, min_value=1.0)
# 配得比 engine/data_cleanup.py 的 ATTACH_MAX_AGE(24h) 长时，engine 那份全局清理会先按 mtime 删。
# 清理按 mtime 判定，TTL 太小会删掉刚下载还没进 prompt 的图。
IMAGE_CACHE_TTL_MIN_SECONDS = 60
IMAGE_CACHE_TTL_SECONDS = _env_int(
    "ONEBOT_IMAGE_CACHE_TTL_SECONDS", 24 * 3600, min_value=IMAGE_CACHE_TTL_MIN_SECONDS,
)
RECENT_IMAGE_MAX_ITEMS = _env_int("ONEBOT_RECENT_IMAGE_MAX_ITEMS", 20, min_value=1, max_value=200)
# 最近图片只认同一发送者的同一条消息，跨人/跨消息一律不兜底（策略固定在 attachment_policy 内）。
RECENT_IMAGE_MAX_AGE_SECONDS = _env_float("ONEBOT_RECENT_IMAGE_MAX_AGE_SECONDS", 60.0, min_value=1.0)
# 文件桶单独定容：一个文件可以有 50MB，不能跟着图片那 20 条的容量走。
RECENT_FILE_MAX_ITEMS = _env_int("ONEBOT_RECENT_FILE_MAX_ITEMS", 6, min_value=1, max_value=50)
# 传文件比发图慢，窗口给得比图片宽一些：等上传转圈完再打字问是常态。
RECENT_FILE_MAX_AGE_SECONDS = _env_float("ONEBOT_RECENT_FILE_MAX_AGE_SECONDS", 180.0, min_value=1.0)

# 合并转发单个 node 的署名。
IMAGE_FORWARD_MERGE_NICKNAME = _env_first("ONEBOT_IMAGE_FORWARD_MERGE_NICKNAME", default="Clonoth")

# 入站 file/image 段给的本地路径只在这些根目录下才允许读取。默认只有工作区：
# 事件里的 path 字段由 OneBot 实现决定，放开等于按对端内容读本机任意文件。
# NapCat 与 Bot 同机部署且只上报缓存绝对路径时，才把那个缓存目录加进来。
LOCAL_SOURCE_ROOTS = parse_path_list(_env_first("ONEBOT_LOCAL_SOURCE_ROOTS", default=""))

# QQ 自然语言转发 Bridge Server 配置。
# AI 通过 qq_forward 工具（子进程）经本地 HTTP Bridge 调用 QQ Bot 进程完成
# “把上面聊到的 xxx 私发给我 / 合并转发到群 xxx”等多选多条消息转发任务。
# 真实 QQ 群号/QQ 号只留在 Bot 进程内，模型上下文只接触匿名下标/关键词。
# 监听地址在启动时绑定，改了要重启；开关与条数上限在 live_config。
FORWARD_BRIDGE_HOST = _env_first("ONEBOT_FORWARD_BRIDGE_HOST", default="127.0.0.1")
FORWARD_BRIDGE_PORT = _env_int("ONEBOT_FORWARD_BRIDGE_PORT", 8769, min_value=1, max_value=65535)
# Bridge 共享令牌，防止本机其他进程随意调用转发能力。工具通过环境变量拿到同一令牌。
FORWARD_BRIDGE_TOKEN = _env_first("ONEBOT_FORWARD_BRIDGE_TOKEN")
# 令牌为空时 bot 自签一份写到这里，工具进程按同一路径读；data/onebot_* 已在
# forward_authz.FILE_DENY_GLOBS 里，这份文件天然不能被 op=file 外传。
FORWARD_BRIDGE_TOKEN_FILE = _env_first(
    "ONEBOT_FORWARD_BRIDGE_TOKEN_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_forward_bridge_token"),
)

# 提交给 Supervisor 的 QQ conversation_key 使用稳定哈希，真实群号/QQ 号只保留在插件本地路由里。
# 显式配置的密钥，优先于自动密钥文件；为空时由 conversation_hash 决定加盐还是钉在无盐。
CONVERSATION_HASH_SECRET = os.environ.get("ONEBOT_CONVERSATION_HASH_SECRET", "").strip()

# 没显式配 secret 时用的自动密钥文件。内容是随机密钥或 legacy-unsalted 标记；
# 与 onebot_anon_map.json 同级敏感，不得进版本库。
CONVERSATION_HASH_SECRET_FILE = _env_first(
    "ONEBOT_CONVERSATION_HASH_SECRET_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_conversation_hash_secret"),
)

# 本地路由状态文件：保存 stable conversation_key/session_id 到真实 QQ 群/用户目标的映射。
ONEBOT_STATE_FILE = _env_first(
    "ONEBOT_STATE_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_plugin_state.json"),
)
# Durable pending/sent claims must survive SDK outbox replay and adapter restart.
ONEBOT_IDEMPOTENCY_STORE_FILE = _env_first(
    "ONEBOT_IDEMPOTENCY_STORE_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_outbound_idempotency.sqlite3"),
)
# Durable SDK/request identities outlive normal SDK backoff, but sent claims are
# retained for a finite period. Context-free safety fallback is intentionally short.
ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS = _env_float(
    "ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS", 7 * 24 * 3600.0, min_value=300.0,
)
ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS = _env_float(
    "ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS", 600.0, min_value=30.0,
)
ONEBOT_IDEMPOTENCY_MAX_ITEMS = _env_int(
    "ONEBOT_IDEMPOTENCY_MAX_ITEMS", 50_000, min_value=100, max_value=2_000_000,
)
# 死信永不过期时，内容摘要型 key 会把「这段文字发不出去」变成永久状态。
# 0 = 永久保留，只能靠 /发送死信 手工放行。
ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS = _env_float(
    "ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS", 24 * 3600.0, min_value=0.0,
)

# 引用消息附件索引缓存：保存 message_id -> 已落盘图片附件路径，用于 get_msg 失败时兜底转发。
# 与路由状态分离，避免 onebot_plugin_state.json 被临时缓存污染。
REPLY_ATTACHMENT_CACHE_FILE = _env_first(
    "ONEBOT_REPLY_ATTACHMENT_CACHE_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "cache", "onebot_reply_attachments.json"),
)

# 匿名别名映射持久化文件：保存 真实 QQ 号/群号 <-> UserX/GroupX 别名 的双向映射。
# 目的：跨重启保持别名一致，避免历史/记忆里同一人出现不同别名，并支持别名反解。
# 注意：这是一份“去匿名化字典”（明文对照真实号 <-> 别名），属于敏感文件，
# 必须与 data/config.yaml 同级别保护、不得提交到版本库。与路由状态分离存放。
ANON_MAP_FILE = _env_first(
    "ONEBOT_ANON_MAP_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_anon_map.json"),
)

# 匿名别名保留期（天）。0 = 不回收：反解 [at:UserX] 与文本掩码都依赖全量映射，
# 回收会让久未出现的人的真实号在文本里不再被替换。
ANON_MAP_RETENTION_DAYS = _env_float("ONEBOT_ANON_MAP_RETENTION_DAYS", 0.0, min_value=0.0)

# 适配器侧待审批保留时长，与 CLONOTH_APPROVAL_TIMEOUT_SECONDS 同量级；超时的不再可批。
PENDING_APPROVAL_TTL_SECONDS = _env_float("ONEBOT_PENDING_APPROVAL_TTL_SECONDS", 3600.0, min_value=60.0)

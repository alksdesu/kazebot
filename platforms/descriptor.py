"""平台描述的数据结构。只有数据，不导入任何平台库。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

CHANNEL_GROUP = "group"
CHANNEL_PRIVATE = "private"
CHANNEL_BRIDGE = "bridge"
CHANNEL_OTHER = "other"
CHANNEL_KINDS: frozenset[str] = frozenset({CHANNEL_GROUP, CHANNEL_PRIVATE, CHANNEL_BRIDGE, CHANNEL_OTHER})
# 真人对话的两种会话；bridge 是转发桥这类系统会话，other 是认不出种类时的兜底前缀。
CONVERSATION_KINDS: frozenset[str] = frozenset({CHANNEL_GROUP, CHANNEL_PRIVATE})

ID_NUMERIC = "numeric"
ID_STRING = "string"
# 平台侧 ID 在多大范围里唯一：整个平台（QQ 号），或各个机器人账号各一套（按应用发的 openid）。
ID_SCOPE_PLATFORM = "platform"
ID_SCOPE_ACCOUNT = "account"
ID_SCOPES: frozenset[str] = frozenset({ID_SCOPE_PLATFORM, ID_SCOPE_ACCOUNT})

STATE_LIVE = "live"
STATE_ROUTES = "routes"
STATE_ALIASES = "aliases"
STATE_ACTIVE_ACCOUNT = "active_account"
STATE_KINDS: frozenset[str] = frozenset({STATE_LIVE, STATE_ROUTES, STATE_ALIASES, STATE_ACTIVE_ACCOUNT})

HOOK_TRIGGER_POLICY = "trigger_policy"
HOOK_CONSOLE_SCHEMA = "console_schema"

# native 由驱动按平台 ID 渲染成真正的提及，text 只能写成「@名字」的文字。
MENTION_NONE = "none"
MENTION_NATIVE = "native"
MENTION_TEXT = "text"
MENTION_FORMS: frozenset[str] = frozenset({MENTION_NONE, MENTION_NATIVE, MENTION_TEXT})

# data/config.yaml 里 platforms.<平台 id>.<密钥名> 这两级键名；平台 id 还是 data/platforms/ 下的目录名。
_KEY_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
# config/ 下的文件控制台和 agent 在审批下就能改，还常被整份贴出去问问题，密钥不放那里。
_CONFIG_DIR = "config/"


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def check_workspace_glob(pattern: str, *, what: str) -> str:
    """工作区相对的 glob：正斜杠分段、不以 / 或盘符开头、没有空段和 . / .. 段。"""
    text = str(pattern or "")
    if not text or text != text.strip():
        raise ValueError(f"{what} must be a non-empty path without surrounding spaces: {pattern!r}")
    if "\\" in text or text.startswith("/") or text[1:2] == ":":
        raise ValueError(f"{what} must be a workspace-relative posix path: {pattern!r}")
    if any(part in {"", ".", ".."} for part in text.split("/")):
        raise ValueError(f"{what} must not contain empty, '.' or '..' segments: {pattern!r}")
    return text


@dataclass(frozen=True)
class ChannelDescriptor:
    # 会话通道名，同时是会话键前缀。
    name: str
    kind: str
    # 会话键是否为「前缀:摘要」。转发桥这类会话直接用真实目标，没有摘要。
    hashed: bool = True

    def __post_init__(self) -> None:
        if self.kind not in CHANNEL_KINDS:
            raise ValueError(f"unknown channel kind: {self.kind!r}")


@dataclass(frozen=True)
class ReplyQuota:
    # 收到一条消息后 window_sec 秒内最多回 limit 条，超出的平台直接拒收。
    limit: int
    window_sec: float

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.window_sec <= 0:
            raise ValueError("a reply quota needs a positive limit and window")

    def public_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "window_sec": self.window_sec}


@dataclass(frozen=True)
class ImageSupport:
    formats: frozenset[str]
    # 0 表示没有已知上限。
    max_bytes: int = 0
    animated: bool = True
    inline_with_text: bool = True
    # 要先上传换凭据才能发，不能直接给路径或 base64。
    upload_first: bool = False

    def __post_init__(self) -> None:
        formats = frozenset(name for name in (normalize_name(fmt) for fmt in self.formats) if name)
        if not formats:
            raise ValueError("image support needs at least one format")
        if self.max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        object.__setattr__(self, "formats", formats)

    def public_dict(self) -> dict[str, Any]:
        return {
            "formats": sorted(self.formats),
            "max_bytes": self.max_bytes,
            "animated": self.animated,
            "inline_with_text": self.inline_with_text,
            "upload_first": self.upload_first,
        }


@dataclass(frozen=True)
class PlatformCapabilities:
    """平台驱动能发什么、有什么限制。默认值按纯文本平台取，富媒体和互动能力没声明就当不支持。"""

    # 单条消息的字数上限；0 表示没有已知上限，只按配置截断。
    max_text_length: int = 0
    markdown: bool = False
    # 一次回复最多分几条；0 表示不限。
    max_split_parts: int = 0
    # 被动回复额度，按会话种类给，没列出的种类不限。
    reply_quotas: Mapping[str, ReplyQuota] = field(default_factory=dict)
    mention: str = MENTION_NONE
    mention_everyone: bool = False
    quote: bool = False
    # 能在别人的消息上贴表态的会话种类；可用的表态由驱动运行时给出。
    reactions: frozenset[str] = frozenset()
    images: ImageSupport | None = None
    # 平台自己的表情，比如 QQ 收藏表情、Discord 服务器表情；目录由驱动运行时给出。
    native_emoji: bool = False
    merged_forward: bool = False
    proactive: bool = True
    # 主动消息每天的额度；0 表示不限。
    proactive_daily_limit: int = 0
    # 能显示「输入中」的会话种类。
    typing: frozenset[str] = frozenset()
    progress_edit: bool = False
    recall: bool = False
    edit: bool = False
    links: bool = True

    def __post_init__(self) -> None:
        if self.mention not in MENTION_FORMS:
            raise ValueError(f"unknown mention form: {self.mention!r}")
        if self.mention_everyone and self.mention == MENTION_NONE:
            raise ValueError("mention_everyone needs a mention form")
        for name in ("max_text_length", "max_split_parts", "proactive_daily_limit"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        kinds = {*self.reply_quotas, *self.reactions, *self.typing}
        if not kinds <= CONVERSATION_KINDS:
            raise ValueError(f"capabilities only apply to conversation kinds, got {sorted(kinds - CONVERSATION_KINDS)}")
        object.__setattr__(self, "reply_quotas", MappingProxyType(dict(self.reply_quotas)))
        object.__setattr__(self, "reactions", frozenset(self.reactions))
        object.__setattr__(self, "typing", frozenset(self.typing))

    def public_dict(self) -> dict[str, Any]:
        return {
            "max_text_length": self.max_text_length,
            "markdown": self.markdown,
            "max_split_parts": self.max_split_parts,
            "reply_quotas": {kind: quota.public_dict() for kind, quota in sorted(self.reply_quotas.items())},
            "mention": self.mention,
            "mention_everyone": self.mention_everyone,
            "quote": self.quote,
            "reactions": sorted(self.reactions),
            "images": None if self.images is None else self.images.public_dict(),
            "native_emoji": self.native_emoji,
            "merged_forward": self.merged_forward,
            "proactive": self.proactive,
            "proactive_daily_limit": self.proactive_daily_limit,
            "typing": sorted(self.typing),
            "progress_edit": self.progress_edit,
            "recall": self.recall,
            "edit": self.edit,
            "links": self.links,
        }


@dataclass(frozen=True)
class SecretField:
    """平台的一项密钥。值写在 data/config.yaml 的 platforms.<平台 id> 段或环境变量里，不进 config/。"""

    name: str
    label: str
    # 部署时惯用的环境变量。data/config.yaml 没写这一项时才读它。
    env: str = ""
    # 两处都没配时平台自己生成、写进这里的文件，相对工作区。空串表示不自动生成。
    file: str = ""
    # 改那份自动文件位置的环境变量。只给已经在用它的平台，新平台不加。
    file_env: str = ""

    def __post_init__(self) -> None:
        if not _KEY_NAME.match(self.name):
            raise ValueError(f"secret name must be lower_snake_case: {self.name!r}")
        if not self.label.strip():
            raise ValueError(f"secret {self.name!r} needs a label")
        for env in (self.env, self.file_env):
            if env and not _ENV_NAME.match(env):
                raise ValueError(f"secret {self.name!r} env name must be UPPER_SNAKE_CASE: {env!r}")
        if self.file_env and not self.file:
            raise ValueError(f"secret {self.name!r} declares file_env without a default file")
        if self.file:
            check_workspace_glob(self.file, what=f"secret {self.name!r} file")
            if self.file.startswith(_CONFIG_DIR):
                raise ValueError(f"secret {self.name!r} file must not live under config/: {self.file!r}")


@dataclass(frozen=True)
class PlatformDescriptor:
    id: str
    display_name: str
    # 称呼这个平台的用户时用的前缀，比如提醒署名「QQ 12345」。
    short_name: str
    # platform_auth.platform 的历史取值，读到它们都算这个平台。
    aliases: frozenset[str] = frozenset()
    channels: tuple[ChannelDescriptor, ...] = ()
    id_shape: str = ID_STRING
    # 按账号分的 ID 在别名表里带上账号记，两个号各自的同一串 ID 不会被当成同一个人。拿不准就按账号分。
    id_scope: str = ID_SCOPE_ACCOUNT
    # 加盐、加账号隔离之前就攒下会话数据的平台：会话键允许不带账号作用域，首次启动发现存量数据就钉在无盐上。
    # 只给已经在用它的平台，新平台不加。
    legacy_conversation_keys: bool = False
    # 平台配置文件，相对工作区；空串表示这个平台没有热载配置。
    config_path: str = ""
    # 覆盖配置路径的环境变量。只给已经在用它的平台，新平台不加。
    config_env: str = ""
    # 按人的资料（称呼、头衔），相对工作区，键和别名表一样；空串表示没有。里面是明文 ID，自动算作私密文件。
    profiles_path: str = ""
    # 覆盖资料文件路径的环境变量。只给已经在用它的平台，新平台不加。
    profiles_env: str = ""
    # 由适配器写、核心只读的状态文件：live 公布生效配置与运行事实，routes 是稳定会话键到真实会话键，
    # aliases 是匿名别名表，active_account 是当前登录账号，会话键摘要按它分作用域。
    # 默认在 data/platforms/<id>/ 下用标准文件名；这里只给升级前就在用老文件名的平台，写 data/ 下的相对路径。
    state_files: Mapping[str, str] = field(default_factory=dict)
    # 改老状态文件位置的环境变量，按状态项。只给已经在用它的平台，新平台不加。
    state_env: Mapping[str, str] = field(default_factory=dict)
    # 核心层需要调用的平台策略：名字 → 模块路径，按需导入。
    hooks: Mapping[str, str] = field(default_factory=dict)
    capabilities: PlatformCapabilities = field(default_factory=PlatformCapabilities)
    # 平台自己的密钥：连接令牌、AppSecret 这类。读取顺序是 data/config.yaml 的 platforms 段 > env > 自动生成的文件。
    secrets: tuple[SecretField, ...] = ()
    # 其他夹着凭据的文件（glob，相对工作区），比如排队的账号操作里带着管理员令牌。
    # 和自动生成的密钥文件一样，agent 读、写、经聊天外传都拒绝。
    secret_files: tuple[str, ...] = ()
    # 只由平台自己的代码写的文件（glob）：agent 改写一律拒绝，也不能外传。state_files 自动算在内。
    owned_files: tuple[str, ...] = ()
    # 记着真实号码、审批下允许修改的文件（glob）：只是不能经聊天外传。配置文件自动算在内。
    private_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _KEY_NAME.match(self.id):
            raise ValueError(f"platform id must be lower_snake_case: {self.id!r}")
        if self.id_shape not in {ID_NUMERIC, ID_STRING}:
            raise ValueError(f"unknown id shape: {self.id_shape!r}")
        if self.id_scope not in ID_SCOPES:
            raise ValueError(f"unknown id scope: {self.id_scope!r}")
        names = [channel.name for channel in self.channels]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate channel names in platform {self.id!r}")
        declared = {*self.capabilities.reply_quotas, *self.capabilities.reactions, *self.capabilities.typing}
        missing = declared - {channel.kind for channel in self.channels}
        if missing:
            raise ValueError(f"platform {self.id!r} declares capabilities for kinds it has no channel for: {sorted(missing)}")
        secret_names = [secret.name for secret in self.secrets]
        if len(secret_names) != len(set(secret_names)):
            raise ValueError(f"duplicate secret names in platform {self.id!r}")
        secret_envs = [secret.env for secret in self.secrets if secret.env]
        if len(secret_envs) != len(set(secret_envs)):
            raise ValueError(f"duplicate secret env names in platform {self.id!r}")
        for kind, patterns in (
            ("secret_files", self.secret_files),
            ("owned_files", self.owned_files),
            ("private_files", self.private_files),
        ):
            for pattern in patterns:
                check_workspace_glob(pattern, what=f"platform {self.id!r} {kind} entry")
        misplaced = [pattern for pattern in self.secret_files if pattern.startswith(_CONFIG_DIR)]
        if misplaced:
            raise ValueError(f"platform {self.id!r} keeps secret files under config/: {misplaced}")
        self._check_movable_file("config", self.config_path, self.config_env)
        self._check_movable_file("profiles", self.profiles_path, self.profiles_env)
        self._check_state_files()
        object.__setattr__(self, "state_files", MappingProxyType(dict(self.state_files)))
        object.__setattr__(self, "state_env", MappingProxyType(dict(self.state_env)))
        object.__setattr__(self, "hooks", MappingProxyType(dict(self.hooks)))
        for name in ("secrets", "secret_files", "owned_files", "private_files"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def _check_movable_file(self, what: str, path: str, env: str) -> None:
        # 保护规则把这个路径原样当 glob 用，写歪了等于没护住。
        if path:
            check_workspace_glob(path, what=f"platform {self.id!r} {what} file")
        if env and not path:
            raise ValueError(f"platform {self.id!r} moves a {what} file it does not have")
        if env and not _ENV_NAME.match(env):
            raise ValueError(f"platform {self.id!r} {what} env name must be UPPER_SNAKE_CASE: {env!r}")

    def _check_state_files(self) -> None:
        unknown = sorted(set(self.state_files) - STATE_KINDS)
        if unknown:
            raise ValueError(f"platform {self.id!r} declares unknown state files: {unknown}")
        for name in self.state_files.values():
            check_workspace_glob(f"data/{name}", what=f"platform {self.id!r} state file")
        standard = sorted(set(self.state_env) - set(self.state_files))
        if standard:
            raise ValueError(f"platform {self.id!r} moves state files it keeps in the standard place: {standard}")
        for env in self.state_env.values():
            if not _ENV_NAME.match(env):
                raise ValueError(f"platform {self.id!r} state env name must be UPPER_SNAKE_CASE: {env!r}")

    def names(self) -> frozenset[str]:
        return frozenset({self.id, *self.aliases, *(channel.name for channel in self.channels)})

    def channel(self, name: Any) -> ChannelDescriptor | None:
        key = normalize_name(name)
        return next((channel for channel in self.channels if channel.name == key), None)

    def secret(self, name: str) -> SecretField:
        """按名字取一项密钥的声明。没声明就是调用方写错了，直接抛。"""
        for secret in self.secrets:
            if secret.name == name:
                return secret
        raise KeyError(f"platform {self.id!r} declares no secret {name!r}")

    def public_dict(self) -> dict[str, Any]:
        """给控制台看的那一份：不含模块路径、环境变量名这类部署细节。"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "short_name": self.short_name,
            "id_shape": self.id_shape,
            "channels": [
                {"name": channel.name, "kind": channel.kind, "hashed": channel.hashed}
                for channel in self.channels
            ],
            "has_config": bool(self.config_path),
            "hooks": sorted(self.hooks),
            "capabilities": self.capabilities.public_dict(),
        }

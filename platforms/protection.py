"""由平台声明推导的保护：经聊天外传的黑名单、policy 的兜底规则、子进程里要剥掉的环境变量，
以及每个环境变量该写在哪：本实例 .env、共享密钥文件，还是只认进程环境。

新平台在描述里声明了密钥和私密文件就自动受保护，这几处不用再跟着改。
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple

import config_layout
from real_paths import real_path

from .descriptor import HOOK_PROCESS_ENV, PlatformDescriptor
from .registry import (
    PLATFORM_ENV,
    MissingPlatformHook,
    alias_numbering_relpath,
    all_platforms,
    env_override,
    platform_data_relpath,
    platform_hook,
    workspace_path,
)

_log = logging.getLogger(__name__)

SHARED_SECRETS_FILE = config_layout.SHARED_SECRETS_FILE

# 和平台无关的凭据位置：dotenv、模型渠道和 MCP 的密钥、平台令牌、管理令牌、policy 本身、私钥与证书。
# 挪进 config/ 之前的旧位置也在：搬家只复制不删，旧文件里还是同一把密钥。
CORE_OUTBOUND_DENY: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    SHARED_SECRETS_FILE,
    config_layout.MODELS_FILE,
    config_layout.POLICY_FILE,
    config_layout.MCP_FILE,
    f"{config_layout.DRAWTOOLS_DIR}/settings.yaml",
    config_layout.PLATFORM_SECRETS_FILE,
    config_layout.LEGACY_MODELS_FILE,
    config_layout.LEGACY_POLICY_FILE,
    config_layout.LEGACY_MCP_FILE,
    f"{config_layout.LEGACY_DRAWTOOLS_DIR}/settings.yaml",
    "data/.admin_token",
    # 配置端点覆盖写入前会留 .bak，副本的敏感度等于原文件（config/models.yaml.bak 里就是明文 api_key），
    # 按原文件名匹配的规则一条都盖不住多出来的后缀。
    "*.bak",
    "id_rsa*",
    "*/id_rsa*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
)


@dataclass(frozen=True)
class FloorRule:
    """policy 兜底规则：排在 config/policy.yaml 的规则前面，改那份文件放不开。"""

    pattern: str
    reason: str


# 改造后才有的两份密钥文件，老部署的 policy.yaml 里没有它们的规则，也没有旧名字可认。
CORE_READ_FLOOR: tuple[FloorRule, ...] = (
    FloorRule(SHARED_SECRETS_FILE, "shared api keys travel with the config group"),
    FloorRule(config_layout.PLATFORM_SECRETS_FILE, "platform tokens are the instance identity"),
)

# 不属于哪个平台、由核心代码维护的文件。编号被改小，新发的别名就会撞上旧记忆里的人。
# 两份密钥文件只经 manage_secret 和控制台逐键改。
CORE_WRITE_FLOOR: tuple[FloorRule, ...] = (
    FloorRule(f"{alias_numbering_relpath()}*", "alias numbering shared by every platform"),
    *CORE_READ_FLOOR,
)


def _platforms(platforms: Iterable[PlatformDescriptor] | None) -> tuple[PlatformDescriptor, ...]:
    return all_platforms() if platforms is None else tuple(platforms)


def _unique(patterns: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(pattern for pattern in patterns if pattern))


def _moved_to(raw: str, workspace: Path | None) -> str:
    """env 改到的文件位置换成判定用的写法：工作区里（含经受信链接通出去的）是相对路径，工作区外是绝对路径。

    和 policy 的 classify_path 走同一个函数，两边才比得上。
    """
    if workspace is None:
        return Path(raw).expanduser().as_posix()
    root = real_path(workspace)
    resolved = real_path(workspace_path(raw, root))
    logical = config_layout.workspace_logical_path(root, resolved)
    return logical if logical is not None else resolved.as_posix()


def secret_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """带着凭据的文件：声明的 secret_files、自动生成的密钥文件，以及 env 把它挪到的位置。"""
    patterns = list(platform.secret_files)
    for secret in platform.secrets:
        if not secret.file:
            continue
        patterns.append(secret.file)
        moved = env_override(secret.file_env, environ)
        if moved:
            patterns.append(_moved_to(moved, workspace))
    return _unique(patterns)


def owned_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """只由平台自己的代码写的文件：平台数据目录、老文件名的状态文件（含 env 改到的位置）、声明的 owned_files，以及带凭据的文件。"""
    state = [f"{platform_data_relpath(platform)}/*"]
    for kind, name in platform.state_files.items():
        state.append(f"data/{name}")
        moved = env_override(platform.state_env.get(kind, ""), environ)
        if moved:
            state.append(_moved_to(moved, workspace))
    secrets = secret_file_patterns(platform, environ=environ, workspace=workspace)
    return _unique([*state, *platform.owned_files, *secrets])


def private_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """不能经聊天外传的文件：配置文件和按人资料（含 env 改到的位置）、声明的 private_files，以及上面两类。"""
    declared: list[str] = []
    for path, env in ((platform.config_path, platform.config_env), (platform.profiles_path, platform.profiles_env)):
        if not path:
            continue
        declared.append(path)
        moved = env_override(env, environ)
        if moved:
            declared.append(_moved_to(moved, workspace))
    owned = owned_file_patterns(platform, environ=environ, workspace=workspace)
    return _unique([*declared, *platform.private_files, *owned])


def outbound_deny_patterns(
    *,
    platforms: Iterable[PlatformDescriptor] | None = None,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """经聊天发出去的文件黑名单。每个平台的私密文件都在里面：从 A 平台发出 B 平台的别名表一样是泄露。"""
    per_platform = (
        pattern
        for platform in _platforms(platforms)
        for pattern in private_file_patterns(platform, environ=environ, workspace=workspace)
    )
    return _unique([*CORE_OUTBOUND_DENY, *per_platform])


def normalize_relative(rel_path: str) -> str:
    """模型会写 ./ 前缀、反斜杠、首尾空白，判定前统一成工作区相对的正斜杠写法。"""
    rel = str(rel_path or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def outbound_deny_reason(
    rel_path: str,
    *,
    via: str,
    platforms: Iterable[PlatformDescriptor] | None = None,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> str:
    """命中外传黑名单时返回拒绝原因，空串表示允许。via 是发出去经过的平台简称，写进原因里。"""
    rel = normalize_relative(rel_path)
    if not rel:
        return ""
    for pattern in outbound_deny_patterns(platforms=platforms, environ=environ, workspace=workspace):
        if fnmatch.fnmatchcase(rel, pattern):
            return f"{rel}：密钥、凭据和记着真实号码的文件禁止通过 {via} 发送。"
    return ""


def policy_floor(
    *,
    platforms: Iterable[PlatformDescriptor] | None = None,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[tuple[FloorRule, ...], tuple[FloorRule, ...]]:
    """返回 (read_file 拒绝, write_file 拒绝) 两组兜底规则。

    读只挡带凭据的文件：状态文件和配置里是真实号码，管理员审批下读得了。
    写挡平台自有的全部文件：它们由平台进程维护，agent 改一笔就能让会话键分叉、路由指错人。
    配置文件不在其中，审批下改 qq.yaml 这类照常可以。
    """
    read: list[FloorRule] = list(CORE_READ_FLOOR)
    write: list[FloorRule] = list(CORE_WRITE_FLOOR)
    for platform in _platforms(platforms):
        for pattern in secret_file_patterns(platform, environ=environ, workspace=workspace):
            read.append(FloorRule(pattern, f"secret file declared by platform {platform.id}"))
        for pattern in owned_file_patterns(platform, environ=environ, workspace=workspace):
            write.append(FloorRule(pattern, f"file maintained by platform {platform.id}"))
    return tuple(read), tuple(write)


def secret_env_names(platforms: Iterable[PlatformDescriptor] | None = None) -> frozenset[str]:
    """平台密钥的环境变量名。子进程里剥掉它们：agent 一句 echo 就能读到。"""
    return frozenset(
        secret.env
        for platform in _platforms(platforms)
        for secret in platform.secrets
        if secret.env
    )


# 核心进程的实例身份：平台、端口、监听地址、URL 前缀、工作区、清单位置、管理令牌、数据清理范围。
_CORE_INSTANCE_ENV: frozenset[str] = frozenset({
    PLATFORM_ENV,
    "CLONOTH_PORT",
    "CLONOTH_BASE_URL",
    "CLONOTH_HOST",
    "CLONOTH_URL_PREFIX",
    "CLONOTH_WORKSPACE",
    "ONEBOT_WORKSPACE_ROOT",
    config_layout.INSTANCES_FILE_ENV,
    "CLONOTH_ADMIN_TOKEN",
    "CLONOTH_MAINTENANCE_SCOPE",
})
# 值是路径的变量指着本实例工作区里的某处，抄给别的实例就指到别人家里去了。
_PATH_ENV_SUFFIXES: tuple[str, ...] = ("_FILE", "_PATH", "_DIR", "_ROOT", "_ROOTS")

# 核心代码直接从进程环境读的设置（os.getenv、env_bool），不经共享密钥文件那一层，写进去也读不到。
_CORE_PROCESS_SETTINGS: frozenset[str] = frozenset({
    "CLONOTH_TASK_MAX_RUNNING_SECONDS",
    "CLONOTH_APPROVAL_TIMEOUT_SECONDS",
    "CLONOTH_LLM_TRUST_ENV",
    "CLONOTH_LOG_LEVEL",
    "CLONOTH_ACCESS_LOG",
    "CLONOTH_SHELL_NEW_CONSOLE",
    "CLONOTH_SPAWN_SHELL_CLI",
    "CLONOTH_SUPERVISOR_LOG_MAX_BYTES",
    "CLONOTH_SUPERVISOR_LOG_BACKUPS",
    "CLONOTH_EVENTS_MAX_BYTES",
    "CLONOTH_EVENTS_BACKUPS",
    "CLONOTH_SIGNALS_MAX_BYTES",
    "CLONOTH_SIGNALS_BACKUPS",
    "CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS",
    "CLONOTH_TASK_CONVERSATION_MAX_AGE_SECONDS",
    "CLONOTH_TRANSCRIPTS_MAX_AGE_SECONDS",
    "CLONOTH_LOG_MAX_AGE_SECONDS",
    "CLONOTH_LLM_ERROR_SNAPSHOT_MAX_AGE_SECONDS",
    "CLONOTH_STOCKTOOL_CACHE_MAX_AGE_SECONDS",
    "CLONOTH_MATERIALS_FONT",
    # 后三个是会被执行的程序路径。
    "CLONOTH_MATERIALS_PYTHON",
    "CLONOTH_MATERIALS_SOFFICE",
    "CLONOTH_MATERIALS_TESSERACT",
})
# 进程拉起子进程、调工具时现填进去的上下文，写进哪份文件都只会让所有子进程带上一个错的值。
_CORE_INJECTED_ENV: frozenset[str] = frozenset({
    "CLONOTH_SUPERVISOR_URL",
    "CLONOTH_URL",
    "CLONOTH_WORKER_ID",
    "CLONOTH_CONVERSATION_KEY",
    "CLONOTH_SESSION_ID",
    "CLONOTH_PARENT_SESSION_ID",
    "CLONOTH_TASK_ID",
    "CLONOTH_NODE_ID",
    "CLONOTH_TOOL_CALL_ID",
    "CLONOTH_REQUEST_ID",
    "CLONOTH_TOOL_INVOCATION_ID",
    "CLONOTH_TOOL_REQUEST_ID",
    "CLONOTH_LLM_REQUEST_ID",
    "CLONOTH_PROCESS_TAG",
    "CLONOTH_ORIGIN_VENV",
})
# httpx、urllib 从进程环境读的代理。Linux 上两种大小写是两个变量，部署的代理配置两种都设，urllib 先读小写的。
_PROXY_ENV: frozenset[str] = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})
# 解释器自己读的设置。
_INTERPRETER_ENV_PREFIXES: tuple[str, ...] = ("PYTHON",)

ENV_INSTANCE = "instance"
ENV_PROCESS = "process"
ENV_SHARED = "shared"

# 判定依据，提示语按它措辞。
BASIS_DECLARED = "declared"
BASIS_PATH = "path"
BASIS_SETTING = "setting"
BASIS_INJECTED = "injected"
BASIS_PROXY = "proxy"
BASIS_INTERPRETER = "interpreter"


class EnvScope(NamedTuple):
    """一个环境变量名归哪一层。

    instance：只写本实例 .env，共享密钥文件不收，建组也不抄；
    process：只认进程环境，共享密钥文件服务不了，manage_secret 一份文件都不写；
    shared：共享密钥文件 config/secrets.env 服务得了。platform 是声明它的平台 id，核心代码的为空。
    """

    kind: str
    basis: str = ""
    platform: str = ""


# 坏掉的平台 process_env 钩子已经告警过的：判定每改一个变量都要跑一遍。
_warned_hooks: set[tuple[str, str]] = set()


def instance_env_names(platforms: Iterable[PlatformDescriptor] | None = None) -> frozenset[str]:
    """实例身份的环境变量名：只写在实例自己的 .env 里，共享密钥文件不收，建组时也不抄过去。"""
    names = set(_CORE_INSTANCE_ENV)
    for platform in _platforms(platforms):
        names.update(_platform_instance_env(platform))
    return frozenset(names)


def _platform_instance_env(platform: PlatformDescriptor) -> frozenset[str]:
    names = set(platform.instance_env)
    names.update(secret.env for secret in platform.secrets if secret.env)
    names.update(secret.file_env for secret in platform.secrets if secret.file_env)
    names.update(env for env in (platform.config_env, platform.profiles_env) if env)
    names.update(platform.state_env.values())
    return frozenset(names)


def _platform_process_env(platform: PlatformDescriptor) -> frozenset[str]:
    """平台 process_env 钩子报的名字。没声明是空的；钩子坏了也按空的算，退回只认核心名单。"""
    try:
        return frozenset(str(name) for name in platform_hook(platform, HOOK_PROCESS_ENV).process_env_names())
    except MissingPlatformHook:
        return frozenset()
    except Exception as exc:
        marker = (platform.id, repr(exc))
        if marker not in _warned_hooks:
            _warned_hooks.add(marker)
            _log.warning("平台 %s 的 process_env 钩子用不了，只按核心名单判：%s", platform.id, exc)
        return frozenset()


def process_env_names(platforms: Iterable[PlatformDescriptor] | None = None) -> frozenset[str]:
    """只认进程环境的变量名：核心的设置、运行时注入的上下文、代理，加上各平台钩子报的。PYTHON* 按前缀另判。"""
    names = set(_CORE_PROCESS_SETTINGS | _CORE_INJECTED_ENV | _PROXY_ENV)
    for platform in _platforms(platforms):
        names.update(_platform_process_env(platform))
    return frozenset(names)


def _upper(names: Iterable[str]) -> frozenset[str]:
    return frozenset(name.upper() for name in names)


def env_scope(name: str, platforms: Iterable[PlatformDescriptor] | None = None) -> EnvScope:
    """name 归哪一层。实例身份先判（声明的，再按路径后缀），其次只认进程环境的，都不是就算共享。

    名字比较不分大小写；共享是兜底：${VAR} 能引用任意名字，共享那一类没法正面列全。
    """
    key = str(name or "").strip().upper()
    listed = _platforms(platforms)
    if key in _upper(_CORE_INSTANCE_ENV):
        return EnvScope(ENV_INSTANCE, BASIS_DECLARED)
    for platform in listed:
        if key in _upper(_platform_instance_env(platform)):
            return EnvScope(ENV_INSTANCE, BASIS_DECLARED, platform.id)
    if key.endswith(_PATH_ENV_SUFFIXES):
        return EnvScope(ENV_INSTANCE, BASIS_PATH)
    if key in _upper(_CORE_INJECTED_ENV):
        return EnvScope(ENV_PROCESS, BASIS_INJECTED)
    if key in _upper(_CORE_PROCESS_SETTINGS):
        return EnvScope(ENV_PROCESS, BASIS_SETTING)
    if key in _upper(_PROXY_ENV):
        return EnvScope(ENV_PROCESS, BASIS_PROXY)
    if key.startswith(_INTERPRETER_ENV_PREFIXES):
        return EnvScope(ENV_PROCESS, BASIS_INTERPRETER)
    for platform in listed:
        if key in _upper(_platform_process_env(platform)):
            return EnvScope(ENV_PROCESS, BASIS_SETTING, platform.id)
    return EnvScope(ENV_SHARED)


def is_instance_env(name: str, platforms: Iterable[PlatformDescriptor] | None = None) -> bool:
    return env_scope(name, platforms).kind == ENV_INSTANCE


def is_process_env(name: str, platforms: Iterable[PlatformDescriptor] | None = None) -> bool:
    return env_scope(name, platforms).kind == ENV_PROCESS


def is_shareable_env(name: str, platforms: Iterable[PlatformDescriptor] | None = None) -> bool:
    """共享密钥文件服务得了、建组时可以抄过去的名字。"""
    return env_scope(name, platforms).kind == ENV_SHARED

"""由平台声明推导的保护：经聊天外传的黑名单、policy 的兜底规则、子进程里要剥掉的环境变量。

新平台在描述里声明了密钥和私密文件就自动受保护，这几处不用再跟着改。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import config_layout

from .descriptor import PlatformDescriptor
from .registry import (
    PLATFORM_ENV,
    alias_numbering_relpath,
    all_platforms,
    env_override,
    platform_data_relpath,
    workspace_path,
)

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
    root = Path(workspace).resolve()
    resolved = workspace_path(raw, root).resolve()
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
# 解释器自己的设置，谈不上配置也谈不上密钥。
_PROCESS_ENV_PREFIXES: tuple[str, ...] = ("PYTHON",)


def instance_env_names(platforms: Iterable[PlatformDescriptor] | None = None) -> frozenset[str]:
    """实例身份的环境变量名：只写在实例自己的 .env 里，共享密钥文件不收，建组时也不抄过去。"""
    names = set(_CORE_INSTANCE_ENV)
    for platform in _platforms(platforms):
        names.update(platform.instance_env)
        names.update(secret.env for secret in platform.secrets if secret.env)
        names.update(secret.file_env for secret in platform.secrets if secret.file_env)
        names.update(env for env in (platform.config_env, platform.profiles_env) if env)
        names.update(platform.state_env.values())
    return frozenset(names)


def is_instance_env(name: str, platforms: Iterable[PlatformDescriptor] | None = None) -> bool:
    key = str(name or "").strip().upper()
    return (
        key in instance_env_names(platforms)
        or key.endswith(_PATH_ENV_SUFFIXES)
        or key.startswith(_PROCESS_ENV_PREFIXES)
    )

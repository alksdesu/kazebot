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
    alias_numbering_relpath,
    all_platforms,
    env_override,
    platform_data_relpath,
    workspace_path,
)

# 共享组的密钥文件：config/ 整份共享时跟着走的那几个 API key。
SHARED_SECRETS_FILE = f"{config_layout.CONFIG_DIR}/secrets.env"

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
    """env 改到的文件位置换成判定用的写法：工作区里是相对路径，工作区外是绝对路径。

    和 policy 的 classify_path 一样先 resolve，两边才比得上。
    """
    if workspace is None:
        return Path(raw).expanduser().as_posix()
    root = Path(workspace).resolve()
    resolved = workspace_path(raw, root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


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

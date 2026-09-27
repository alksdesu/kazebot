"""由平台声明推导的保护：经聊天外传的黑名单、policy 的兜底规则、子进程里要剥掉的环境变量。

新平台在描述里声明了密钥和私密文件就自动受保护，这几处不用再跟着改。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .descriptor import PlatformDescriptor
from .registry import all_platforms

# 和平台无关的凭据位置：dotenv、模型渠道密钥、管理令牌、policy 本身、私钥与证书。
CORE_OUTBOUND_DENY: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "data/config.yaml",
    "data/.admin_token",
    "data/policy.yaml",
    # 配置端点覆盖写入前会留 .bak，副本的敏感度等于原文件（data/config.yaml.bak 里就是明文 api_key），
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
    """policy 兜底规则：排在 data/policy.yaml 的规则前面，改那份文件放不开。"""

    pattern: str
    reason: str


def _platforms(platforms: Iterable[PlatformDescriptor] | None) -> tuple[PlatformDescriptor, ...]:
    return all_platforms() if platforms is None else tuple(platforms)


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _unique(patterns: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(pattern for pattern in patterns if pattern))


def _moved_to(raw: str, workspace: Path | None) -> str:
    """env 改到的文件位置换成判定用的写法：工作区里是相对路径，工作区外是绝对路径。

    和 policy 的 classify_path 一样先 resolve，两边才比得上。相对写法按工作区算：bot 和工具都以它为 cwd。
    """
    path = Path(raw).expanduser()
    if workspace is None:
        return path.as_posix()
    root = Path(workspace).resolve()
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _override(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "") or "").strip() if name else ""


def secret_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """带着凭据的文件：声明的 secret_files、自动生成的密钥文件，以及 env 把它挪到的位置。"""
    env = _environ(environ)
    patterns = list(platform.secret_files)
    for secret in platform.secrets:
        if not secret.file:
            continue
        patterns.append(secret.file)
        moved = _override(env, secret.file_env)
        if moved:
            patterns.append(_moved_to(moved, workspace))
    return _unique(patterns)


def owned_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """只由平台自己的代码写的文件：状态文件、声明的 owned_files，以及带凭据的文件。"""
    state = (f"data/{name}" for name in platform.state_files.values())
    secrets = secret_file_patterns(platform, environ=environ, workspace=workspace)
    return _unique([*state, *platform.owned_files, *secrets])


def private_file_patterns(
    platform: PlatformDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """不能经聊天外传的文件：配置文件（含 env 改到的位置）、声明的 private_files，以及上面两类。"""
    config: list[str] = []
    if platform.config_path:
        config.append(platform.config_path)
        moved = _override(_environ(environ), platform.config_env)
        if moved:
            config.append(_moved_to(moved, workspace))
    owned = owned_file_patterns(platform, environ=environ, workspace=workspace)
    return _unique([*config, *platform.private_files, *owned])


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
    read: list[FloorRule] = []
    write: list[FloorRule] = []
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

from __future__ import annotations

import contextlib
import copy
import fnmatch
import ipaddress
import logging
import os
import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

import config_layout
from clonoth_runtime import classify_path, parse_bool, parse_extra_roots
from platforms.protection import policy_floor
from platforms.registry import all_platforms

from .types import SafetyLevel

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyDecision:
    safety_level: SafetyLevel
    reason: str
    # Why: 管理员触发的 QQ 任务对普通命令/文件操作免审批，但改源码、
    # 重启、动 config/nodes、data 等敏感操作仍需人工确认。sensitive=True 表示
    # 本决策命中了显式敏感规则(而非默认放行或普通命令)，即使是管理员也不免审批。
    sensitive: bool = False


@dataclass(frozen=True)
class PathScope:
    """按哪个任务判路径。

    隔离在 git worktree 里的任务带 worktree（登记号）、project_root（副本里的项目根）和 project_origin（它在原仓库里
    对应的目录）：副本里的路径按原仓库里同一位置判。foreign_root 是放全部副本的目录，不是本任务的一律算外部。
    mappings 是各个副本目录到原仓库目录的对照，命令文本里出现副本的绝对路径时换成原仓库的路径再查敏感路径。
    invalid 非空表示这个任务登记的副本已经用不了，跟路径有关的操作一律拒绝。
    """

    project_root: Path | None = None
    project_origin: Path | None = None
    foreign_root: Path | None = None
    origin_repo: Path | None = None
    worktree: str = ""
    mappings: tuple[tuple[Path, Path], ...] = ()
    invalid: str = ""

    @property
    def isolated(self) -> bool:
        return bool(self.worktree)


APPROVAL_GRANT_DEFAULT_MINUTES = 30
APPROVAL_GRANT_MAX_MINUTES = 1440
# 再长就等于永久免审。
APPROVAL_GRANT_MINUTES_CEILING = 365 * 24 * 60


@dataclass(frozen=True)
class ApprovalGrantSettings:
    """同意审批时给审批人本人授权多久；permanent_for_admins 为真时管理员名单里的人可以要永久授权。"""

    default_minutes: int = APPROVAL_GRANT_DEFAULT_MINUTES
    max_minutes: int = APPROVAL_GRANT_MAX_MINUTES
    permanent_for_admins: bool = True

    def minutes_for(self, requested: int | None) -> int:
        """没指定按默认，指定了按指定的，都不超过上限；0 表示不授权。"""
        wanted = self.default_minutes if requested is None else requested
        return max(0, min(int(wanted), self.max_minutes))


def _entrypoint_write_rules() -> list[dict[str, str]]:
    """平台进程的入口脚本，由各平台描述声明：新平台不用来这里加一行。"""
    return [
        {
            "pattern": pattern,
            "decision": "approval_required",
            "reason": f"{platform.id} adapter entrypoint changes require approval",
        }
        for platform in all_platforms()
        for pattern in platform.entrypoints
    ]


def _default_policy_dict() -> dict[str, Any]:
    """Built-in default policy.

    注意：在不引入 OS 沙盒的前提下，这套策略不是强安全边界；
    它是控制面约束和人类确认的执行规范。

    命令审核采用双层机制：
    1. cmd_reviewer 节点做 AI 前置审核；
    2. Supervisor 继续做人类审批与硬规则兜底。

    AI 审核不能替代人类审批。
    """

    return {
        "version": 1,
        "extra_roots": ["/opt/kazebot"],
        "read_file": {
            "default": "auto",
            "rules": [
                {"pattern": "/opt/kazebot", "decision": "approval_required", "reason": "code directory is outside the instance workspace"},
                {"pattern": "/opt/kazebot/**", "decision": "approval_required", "reason": "code directory is outside the instance workspace"},
                {"pattern": ".env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
                {"pattern": "**/.env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
                {"pattern": "config/secrets.env", "decision": "deny", "reason": "do not allow reading shared dotenv secrets"},
                # manage_secret 的虚拟路径，不是真目录；list 只回键名与是否已配，不回值。
                {"pattern": ".secret", "decision": "auto", "reason": "listing secret names exposes no values"},
                {"pattern": "config/nodes/**", "decision": "deny", "reason": "node prompts are internal"},
                # 仓库自带的默认节点，实例没改过的那几份就读它。
                {"pattern": "defaults/nodes/**", "decision": "deny", "reason": "node prompts are internal"},
                {"pattern": "engine/system_nodes/**", "decision": "deny", "reason": "system prompts are internal"},
                {"pattern": "config/models.yaml", "decision": "approval_required", "reason": "model channels may hold api keys in plaintext"},
                {"pattern": "config/mcp.yaml", "decision": "approval_required", "reason": "mcp clients may carry tokens in env or headers"},
                {"pattern": "config/policy.yaml", "decision": "approval_required", "reason": "policy exposes the shape of every guard"},
                {"pattern": "config/drawtools/settings.yaml", "decision": "approval_required", "reason": "drawtools settings may hold the NovelAI api key"},
                {"pattern": "config/plugins.yaml", "decision": "approval_required", "reason": "plugin settings may hold passwords and tokens"},
                {"pattern": "config/plugins/**", "decision": "approval_required", "reason": "installed plugin packages may bundle credentials"},
                # data/ 下有平台密钥、.admin_token、会话与事件流。落到 default:auto
                # 等于任何人问一句就能把密钥读出来。
                {"pattern": "data/**", "decision": "approval_required", "reason": "runtime data may contain secrets or private context"},
            ],
        },
        "write_file": {
            "default": "auto",
            "rules": [
                {"pattern": "/opt/kazebot", "decision": "approval_required", "reason": "code directory changes require approval"},
                {"pattern": "/opt/kazebot/**", "decision": "approval_required", "reason": "code directory changes require approval"},
                {"pattern": "tools/**", "decision": "approval_required", "reason": "creating/updating tools requires approval"},
                {"pattern": "config/runtime.yaml", "decision": "auto", "reason": "runtime tuning config"},
                {"pattern": "config/nodes/**", "decision": "approval_required", "reason": "node definition changes affect execution, prompts, and model selection"},
                {"pattern": "config/workflows/**", "decision": "approval_required", "reason": "workflow changes affect node graph"},
                {"pattern": "config/policy.yaml", "decision": "deny", "reason": "policy is high-risk (human-only)"},
                # 共享的密钥只经 manage_secret 的虚拟路径写，一次一个键。
                {"pattern": "config/secrets.env", "decision": "deny", "reason": "do not allow writing shared dotenv secrets"},
                {"pattern": "config/models.yaml", "decision": "approval_required", "reason": "config changes require approval"},
                # stdio 客户端的 command 会在 reload 后被启动，等价于 config/tools/** 的代码执行风险。
                {"pattern": "config/mcp.yaml", "decision": "approval_required", "reason": "mcp stdio clients execute local commands"},
                {"pattern": "config/tools/**", "decision": "approval_required", "reason": "creating/updating tools requires approval"},
                # 插件包在 supervisor 和控制台里当代码跑，配置里还有插件的密码类参数。
                {"pattern": "config/plugins/**", "decision": "approval_required", "reason": "plugin packages run as code once loaded"},
                {"pattern": "config/plugins.yaml", "decision": "approval_required", "reason": "plugin settings hold credentials and switch plugins on"},
                # skill 正文直接进系统提示词，和 config/nodes/** 是同一类影响。
                # 注意副作用：dream 想把高频记忆提升成 skill 会被这条挡下 —— scheduler
                # 会话遇到 sensitive 决策是直接拒绝而非转审批。要那个功能就在
                # config/policy.yaml 里把这条连同 skills/** 一起放宽，别在这里改默认值。
                {"pattern": "config/skills/**", "decision": "approval_required", "reason": "skill bodies are injected into prompts"},
                # 第一个命中的规则赢，所以这条兜底必须排在上面几条具体规则之后。config/ 下的任何
                # 文件都参与决定 engine 怎么跑，落到 default:auto 等于免审批改行为。
                {"pattern": "config/**", "decision": "approval_required", "reason": "config changes affect execution"},
                # 下面这些以前落在 default:auto 上，而它们和 engine/** 一样是重启即执行的代码。
                *_entrypoint_write_rules(),
                {"pattern": "adapters/**", "decision": "approval_required", "reason": "adapter source changes require approval"},
                {"pattern": "clonoth_sdk/**", "decision": "approval_required", "reason": "sdk source changes require approval"},
                {"pattern": "plugins/**", "decision": "approval_required", "reason": "plugin source changes require approval"},
                {"pattern": "deploy/**", "decision": "approval_required", "reason": "deployment units run as services"},
                # 仓库自带的默认节点和模板，跟代码一起发布；实例要改的写进 config/。
                {"pattern": "defaults/**", "decision": "approval_required", "reason": "repository defaults ship with the code"},
                # 下面四条是挪进 config/ 之前的位置。判定时新位置也按这些旧名字判一遍，老 policy 给它们写的规则照样生效。
                {"pattern": "skills/**", "decision": "approval_required", "reason": "skill bodies are injected into prompts"},
                {"pattern": "data/config.yaml", "decision": "approval_required", "reason": "config changes require approval"},
                {"pattern": "data/policy.yaml", "decision": "deny", "reason": "policy is high-risk (human-only)"},
                {"pattern": "data/mcp_clients.yaml", "decision": "approval_required", "reason": "mcp stdio clients execute local commands"},
                {"pattern": "data/platform_secrets.yaml", "decision": "deny", "reason": "platform tokens are the instance identity"},
                {"pattern": "data/events.jsonl", "decision": "deny", "reason": "event log is append-only; never modify"},
                # 清理按它删目录：改了它就能让 supervisor 去删别的地方
                {"pattern": "data/worktrees.json", "decision": "deny", "reason": "the worktree registry is written by the supervisor only"},
                {"pattern": "data/schedules.yaml", "decision": "approval_required", "reason": "schedule changes require approval"},
                {"pattern": "engine/**", "decision": "approval_required", "reason": "engine source changes require approval"},
                {"pattern": "supervisor/**", "decision": "approval_required", "reason": "supervisor source changes require approval"},
                {"pattern": "toolbox/**", "decision": "approval_required", "reason": "toolbox source changes require approval"},
                {"pattern": "providers/**", "decision": "approval_required", "reason": "provider source changes require approval"},
                {"pattern": "shell/**", "decision": "approval_required", "reason": "shell source changes require approval"},
                {"pattern": "clonoth_runtime.py", "decision": "approval_required", "reason": "runtime lib changes require approval"},
                {"pattern": "main.py", "decision": "approval_required", "reason": "entrypoint changes require approval"},
                {"pattern": ".env", "decision": "deny", "reason": "do not allow writing dotenv secrets"},
                {"pattern": "**/.env", "decision": "deny", "reason": "do not allow writing dotenv secrets"},
                # 同上，虚拟路径。写的是 .env 里的单个键，值不经过 supervisor。
                {"pattern": ".secret/**", "decision": "auto", "reason": "admins may set api keys via manage_secret"},
            ],
        },
        "execute_command": {
            "default": "approval_required",
            # deny_patterns：硬拦截（即使是管理员任务也不能放行、不发审批）。
            # 仅用于不可逆破坏与极危险的“下载后直接执行/反弹 shell”用法。
            "deny_patterns": [
                r"\brm\s+-rf\s+/",
                r"\brm\s+-rf\s+~",
                r"\brm\s+-rf\s+\*",
                r"\bformat\b",
                r"\bmkfs\b",
                r"\bfdisk\b",
                r"\bdd\s+if=/dev/zero\b",
                r"\bshutdown\b",
                r"\breboot\b",
                # 下载后直接管道到 shell/解释器执行（curl ... | sh / wget ... | bash 等）。
                r"(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python[0-9.]*|perl|ruby|node)\b",
                # 反弹 shell：bash -i >& /dev/tcp/...。
                r"/dev/tcp/",
                r"\bbash\s+-i\b",
            ],
            # sensitive_patterns：敏感但允许“管理员亲自决定”。
            # 命中则保持 approval_required 且标记为敏感，使管理员任务不再自动放行、
            # 必须弹审批；普通命令（如 curl 纯读取网页不落盘）仍可对管理员免审批。
            "sensitive_patterns": [
                # 下载到本地文件：curl -o/-O/--output、wget（默认落盘）、重定向到文件。
                r"\bcurl\b[^|]*(?:\s-O\b|\s-o\b|--output\b|--remote-name\b)",
                r"\bwget\b",
                r"(?:curl|wget)\b[^|<>]*>\s*\S+",
                # 包管理器安装（引入外部代码）。
                r"\bpip[0-9.]*\s+install\b",
                r"\bpipx\s+install\b",
                r"\bnpm\s+(?:install|i|add)\b",
                r"\b(?:pnpm|yarn)\s+add\b",
                r"\b(?:apt|apt-get|yum|dnf|pacman|apk|brew|zypper)\s+(?:install|add|-S)\b",
                # 给文件加可执行权限（常与下载可执行文件配套）。
                r"\bchmod\b[^|]*\+x\b",
                # 网络监听（可能开后门）。
                r"\b(?:nc|ncat|netcat|socat)\b[^|]*(?:\s-l\b|--listen\b)",
            ],
            # 命令里提到这些路径时，按 read_file 对同一路径的档次处理。read_file 的
            # 规则会自动并进来，这里只补工作区之外的凭据位置。写成 [] 表示关掉这层。
            "sensitive_path_patterns": list(_DEFAULT_SENSITIVE_PATH_LITERALS),
        },
        "restart": {
            "default": "approval_required",
        },
        "approval_grant": {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
            "permanent_for_admins": True,
        },
    }


# 工作区之外的经典凭据位置。read_file 的规则只覆盖工作区内，命令却能读任意路径。
_DEFAULT_SENSITIVE_PATH_LITERALS: tuple[str, ...] = (
    ".ssh/", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".aws/credentials", ".docker/config.json", ".kube/config",
    ".netrc", ".git-credentials", ".pgpass",
    "/etc/shadow", "/etc/sudoers", "/proc/self/environ",
)

# glob 从第一个通配符起截断，只留字面前缀：data/** → data/，**/.env → .env。
_GLOB_TAIL = re.compile(r"[*?\[].*$")


def _rule_path_literal(pattern: str) -> str:
    """把 read_file 的 glob 规则还原成能在命令行文本里查找的字面片段。"""
    p = pattern.strip().replace("\\", "/")
    if p.startswith("**/"):
        p = p[3:]
    return _GLOB_TAIL.sub("", p).strip()


def _path_text_pattern(literal: str) -> re.Pattern[str] | None:
    r"""字面路径 → 命令行文本匹配器。

    前一个字符不能是 "\w"/"."/"-"，否则 metadata/ 会被 data/ 命中；但要允许 / 打头，
    因为 /opt/kazebot/config/models.yaml 正是要拦的形态。
    """
    token = literal.strip().replace("\\", "/")
    if len(token) < 4:
        # 太短的片段（如 a/）在自由文本里几乎必然误报，宁可不拦。
        return None
    tail = "" if token.endswith("/") else r"(?![\w])"
    return re.compile(r"(?<![\w.\-])" + re.escape(token) + tail, re.IGNORECASE)


# 目录名后面接着的那一段路径：到空白、引号或命令分隔符为止
_PATH_TAIL = r"(?P<tail>(?:[\\/][^\s\"'`|;&<>()]*)?)"


def _root_pattern(root: Path) -> re.Pattern[str] | None:
    """命令文本里一个目录连同它后面那段路径的各种写法：\\ 或 / 分隔，Windows 上盘符还可能写成 /e 这样的 MSYS 形式。"""
    parts = [part for part in root.as_posix().split("/") if part]
    if not parts:
        return None
    pieces: list[str] = []
    first = parts[0]
    if re.fullmatch(r"[A-Za-z]:", first):
        pieces.append(rf"(?:{re.escape(first)}|/{re.escape(first[0].lower())})")
        rest = parts[1:]
    else:
        pieces.append("/" + re.escape(first))
        rest = parts[1:]
    body = pieces[0] + "".join(r"[\\/]+" + re.escape(part) for part in rest)
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(r"(?<![\w.\-])" + body + r"(?=[\\/]|$|[^\w.\-])" + _PATH_TAIL, flags)


def map_worktree_paths(text: str, mappings: tuple[tuple[Path, Path], ...]) -> str:
    """命令文本里出现的隔离副本绝对路径换成原仓库里同一位置的路径，换过的那段统一写成 / 分隔；没有出现时原样返回。"""
    mapped = text
    for worktree, origin in mappings:
        pattern = _root_pattern(worktree)
        if pattern is None:
            continue
        replacement = origin.as_posix().rstrip("/")
        # 敏感路径的规则按 / 分隔写，副本路径后面那段一起换成 / 才对得上
        mapped = pattern.sub(lambda match: replacement + match.group("tail").replace("\\", "/"), mapped)
    return mapped


# 实例的配置和运行数据：被 gitignore，不在隔离副本里，交付给用户的文件也写在 data/attachments 下
_INSTANCE_DIRS = frozenset({config_layout.CONFIG_DIR, "data"})


def _instance_file(rel: str, external: bool) -> bool:
    """工作区里按逻辑名属于实例的 config/、data/ 的路径。"""
    return not external and rel.split("/", 1)[0] in _INSTANCE_DIRS


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_list(raw: Any) -> list[str] | None:
    """write_file 的 paths：一串路径字符串。写得不对返回 None。"""
    if not isinstance(raw, list) or not raw:
        return None
    paths = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    return paths if len(paths) == len(raw) else None


def _to_safety_level(s: str) -> SafetyLevel:
    v = (s or "").strip()
    try:
        return SafetyLevel(v)
    except Exception:
        return SafetyLevel.deny


_SAFETY_NAMES: frozenset[str] = frozenset(level.value for level in SafetyLevel)
_SAFETY_HINT = " / ".join(sorted(_SAFETY_NAMES))

# 一个路径按几个名字各判一遍时，谁更严听谁。
_STRICTNESS: dict[SafetyLevel, int] = {
    SafetyLevel.auto: 0,
    SafetyLevel.approval_required: 1,
    SafetyLevel.deny: 2,
}

# 策略暴露的是整套防护面的形状，同机其他用户没有理由读到。
_POLICY_FILE_MODE = 0o600

_SECRET_FLOOR_REASON = "config file holds plaintext secrets"


def _validate_rules_section(name: str, section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ValueError(f"{name} 必须是一个映射")
    default = str(section.get("default", "deny")).strip()
    if default not in _SAFETY_NAMES:
        raise ValueError(f"{name}.default 只能是 {_SAFETY_HINT}，收到 {default!r}")
    raw_rules = section.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ValueError(f"{name}.rules 必须是列表")
    rules: list[dict[str, Any]] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise ValueError(f"{name}.rules[{index}] 必须是一个映射")
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            raise ValueError(f"{name}.rules[{index}].pattern 不能为空")
        decision = str(item.get("decision") or "").strip()
        if decision not in _SAFETY_NAMES:
            raise ValueError(
                f"{name}.rules[{index}].decision 只能是 {_SAFETY_HINT}，收到 {decision!r}"
            )
        rule: dict[str, Any] = {"pattern": pattern, "decision": decision}
        reason = str(item.get("reason") or "").strip()
        if reason:
            rule["reason"] = reason
        rules.append(rule)
    return {"default": default, "rules": rules}


def _validate_command_section(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ValueError("execute_command 必须是一个映射")
    default = str(section.get("default", "approval_required")).strip()
    if default not in _SAFETY_NAMES:
        raise ValueError(f"execute_command.default 只能是 {_SAFETY_HINT}，收到 {default!r}")
    out: dict[str, Any] = {"default": default}
    for key in ("deny_patterns", "sensitive_patterns"):
        raw = section.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"execute_command.{key} 必须是列表")
        items: list[str] = []
        for index, entry in enumerate(raw):
            text = str(entry or "").strip()
            if not text:
                continue
            try:
                re.compile(text)
            except re.error as exc:
                raise ValueError(
                    f"execute_command.{key}[{index}] 不是合法正则：{exc}"
                ) from exc
            items.append(text)
        out[key] = items
    raw_paths = section.get("sensitive_path_patterns")
    if raw_paths is not None:
        if not isinstance(raw_paths, list):
            raise ValueError("execute_command.sensitive_path_patterns 必须是列表")
        out["sensitive_path_patterns"] = [
            str(entry or "").strip() for entry in raw_paths if str(entry or "").strip()
        ]
    return out


def _grant_minutes_field(section: dict[str, Any], key: str, fallback: int) -> int:
    value = section.get(key)
    if value is None:
        return fallback
    # YAML 的 true 也是 int，不能当成 1 分钟收下。
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"approval_grant.{key} 必须是非负整数，收到 {value!r}")
    return value


def _validate_approval_grant_section(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ValueError("approval_grant 必须是一个映射")
    max_minutes = _grant_minutes_field(section, "max_minutes", APPROVAL_GRANT_MAX_MINUTES)
    if not 1 <= max_minutes <= APPROVAL_GRANT_MINUTES_CEILING:
        raise ValueError(
            f"approval_grant.max_minutes 要在 1 到 {APPROVAL_GRANT_MINUTES_CEILING}（365 天）之间，收到 {max_minutes}"
        )
    default_minutes = _grant_minutes_field(section, "default_minutes", APPROVAL_GRANT_DEFAULT_MINUTES)
    raw_permanent = section.get("permanent_for_admins")
    permanent_for_admins = True if raw_permanent is None else parse_bool(raw_permanent)
    if permanent_for_admins is None:
        raise ValueError(f"approval_grant.permanent_for_admins 只能是 true 或 false，收到 {raw_permanent!r}")
    return {
        "default_minutes": min(default_minutes, max_minutes),
        "max_minutes": max_minutes,
        "permanent_for_admins": permanent_for_admins,
    }


def _validate_policy_dict(data: Any) -> dict[str, Any]:
    """整份校验并规范化。宁可整体拒绝，也不要落一份半对的策略。"""
    if not isinstance(data, dict):
        raise ValueError("policy 必须是一个映射")
    raw_roots = data.get("extra_roots") or []
    if not isinstance(raw_roots, list):
        raise ValueError("extra_roots 必须是列表")
    out: dict[str, Any] = {
        "version": 1,
        "extra_roots": [str(e or "").strip() for e in raw_roots if str(e or "").strip()],
    }
    for name in ("read_file", "write_file"):
        if name in data:
            out[name] = _validate_rules_section(name, data[name])
    if "execute_command" in data:
        out["execute_command"] = _validate_command_section(data["execute_command"])
    restart = data.get("restart")
    if restart is not None:
        if not isinstance(restart, dict):
            raise ValueError("restart 必须是一个映射")
        value = str(restart.get("default", "approval_required")).strip()
        if value not in _SAFETY_NAMES:
            raise ValueError(f"restart.default 只能是 {_SAFETY_HINT}，收到 {value!r}")
        out["restart"] = {"default": value}
    grant = data.get("approval_grant")
    if grant is not None:
        out["approval_grant"] = _validate_approval_grant_section(grant)
    return out


def _is_public_http_url(raw: str) -> bool:
    """判定 URL 是否为“公网 http(s) 地址”，用于防 SSRF。

    仅允许 http/https；主机不得为 localhost/环回/私有/链路本地/保留地址。
    无法确定时一律返回 False。
    """
    raw = (raw or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return False
    # 主机为 IP 时，拒绝非公网地址；为域名时只做关键字拦截。
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global and not ip.is_multicast
    except ValueError:
        pass
    # 域名：拦截明显指向内网的特殊名。
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return False
    return True


# 每一段认哪些键。写错的键运行期被静默忽略，以为加上的规则其实从没生效。
_POLICY_KEYS: dict[str, frozenset[str]] = {
    "": frozenset({
        "version", "extra_roots", "read_file", "write_file", "execute_command", "restart", "approval_grant",
    }),
    "read_file": frozenset({"default", "rules"}),
    "write_file": frozenset({"default", "rules"}),
    "execute_command": frozenset({"default", "deny_patterns", "sensitive_patterns", "sensitive_path_patterns"}),
    "restart": frozenset({"default"}),
    "approval_grant": frozenset({"default_minutes", "max_minutes", "permanent_for_admins"}),
}
_RULE_KEYS = frozenset({"pattern", "decision", "reason"})


def _unknown_policy_keys(data: dict[str, Any]) -> list[str]:
    """策略里没有任何地方读的键，按点分路径写。"""
    unknown = [str(key) for key in data if key not in _POLICY_KEYS[""]]
    for section, known in _POLICY_KEYS.items():
        body = data.get(section) if section else None
        if not isinstance(body, dict):
            continue
        unknown.extend(f"{section}.{key}" for key in body if key not in known)
        rules = body.get("rules") if section in {"read_file", "write_file"} else None
        for index, rule in enumerate(rules if isinstance(rules, list) else []):
            if isinstance(rule, dict):
                unknown.extend(f"{section}.rules[{index}].{key}" for key in rule if key not in _RULE_KEYS)
    return unknown


def load_policy_text(text: str) -> dict[str, Any]:
    """policy 原文解析成映射。解析不了、顶层不是映射（空文件也算）都抛 ValueError，原因只有位置和问题。"""
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败：{config_layout.yaml_error_text(exc)}") from exc
    if not isinstance(data, dict):
        raise ValueError("policy 顶层必须是映射（key: value）")
    return data


def parse_policy_text(text: str) -> dict[str, Any]:
    """policy 原文解析并校验成运行期用的那份，用不了抛 ValueError。PolicyEngine 收不收哪一版，用的就是这两步。"""
    return _validate_policy_dict(load_policy_text(text))


def default_policy() -> dict[str, Any]:
    """内置默认策略校验后的样子：一份 policy.yaml 都没读成过时，PolicyEngine 用的就是它。"""
    return _validate_policy_dict(_default_policy_dict())


def _unknown_keys_note(unknown: list[str]) -> str:
    return f"这些键没有任何地方读，写在那里的规则不会生效：{', '.join(unknown)}"


def check_policy_text(text: str) -> list[str]:
    """原文编辑器保存前的校验，和运行期重读是同一套：解析不了、结构不对抛 ValueError；没人读的键照存，返回提醒。"""
    data = load_policy_text(text)
    _validate_policy_dict(data)
    unknown = _unknown_policy_keys(data)
    return [_unknown_keys_note(unknown)] if unknown else []


def _load_policy_bytes(raw: bytes) -> dict[str, Any]:
    """解析读到的那份字节：指纹也按这份字节算，内容和指纹才出自同一版。用不了抛 ValueError。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"不是 UTF-8 文本：{exc}") from exc
    return load_policy_text(text)


class PolicyEngine:
    """策略引擎。

    - read_file / write_file: 用 glob 规则匹配。
    - execute_command: 先做硬拒绝，再做人类审批。

    命令的语义审核交给 cmd_reviewer 节点。
    是否真正放行，仍由 Supervisor 的审批流程决定。
    """

    def __init__(self, *, workspace_root: Path, policy_path: Path | None = None):
        self._root = workspace_root
        self._policy_path = policy_path or config_layout.policy_path(workspace_root)
        self._policy_name = config_layout.logical_path(workspace_root, self._policy_path)

        self._cached_stamp: tuple[int, int, int] | None = None
        # 生效的这份策略出自哪一版文件，按内容算；文件不在是空串。编辑器保存时交回比对。
        self._cfg_hash = ""
        # 文件现在有什么问题；空串表示生效的就是文件里那份。
        self._load_error = ""
        # 文件读成过没有。一次都没读成时生效的是下面的内置默认，说成「上一份」会让人以为自定义的 deny 还在管用。
        self._adopted = False
        # 读不了的那一版已经记过日志：读不了时不记指纹、每次都重试，日志得另外去重。
        self._unreadable_stamp: tuple[int, int, int] | None = None
        # 重读和导出都在锁里：交给编辑器的内容和指纹必须出自同一版文件。
        self._lock = threading.RLock()

        # 第一次读文件就读坏时，生效的是这份内置默认。
        self._cfg: dict[str, Any] = default_policy()

        self._extra_roots: list[Path] = []

        self._read_default: SafetyLevel = SafetyLevel.auto
        self._write_default: SafetyLevel = SafetyLevel.auto
        self._restart_default: SafetyLevel = SafetyLevel.approval_required
        self._command_default: SafetyLevel = SafetyLevel.approval_required

        self._read_rules: list[tuple[str, SafetyLevel, str]] = []
        self._write_rules: list[tuple[str, SafetyLevel, str]] = []
        self._deny_command_patterns: list[re.Pattern[str]] = []
        self._sensitive_command_patterns: list[re.Pattern[str]] = []
        self._sensitive_path_patterns: list[tuple[re.Pattern[str], SafetyLevel, str]] = []
        self._approval_grant = ApprovalGrantSettings()

        self._compile()
        self._ensure_policy_file_exists()
        self._reload_if_needed(force=True)

    def _ensure_policy_file_exists(self) -> None:
        if self._policy_path.exists():
            return
        text = yaml.safe_dump(_default_policy_dict(), sort_keys=False, allow_unicode=True)
        with contextlib.suppress(Exception), config_layout.locked(self._policy_path):
            # 共享组里几个实例同时首启，拿到锁时别人可能已经写好了。
            if not self._policy_path.exists():
                config_layout.write_locked(self._policy_path, text, mode=_POLICY_FILE_MODE)

    def export_config(self) -> dict[str, Any]:
        return self.export_versioned()[0]

    def export_versioned(self) -> tuple[dict[str, Any], str]:
        """当前生效的 policy 和它出自的那一版文件的指纹，给控制台编辑用；保存时交回指纹，中途被别处改过就不写。

        缺省项补成实际生效的值。文件里没写 sensitive_path_patterns 时运行期用的是内置
        清单，直接回原文会让界面显示成空 —— 照着存一次就把默认换成了「明确关闭」。
        """
        with self._lock:
            self._reload_if_needed()
            cfg = copy.deepcopy(self._cfg)
            digest = self._cfg_hash
            grant = self._approval_grant
        cmd = cfg.get("execute_command")
        if not isinstance(cmd, dict):
            cmd = {"default": self._command_default.value}
            cfg["execute_command"] = cmd
        if cmd.get("sensitive_path_patterns") is None:
            cmd["sensitive_path_patterns"] = list(_DEFAULT_SENSITIVE_PATH_LITERALS)
        # 没写这一段时运行期用的是代码默认值，界面上要看到、存回去也不能丢。
        cfg["approval_grant"] = {
            "default_minutes": grant.default_minutes,
            "max_minutes": grant.max_minutes,
            "permanent_for_admins": grant.permanent_for_admins,
        }
        return cfg, digest

    def replace_config(self, data: Any, *, base_hash: str | None = None) -> tuple[dict[str, Any], str]:
        """整份替换并落盘，返回落盘后的内容和指纹。

        校验不过就抛 ValueError 且不写盘：坏的那份落下去，运行期只能沿用上一份，界面上却显示保存成功。
        给了 base_hash 而文件已被别处改过，抛 config_layout.ConfigConflict，同样不写。
        """
        validated = _validate_policy_dict(data)
        text = yaml.safe_dump(validated, sort_keys=False, allow_unicode=True)
        try:
            config_layout.write_config_text(self._policy_path, text, base_hash=base_hash, mode=_POLICY_FILE_MODE)
        except OSError as exc:
            raise ValueError('策略写入失败：' + str(exc)) from exc
        self._reload_if_needed(force=True)
        return self.export_versioned()

    @property
    def load_error(self) -> str:
        """策略文件现在有什么问题、生效的是哪一份；空串表示生效的就是文件里写的那份。健康检查报这个。"""
        with self._lock:
            self._reload_if_needed()
            return self._load_error

    def _reload_if_needed(self, *, force: bool = False) -> None:
        # 共享组里别的实例也改这份文件：按 file_stamp 比，同一刻写进的新规则也认得出来。
        stamp = config_layout.file_stamp(self._policy_path)
        with self._lock:
            if stamp is None:
                # 文件被删了：规则照旧用手上那份，指纹记成「不存在」，编辑器保存时把它重新建出来。
                self._cfg_hash = ""
                self._cached_stamp = None
                kept = "沿用上一份策略" if self._adopted else "生效的是内置默认策略"
                self._load_error = f"{self._policy_name} 不见了，{kept}；在控制台保存一次就会重新建出来"
                return

            if not force and stamp == self._cached_stamp:
                return

            try:
                raw = self._policy_path.read_bytes()
            except OSError as exc:
                # 不记指纹：权限、占用这类问题常常一会儿就好，下一次照样重读。
                self._load_error = f"{self._policy_name} 读不了，{self._fallback_note_locked()}：{exc}"
                if stamp != self._unreadable_stamp:
                    self._unreadable_stamp = stamp
                    _log.warning("%s", self._load_error)
                return
            self._unreadable_stamp = None
            self._cached_stamp = stamp
            # 写坏了指纹也按这一版算：编辑器拿生效的那份存一次，坏文件就被盖掉了。
            self._cfg_hash = config_layout.bytes_hash(raw)
            try:
                data = _load_policy_bytes(raw)
                cfg = _validate_policy_dict(data)
            except ValueError as exc:
                # 不退回内置默认：写坏一个字，自定义的 deny 规则就会静默消失。
                self._load_error = f"{self._policy_name} 用不了，{self._fallback_note_locked()}：{exc}"
                _log.warning("%s", self._load_error)
                return
            unknown = _unknown_policy_keys(data)
            self._load_error = f"{self._policy_name} 里{_unknown_keys_note(unknown)}" if unknown else ""
            if unknown:
                _log.warning("%s", self._load_error)
            self._cfg = cfg
            self._adopted = True
            self._compile()

    def _fallback_note_locked(self) -> str:
        """文件用不了时生效的是哪份：读成过就是上一份，一次都没读成是内置默认。"""
        if self._adopted:
            return "沿用上一份能用的策略"
        return "生效的是内置默认策略，文件里自定义的规则（包括 deny）在修好之前都不生效"

    def _compile_rules(self, section: dict[str, Any]) -> tuple[SafetyLevel, list[tuple[str, SafetyLevel, str]]]:
        default_s = str(section.get("default", "deny"))
        default = _to_safety_level(default_s)

        rules: list[tuple[str, SafetyLevel, str]] = []
        raw_rules = section.get("rules")
        if isinstance(raw_rules, list):
            for r in raw_rules:
                if not isinstance(r, dict):
                    continue
                pat = str(r.get("pattern", "")).strip()
                dec = _to_safety_level(str(r.get("decision", "deny")))
                reason = str(r.get("reason", ""))
                if not pat:
                    continue
                rules.append((pat, dec, reason))
        return default, rules

    def _compile_sensitive_paths(
        self, extra_literals: Any,
    ) -> list[tuple[re.Pattern[str], SafetyLevel, str]]:
        """命令行里出现哪些路径要算敏感。

        主体从 read_file 规则推导：命令能读到的东西不该比 read_file 宽，两边各写一份
        迟早漂移。额外清单只补工作区之外的凭据位置，read_file 的 glob 管不到那里。
        """
        compiled: list[tuple[re.Pattern[str], SafetyLevel, str]] = []
        seen: set[str] = set()

        def add(literal: str, level: SafetyLevel) -> None:
            if not literal or literal in seen:
                return
            pattern = _path_text_pattern(literal)
            if pattern is None:
                return
            seen.add(literal)
            compiled.append((pattern, level, literal))

        for pat, dec, _reason in self._read_rules:
            if dec == SafetyLevel.auto:
                continue
            literal = _rule_path_literal(pat)
            add(literal, dec)
            # 按旧名字写的规则也要认出新位置：data/ 那条连带拦住 cat config/models.yaml。
            for alias in config_layout.policy_literal_aliases(literal):
                add(alias, dec)
        for item in (extra_literals if isinstance(extra_literals, list) else []):
            add(str(item or "").strip(), SafetyLevel.approval_required)
        return compiled

    def _compile(self) -> None:
        self._extra_roots = parse_extra_roots(self._root, self._cfg.get("extra_roots"))

        read_sec = self._cfg.get("read_file")
        if isinstance(read_sec, dict):
            self._read_default, self._read_rules = self._compile_rules(read_sec)
        else:
            self._read_default, self._read_rules = SafetyLevel.auto, []

        write_sec = self._cfg.get("write_file")
        if isinstance(write_sec, dict):
            self._write_default, self._write_rules = self._compile_rules(write_sec)
        else:
            self._write_default, self._write_rules = SafetyLevel.auto, []

        # 平台声明的密钥文件和自有文件排在最前：第一个命中的规则赢，老部署的 policy.yaml 没写它们也照样护住。
        # 命令里的敏感路径由读规则推导，所以要在下面编译 sensitive_path_patterns 之前并进去。
        read_floor, write_floor = policy_floor(workspace=self._root)
        self._read_rules = [(rule.pattern, SafetyLevel.deny, rule.reason) for rule in read_floor] + self._read_rules
        self._write_rules = [(rule.pattern, SafetyLevel.deny, rule.reason) for rule in write_floor] + self._write_rules
        self._read_rules = self._secret_floor(self._read_rules, self._read_default) + self._read_rules
        self._write_rules = self._secret_floor(self._write_rules, self._write_default) + self._write_rules

        cmd_sec = self._cfg.get("execute_command")
        if isinstance(cmd_sec, dict):
            self._command_default = _to_safety_level(str(cmd_sec.get("default", "approval_required")))
            deny_pats = cmd_sec.get("deny_patterns")
            sensitive_pats = cmd_sec.get("sensitive_patterns")
            path_literals = cmd_sec.get("sensitive_path_patterns")
        else:
            self._command_default = SafetyLevel.approval_required
            deny_pats = None
            sensitive_pats = None
            path_literals = None

        self._deny_command_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (deny_pats if isinstance(deny_pats, list) else [])
            if isinstance(p, str) and p.strip()
        ]
        self._sensitive_command_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (sensitive_pats if isinstance(sensitive_pats, list) else [])
            if isinstance(p, str) and p.strip()
        ]

        if path_literals is None:
            # 升级上来的旧 policy.yaml 没有这一项。当成空表等于静默丢掉这层防护，
            # 所以缺省用内置清单；只有显式写成 [] 才是关闭。
            path_literals = list(_DEFAULT_SENSITIVE_PATH_LITERALS)
        self._sensitive_path_patterns = self._compile_sensitive_paths(path_literals)

        restart_sec = self._cfg.get("restart")
        if isinstance(restart_sec, dict):
            self._restart_default = _to_safety_level(str(restart_sec.get("default", "approval_required")))
        else:
            self._restart_default = SafetyLevel.approval_required

        grant_sec = self._cfg.get("approval_grant")
        if isinstance(grant_sec, dict):
            self._approval_grant = ApprovalGrantSettings(
                default_minutes=int(grant_sec["default_minutes"]),
                max_minutes=int(grant_sec["max_minutes"]),
                permanent_for_admins=bool(grant_sec["permanent_for_admins"]),
            )
        else:
            self._approval_grant = ApprovalGrantSettings()

    @classmethod
    def _secret_floor(
        cls, rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel,
    ) -> list[tuple[str, SafetyLevel, str]]:
        """装着明文密钥的配置每个名字一条，最低是审批，排在所有规则前面。

        清单只认 config_layout 那一份：老部署的 policy.yaml 没写到的新密钥文件（比如 config/plugins.yaml）
        也落不到 default:auto 上。按 rules 算出来已经够严的，档次和理由原样留着。
        """
        floor: list[tuple[str, SafetyLevel, str]] = []
        least = SafetyLevel.approval_required
        for name in config_layout.secret_bearing_names():
            verdict = cls._first_match(name, rules, default)
            level = verdict.safety_level if _STRICTNESS[verdict.safety_level] >= _STRICTNESS[least] else least
            reason = verdict.reason if verdict.sensitive and level == verdict.safety_level else _SECRET_FLOOR_REASON
            floor.append((name, level, reason))
        return floor

    def extra_roots(self) -> list[Path]:
        """当前生效的 extra_roots，解析到底。"""
        self._reload_if_needed()
        return list(self._extra_roots)

    def _resolve_relpath(
        self, path_str: str, scope: PathScope | None = None, *, in_project: bool = True,
    ) -> tuple[Path | None, str, bool]:
        if scope is None:
            return classify_path(self._root, self._extra_roots, path_str)
        if scope.isolated and in_project:
            return classify_path(
                self._root, self._extra_roots, path_str,
                project_root=scope.project_root,
                project_origin=scope.project_origin,
                foreign_root=scope.foreign_root,
            )
        return classify_path(self._root, self._extra_roots, path_str, foreign_root=scope.foreign_root)

    def _readings(self, path_str: str, scope: PathScope | None) -> list[tuple[Path | None, str, bool]]:
        """一个路径按哪几种读法判。隔离任务写的相对路径按副本和工作区各解析一次，两边都要过。"""
        first = self._resolve_relpath(path_str, scope)
        try:
            absolute = Path(path_str).is_absolute()
        except (TypeError, ValueError):
            absolute = False
        if scope is None or not scope.isolated or absolute:
            return [first]
        return [first, self._resolve_relpath(path_str, scope, in_project=False)]

    def logical_path(self, path_str: str, scope: PathScope | None = None) -> str:
        """审批卡上显示的路径：隔离副本里的按原仓库里的名字写；路径无效时给空串。"""
        self._reload_if_needed()
        resolved, display, _external = self._resolve_relpath(path_str, scope)
        return display if resolved is not None else ""

    @staticmethod
    def _strictest(decisions: list[PolicyDecision]) -> PolicyDecision:
        """几种判法里最严的那个；一样严时带敏感标记的算数。"""
        chosen = decisions[0]
        for decision in decisions[1:]:
            stricter = _STRICTNESS[decision.safety_level] - _STRICTNESS[chosen.safety_level]
            if stricter > 0 or (stricter == 0 and decision.sensitive and not chosen.sensitive):
                chosen = decision
        return chosen

    @staticmethod
    def _first_match(rel: str, rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel) -> PolicyDecision:
        for pat, dec, reason in rules:
            if fnmatch.fnmatchcase(rel, pat):
                # 命中显式规则(如 engine/**、config/nodes/**、config/models.yaml 等)，
                # 标记为敏感，使管理员任务也无法自动放行。
                return PolicyDecision(dec, reason or f"matched rule: {pat}", sensitive=True)
        return PolicyDecision(default, "default policy")

    @classmethod
    def _match_rules(cls, rel: str, rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel) -> PolicyDecision:
        """挪了位置的文件连同旧名字各判一遍，取最严的；一样严时命中显式规则的那个算数，它带着敏感标记。"""
        chosen: PolicyDecision | None = None
        for name in config_layout.policy_names(rel):
            decision = cls._first_match(name, rules, default)
            if chosen is None:
                chosen = decision
                continue
            stricter = _STRICTNESS[decision.safety_level] - _STRICTNESS[chosen.safety_level]
            if stricter > 0 or (stricter == 0 and decision.sensitive and not chosen.sensitive):
                chosen = decision
        return chosen if chosen is not None else PolicyDecision(default, "default policy")

    def _file_decision(
        self, reading: tuple[Path | None, str, bool], rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel,
    ) -> PolicyDecision:
        resolved, rel, is_external = reading
        if resolved is None:
            return PolicyDecision(SafetyLevel.deny, rel)
        if is_external:
            # 工作区外部路径视为敏感，即使是管理员任务也需人工确认。
            dec = self._match_rules(rel, rules, SafetyLevel.approval_required)
            if dec.safety_level == SafetyLevel.approval_required:
                return PolicyDecision(dec.safety_level, dec.reason, sensitive=True)
            return dec
        return self._match_rules(rel, rules, default)

    def evaluate_read_file(self, *, path: str, scope: PathScope | None = None) -> PolicyDecision:
        self._reload_if_needed()
        if scope is not None and scope.invalid:
            return PolicyDecision(SafetyLevel.deny, scope.invalid)
        return self._strictest([
            self._file_decision(reading, self._read_rules, self._read_default)
            for reading in self._readings(path, scope)
        ])

    @staticmethod
    def _origin_write_guard(
        readings: list[tuple[Path | None, str, bool]], scope: PathScope | None,
    ) -> PolicyDecision | None:
        """隔离任务绕开自己的副本、直接改原仓库里的文件：至少要人确认。实例自己的 config/、data/ 副本里没有，照普通规则判。"""
        if scope is None or not scope.isolated or scope.origin_repo is None:
            return None
        for resolved, rel, external in readings:
            if resolved is None or _inside(resolved, scope.project_root) or _instance_file(rel, external):
                continue
            if _inside(resolved, scope.origin_repo):
                return PolicyDecision(
                    SafetyLevel.approval_required,
                    f"隔离任务要直接改原仓库 {scope.origin_repo.as_posix()} 里的文件，不是它隔离副本里的那份",
                    sensitive=True,
                )
        return None

    def evaluate_write_file(self, *, path: str, scope: PathScope | None = None) -> PolicyDecision:
        self._reload_if_needed()
        if scope is not None and scope.invalid:
            return PolicyDecision(SafetyLevel.deny, scope.invalid)
        readings = self._readings(path, scope)
        decisions = [self._file_decision(reading, self._write_rules, self._write_default) for reading in readings]
        # 只看工具真正会写到的那个位置：相对路径按工作区再判一遍是为了套更严的规则，写的仍是副本里的文件
        guard = self._origin_write_guard(readings[:1], scope)
        if guard is not None:
            decisions.append(guard)
        return self._strictest(decisions)

    def evaluate_write_paths(self, *, paths: list[str], scope: PathScope | None = None) -> PolicyDecision:
        """一次写好几个文件（比如合回隔离副本）：逐个按 write_file 的规则判，取最严的，理由前面写上是哪个文件。"""
        if not paths:
            return PolicyDecision(SafetyLevel.deny, "没有给出要写的文件")
        judged = [(path, self.evaluate_write_file(path=path, scope=scope)) for path in paths]
        decision = self._strictest([item for _path, item in judged])
        if len(judged) == 1:
            return decision
        deciding = next(path for path, item in judged if item is decision)
        shown = self.logical_path(deciding, scope) or deciding
        return PolicyDecision(decision.safety_level, f"{shown}: {decision.reason}", sensitive=decision.sensitive)

    def evaluate_execute_command(self, *, command: str, scope: PathScope | None = None) -> PolicyDecision:
        self._reload_if_needed()
        cmd = command.strip()
        if not cmd:
            return PolicyDecision(SafetyLevel.deny, "empty command")
        if scope is not None and scope.invalid:
            return PolicyDecision(SafetyLevel.deny, scope.invalid)
        texts = [cmd]
        if scope is not None and scope.mappings:
            mapped = map_worktree_paths(cmd, scope.mappings)
            if mapped != cmd:
                texts.append(mapped)
        return self._strictest([self._command_decision(text) for text in texts])

    def _command_decision(self, cmd: str) -> PolicyDecision:
        for pat in self._deny_command_patterns:
            if pat.search(cmd):
                return PolicyDecision(SafetyLevel.deny, f"command denied by pattern: {pat.pattern}")

        # read_file 拦得住 read_file config/models.yaml，拦不住 cat config/models.yaml。
        # 命令提到敏感路径时按同一档处理，否则管理员免审批那条快车道等于给凭据开了后门。
        # 一条命令可能同时点到好几个，取最严的那个：按排列顺序取第一个的话，前面一条审批会盖住后面的拒绝。
        touched = [(level, literal) for path_pat, level, literal in self._sensitive_path_patterns if path_pat.search(cmd)]
        if touched:
            level, literal = max(touched, key=lambda item: _STRICTNESS[item[0]])
            if level == SafetyLevel.deny:
                return PolicyDecision(
                    SafetyLevel.deny, f"command touches denied path: {literal}",
                )
            return PolicyDecision(
                SafetyLevel.approval_required,
                f"command touches sensitive path: {literal}",
                sensitive=True,
            )

        # 敏感命令（下载落盘、包安装、chmod +x、监听等）：保持审批且标记敏感，
        # 使管理员任务也不自动放行，必须管理员亲自审批确认。
        for pat in self._sensitive_command_patterns:
            if pat.search(cmd):
                return PolicyDecision(
                    SafetyLevel.approval_required,
                    f"sensitive command requires admin approval: {pat.pattern}",
                    sensitive=True,
                )

        if self._command_default == SafetyLevel.auto:
            return PolicyDecision(SafetyLevel.auto, "command auto-allowed by default")
        if self._command_default == SafetyLevel.deny:
            return PolicyDecision(SafetyLevel.deny, "command denied by default")
        return PolicyDecision(SafetyLevel.approval_required, "command requires approval")

    def is_safe_public_curl(self, command: str) -> bool:
        """判定是否为“安全的 curl 纯 GET 读取外网”命令。

        用于允许 QQ 非管理员群友让 bot 用 curl 读网页，但严格限制：
        - 必须是单条 curl 命令，不得含命令拼接/注入（; & | ` $() 等）；
        - 不得命中 deny/sensitive 规则（不落盘、不管道到 shell 等）；
        - 不得带写方法/上传参数（-d/--data/-X/--request/-T/--upload-file/-F/-o/-O 等）；
        - URL 必须是 http(s)://，且主机不得为 localhost/环回/内网段（防 SSRF）。
        任何不确定的情况一律返回 False（宁可拒绝）。
        """
        self._reload_if_needed()
        cmd = (command or "").strip()
        if not cmd:
            return False

        # 命令拼接/注入/重定向字符一律拒绝。
        if any(ch in cmd for ch in (";", "|", "&", "`", ">", "<", "\n")):
            return False
        if "$(" in cmd or "${" in cmd:
            return False

        # 不得命中任何 deny/sensitive 规则。
        for pat in self._deny_command_patterns:
            if pat.search(cmd):
                return False
        for pat in self._sensitive_command_patterns:
            if pat.search(cmd):
                return False
        for path_pat, _level, _literal in self._sensitive_path_patterns:
            if path_pat.search(cmd):
                return False

        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return False
        if not tokens or tokens[0].lower() != "curl":
            return False

        # 写方法/上传/落盘类危险选项一律拒绝。
        # 均以小写存储，与 tok.lower() 比较（curl 短选项大小写敏感，
        # 但这里只需拦截；大写 -O/-T 等也属危险，统一拒绝更安全）。
        forbidden_flags = {
            "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
            "-x", "--request", "-t", "--upload-file", "-f", "--form",
            "-o", "--output", "-O", "--remote-name", "-k", "--config",
        }
        urls: list[str] = []
        for tok in tokens[1:]:
            low = tok.lower()
            if low in forbidden_flags:
                return False
            # -X POST / --request=POST 等变体。
            if low.startswith("--data") or low.startswith("--request") or low.startswith("--output"):
                return False
            if tok.startswith("http://") or tok.startswith("https://"):
                urls.append(tok)
            elif not tok.startswith("-") and ("://" in tok or "." in tok):
                # 裸域名（如 example.com）也当作 URL 候选校验。
                urls.append(tok)

        if len(urls) != 1:
            return False
        return _is_public_http_url(urls[0])

    def approval_grant_settings(self) -> ApprovalGrantSettings:
        """同意审批时授权的默认时长、上限和能不能要永久的，按当前生效的那份策略。"""
        self._reload_if_needed()
        return self._approval_grant

    def evaluate_restart(self, *, target: str) -> PolicyDecision:
        self._reload_if_needed()
        if target not in {"engine", "all"}:
            return PolicyDecision(SafetyLevel.deny, f"unknown restart target: {target}")
        if self._restart_default == SafetyLevel.auto:
            return PolicyDecision(SafetyLevel.auto, f"restart {target} auto-allowed")
        if self._restart_default == SafetyLevel.deny:
            return PolicyDecision(SafetyLevel.deny, f"restart {target} denied")
        # 重启服务属于危险操作，即使是管理员任务也保持审批。
        return PolicyDecision(SafetyLevel.approval_required, f"restart {target} requires approval", sensitive=True)

    def evaluate(self, *, op: str, parameters: dict[str, Any], scope: PathScope | None = None) -> PolicyDecision:
        if op == "read_file":
            return self.evaluate_read_file(path=str(parameters.get("path", "")), scope=scope)
        if op == "write_file":
            if "paths" in parameters:
                paths = _path_list(parameters.get("paths"))
                if paths is None:
                    return PolicyDecision(SafetyLevel.deny, "paths 要是一串非空的文件路径")
                return self.evaluate_write_paths(paths=paths, scope=scope)
            return self.evaluate_write_file(path=str(parameters.get("path", "")), scope=scope)
        if op == "execute_command":
            return self.evaluate_execute_command(command=str(parameters.get("command", "")), scope=scope)
        if op == "restart":
            return self.evaluate_restart(target=str(parameters.get("target", "")))
        return PolicyDecision(SafetyLevel.deny, f"unknown op: {op}")

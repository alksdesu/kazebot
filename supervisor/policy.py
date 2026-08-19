from __future__ import annotations

import contextlib
import copy
import fnmatch
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from clonoth_runtime import classify_path, load_yaml_dict, parse_extra_roots

from .types import SafetyLevel


@dataclass(frozen=True)
class PolicyDecision:
    safety_level: SafetyLevel
    reason: str
    # Why: 管理员触发的 QQ 任务对普通命令/文件操作免审批，但改源码、
    # 重启、动 config/nodes、data 等敏感操作仍需人工确认。sensitive=True 表示
    # 本决策命中了显式敏感规则(而非默认放行或普通命令)，即使是管理员也不免审批。
    sensitive: bool = False


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
        "extra_roots": [],
        "read_file": {
            "default": "auto",
            "rules": [
                {"pattern": ".env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
                {"pattern": "**/.env", "decision": "deny", "reason": "do not allow reading dotenv secrets"},
                {"pattern": "config/nodes/**", "decision": "deny", "reason": "node prompts are internal"},
                {"pattern": "engine/system_nodes/**", "decision": "deny", "reason": "system prompts are internal"},
                # data/ 下有 config.yaml、.admin_token、会话与事件流。落到 default:auto
                # 等于任何人问一句就能把密钥读出来。
                {"pattern": "data/**", "decision": "approval_required", "reason": "runtime data may contain secrets or private context"},
            ],
        },
        "write_file": {
            "default": "auto",
            "rules": [
                {"pattern": "tools/**", "decision": "approval_required", "reason": "creating/updating tools requires approval"},
                {"pattern": "config/runtime.yaml", "decision": "auto", "reason": "runtime tuning config"},
                {"pattern": "config/nodes/**", "decision": "approval_required", "reason": "node definition changes affect execution, prompts, and model selection"},
                {"pattern": "config/workflows/**", "decision": "approval_required", "reason": "workflow changes affect node graph"},
                # 第一个命中的规则赢，所以这条兜底必须排在上面两条之后。config/ 下的任何
                # 文件都参与决定 engine 怎么跑，落到 default:auto 等于免审批改行为。
                {"pattern": "config/**", "decision": "approval_required", "reason": "config changes affect execution"},
                # 下面这些以前落在 default:auto 上，而它们和 engine/** 一样是重启即执行的代码。
                {"pattern": "bot.py", "decision": "approval_required", "reason": "qq adapter entrypoint changes require approval"},
                {"pattern": "adapters/**", "decision": "approval_required", "reason": "adapter source changes require approval"},
                {"pattern": "clonoth_sdk/**", "decision": "approval_required", "reason": "sdk source changes require approval"},
                {"pattern": "plugins/**", "decision": "approval_required", "reason": "plugin source changes require approval"},
                {"pattern": "deploy/**", "decision": "approval_required", "reason": "deployment units run as services"},
                # skill 正文直接进系统提示词，和 config/nodes/** 是同一类影响。
                {"pattern": "skills/**", "decision": "approval_required", "reason": "skill bodies are injected into prompts"},
                {"pattern": "data/config.yaml", "decision": "approval_required", "reason": "config changes require approval"},
                {"pattern": "data/policy.yaml", "decision": "deny", "reason": "policy is high-risk (human-only)"},
                # stdio 客户端的 command 会在 reload 后被启动，等价于 tools/** 的代码执行风险。
                {"pattern": "data/mcp_clients.yaml", "decision": "approval_required", "reason": "mcp stdio clients execute local commands"},
                {"pattern": "data/events.jsonl", "decision": "deny", "reason": "event log is append-only; never modify"},
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
    因为 /opt/kazebot/data/config.yaml 正是要拦的形态。
    """
    token = literal.strip().replace("\\", "/")
    if len(token) < 4:
        # 太短的片段（如 a/）在自由文本里几乎必然误报，宁可不拦。
        return None
    tail = "" if token.endswith("/") else r"(?![\w])"
    return re.compile(r"(?<![\w.\-])" + re.escape(token) + tail, re.IGNORECASE)


def _to_safety_level(s: str) -> SafetyLevel:
    v = (s or "").strip()
    try:
        return SafetyLevel(v)
    except Exception:
        return SafetyLevel.deny


_SAFETY_NAMES: frozenset[str] = frozenset(level.value for level in SafetyLevel)
_SAFETY_HINT = " / ".join(sorted(_SAFETY_NAMES))


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


class PolicyEngine:
    """策略引擎。

    - read_file / write_file: 用 glob 规则匹配。
    - execute_command: 先做硬拒绝，再做人类审批。

    命令的语义审核交给 cmd_reviewer 节点。
    是否真正放行，仍由 Supervisor 的审批流程决定。
    """

    def __init__(self, *, workspace_root: Path, policy_path: Path | None = None):
        self._root = workspace_root
        self._policy_path = policy_path or (workspace_root / "data" / "policy.yaml")

        self._cached_mtime: float | None = None

        self._cfg: dict[str, Any] = _default_policy_dict()

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

        self._ensure_policy_file_exists()
        self._reload_if_needed(force=True)

    def _ensure_policy_file_exists(self) -> None:
        if self._policy_path.exists():
            return
        try:
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            text = yaml.safe_dump(_default_policy_dict(), sort_keys=False, allow_unicode=True)
            self._policy_path.write_text(text, encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(self._policy_path, 0o600)
        except Exception:
            pass

    def export_config(self) -> dict[str, Any]:
        """当前生效的 policy，给控制台编辑用。

        缺省项补成实际生效的值。文件里没写 sensitive_path_patterns 时运行期用的是内置
        清单，直接回原文会让界面显示成空 —— 照着存一次就把默认换成了「明确关闭」。
        """
        self._reload_if_needed()
        cfg = copy.deepcopy(self._cfg)
        cmd = cfg.get("execute_command")
        if not isinstance(cmd, dict):
            cmd = {"default": self._command_default.value}
            cfg["execute_command"] = cmd
        if cmd.get("sensitive_path_patterns") is None:
            cmd["sensitive_path_patterns"] = list(_DEFAULT_SENSITIVE_PATH_LITERALS)
        return cfg

    def replace_config(self, data: Any) -> dict[str, Any]:
        """整份替换并落盘，返回落盘后的内容。

        校验不过就抛 ValueError 且不写盘：一份解析不了的 yaml 会让 _reload_if_needed
        静默退回内置默认，策略被换掉却看不出任何迹象。
        """
        validated = _validate_policy_dict(data)
        text = yaml.safe_dump(validated, sort_keys=False, allow_unicode=True)
        tmp = self._policy_path.with_name(self._policy_path.name + '.' + str(os.getpid()) + '.tmp')
        # 新建的临时文件走 umask 通常是 0644，直接替换过去会把原文件的权限降下来。
        # 策略暴露的是整套防护面的形状，同机其他用户没有理由读到。
        mode = 0o600
        with contextlib.suppress(OSError):
            mode = self._policy_path.stat().st_mode & 0o777
        try:
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding='utf-8')
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
            os.replace(tmp, self._policy_path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ValueError('策略写入失败：' + str(exc)) from exc
        self._reload_if_needed(force=True)
        return self.export_config()

    def _reload_if_needed(self, *, force: bool = False) -> None:
        try:
            st = self._policy_path.stat()
            mtime = float(st.st_mtime)
        except Exception:
            return

        if not force and self._cached_mtime is not None and mtime == self._cached_mtime:
            return

        data = load_yaml_dict(self._policy_path)
        if not isinstance(data, dict):
            data = _default_policy_dict()

        self._cfg = data
        self._cached_mtime = mtime
        self._compile()

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
            add(_rule_path_literal(pat), dec)
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

    def _resolve_relpath(self, path_str: str) -> tuple[Path | None, str, bool]:
        return classify_path(self._root, self._extra_roots, path_str)

    @staticmethod
    def _match_rules(rel: str, rules: list[tuple[str, SafetyLevel, str]], default: SafetyLevel) -> PolicyDecision:
        for pat, dec, reason in rules:
            if fnmatch.fnmatchcase(rel, pat):
                # 命中显式规则(如 engine/**、config/nodes/**、data/config.yaml 等)，
                # 标记为敏感，使管理员任务也无法自动放行。
                return PolicyDecision(dec, reason or f"matched rule: {pat}", sensitive=True)
        return PolicyDecision(default, "default policy")

    def evaluate_read_file(self, *, path: str) -> PolicyDecision:
        self._reload_if_needed()
        resolved, rel, is_external = self._resolve_relpath(path)
        if resolved is None:
            return PolicyDecision(SafetyLevel.deny, rel)
        default = self._read_default
        if is_external:
            # 工作区外部路径视为敏感，即使是管理员任务也需人工确认。
            dec = self._match_rules(rel, self._read_rules, SafetyLevel.approval_required)
            if dec.safety_level == SafetyLevel.approval_required:
                return PolicyDecision(dec.safety_level, dec.reason, sensitive=True)
            return dec
        return self._match_rules(rel, self._read_rules, default)

    def evaluate_write_file(self, *, path: str) -> PolicyDecision:
        self._reload_if_needed()
        resolved, rel, is_external = self._resolve_relpath(path)
        if resolved is None:
            return PolicyDecision(SafetyLevel.deny, rel)
        default = self._write_default
        if is_external:
            # 工作区外部路径视为敏感，即使是管理员任务也需人工确认。
            dec = self._match_rules(rel, self._write_rules, SafetyLevel.approval_required)
            if dec.safety_level == SafetyLevel.approval_required:
                return PolicyDecision(dec.safety_level, dec.reason, sensitive=True)
            return dec
        return self._match_rules(rel, self._write_rules, default)

    def evaluate_execute_command(self, *, command: str) -> PolicyDecision:
        self._reload_if_needed()
        cmd = command.strip()
        if not cmd:
            return PolicyDecision(SafetyLevel.deny, "empty command")

        for pat in self._deny_command_patterns:
            if pat.search(cmd):
                return PolicyDecision(SafetyLevel.deny, f"command denied by pattern: {pat.pattern}")

        # read_file 拦得住 read_file data/config.yaml，拦不住 cat data/config.yaml。
        # 命令提到敏感路径时按同一档处理，否则管理员免审批那条快车道等于给凭据开了后门。
        for path_pat, level, literal in self._sensitive_path_patterns:
            if not path_pat.search(cmd):
                continue
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

    def evaluate(self, *, op: str, parameters: dict[str, Any]) -> PolicyDecision:
        if op == "read_file":
            return self.evaluate_read_file(path=str(parameters.get("path", "")))
        if op == "write_file":
            return self.evaluate_write_file(path=str(parameters.get("path", "")))
        if op == "execute_command":
            return self.evaluate_execute_command(command=str(parameters.get("command", "")))
        if op == "restart":
            return self.evaluate_restart(target=str(parameters.get("target", "")))
        return PolicyDecision(SafetyLevel.deny, f"unknown op: {op}")

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
import yaml
from dotenv import load_dotenv

import config_layout
from engine.cron import cron_faults
from real_paths import real_path
from workspace import pin_workspace_env, resolve_workspace_root, workspace_env_root

if TYPE_CHECKING:
    from platforms.descriptor import PlatformDescriptor


_log = logging.getLogger(__name__)


# defaults/runtime.yaml 读不了时的兜底。以那份文件为准：同名键的值必须和它一致。
DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "engine": {
        "max_steps": 64,
        "streaming": True,
        "poll_interval_sec": 1.0,
        "http": {"client_timeout_sec": 60.0},
        "compact": {
            "keep_recent": 6,
        },
        "retry": {
            "max_retries": 3,
            "initial_delay_sec": 1.0,
            "max_delay_sec": 30.0,
            "backoff_multiplier": 2.0,
        },
    },
    "providers": {
        "openai": {
            "timeout_sec": 600.0,
            "options": {},
        }
    },
    "meta": {
        "execute_command": {
            "default_timeout_sec": 90.0,
            "max_output_chars": 0,
            "async_upgrade": {
                "enabled": True,
                "threshold_sec": 60,
            },
        },
        "git": {"diff_max_chars": 600000},
        "search": {"max_file_size_bytes": 3000000},
    },
    "tools": {
        "script": {
            "default_timeout_sec": 60.0,
        },
        "mcp": {
            "list_timeout_sec": 120.0,
        },
    },
    "skills": {
        "max_budget_chars": 0,
    },
    "shell": {
        "entry_node_id": "bootstrap.shell_orchestrator",
        "http": {"client_timeout_sec": 10.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "events_poll_interval_sec": 0.5,
    },
    "supervisor": {
        "process_manager": {
            "stop_wait_timeout_sec": 5.0,
            "shell_new_console": False,
            "engine_workers": 2,
            "spawn_shell_cli": False,
        },
    },
}


_CACHE: dict[str, tuple[tuple[tuple[int, int, int] | None, tuple[int, int, int] | None], dict[str, Any]]] = {}


# 键 → 去处，空串表示没有替代项。旧工作区的 runtime.yaml 里可能还留着它们。
RETIRED_RUNTIME_KEYS: dict[str, str] = {
    "engine.history_limit": "对话历史不按条数截断，长度交给 engine.compact 控制",
    "engine.model": "模型写在 config/models.yaml 的渠道块或节点 yaml 里",
    "engine.approval_poll_interval_sec": "",
    "engine.supervisor": "",
    "engine.tool_trace": "",
    "engine.compact.threshold_chars": "改用 engine.compact.threshold_tokens",
    "engine.child_session.max_per_parent": "",
    "memory.auto_extract.min_messages": "",
    "memory.dream.min_sessions": "",
    "meta.search.max_matches": "匹配上限由 search_in_files 的 maxResults 参数决定",
    "shell.default_conversation_key": "会话键由 --conversation-key 或 CLONOTH_CONVERSATION_KEY 决定",
    "tools.command": "已改名为 tools.script",
}

_WARNED_RETIRED: set[tuple[str, str]] = set()
_WARNED_CRONS: set[tuple[str, str, str]] = set()


def _has_dotted(data: Mapping[str, Any], dotted_key: str) -> bool:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False
        cur = cur[part]
    return True


def retired_runtime_keys(cfg: Mapping[str, Any]) -> list[str]:
    """cfg 里还写着的退役键，顺序同 RETIRED_RUNTIME_KEYS。"""
    return [key for key in RETIRED_RUNTIME_KEYS if _has_dotted(cfg, key)]


def _warn_retired_keys(path: Path, user_cfg: Mapping[str, Any]) -> None:
    for key in retired_runtime_keys(user_cfg):
        marker = (str(path), key)
        if marker in _WARNED_RETIRED:
            continue
        _WARNED_RETIRED.add(marker)
        _log.warning("%s 里的 %s 已经没有代码读取，%s", path, key, RETIRED_RUNTIME_KEYS[key] or "可以删掉")


def _runtime_crons(cfg: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """cfg 里叫 cron 的键（点分写法）和写的值，空值不算：dream、data_cleanup 的定时都写在这种键里，写成空串是不跑。"""
    found: list[tuple[str, str]] = []
    for key, value in cfg.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            found.extend(_runtime_crons(value, f"{dotted}."))
        elif key == "cron" and value is not None and str(value).strip():
            found.append((dotted, str(value).strip()))
    return found


def _warn_cron_faults(path: Path, user_cfg: Mapping[str, Any]) -> None:
    for key, expr in _runtime_crons(user_cfg):
        marker = (str(path), key, expr)
        if marker in _WARNED_CRONS:
            continue
        _WARNED_CRONS.add(marker)
        faults = cron_faults(expr)
        if faults:
            _log.warning("%s 里的 %s「%s」%s", path, key, expr, "；".join(faults))


def runtime_config_path(workspace_root: Path) -> Path:
    """实例改过的那份 runtime。没有时整份用仓库默认的 defaults/runtime.yaml。"""
    return config_layout.runtime_path(workspace_root)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        # 段名底下的键全注释掉时 yaml 读出来是 null：那是没写，不是把整段换成 null。
        if v is None and isinstance(out.get(k), dict):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class RuntimeConfigError(ValueError):
    """runtime 原文用不了：解析不了，或者该写一整段的地方写成了别的。"""


def _misplaced_sections(data: Mapping[str, Any], shape: Mapping[str, Any], prefix: str = "") -> list[str]:
    """默认值里是一整段、data 里却写成标量或列表的那些键。照样盖上去，读它底下的键的地方全都会崩。"""
    found: list[str] = []
    for key, value in data.items():
        expected = shape.get(key)
        if not isinstance(expected, dict) or value is None:
            continue
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            found.extend(_misplaced_sections(value, expected, f"{dotted}."))
        else:
            found.append(dotted)
    return found


def parse_runtime_text(text: str, shape: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """runtime 原文解析成映射，抛 RuntimeConfigError 说明哪里不对。shape 是该有哪些段，不给就按代码里的默认值。"""
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        raise RuntimeConfigError(f"YAML 解析失败：{config_layout.yaml_error_text(exc)}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeConfigError("顶层必须是映射（key: value）")
    misplaced = _misplaced_sections(data, DEFAULT_RUNTIME_CONFIG if shape is None else shape)
    if misplaced:
        raise RuntimeConfigError(f"这几项底下应当是一整段配置（key: value），写成了别的：{', '.join(misplaced)}")
    return data


@dataclass(frozen=True)
class _RuntimeLayer:
    """runtime 的一层按某一版文件读出来的样子。文件写坏时 data 停在上一份能用的，error 说明为什么。"""

    stamp: tuple[int, int, int] | None
    data: dict[str, Any]
    error: str = ""


_LAYERS: dict[str, _RuntimeLayer] = {}
_LAYERS_LOCK = threading.Lock()


def _read_runtime_layer(path: Path, shape: Mapping[str, Any]) -> dict[str, Any]:
    """这一层文件现在的内容。读不了、用不了都抛 RuntimeConfigError。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeConfigError(f"读不了：{exc}") from exc
    return parse_runtime_text(text, shape)


def _runtime_layer(path: Path, shape: Mapping[str, Any]) -> _RuntimeLayer:
    """按 file_stamp 认出改动；新的那版用不了时记日志、沿用上一份能用的，和 qq.yaml、features.yaml 的做法一样。"""
    stamp = config_layout.file_stamp(path)
    key = str(path)
    with _LAYERS_LOCK:
        cached = _LAYERS.get(key)
        if cached is not None and cached.stamp == stamp:
            return cached
        if stamp is None:
            layer = _RuntimeLayer(None, {})
        else:
            try:
                layer = _RuntimeLayer(stamp, _read_runtime_layer(path, shape))
            except RuntimeConfigError as exc:
                kept = cached.data if cached is not None else {}
                _log.warning("%s 用不了，%s：%s", path, "沿用上一份能用的" if kept else "这一层按没写算", exc)
                layer = _RuntimeLayer(stamp, kept, str(exc))
        _LAYERS[key] = layer
        return layer


def _instance_shape(default_layer: _RuntimeLayer) -> dict[str, Any]:
    """实例那份按「代码默认 + 仓库默认」认哪些键底下是一整段：仓库默认新加的段也算。"""
    return _deep_merge(DEFAULT_RUNTIME_CONFIG, default_layer.data)


def _runtime_layers(workspace_root: Path) -> tuple[tuple[Path, _RuntimeLayer], tuple[Path, _RuntimeLayer]]:
    default_path, own_path = config_layout.runtime_layers(workspace_root)
    default_layer = _runtime_layer(default_path, DEFAULT_RUNTIME_CONFIG)
    own_layer = _runtime_layer(own_path, _instance_shape(default_layer))
    return (default_path, default_layer), (own_path, own_layer)


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    """代码里的默认值 ← 仓库的 defaults/runtime.yaml ← 实例改过的 config/runtime.yaml，逐键往上盖。

    实例那份只盖它写了的键：发版给默认 runtime 加的新键，改过 runtime 的实例也照样拿得到。
    哪一层写坏了就沿用它上一份能用的，原因见 runtime_config_error。
    """
    (default_path, default_layer), (own_path, own_layer) = _runtime_layers(workspace_root)
    # 共享组里别的实例也改 config/runtime.yaml：按 file_stamp 比，同一刻写进的改动也认得出来。
    stamp = (default_layer.stamp, own_layer.stamp)

    key = str(own_path.resolve())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return copy.deepcopy(cached[1])

    _warn_retired_keys(default_path, default_layer.data)
    _warn_retired_keys(own_path, own_layer.data)
    _warn_cron_faults(default_path, default_layer.data)
    _warn_cron_faults(own_path, own_layer.data)
    merged = _deep_merge(_deep_merge(DEFAULT_RUNTIME_CONFIG, default_layer.data), own_layer.data)
    _CACHE[key] = (stamp, merged)
    return copy.deepcopy(merged)


def runtime_config_error(workspace_root: Path) -> str:
    """runtime 哪一层现在用不了、在用的是什么；空串表示两层都按文件里写的在跑。健康检查报这个。"""
    problems: list[str] = []
    for path, layer in _runtime_layers(workspace_root):
        if layer.error:
            fallback = "沿用上一份能用的" if layer.data else "这一层按没写算"
            problems.append(f"{config_layout.logical_path(workspace_root, path)} 用不了，{fallback}：{layer.error}")
    return "；".join(problems)


def check_runtime_text(text: str) -> list[str]:
    """原文编辑器保存实例那份之前的校验：用不了就抛 RuntimeConfigError。返回保存之后该提醒的事：退役键、永远不会触发的 cron。"""
    default_layer = _runtime_layer(config_layout.default_runtime_path(), DEFAULT_RUNTIME_CONFIG)
    data = parse_runtime_text(text, _instance_shape(default_layer))
    retired = [
        f"{key} 已经没有代码读取，{RETIRED_RUNTIME_KEYS[key] or '可以删掉'}"
        for key in retired_runtime_keys(data)
    ]
    return retired + [f"{key} {fault}" for key, expr in _runtime_crons(data) for fault in cron_faults(expr)]


def get_str(data: dict[str, Any], dotted_key: str, default: str = "") -> str:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return str(cur) if cur is not None else default


def get_int(data: dict[str, Any], dotted_key: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = get_str(data, dotted_key, "")
    try:
        val = int(raw)
    except Exception:
        val = int(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


def get_float(data: dict[str, Any], dotted_key: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = get_str(data, dotted_key, "")
    try:
        val = float(raw)
    except Exception:
        val = float(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off"})
# 拒绝原因里收到的值最多露这么多字
_SHOWN_SWITCH_CHARS = 40
_SwitchDefault = TypeVar("_SwitchDefault", bool, None)


def parse_bool(raw: Any, default: bool | None = None) -> bool | None:
    """配置和环境变量里的开关。认不出的（包括空）给 default，和 get_int、get_float 读坏值时一样；模型填的工具参数用 switch_arg。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def env_bool(name: str, default: bool) -> bool:
    """环境变量当开关读。没设或写了认不出的值，照 default。"""
    return bool(parse_bool(os.environ.get(name), default))


def get_bool(data: dict[str, Any], dotted_key: str, default: bool = False) -> bool:
    return bool(parse_bool(get_str(data, dotted_key, ""), bool(default)))


class SwitchValueError(ValueError):
    """模型填的开关写了 true、false 以外认不出的值。"""


def switch_refusal(name: str, raw: Any) -> str:
    shown = json.dumps(raw, ensure_ascii=False, default=str)
    if len(shown) > _SHOWN_SWITCH_CHARS:
        shown = shown[:_SHOWN_SWITCH_CHARS] + "...<truncated>"
    return f"{name} 只能是 true 或 false，收到的是 {shown}"


def parse_switch(raw: Any, default: _SwitchDefault, *, name: str) -> bool | _SwitchDefault:
    """模型填的开关：没写、null、空串照 default，parse_bool 认得的写法照读，认不出的抛 SwitchValueError，不照 default 执行。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    value = parse_bool(raw)
    if value is None:
        raise SwitchValueError(switch_refusal(name, raw))
    return value


def switch_arg(args: Mapping[str, Any], key: str, default: _SwitchDefault) -> bool | _SwitchDefault:
    """工具参数里名为 key 的开关，读法同 parse_switch。"""
    return parse_switch(args.get(key), default, name=key)


def main_session_enabled(config: dict[str, Any]) -> bool:
    """主节点的历史从 data/conversations 读；关掉时退回旧的 context 快照。supervisor 和 engine 两边得读出同一个结论。"""
    return get_bool(config, "engine.child_session.main_session_enabled", True)


DEFAULT_TASK_MAX_RUNNING_SECONDS = 3600.0
_MIN_TASK_MAX_RUNNING_SECONDS = 300.0
# supervisor 每隔这么久检查一次卡死的 running 任务；dream 要据此留足等待余量，所以放在两边都能导入的地方
TASK_REAP_INTERVAL_SECONDS = 60.0


def task_max_running_seconds() -> float:
    """running 任务的运行时长硬上限：CLONOTH_TASK_MAX_RUNNING_SECONDS，默认 3600 秒，最低 300 秒。"""
    raw = os.getenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TASK_MAX_RUNNING_SECONDS
    except ValueError:
        value = DEFAULT_TASK_MAX_RUNNING_SECONDS
    return max(_MIN_TASK_MAX_RUNNING_SECONDS, value)


def load_yaml_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_text_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def read_admin_token(workspace_root: Path) -> str:
    """Supervisor 管理令牌：环境变量优先，其次 supervisor 启动时写出的 data/.admin_token。"""
    token = os.getenv("CLONOTH_ADMIN_TOKEN", "").strip()
    if token:
        return token
    return load_text_file(workspace_root / "data" / ".admin_token").strip()


def admin_auth_headers(workspace_root: Path) -> dict[str, str]:
    """取不到令牌时返回空 dict：请求照发，由服务端决定拒不拒，不在客户端提前失败。"""
    token = read_admin_token(workspace_root)
    return {"Authorization": f"Bearer {token}"} if token else {}


def shared_env_value(key: str) -> str:
    """本进程工作区的共享密钥文件里这个键的值。没钉工作区的进程（测试、临时脚本）没有这一层。"""
    root = workspace_env_root()
    if root is None:
        return ""
    return config_layout.shared_secrets(root).get(key, "")


def _resolve_env_names(
    names: str,
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """``A|B`` 这样的变量名列表，取第一个有值的，都没有是空串。

    每个名字依次查：进程环境（给了 environ 就用它代替进程环境）、extra（解析好的 .env）、共享密钥文件。
    ``DRAW_TAG_MODEL|DRAW_PLANNER_MODEL`` 这种写法让新名字优先，旧名字照样认。
    """
    source = os.environ if environ is None else environ
    for name in names.split("|"):
        key = name.strip()
        if not key:
            continue
        # 共享密钥垫在最底下：实例 .env 的同名项优先；不灌进 os.environ，组里别的实例改了它这边下一次取值就生效。
        val = str(source.get(key, "") or "") or (extra.get(key, "") if extra else "") or shared_env_value(key)
        # 返回 strip 后的值：.env 在 CRLF 仓库里解析出来常带尾随 \r，原样送上游会 400。
        if val and val.strip():
            return val.strip()
    return ""


def env_value(names: str) -> str:
    """``A|B`` 这样的变量名列表取第一个有值的：进程环境优先，再看共享密钥文件。都没有是空串。"""
    return _resolve_env_names(names)


def resolve_env_ref(
    text: str,
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """``${VAR}`` 和 ``$ENV{NEW|OLD}`` 换成变量的值，别的原样返回。查找顺序同 _resolve_env_names。"""
    s = str(text or "")
    if s.startswith("$ENV{") and s.endswith("}"):
        return _resolve_env_names(s[5:-1], extra, environ=environ)
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return _resolve_env_names(s[2:-1], extra, environ=environ)
    return s


def wait_supervisor(
    base_url: str,
    *,
    label: str,
    health_timeout_sec: float = 2.0,
    poll_interval_sec: float = 0.5,
    max_wait_sec: float = 0.0,
) -> bool:
    """轮询 /v1/health 直到 supervisor 应答。max_wait_sec 为 0 表示一直等。

    返回是否等到。等不到也让调用方自己决定要不要继续，卡死在这里更难排查。
    """
    url = f"{base_url.rstrip('/')}/v1/health"
    print(f"[{label}] waiting for supervisor: {base_url}", flush=True)
    deadline = (time.monotonic() + max_wait_sec) if max_wait_sec > 0 else 0.0
    with httpx.Client() as client:
        while True:
            try:
                if client.get(url, timeout=health_timeout_sec).status_code == 200:
                    print(f"[{label}] supervisor connected", flush=True)
                    return True
            except Exception:
                pass
            if deadline and time.monotonic() >= deadline:
                print(f"[{label}] supervisor still down after {max_wait_sec:.0f}s, going ahead", flush=True)
                return False
            time.sleep(poll_interval_sec)


async def fetch_openai_secret(http: httpx.AsyncClient, supervisor_url: str) -> dict[str, Any]:
    r = await http.get(f"{supervisor_url.rstrip('/')}/v1/config/openai/secret")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class MainChannel:
    """主渠道解析结果。provider 是 ProviderRegistry 的 key，决定用哪套请求格式。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = "openai"
    channel_name: str = ""
    # 渠道块里手写的 options，和地址、密钥出自 Supervisor 手上同一份配置。
    options: dict[str, Any] = field(default_factory=dict)


def normalize_openai_secret(data: dict[str, Any]) -> MainChannel:
    if not isinstance(data, dict):
        return MainChannel()
    raw_options = data.get("options")
    return MainChannel(
        api_key=resolve_env_ref(str(data.get("api_key") or "").strip()),
        base_url=resolve_env_ref(str(data.get("base_url") or "").strip()),
        model=resolve_env_ref(str(data.get("model") or "gpt-4o-mini").strip()) or "gpt-4o-mini",
        # 老 supervisor 不回这个字段，回落 openai 即改动前的行为。
        provider=str(data.get("provider") or "").strip().lower() or "openai",
        channel_name=str(data.get("channel_name") or "").strip(),
        # 老 supervisor 也不回 options，按渠道块没写算；${VAR} 不展开，和点名渠道的 options 一样原样用。
        options=dict(raw_options) if isinstance(raw_options, dict) else {},
    )


# ---------------------------------------------------------------------------
#  系统模型渠道解析（compact / summary / intent / review / image）
# ---------------------------------------------------------------------------
# [2026-07-16] Why: 压缩/轮摘要/读图这三个系统用途，用户希望：
#   1) 不填时跟随主渠道 url/key（只换模型名，甚至模型名也可留空跟随主模型）；
#   2) 也能单独指定 url/key/model；
#   3) 环境变量和 config/models.yaml 两种方式都支持。
# How: 统一读 config/models.yaml 的 system_models.<slot> 块，每个字段解析 ${VAR}；
#   字段留空时按 slot 专属环境变量兜底，再留空则跟随主渠道对应值。
# Purpose: 三个系统用途共用同一套解析规则，配置集中在 config/models.yaml + .env。
#
# config/models.yaml 布局：
#   system_models:
#     compact:  { model: "...", base_url: "${...}", api_key: "${...}" }
#     summary:  { model: "..." }                 # 只换模型，url/key 跟随主渠道
#     image:    { model: "gemini-3.5-flash" }
#
# slot -> 环境变量映射：
#   compact -> CLONOTH_COMPACT_MODEL / CLONOTH_COMPACT_BASE_URL / CLONOTH_COMPACT_API_KEY
#   （每个 slot 还有一个 _PROVIDER，指定这一槽用哪套请求格式）
#   summary -> CLONOTH_SUMMARY_MODEL / CLONOTH_SUMMARY_BASE_URL / CLONOTH_SUMMARY_API_KEY
#   image   -> CLONOTH_IMAGE_MODEL   / CLONOTH_IMAGE_BASE_URL   / CLONOTH_IMAGE_API_KEY
#   intent  -> CLONOTH_INTENT_MODEL  / CLONOTH_INTENT_BASE_URL  / CLONOTH_INTENT_API_KEY
#   review  -> CLONOTH_REVIEW_MODEL  / CLONOTH_REVIEW_BASE_URL  / CLONOTH_REVIEW_API_KEY

@dataclass(frozen=True)
class SlotSpec:
    """一个系统槽位。engine=False 的由工具子进程自己解析。"""

    key: str
    label: str
    desc: str
    engine: bool
    # 能不能自选渠道。生图那两个的请求格式写死在工具源码里，配了也不生效。
    supports_provider: bool = True

    @property
    def env_prefix(self) -> str:
        return "CLONOTH_" + self.key.upper()


SYSTEM_MODEL_SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec("compact", "上下文压缩", "对话太长时压缩历史。换个便宜模型最划算。", True),
    SlotSpec("summary", "轮摘要", "每轮结束后写一条摘要，供回忆和抽取用。", True),
    SlotSpec("intent", "接话意愿", "判断群里这句话要不要接。调用频繁，务必用便宜的。", True),
    SlotSpec("review", "代码审查", "代码审查节点审改动用的模型。留空跟随主渠道。", True),
    SlotSpec("image", "读图", "给看不了图的模型描述图片内容。留空跟随主渠道。", False),
    SlotSpec("image_gpt", "生图（GPT）", "gpt_image_2 工具用的渠道。", False, supports_provider=False),
    SlotSpec(
        "image_gemini", "生图（Gemini）", "gemini_image 工具用的渠道，走 Gemini 原生接口。",
        False, supports_provider=False,
    ),
)

_SYSTEM_MODEL_ENV_PREFIX = {spec.key: spec.env_prefix for spec in SYSTEM_MODEL_SLOTS}

# 生图工具名 → system_models 槽位。控制台的默认渠道选项也认这两个名字。
IMAGE_TOOL_SLOTS: dict[str, str] = {
    "gpt_image_2": "image_gpt",
    "gemini_image": "image_gemini",
}

_loaded_modules: dict[str, Any] = {}


def load_module_from(path: Path, module_name: str) -> Any:
    """按文件路径加载模块并缓存，取不到返回 None。

    工具那几个文件得让主进程也能读（判定必须同源），但它们的模块名 —— `_channel`、
    `common` —— 太通用，插进 sys.path 迟早跟别的东西撞。
    """
    key = str(path)
    if key in _loaded_modules:
        return _loaded_modules[key]
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _loaded_modules[key] = module
    return module


@dataclass(frozen=True)
class ImageToolStatus:
    """一个绘图工具现在能不能出图。problem 非空表示判断不了：配置读不了或判定本身出错，不能当成没配。"""

    available: bool = False
    problem: str = ""

    @property
    def unconfigured(self) -> bool:
        """确定没配。门控只摘这一种，判断不了的留着，让工具自己报出读不了的原因。"""
        return not self.available and not self.problem


def _image_channel_module(root: Path) -> Any:
    return load_module_from(root / "tools" / "_channel.py", "clonoth_image_channel")


def _judgement_failed(subject: str, exc: Exception) -> str:
    # 只报异常类型：消息里可能带着配置里的值，这句原因会进提示词和日志
    return f"判断 {subject} 能不能用时出错（{type(exc).__name__}）"


def image_tool_status(workspace_root: Path, tool_name: str) -> ImageToolStatus:
    """这个生图工具现在能不能跑通，和工具子进程同一份 resolve_image_channel。"""
    name = str(tool_name or "").strip()
    slot = IMAGE_TOOL_SLOTS.get(name)
    if not slot:
        return ImageToolStatus()
    root = Path(workspace_root)
    module = _image_channel_module(root)
    if module is None:
        return ImageToolStatus()
    try:
        channel = module.resolve_image_channel(slot, root=root)
    except Exception as exc:
        return ImageToolStatus(problem=_judgement_failed(name, exc))
    if channel.error:
        return ImageToolStatus(problem=channel.error)
    return ImageToolStatus(available=bool(channel.usable))


def image_tool_available(workspace_root: Path, tool_name: str) -> bool:
    """这个生图工具现在能不能跑通。判断不了也是 False，要分清的用 image_tool_status。"""
    return image_tool_status(workspace_root, tool_name).available


def image_default_channel(workspace_root: Path) -> str:
    """控制台选的默认生图工具，和可用性判断读同一份 models.yaml。没选、不是生图工具名或读不了是空串。"""
    root = Path(workspace_root)
    module = _image_channel_module(root)
    if module is None:
        return ""
    name = module.image_default_channel(root=root)
    return name if name in IMAGE_TOOL_SLOTS else ""


def novelai_status(workspace_root: Path) -> ImageToolStatus:
    """NovelAI 配了 key 才算可用：settings.yaml 优先，其次 NOVELAI_API_KEY。settings.yaml 读不了时判断不了。

    判定借 drawtools 自己那份 —— 它按工作区变量找 config/drawtools，supervisor 早把那组变量
    钉在本工作区上。settings.example.yaml 里 api_key 是空串，回退到示例不会判成已配。
    """
    module = load_module_from(
        Path(workspace_root) / "tools" / "drawtools" / "common.py", "clonoth_drawtools_common")
    if module is None:
        return ImageToolStatus()
    try:
        settings = module.load_settings()
    except module.DrawtoolsConfigUnreadable as exc:
        return ImageToolStatus(problem=str(exc))
    except Exception as exc:
        return ImageToolStatus(problem=_judgement_failed("NovelAI", exc))
    try:
        return ImageToolStatus(available=bool(module.resolve_api_key(settings)))
    except Exception as exc:
        return ImageToolStatus(problem=_judgement_failed("NovelAI", exc))


def novelai_available(workspace_root: Path) -> bool:
    """NovelAI 现在能不能出图。判断不了也是 False，要分清的用 novelai_status。"""
    return novelai_status(workspace_root).available


class _ConfigUnusable(ValueError):
    """这一版配置文件读不了、解析不了，或者内容用不了。消息只有原因，不带文件内容。"""


@dataclass(frozen=True)
class _ConfigVersion:
    """一份配置文件按某一版读出来的样子。

    这一版用不了时 data 留着上一份能用的、error 写着为什么；adopted 是这个进程有没有读成过一份。
    """

    stamp: tuple[int, int, int] | None
    data: dict[str, Any]
    adopted: bool
    error: str = ""


class _LastGoodConfig:
    """按 file_stamp 缓存一份 YAML 配置，交出去的都是副本。

    新的那一版用不了时记一次日志、沿用这个进程上一份读成的；一份都没读成过就交出 fallback。
    文件不在时，keep_when_missing 决定沿用上一份还是交出 fallback。
    """

    def __init__(
        self,
        *,
        label: str,
        parse: Callable[[Path], dict[str, Any] | None],
        fallback: Callable[[], dict[str, Any]],
        keep_when_missing: bool,
        kept_note: str,
        fallback_note: str,
    ) -> None:
        self._label = label
        self._parse = parse
        self._fallback = fallback
        self._keep_when_missing = keep_when_missing
        self._kept_note = kept_note
        self._fallback_note = fallback_note
        self._versions: dict[str, _ConfigVersion] = {}
        self._lock = threading.Lock()

    def fresh(self) -> _LastGoodConfig:
        """设置一样、一份都还没读过的缓存。"""
        return _LastGoodConfig(
            label=self._label,
            parse=self._parse,
            fallback=self._fallback,
            keep_when_missing=self._keep_when_missing,
            kept_note=self._kept_note,
            fallback_note=self._fallback_note,
        )

    def load(self, path: Path) -> dict[str, Any]:
        return copy.deepcopy(self.version(path).data)

    def version(self, path: Path) -> _ConfigVersion:
        stamp = config_layout.file_stamp(path)
        key = str(path)
        with self._lock:
            cached = self._versions.get(key)
            if cached is not None and cached.stamp == stamp:
                return cached
            version = self._next_version_locked(path, stamp, cached)
            self._versions[key] = version
            return version

    def _next_version_locked(
        self, path: Path, stamp: tuple[int, int, int] | None, cached: _ConfigVersion | None,
    ) -> _ConfigVersion:
        kept = cached if cached is not None and cached.adopted else None
        try:
            data = None if stamp is None else self._parse(path)
        except _ConfigUnusable as exc:
            version = self._fallen_back(stamp, kept, str(exc))
            _log.warning("%s 用不了，%s：%s", self._label, self._note(version), version.error)
            return version
        if data is not None:
            return _ConfigVersion(stamp, data, adopted=True)
        if not self._keep_when_missing or kept is None:
            return _ConfigVersion(None, self._fallback(), adopted=False)
        _log.warning("%s 不见了，%s", self._label, self._kept_note)
        return _ConfigVersion(None, kept.data, adopted=True, error="文件不在")

    def _fallen_back(
        self, stamp: tuple[int, int, int] | None, kept: _ConfigVersion | None, error: str,
    ) -> _ConfigVersion:
        if kept is not None:
            return _ConfigVersion(stamp, kept.data, adopted=True, error=error)
        return _ConfigVersion(stamp, self._fallback(), adopted=False, error=error)

    def _note(self, version: _ConfigVersion) -> str:
        return self._kept_note if version.adopted else self._fallback_note


def _parse_models_config(path: Path) -> dict[str, Any] | None:
    """这一版文件的内容，空的按空配置算，文件这会儿不在了是 None。读不了、解析不了、顶层不是映射抛 _ConfigUnusable。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise _ConfigUnusable(f"读不了：{exc}") from exc
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        raise _ConfigUnusable(f"YAML 解析失败：{config_layout.yaml_error_text(exc)}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _ConfigUnusable("顶层必须是映射（key: value）")
    return data


# 文件不在是没配渠道，不是写坏，不沿用上一份。
_MODELS_CONFIG = _LastGoodConfig(
    label=config_layout.MODELS_FILE,
    parse=_parse_models_config,
    fallback=dict,
    keep_when_missing=False,
    kept_note="沿用上一份能用的",
    fallback_note="按空配置算，点名渠道、系统槽位和备选链在修好之前都不生效",
)


def load_models_config(workspace_root: Path | str) -> dict[str, Any]:
    """模型渠道配置 config/models.yaml，给调用方一份自己的副本。

    运行中写坏时沿用这个进程上一份读成的：Supervisor 给主渠道的那份也停在上一份，点名渠道、系统槽位和备选链才不会单独没了。
    """
    return _MODELS_CONFIG.load(config_layout.models_path(workspace_root))


# 每份平台密钥文件最近告警过的那一版：同一版坏文件只告警一次。
_platform_secrets_warned: dict[str, tuple[int, int, int] | None] = {}
_platform_secrets_warned_lock = threading.Lock()


def load_platform_secrets(workspace_root: Path | str) -> dict[str, Any]:
    """平台密钥 data/platform_secrets.yaml：顶层按平台 id 分段。

    读不了按空表算，和工具子进程同一个回退；同一版坏文件只告警一次。
    """
    path = config_layout.platform_secrets_path(workspace_root)
    stamp = config_layout.file_stamp(path)
    try:
        data = config_layout.load_yaml_text(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except Exception as exc:
        # 只报位置和问题：出错那一行多半就是令牌本身
        _warn_platform_secrets_unreadable(path, stamp, config_layout.config_error_text(exc))
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        _warn_platform_secrets_unreadable(path, stamp, "顶层不是映射")
        return {}
    return data


def _warn_platform_secrets_unreadable(path: Path, stamp: tuple[int, int, int] | None, reason: str) -> None:
    key = str(path)
    with _platform_secrets_warned_lock:
        if key in _platform_secrets_warned and _platform_secrets_warned[key] == stamp:
            return
        _platform_secrets_warned[key] = stamp
    _log.warning("%s 读不了（%s），平台密钥全按没配算", path, reason)


def stray_platforms_section(workspace_root: Path | str) -> bool:
    """模型配置里还写着 platforms 段：照改造前的文档配的，那一段现在谁也不读。"""
    return config_layout.LEGACY_PLATFORMS_KEY in load_models_config(workspace_root)


# ---------------------------------------------------------------------------
#  命名渠道解析
# ---------------------------------------------------------------------------
# 平台密钥原先写在模型配置顶层的这一段下。挪走之后仍占着这个名字：老配置里还留着这一段时，
# 不能把它当成一个叫 platforms 的渠道。
PLATFORMS_CONFIG_KEY = config_layout.LEGACY_PLATFORMS_KEY

# 模型配置顶层的渠道块由用户自己命名，只能靠排除法认出来。
CONFIG_META_KEYS = frozenset({
    "version", "provider", "fallbacks", "node_fallbacks", "system_models", "image_gen",
    PLATFORMS_CONFIG_KEY,
})

_CHANNEL_BLOCK_KEYS = ("base_url", "api_key", "model", "type")


def is_channel_block(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in _CHANNEL_BLOCK_KEYS)


@dataclass(frozen=True)
class Channel:
    """config/models.yaml 里的一个渠道块。known=False 表示这名字底下没有块。"""

    name: str = ""
    wire: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    known: bool = False


def resolve_channel(
    workspace_root: Path | str,
    name: str,
    *,
    config: dict[str, Any] | None = None,
) -> Channel:
    """把 provider 字段上的那个名字解析成一个渠道。

    渠道名区分大小写（`gemini-中转A` 就是这么存进 config/models.yaml 的），线格式名不区分。
    名字底下没有块时按线格式名处理，老配置的 `provider: deepseek` 因此原样继续可用。
    """
    requested = str(name or "").strip()
    if not requested:
        return Channel()

    if isinstance(config, dict):
        data = config
    elif workspace_root:
        data = load_models_config(workspace_root)
    else:
        data = {}
    block = data.get(requested)
    if requested in CONFIG_META_KEYS or not is_channel_block(block):
        return Channel(name=requested, wire=requested.lower())

    declared = str(block.get("type") or "").strip().lower()
    raw_options = block.get("options")
    return Channel(
        name=requested,
        wire=declared or requested.lower(),
        base_url=resolve_env_ref(str(block.get("base_url") or "").strip()),
        api_key=resolve_env_ref(str(block.get("api_key") or "").strip()),
        model=resolve_env_ref(str(block.get("model") or "").strip()),
        options=dict(raw_options) if isinstance(raw_options, dict) else {},
        known=True,
    )


# 平台密钥是从哪一处读到的，写进 PlatformSecret.source。
SECRET_FROM_CONFIG = "config"
SECRET_FROM_ENV = "env"


@dataclass(frozen=True)
class PlatformSecret:
    """平台显式配的一项密钥。source 为空表示两处都没配，由平台自己决定怎么办，比如自动生成。"""

    value: str = ""
    source: str = ""
    # data/platform_secrets.yaml 那份盖住了一个不同的 env 值。改了 .env 不见效时，这是唯一的线索。
    env_shadowed: bool = False


def _platform_section(secrets: Mapping[str, Any], platform_id: str) -> Mapping[str, Any]:
    block = secrets.get(platform_id)
    return block if isinstance(block, dict) else {}


def _secret_text(raw: Any, environ: Mapping[str, str] | None = None) -> str:
    # YAML 会把纯数字的令牌读成 int；布尔、列表、映射当成没配，免得 True 被当成令牌 "True"。
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return ""
    return resolve_env_ref(str(raw).strip(), environ=environ)


def platform_secret(
    workspace_root: Path | str,
    platform: PlatformDescriptor,
    name: str,
    *,
    secrets: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> PlatformSecret:
    """平台显式配的密钥：data/platform_secrets.yaml 的 <id>.<name> 优先，其次声明的环境变量。

    和系统槽位一样文件优先：运维改的是文件，一行早就忘了的 env 不该顶掉它。文件里可以写
    ${VAR} 引用 .env。自动生成的密钥文件由平台自己管，不在这里读。secrets 给了就不再读文件；
    environ 给了就拿它代替进程环境，声明的变量和 ${VAR} 引用一样，供要每次重读 .env 文件的调用方用。
    """
    spec = platform.secret(name)
    data = load_platform_secrets(workspace_root) if secrets is None else secrets
    from_config = _secret_text(_platform_section(data, platform.id).get(name), environ)
    source = os.environ if environ is None else environ
    from_env = str(source.get(spec.env, "") or "").strip() if spec.env else ""
    if from_config:
        return PlatformSecret(from_config, SECRET_FROM_CONFIG, env_shadowed=bool(from_env) and from_env != from_config)
    if from_env:
        return PlatformSecret(from_env, SECRET_FROM_ENV)
    return PlatformSecret()


# 离线工具不给工作区、env 里也没设时退回代码目录，和 bot.py、supervisor 一样。
_CODE_ROOT = Path(__file__).resolve().parent
# 没设工作区时从这份认工作区，和 bot.py 找不到 cwd 下那份时一样。
_CODE_ENV_FILE = _CODE_ROOT / ".env"


def load_workspace_env(workspace_root: Path | str, *, override: bool = False) -> Path | None:
    """认定本进程的工作区是 workspace_root 并灌它的 .env，返回灌的是哪份；工作区里没有就什么也不灌。

    路径覆盖和平台密钥常常只写在 .env 里，不灌进来的话读到的是另一份文件、另一把密钥。
    不借代码目录那份：多开时那是主实例的 .env，借过来这个实例就挂到主实例的端口、拿主实例的令牌。
    灌完把工作区变量钉回 workspace_root：从别的实例整份抄来的 .env 带着那个实例的 CLONOTH_WORKSPACE，
    不钉的话这个进程用的是这里，拉起的 engine 却认到那边。
    """
    own = Path(workspace_root) / ".env"
    loaded: Path | None = None
    if own.is_file():
        load_dotenv(own, override=override)
        loaded = own
    pin_workspace_env(workspace_root)
    return loaded


def _settle_workspace(explicit: Path | str | None, *, override: bool) -> Path:
    """定工作区并灌它的 .env，返回工作区根。

    没给工作区、env 里也没设时，先灌 cwd 下或代码目录那份 .env 再从里面认，和 bot.py 一样。
    env 里设了就不碰 cwd 那份：在代码目录里跑别的实例，读进来的会是主实例的端口和密钥。
    """
    seed: Path | None = None
    if explicit is not None and str(explicit).strip():
        root = Path(explicit).expanduser().resolve()
    else:
        root = workspace_env_root()
        if root is None:
            local = Path.cwd() / ".env"
            candidate = local if local.is_file() else _CODE_ENV_FILE
            if candidate.is_file():
                load_dotenv(candidate, override=False)
                seed = candidate
            root = resolve_workspace_root(_CODE_ROOT)
    if load_workspace_env(root, override=override) is None and seed is None:
        _log.warning("%s 下没有 .env：端口、令牌这些实例身份只剩进程环境里那些", root)
    return root


def offline_workspace(explicit: Path | str | None = None) -> Path:
    """离线工具定工作区并灌 .env，返回工作区根。进程里已经有的变量都不覆盖。

    升级后服务还没起过就跑迁移脚本时，配置还在旧位置：先搬好再读，否则按空的平台密钥算出来的全是错的。
    """
    root = _settle_workspace(explicit, override=False)
    config_layout.ensure_layout(root)
    return root


def process_workspace() -> Path:
    """常驻进程启动时定工作区并灌 .env，工作区自己那份盖过继承来的值。

    supervisor 退出码 75 自己重启时，launcher 的环境还停在服务启动那一刻；之后写进本实例 .env 的值
    （manage_secret 写的实例身份、部署脚本回填的令牌）要靠这次重灌才生效。
    """
    return _settle_workspace(None, override=True)


def undeclared_platform_secrets(
    workspace_root: Path | str,
    platform: PlatformDescriptor,
    *,
    secrets: Mapping[str, Any] | None = None,
) -> list[str]:
    """平台密钥文件里 <id> 段下平台没声明的键。拼错的键会被静默无视，运营者还以为配上了。"""
    data = load_platform_secrets(workspace_root) if secrets is None else secrets
    declared = {secret.name for secret in platform.secrets}
    return sorted(str(key) for key in _platform_section(data, platform.id) if str(key) not in declared)


@dataclass(frozen=True)
class SystemModel:
    """系统槽位解析结果。空字段表示这一项跟随主渠道。"""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    provider: str = ""


def resolve_system_model(
    workspace_root: Path,
    slot: str,
    *,
    default_model: str = "",
    default_base_url: str = "",
    default_api_key: str = "",
    default_provider: str = "",
) -> SystemModel:
    """解析一个系统槽位的渠道四项。

    每项独立回退：config/models.yaml system_models.<slot> > slot 专属环境变量 > 调用方给的
    主渠道值。留空表示跟随主渠道。

    provider 决定用哪套请求格式，也可以写成一个渠道名让调用方去继承那个块。给了
    base_url 却不给它，就会拿主渠道那家的格式去打这个地址 —— 换中转没事，换家必挂。
    """
    slot = (slot or "").strip().lower()
    prefix = _SYSTEM_MODEL_ENV_PREFIX.get(slot, "")

    block: dict[str, Any] = {}
    cfg = load_models_config(workspace_root)
    sm = cfg.get("system_models")
    if isinstance(sm, dict):
        raw_block = sm.get(slot)
        if isinstance(raw_block, dict):
            block = raw_block

    def _pick(field: str, env_suffix: str, fallback: str) -> str:
        v = resolve_env_ref(str(block.get(field) or "").strip())
        if v:
            return v
        # 槽位变量和 ${VAR} 走同一套查找：manage_secret 把它写进 config/secrets.env 时，进程环境里没有它。
        if prefix:
            v = _resolve_env_names(f"{prefix}_{env_suffix}")
            if v:
                return v
        return fallback

    return SystemModel(
        model=_pick("model", "MODEL", default_model),
        base_url=_pick("base_url", "BASE_URL", default_base_url),
        api_key=_pick("api_key", "API_KEY", default_api_key),
        # 不转小写：渠道名区分大小写，转了就查不到那个块。
        provider=_pick("provider", "PROVIDER", default_provider).strip(),
    )


def _parse_policy_config(path: Path) -> dict[str, Any] | None:
    """这一版 policy.yaml 解析并校验成运行期用的那份，文件这会儿不在了是 None，用不了抛 _ConfigUnusable。

    收不收哪一版和 Supervisor 的 PolicyEngine 是同一套：空文件、顶层不是映射、结构不对都算用不了。
    """
    # 放在函数里导入：supervisor.policy 顶上就导入了本模块。
    from supervisor.policy import parse_policy_text

    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise _ConfigUnusable(f"不是 UTF-8 文本：{exc}") from exc
    except OSError as exc:
        raise _ConfigUnusable(f"读不了：{exc}") from exc
    try:
        return parse_policy_text(text)
    except ValueError as exc:
        raise _ConfigUnusable(str(exc)) from exc


def _default_policy_config() -> dict[str, Any]:
    from supervisor.policy import default_policy

    return default_policy()


# 写坏、被删都沿用上一份，和 PolicyEngine 一样：两边认的 extra_roots 不能一边还在、一边没了。
_POLICY_CONFIG = _LastGoodConfig(
    label=config_layout.POLICY_FILE,
    parse=_parse_policy_config,
    fallback=_default_policy_config,
    keep_when_missing=True,
    kept_note="文件工具和项目说明沿用上一份能用的 extra_roots",
    fallback_note="文件工具和项目说明按内置默认策略的 extra_roots 算",
)


def load_policy_config(workspace_root: Path) -> dict[str, Any]:
    """policy.yaml 里 engine 和工具要用的那份（extra_roots），给调用方一份自己的副本。

    哪一版能用、用不了时生效哪份都跟 Supervisor 的 PolicyEngine 一致：运行中写坏或被删沿用这个进程上一份读成的，
    一份都没读成过就是内置默认策略。
    """
    return _POLICY_CONFIG.load(config_layout.policy_path(workspace_root))


def parse_extra_roots(workspace_root: Path, raw: Any) -> list[Path]:
    items = raw if isinstance(raw, list) else []
    out: list[Path] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        p = Path(item)
        out.append(real_path(p if p.is_absolute() else workspace_root / p))
    return out


def _inside_root(path: Path, root: Path | None) -> Path | None:
    """path 在 root 里时给出相对 root 的部分（root 本身是 "."），不在给 None。"""
    if root is None:
        return None
    try:
        return path.relative_to(real_path(root))
    except (OSError, RuntimeError, ValueError):
        return None


def _classify_mapped(workspace_root: Path, extras: list[Path], mapped: Path) -> tuple[str, bool]:
    """隔离副本里的路径换成原仓库里同一位置之后，按工作区、extra_roots、外部的次序判出显示名和是否外部。"""
    logical = config_layout.workspace_logical_path(workspace_root, mapped)
    if logical is not None:
        return logical, False
    for r in extras:
        if mapped == r or r in mapped.parents:
            return mapped.as_posix(), False
    return mapped.as_posix(), True


def classify_path(
    workspace_root: Path,
    extra_roots: list[Path],
    path_str: str,
    *,
    project_root: Path | None = None,
    project_origin: Path | None = None,
    foreign_root: Path | None = None,
) -> tuple[Path | None, str, bool]:
    """Resolve and classify a filesystem path.

    Relative paths resolve under project_root when it is given, otherwise under workspace_root.
    project_origin is given for a task isolated in a git worktree: paths under project_root are judged
    as the same place under project_origin. Paths under foreign_root (where every worktree lives) that
    are not the task's own count as external, or invalid when written relative.

    Returns (resolved, display_path, is_external):
      - resolved: resolved Path, or None if invalid
      - display_path: workspace-relative posix for workspace paths,
                      project-relative posix for paths only under project_root,
                      the mapped name for paths in the task's own worktree,
                      absolute posix for external, or error message if invalid
      - is_external: True for absolute paths outside workspace + project_root + extra_roots
    """
    base = workspace_root if project_root is None else project_root
    try:
        raw = Path(path_str)
        p = real_path(raw if raw.is_absolute() else base / path_str)
    except Exception as e:
        return None, f"invalid path: {e}", False

    # Ensure roots are resolved for reliable comparison
    extras = [real_path(r) for r in extra_roots]

    # 本任务的隔离副本：按原仓库里同一位置判，策略规则照常匹配
    if project_origin is not None:
        own = _inside_root(p, project_root)
        if own is not None:
            display, external = _classify_mapped(workspace_root, extras, Path(project_origin) / own)
            return p, display, external

    # 别的任务的隔离副本一律是外部
    if _inside_root(p, foreign_root) is not None:
        if raw.is_absolute():
            return p, p.as_posix(), True
        return None, "path escapes workspace root", False

    # Tier 1: workspace。多开实例的代码目录、共享表情包库和共享组配置经受信链接通到外面，照样按工作区里的名字判。
    logical = config_layout.workspace_logical_path(workspace_root, p)
    if logical is not None:
        return p, logical, False

    # Tier 1b: 项目根不是工作区时排在工作区之后，工作区里的东西不管从哪个根走到都按工作区里的名字判
    if project_root is not None and Path(project_root) != Path(workspace_root):
        logical = config_layout.workspace_logical_path(project_root, p)
        if logical is not None:
            return p, logical, False

    # Tier 2: trusted extra_roots
    for r in extras:
        try:
            p.relative_to(r)
            return p, p.as_posix(), False
        except ValueError:
            continue

    # Tier 3: untrusted external (absolute path)
    if raw.is_absolute():
        return p, p.as_posix(), True

    # Relative path that escapes workspace — invalid
    return None, "path escapes workspace root", False


def strip_tool_trace_blocks(text: str) -> str:
    s = str(text or "")
    start = "[CLONOTH_TOOL_TRACE v2]"
    end = "[/CLONOTH_TOOL_TRACE]"
    while True:
        i = s.find(start)
        if i < 0:
            break
        j = s.find(end, i)
        if j < 0:
            s = s[:i].rstrip()
            break
        s = (s[:i] + s[j + len(end):]).strip()
    return s.strip()

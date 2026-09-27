from __future__ import annotations

import copy
import importlib.util
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml
from dotenv import load_dotenv

from workspace import pin_workspace_env, resolve_workspace_root, workspace_env_root

if TYPE_CHECKING:
    from platforms.descriptor import PlatformDescriptor


DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "engine": {
        "max_steps": 32,
        "streaming": True,
        "history_limit": 80,
        "poll_interval_sec": 1.0,
        "approval_poll_interval_sec": 0.5,
        "http": {"client_timeout_sec": 60.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "tool_trace": {
            "max_inline_chars": 8000,
            "max_progress_arg_chars": 320,
        },
        "compact": {
            "threshold_chars": 800000,
            "keep_recent": 6,
        },
        "retry": {
            "max_retries": 3,
            "initial_delay_sec": 1.0,
            "max_delay_sec": 30.0,
            "backoff_multiplier": 2.0,
        },
        "model": "",
    },
    "providers": {
        "openai": {
            "timeout_sec": 60.0,
            # [AutoC 2026-06-01] Why: provider_options previously existed only on
            # node YAML, so provider-wide defaults had no documented global home.
            # How: add an empty options dict under the OpenAI provider defaults.
            # Purpose: runner can merge providers.<type>.options with node-level
            # provider_options while keeping old runtime.yaml files compatible.
            "options": {},
        }
    },
    "meta": {
        "execute_command": {
            "default_timeout_sec": 90.0,
            "max_output_chars": 12000,
            # [2026-07-14] 长任务自动转异步：同步等待超过
            # threshold_sec（且小于 timeout_sec）时，把 execute_command 转为后台
            # 异步交付，不阻塞推理循环；timeout_sec 仍是硬 kill 上限。
            "async_upgrade": {
                "enabled": True,
                "threshold_sec": 60.0,
            },
        },
        "git": {"diff_max_chars": 600000},
        "search": {"max_file_size_bytes": 3000000, "max_matches": 100},
    },
    "tools": {
        "command": {
            "default_timeout_sec": 60.0,
        }
    },
    "skills": {
        "max_budget_chars": 0,
    },
    "shell": {
        "default_conversation_key": "cli:default",
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
            "shell_new_console": None,
            "engine_workers": 2,
        },
    },
}


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def runtime_config_path(workspace_root: Path) -> Path:
    return workspace_root / "config" / "runtime.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    path = runtime_config_path(workspace_root)
    try:
        mtime = path.stat().st_mtime if path.exists() else -1.0
    except Exception:
        mtime = -1.0

    key = str(path.resolve())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return copy.deepcopy(cached[1])

    user_cfg: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                user_cfg = loaded
        except Exception:
            user_cfg = {}

    merged = _deep_merge(DEFAULT_RUNTIME_CONFIG, user_cfg)
    _CACHE[key] = (mtime, merged)
    return copy.deepcopy(merged)


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


def get_bool(data: dict[str, Any], dotted_key: str, default: bool = False) -> bool:
    raw = get_str(data, dotted_key, "")
    if not raw:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _resolve_env_names(names: str, extra: Mapping[str, str] | None = None) -> str:
    """Resolve a ``|``-separated list of env var names with fallback.

    Why: config wants "new var wins, fall back to old var" without duplicating
    node YAML. How: split on ``|``; return the first name whose value is a
    non-empty string, reading the process environment before ``extra`` (a
    parsed .env mapping); otherwise "". Purpose: e.g.
    ``DRAW_TAG_MODEL|DRAW_PLANNER_MODEL`` prefers the new name but stays
    backward compatible with the old one.
    """
    for name in names.split("|"):
        key = name.strip()
        if not key:
            continue
        val = os.getenv(key, "") or (extra.get(key, "") if extra else "")
        # 返回 strip 后的值：.env 在 CRLF 仓库里解析出来常带尾随 \r，原样送上游会 400。
        if val and val.strip():
            return val.strip()
    return ""


def resolve_env_ref(text: str, extra: Mapping[str, str] | None = None) -> str:
    s = str(text or "")
    # Supports single names and ``|``-separated fallback lists, e.g.
    #   $ENV{NEW_VAR}                 -> os.getenv("NEW_VAR")
    #   $ENV{NEW_VAR|OLD_VAR}         -> NEW_VAR if set (non-empty) else OLD_VAR
    if s.startswith("$ENV{") and s.endswith("}"):
        return _resolve_env_names(s[5:-1], extra)
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return _resolve_env_names(s[2:-1], extra)
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


def normalize_openai_secret(data: dict[str, Any]) -> MainChannel:
    if not isinstance(data, dict):
        return MainChannel()
    return MainChannel(
        api_key=resolve_env_ref(str(data.get("api_key") or "").strip()),
        base_url=resolve_env_ref(str(data.get("base_url") or "").strip()),
        model=resolve_env_ref(str(data.get("model") or "gpt-4o-mini").strip()) or "gpt-4o-mini",
        # 老 supervisor 不回这个字段，回落 openai 即改动前的行为。
        provider=str(data.get("provider") or "").strip().lower() or "openai",
    )


# ---------------------------------------------------------------------------
#  系统模型渠道解析（compact / summary / image / intent）
# ---------------------------------------------------------------------------
# [2026-07-16] Why: 压缩/轮摘要/读图这三个系统用途，用户希望：
#   1) 不填时跟随主渠道 url/key（只换模型名，甚至模型名也可留空跟随主模型）；
#   2) 也能单独指定 url/key/model；
#   3) 环境变量和 config.yaml 两种方式都支持。
# How: 统一读 data/config.yaml 的 system_models.<slot> 块，每个字段解析 ${VAR}；
#   字段留空时按 slot 专属环境变量兜底，再留空则跟随主渠道对应值。
# Purpose: 三个系统用途共用同一套解析规则，配置集中在 config.yaml + .env。
#
# config.yaml 布局：
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


def image_tool_available(workspace_root: Path, tool_name: str) -> bool:
    """这个生图工具现在能不能跑通。"""
    slot = IMAGE_TOOL_SLOTS.get(str(tool_name or "").strip())
    if not slot:
        return False
    root = Path(workspace_root)
    module = load_module_from(root / "tools" / "_channel.py", "clonoth_image_channel")
    if module is None:
        return False
    try:
        return bool(module.resolve_image_channel(slot, root=root).usable)
    except Exception:
        return False


def novelai_available(workspace_root: Path) -> bool:
    """NovelAI 配了 key 才算可用：settings.yaml 优先，其次 NOVELAI_API_KEY。

    判定借 drawtools 自己那份 —— 它的路径常量按模块位置算，从这里加载正好指向同一个
    工作区。settings.example.yaml 里 api_key 是空串，回退到示例不会判成已配。
    """
    module = load_module_from(
        Path(workspace_root) / "tools" / "drawtools" / "common.py", "clonoth_drawtools_common")
    if module is None:
        return False
    try:
        return bool(module.resolve_api_key(module.load_settings()))
    except Exception:
        return False


def _load_config_yaml(workspace_root: Path) -> dict[str, Any]:
    p = workspace_root / "data" / "config.yaml"
    data = load_yaml_dict(p)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
#  命名渠道解析
# ---------------------------------------------------------------------------
# 各平台的密钥写在这一段下：platforms.<平台 id>.<密钥名>。
PLATFORMS_CONFIG_KEY = "platforms"

# config.yaml 顶层的渠道块由用户自己命名，只能靠排除法认出来。
CONFIG_META_KEYS = frozenset({
    "version", "provider", "fallbacks", "node_fallbacks", "system_models", "image_gen",
    PLATFORMS_CONFIG_KEY,
})

_CHANNEL_BLOCK_KEYS = ("base_url", "api_key", "model", "type")


def is_channel_block(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in _CHANNEL_BLOCK_KEYS)


@dataclass(frozen=True)
class Channel:
    """config.yaml 里的一个渠道块。known=False 表示这名字底下没有块。"""

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

    渠道名区分大小写（`gemini-中转A` 就是这么存进 config.yaml 的），线格式名不区分。
    名字底下没有块时按线格式名处理，老配置的 `provider: deepseek` 因此原样继续可用。
    """
    requested = str(name or "").strip()
    if not requested:
        return Channel()

    if isinstance(config, dict):
        data = config
    elif workspace_root:
        data = _load_config_yaml(Path(workspace_root))
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
    # data/config.yaml 那份盖住了一个不同的 env 值。改了 .env 不见效时，这是唯一的线索。
    env_shadowed: bool = False


def _platform_section(config: Mapping[str, Any], platform_id: str) -> Mapping[str, Any]:
    section = config.get(PLATFORMS_CONFIG_KEY)
    block = section.get(platform_id) if isinstance(section, dict) else None
    return block if isinstance(block, dict) else {}


def _secret_text(raw: Any, extra: Mapping[str, str] | None = None) -> str:
    # YAML 会把纯数字的令牌读成 int；布尔、列表、映射当成没配，免得 True 被当成令牌 "True"。
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return ""
    return resolve_env_ref(str(raw).strip(), extra)


def platform_secret(
    workspace_root: Path | str,
    platform: PlatformDescriptor,
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> PlatformSecret:
    """平台显式配的密钥：data/config.yaml 的 platforms.<id>.<name> 优先，其次声明的环境变量。

    和系统槽位一样文件优先：运维改的是文件，一行早就忘了的 env 不该顶掉它。config 里可以写
    ${VAR} 引用 .env。自动生成的密钥文件由平台自己管，不在这里读。environ 给了就拿它代替进程环境，
    供要每次重读 .env 文件的调用方用。
    """
    spec = platform.secret(name)
    data = _load_config_yaml(Path(workspace_root)) if config is None else config
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
# 工作区里没有 .env 时退回代码目录那份，和 bot.py 找不到 cwd 下那份时一样。
_CODE_ENV_FILE = _CODE_ROOT / ".env"


def load_workspace_env(workspace_root: Path | str, *, override: bool = False) -> Path | None:
    """认定本进程的工作区是 workspace_root 并灌它的 .env：工作区那份优先，没有就用代码目录那份。返回灌的是哪份。

    路径覆盖和平台密钥常常只写在 .env 里，不灌进来的话读到的是另一份文件、另一把密钥。
    override 只对工作区自己那份生效：代码目录那份是几个实例共用的兜底，只补缺，不能盖掉进程拿到的值。
    灌完把工作区变量钉回 workspace_root：从别的实例整份抄来的 .env 带着那个实例的 CLONOTH_WORKSPACE，
    不钉的话这个进程用的是这里，拉起的 engine 却认到那边。
    """
    own = Path(workspace_root) / ".env"
    loaded: Path | None = None
    if own.is_file():
        load_dotenv(own, override=override)
        loaded = own
    elif _CODE_ENV_FILE.is_file():
        load_dotenv(_CODE_ENV_FILE, override=False)
        loaded = _CODE_ENV_FILE
    pin_workspace_env(workspace_root)
    return loaded


def _settle_workspace(explicit: Path | str | None, *, override: bool) -> Path:
    """定工作区并灌它的 .env，返回工作区根。

    没给工作区、env 里也没设时，先灌 cwd 下或代码目录那份 .env 再从里面认，和 bot.py 一样。
    env 里设了就不碰 cwd 那份：在代码目录里跑别的实例，读进来的会是主实例的端口和密钥。
    """
    if explicit is not None and str(explicit).strip():
        root = Path(explicit).expanduser().resolve()
    else:
        root = workspace_env_root()
        if root is None:
            local = Path.cwd() / ".env"
            bot_env = local if local.is_file() else _CODE_ENV_FILE
            if bot_env.is_file():
                load_dotenv(bot_env, override=False)
            root = resolve_workspace_root(_CODE_ROOT)
    load_workspace_env(root, override=override)
    return root


def offline_workspace(explicit: Path | str | None = None) -> Path:
    """离线工具定工作区并灌 .env，返回工作区根。进程里已经有的变量都不覆盖。"""
    return _settle_workspace(explicit, override=False)


def process_workspace() -> Path:
    """常驻进程启动时定工作区并灌 .env，工作区自己那份盖过继承来的值。

    supervisor 退出码 75 自己重启时，launcher 的环境还停在服务启动那一刻；控制台改过的密钥要靠这个生效。
    """
    return _settle_workspace(None, override=True)


def undeclared_platform_secrets(
    workspace_root: Path | str,
    platform: PlatformDescriptor,
    *,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """platforms.<id> 下平台没声明的键。拼错的键会被静默无视，运营者还以为配上了。"""
    data = _load_config_yaml(Path(workspace_root)) if config is None else config
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

    每项独立回退：config.yaml system_models.<slot> > slot 专属环境变量 > 调用方给的
    主渠道值。留空表示跟随主渠道。

    provider 决定用哪套请求格式，也可以写成一个渠道名让调用方去继承那个块。给了
    base_url 却不给它，就会拿主渠道那家的格式去打这个地址 —— 换中转没事，换家必挂。
    """
    slot = (slot or "").strip().lower()
    prefix = _SYSTEM_MODEL_ENV_PREFIX.get(slot, "")

    block: dict[str, Any] = {}
    cfg = _load_config_yaml(workspace_root)
    sm = cfg.get("system_models")
    if isinstance(sm, dict):
        raw_block = sm.get(slot)
        if isinstance(raw_block, dict):
            block = raw_block

    def _pick(field: str, env_suffix: str, fallback: str) -> str:
        # 1) config.yaml 字段（支持 ${VAR}）
        v = resolve_env_ref(str(block.get(field) or "").strip())
        if v:
            return v
        # 2) slot 专属环境变量
        if prefix:
            v = os.getenv(f"{prefix}_{env_suffix}", "").strip()
            if v:
                return v
        # 3) 主渠道兜底
        return fallback

    return SystemModel(
        model=_pick("model", "MODEL", default_model),
        base_url=_pick("base_url", "BASE_URL", default_base_url),
        api_key=_pick("api_key", "API_KEY", default_api_key),
        # 不转小写：渠道名区分大小写，转了就查不到那个块。
        provider=_pick("provider", "PROVIDER", default_provider).strip(),
    )


def load_policy_config(workspace_root: Path) -> dict[str, Any]:
    p = workspace_root / "data" / "policy.yaml"
    data = load_yaml_dict(p)
    return data if isinstance(data, dict) else {}


def parse_extra_roots(workspace_root: Path, raw: Any) -> list[Path]:
    items = raw if isinstance(raw, list) else []
    out: list[Path] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        p = Path(item)
        p = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
        out.append(p)
    return out


def classify_path(
    workspace_root: Path,
    extra_roots: list[Path],
    path_str: str,
) -> tuple[Path | None, str, bool]:
    """Resolve and classify a filesystem path.

    Returns (resolved, display_path, is_external):
      - resolved: resolved Path, or None if invalid
      - display_path: workspace-relative posix for workspace paths,
                      absolute posix for external, or error message if invalid
      - is_external: True for absolute paths outside workspace + extra_roots
    """
    try:
        raw = Path(path_str)
        p = raw.resolve() if raw.is_absolute() else (workspace_root / path_str).resolve()
    except Exception as e:
        return None, f"invalid path: {e}", False

    # Ensure roots are resolved for reliable comparison
    ws = workspace_root.resolve()
    extras = [r.resolve() for r in extra_roots]

    # Tier 1: workspace
    try:
        rel = p.relative_to(ws)
        return p, rel.as_posix(), False
    except ValueError:
        pass

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

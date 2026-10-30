"""工具子进程共用的渠道、平台密钥、开关参数解析和 YAML 配置读取。

工具是独立子进程，导不了 clonoth_runtime，所以在这里放一份而不是各抄一遍；YAML 的解析和报错按需借仓库根下的 config_layout。
密钥只能从 .env 和 config/secrets.env 读（平台令牌只认 .env）：safe_subprocess_env() 会把 *_API_KEY 和平台声明的密钥变量从环境里剥掉。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, TypeVar

_log = logging.getLogger(__name__)

# 这几家收 OpenAI 格式的 /chat/completions；anthropic 与 gemini 原生走自己的端点和请求体。
OPENAI_COMPATIBLE = frozenset({"openai", "deepseek"})
# 和 Engine 的 _OPENAI_COMPATIBLE_PROVIDERS 同一族（测试比对两份）：同一个兼容网关的根下同时挂着 chat/completions、responses 和 images。
OPENAI_FAMILY = frozenset({"openai", "openai-responses"})
# 和 config_layout 的 MODELS_FILE、PLATFORM_SECRETS_FILE、SHARED_SECRETS_FILE 同值：config_layout 到解析 YAML 时才按需导入，模块加载时还用不上它。
_MODELS_FILE = ("config", "models.yaml")
_PLATFORM_SECRETS_FILE = ("data", "platform_secrets.yaml")
_SHARED_SECRETS_FILE = ("config", "secrets.env")
_INSTANCE_ENV_FILE = ".env"
# 和 clonoth_runtime 的 parse_bool、parse_switch 认同一套写法、报同一句错（测试逐个比对两份）：写成字符串的 "false"、no 也算关
_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off"})
_SHOWN_SWITCH_CHARS = 40
_SwitchDefault = TypeVar("_SwitchDefault", bool, None)


def parse_bool(raw: object, default: bool | None = None) -> bool | None:
    """配置里的开关。认不出的（包括空）给 default；模型填的工具参数用 switch_arg。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


class SwitchValueError(ValueError):
    """模型填的开关写了 true、false 以外认不出的值。"""


def switch_refusal(name: str, raw: object) -> str:
    shown = json.dumps(raw, ensure_ascii=False, default=str)
    if len(shown) > _SHOWN_SWITCH_CHARS:
        shown = shown[:_SHOWN_SWITCH_CHARS] + "...<truncated>"
    return f"{name} 只能是 true 或 false，收到的是 {shown}"


def parse_switch(raw: object, default: _SwitchDefault, *, name: str) -> bool | _SwitchDefault:
    """模型填的开关：没写、null、空串照 default，parse_bool 认得的写法照读，认不出的抛 SwitchValueError，不照 default 执行。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    value = parse_bool(raw)
    if value is None:
        raise SwitchValueError(switch_refusal(name, raw))
    return value


def switch_arg(args: Mapping[str, object], key: str, default: _SwitchDefault) -> bool | _SwitchDefault:
    """工具参数里名为 key 的开关，读法同 parse_switch。"""
    return parse_switch(args.get(key), default, name=key)


class MainChannel(NamedTuple):
    """config/models.yaml 里当前活跃的那个渠道块。字段可能为空。"""

    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


_DOUBLE_QUOTE_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t"}


def _env_value(raw: str) -> str:
    """一行赋值等号右边的值，引号规则照 python-dotenv：单引号里原样，双引号里认转义，不带引号的截掉「空白+#」起的注释。"""
    raw = raw.strip()
    quote = raw[:1]
    if quote in ("'", '"'):
        chars: list[str] = []
        index = 1
        while index < len(raw):
            char = raw[index]
            if char == quote:
                return "".join(chars)
            if char == "\\" and index + 1 < len(raw):
                escaped = raw[index + 1]
                if quote == '"' and escaped in _DOUBLE_QUOTE_ESCAPES:
                    chars.append(_DOUBLE_QUOTE_ESCAPES[escaped])
                    index += 2
                    continue
                if quote == "'" and escaped in ("\\", "'"):
                    chars.append(escaped)
                    index += 2
                    continue
            chars.append(char)
            index += 1
    return re.sub(r"\s+#.*", "", raw).rstrip()


def _parse_env_text(text: str) -> dict[str, str | None]:
    pairs: dict[str, str | None] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        pairs[key] = _env_value(value)
    return pairs


def _read_env_file(path: Path) -> dict[str, str]:
    """dotenv 文件里有值的键。和主进程一样用 python-dotenv 解析，装不上时退回上面照它写的解析；
    空值不收：.env 里留着「KEY=」占位时，共享密钥里的同名项照样生效，主进程也是这么取的。"""
    try:
        from dotenv import dotenv_values as parse  # type: ignore
    except Exception:
        parse = None
    try:
        if parse is not None:
            if not path.is_file():
                return {}
            raw = parse(path, encoding="utf-8")
        else:
            raw = _parse_env_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return {str(key): str(value).strip() for key, value in raw.items() if value is not None and str(value).strip()}


def _dotenv_layers(base: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(共享密钥文件 config/secrets.env, 本实例 .env) 各自有值的键。"""
    return _read_env_file(base.joinpath(*_SHARED_SECRETS_FILE)), _read_env_file(base / _INSTANCE_ENV_FILE)


def dotenv_values(root: Path | None = None) -> dict[str, str]:
    """工作区的 dotenv 值：共享密钥文件 config/secrets.env 垫底，本实例 .env 的同名项盖在上面。

    root 默认 cwd：工具是以工作区为 cwd 拉起的子进程。
    """
    shared, instance = _dotenv_layers(Path(root) if root is not None else Path.cwd())
    return {**shared, **instance}


def _config_layout():
    """仓库根下的 config_layout：YAML 的解析和报错和主进程用同一份，抄自文件的内容进不了原因。

    按路径加载时仓库根已经在 sys.path 上；工具子进程的 sys.path 里只有脚本自己那一级，导不到再把仓库根补进去。
    """
    try:
        import config_layout
    except ImportError:
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        import config_layout
    return config_layout


def _import_failure(exc: ImportError) -> str:
    if exc.name == "yaml":
        return "没装 PyYAML"
    return f"导入不了 {exc.name or 'config_layout'}"


def _failure_reason(exc: BaseException) -> str:
    try:
        return _config_layout().config_error_text(exc)
    except ImportError as missing:
        return _import_failure(missing)


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    """和 config_layout.file_stamp 同一个指纹（测试比对两份）：修改时间、大小和 inode，不存在是 None。"""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def _parse_yaml_file(path: Path) -> tuple[dict, str, bool]:
    """(内容, 读不了的原因, 文件不存在)。空文件按空表算。原因只有位置和问题，不带文件内容。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return {}, "", True
    except (OSError, UnicodeDecodeError) as exc:
        return {}, _failure_reason(exc), False
    try:
        data = _config_layout().load_yaml_text(text)
    except ImportError as exc:
        return {}, _import_failure(exc), False
    except Exception as exc:
        return {}, _failure_reason(exc), False
    if data is None:
        return {}, "", False
    if not isinstance(data, dict):
        return {}, "顶层不是映射（key: value）", False
    return data, "", False


class YamlDocument(NamedTuple):
    """一份 YAML 配置读出来的样子。

    problem 非空：这一版读不了，又没有上一份可沿用，data 是空表。
    stale 非空：这一版读不了，data 是这个进程上一次读成的那份。
    """

    data: dict
    problem: str = ""
    stale: str = ""
    missing: bool = False


class _LastGood(NamedTuple):
    stamp: tuple[int, int, int] | None
    data: dict


_yaml_lock = threading.Lock()
_yaml_last_good: dict[str, _LastGood] = {}
# 每份文件最近告警过的那一版：同一版坏文件只告警一次。
_yaml_warned: dict[str, tuple[int, int, int] | None] = {}


def read_yaml_document(
    path: Path, *, label: str, keep_last_good: bool = True, unread_note: str = "",
) -> YamlDocument:
    """读一份顶层是映射的 YAML 配置，文件不存在或是空的按空表算。

    原因里只有位置和问题，不带原文。读不了时 keep_last_good 就沿用这个进程上一次读成的那份，
    同一版坏文件只告警一次；没有上一份可沿用时只在给了 unread_note 时告警（写成「按什么算」），
    脚本工具每次都是新进程，由调用方把 problem 交给模型。交出去的是副本，调用方改了不影响下一次读。
    """
    path = Path(path)
    key = os.path.abspath(path)
    stamp = _file_stamp(path)
    with _yaml_lock:
        cached = _yaml_last_good.get(key)
    if cached is not None and stamp is not None and cached.stamp == stamp:
        return YamlDocument(copy.deepcopy(cached.data))
    data, reason, missing = _parse_yaml_file(path)
    if missing:
        with _yaml_lock:
            _yaml_last_good.pop(key, None)
            _yaml_warned.pop(key, None)
        return YamlDocument({}, missing=True)
    if not reason:
        with _yaml_lock:
            _yaml_last_good[key] = _LastGood(stamp, data)
            _yaml_warned.pop(key, None)
        return YamlDocument(copy.deepcopy(data))
    problem = f"{label} 读不了：{reason}"
    with _yaml_lock:
        fallback = _yaml_last_good.get(key) if keep_last_good else None
        first = key not in _yaml_warned or _yaml_warned[key] != stamp
        _yaml_warned[key] = stamp
    if fallback is not None:
        if first:
            _log.warning("%s，沿用上一份能读的内容", problem)
        return YamlDocument(copy.deepcopy(fallback.data), stale=problem)
    if first and unread_note:
        _log.warning("%s，%s", problem, unread_note)
    return YamlDocument({}, problem=problem)


def _models_document(base: Path) -> YamlDocument:
    return read_yaml_document(base.joinpath(*_MODELS_FILE), label="/".join(_MODELS_FILE))


def _is_provider_block(value: object) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("base_url", "api_key", "model", "type"))


def _env_ref_names(text: str) -> str | None:
    """整值写成 $ENV{A|B} 或 ${A|B} 时里面的变量名列表，不是引用是 None。认法和 clonoth_runtime.resolve_env_ref 一样（测试拿同一张表比对两份）。"""
    if text.startswith("$ENV{") and text.endswith("}"):
        return text[len("$ENV{"):-1]
    if text.startswith("${") and text.endswith("}") and len(text) > len("${}"):
        return text[len("${"):-1]
    return None


class _EnvView:
    """工作区的 .env 与共享密钥文件，盖在进程环境底下。"""

    def __init__(self, *, root: Path | None = None, hidden_env: Callable[[str], bool] | None = None) -> None:
        # root 默认 cwd：工具是以工作区为 cwd 拉起的子进程。主进程里的调用方
        # （提示词注入）cwd 不一定在工作区，得把 root 显式传进来。
        self._base = Path(root) if root is not None else Path.cwd()
        shared, self._instance_env = _dotenv_layers(self._base)
        self._dotenv = {**shared, **self._instance_env}
        # 主进程替工具判能不能用时给：它认作工具子进程看不到的进程环境变量（safe_subprocess_env 剥掉的那些）按没设算。
        self._hidden_env = hidden_env

    def _sees(self, key: str) -> bool:
        return self._hidden_env is None or not self._hidden_env(key)

    def env(self, key: str) -> str:
        return ((os.environ.get(key, "") if self._sees(key) else "") or self._dotenv.get(key, "")).strip()

    def instance_env(self, key: str) -> str:
        """进程环境 > 本实例 .env，不看共享密钥文件。"""
        return ((os.environ.get(key, "") if self._sees(key) else "") or self._instance_env.get(key, "")).strip()

    def first_env(self, names: str) -> str:
        """取第一个有值的变量。仓库的节点文件在用 $ENV{NEW|OLD} 这种回退写法。"""
        for name in str(names or "").split("|"):
            value = self.env(name.strip())
            if value:
                return value
        return ""

    def _deref(self, value: object) -> str:
        text = str(value or "").strip()
        names = _env_ref_names(text)
        return text if names is None else self.first_env(names)


def resolve_env_ref(
    value: object, *, root: Path | None = None, hidden_env: Callable[[str], bool] | None = None,
) -> str:
    """整值写成 ${VAR}、$ENV{NEW|OLD} 的换成变量的值，没设是空串，别的原样返回。

    变量按进程环境 > 本实例 .env > 共享密钥文件 config/secrets.env 找；root、hidden_env 同 _EnvView。
    """
    return _EnvView(root=root, hidden_env=hidden_env)._deref(value)


class _ConfigView(_EnvView):
    """_EnvView 加上模型渠道配置 config/models.yaml 和平台密钥 data/platform_secrets.yaml。"""

    def __init__(self, *, root: Path | None = None, hidden_env: Callable[[str], bool] | None = None) -> None:
        super().__init__(root=root, hidden_env=hidden_env)
        models = _models_document(self._base)
        self._config = models.data
        # models.yaml 读不了又没有上一份时的原因。调用方先报它：空配置下报「没配 key」是在误导人。
        self.models_problem = models.problem
        # 读不了按空表算、不沿用上一份，和主进程的 load_platform_secrets 同一个回退。
        self._platform_secrets = read_yaml_document(
            self._base.joinpath(*_PLATFORM_SECRETS_FILE), label="/".join(_PLATFORM_SECRETS_FILE), keep_last_good=False,
        ).data

    def platform_secret(self, platform_id: str, name: str, env: str = "") -> str:
        """和主进程同一个顺序：data/platform_secrets.yaml 的 <id>.<name> > 环境变量 > 本实例 .env。

        平台令牌是实例身份，不读共享的 config/secrets.env：适配进程和账号操作也只认本实例那份。
        """
        block = self._platform_secrets.get(platform_id)
        raw = block.get(name) if isinstance(block, dict) else None
        # YAML 会把纯数字的令牌读成 int；布尔、列表、映射当成没配，和主进程判得一样。
        if isinstance(raw, (str, int)) and not isinstance(raw, bool):
            value = self._deref(str(raw))
            if value:
                return value
        return self.instance_env(env) if env else ""


def platform_secret(platform_id: str, name: str, env: str = "", *, root: Path | None = None) -> str:
    """平台显式配的一项密钥，都没配返回空串，由调用方去读平台自动生成的密钥文件。

    子进程的环境里剥掉了平台密钥，env 那一档通常是空的，实际落到 data/platform_secrets.yaml 或本实例 .env。
    """
    return _ConfigView(root=root).platform_secret(platform_id, name, env)


class Channel(_ConfigView):
    """一个 system_models 槽位的配置视图。"""

    def __init__(self, slot: str, *, root: Path | None = None, hidden_env: Callable[[str], bool] | None = None) -> None:
        super().__init__(root=root, hidden_env=hidden_env)
        self.slot = (slot or "").strip().lower()
        block = (self._config.get("system_models") or {}) if isinstance(self._config, dict) else {}
        block = block.get(self.slot) if isinstance(block, dict) else None
        self._block = block if isinstance(block, dict) else {}

    def own(self, field: str, env_suffix: str) -> str:
        """只看这一槽自己配了什么：models.yaml 的槽位块 > 槽位专属环境变量。"""
        value = self._deref(self._block.get(field))
        if value:
            return value
        prefix = self.slot.upper()
        return self.env("CLONOTH_" + prefix + "_" + env_suffix) if prefix else ""

    def pick(self, field: str, env_suffix: str, *fallbacks: str) -> str:
        """槽位自己的配置优先，然后按顺序取第一个非空回退值。"""
        value = self.own(field, env_suffix)
        if value:
            return value
        for candidate in fallbacks:
            text = (candidate or "").strip()
            if text:
                return text
        return ""

    def named(self, name: str) -> MainChannel:
        """按名字取一个渠道块。名字底下没有块时只当它是个线格式名。

        provider 报的一律是线格式，不是块名 —— 块名由用户随便起，发请求认不得。
        """
        text = str(name or "").strip()
        block = self._config.get(text) if (text and isinstance(self._config, dict)) else None
        if not _is_provider_block(block):
            return MainChannel(provider=text.lower())
        return MainChannel(
            provider=str(block.get("type") or "").strip().lower() or text.lower(),
            base_url=self._deref(block.get("base_url")),
            api_key=self._deref(block.get("api_key")),
            model=self._deref(block.get("model")),
        )

    def main(self) -> MainChannel:
        """当前活跃渠道。"""
        if not isinstance(self._config, dict):
            return MainChannel()
        name = str(self._config.get("provider") or "").strip() or "openai"
        if not _is_provider_block(self._config.get(name)):
            name = "openai"
        if not _is_provider_block(self._config.get(name)):
            return MainChannel()
        return self.named(name)

    def main_for(self, *providers: str) -> MainChannel:
        """主渠道属于这几家之一时才把它交出去，否则当作没配。

        专用端点的请求格式跟 provider 绑死，拿 Gemini 的地址发 OpenAI 格式只会 404。
        """
        wanted = {str(p or "").strip().lower() for p in providers if str(p or "").strip()}
        main = self.main()
        return main if main.provider in wanted else MainChannel()


class ImageSlotSpec(NamedTuple):
    """一个生图槽位的回退链。"""

    providers: tuple[str, ...]      # 主渠道属于这几家时才可借
    key_envs: tuple[str, ...]
    url_envs: tuple[str, ...]
    default_model: str
    default_base_url: str = ""      # 留空表示这家没有公开的默认端点，必须配地址
    wire: str = "openai"            # 地址按哪家的规则归一，和 provider 发对话时同一套


IMAGE_SLOTS: dict[str, ImageSlotSpec] = {
    "image_gpt": ImageSlotSpec(
        providers=tuple(sorted(OPENAI_FAMILY)),
        key_envs=("OPENAI_API_KEY",),
        url_envs=("OPENAI_BASE_URL",),
        default_model="gpt-image-2",
    ),
    "image_gemini": ImageSlotSpec(
        providers=("gemini",),
        key_envs=("GEMINI_API_KEY", "OPENAI_API_KEY"),
        url_envs=("GEMINI_BASE_URL", "OPENAI_BASE_URL"),
        default_model="gemini-3-pro-image-preview",
        default_base_url="https://generativelanguage.googleapis.com",
        wire="gemini",
    ),
}


class ImageChannel(NamedTuple):
    """生图渠道。error 非空时不可用：配置读不了，判断不了该用哪条渠道。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and not self.error)


class VisionChannel(NamedTuple):
    """看图渠道。error 非空时不可用，文案由调用方自己决定怎么呈现。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    error: str = ""
    provider: str = ""
    wire: str = "openai"

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and self.model and not self.error)

    @property
    def family(self) -> str:
        """图片格式接受集用哪一套。渠道是明说的，不必再拿域名去猜。"""
        return _wire_module().family_for_wire(self.wire)

    def endpoint(self) -> str:
        return _wire_module().endpoint(self.wire, self.base_url, self.model)


# 什么都没配时的兜底。Gemini 的 OpenAI 兼容端点谁都收得下，且它的免费额度最容易拿到。
_VISION_FALLBACK_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_VISION_FALLBACK_WIRE = "openai"
# 按线格式给默认模型。Anthropic 没有能猜的名字，缺了就让人自己填，别拿别家的名字去撞 404。
_VISION_DEFAULT_MODELS = {
    "openai": "gemini-3.5-flash",
    "openai-responses": "gemini-3.5-flash",
    "gemini": "gemini-3.5-flash",
}
# 各家没配地址时的官方根。和 providers/*.py 的 default_base_url 同值。
_WIRE_DEFAULT_URLS = {
    "gemini": "https://generativelanguage.googleapis.com",
    "anthropic": "https://api.anthropic.com",
}


def _wire_module():
    """加载同目录的 _wire。

    这个文件有三种被加载的方式 —— 包内 `tools._channel`、工具子进程里的裸 `_channel`、
    以及 clonoth_runtime 按路径加载 —— 模块级 import 会在后两种下炸掉整个模块。
    """
    try:
        from . import _wire  # type: ignore[no-redef]
        return _wire
    except ImportError:
        pass
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import _wire  # type: ignore[no-redef]
    return _wire


def resolve_vision_channel(*, root: Path | None = None) -> VisionChannel:
    """看图渠道的完整解析。读图工具和表情包打标共用这一份，判定才不会打架。

    先看 system_models.image；没配就跟随主渠道 —— 常见部署是同一个中转站换个模型名。
    模型名不跟随：主渠道多半正是那个看不了图的纯文本模型。
    """
    wire_mod = _wire_module()
    channel = Channel("image", root=root)
    if channel.models_problem:
        return VisionChannel(error=channel.models_problem)
    main = channel.main()

    # 槽位的 provider 可以写成一个渠道名，那就连它的地址密钥一并用上。
    referenced = channel.named(channel.own("provider", "PROVIDER"))
    base_url = channel.own("base_url", "BASE_URL") or referenced.base_url
    # 地址和格式是绑死的一对：这一槽自己指了地址，就不能假定对面还说主渠道那套话。
    # 没明说是哪家时按中转站的最小公约数算，只有整条跟随主渠道时才连格式一起跟。
    provider = referenced.provider or ("openai" if base_url else main.provider)
    if not base_url:
        base_url = main.base_url or channel.env("OPENAI_BASE_URL")
        api_key = channel.pick(
            "api_key", "API_KEY", referenced.api_key, main.api_key,
            channel.env("GEMINI_API_KEY"), channel.env("OPENAI_API_KEY"),
        )
    else:
        api_key = channel.pick(
            "api_key", "API_KEY", referenced.api_key,
            channel.env("GEMINI_API_KEY"), channel.env("OPENAI_API_KEY"),
        )
    if not api_key:
        # 与同目录其它工具同一句：排障时按这句话搜得到所有缺 key 的场景。
        return VisionChannel(error="No API key found in config/models.yaml, .env or config/secrets.env")

    wire = wire_mod.wire_for(provider)
    if not base_url:
        base_url = _WIRE_DEFAULT_URLS.get(wire, "")
        if not base_url:
            # 连主渠道都没地址，退到那个谁都收的兼容端点，渠道类型也跟着退。
            base_url, wire, provider = _VISION_FALLBACK_URL, _VISION_FALLBACK_WIRE, "gemini"
    base_url = wire_mod.normalize_base_url(wire, base_url)

    model = channel.pick("model", "MODEL", referenced.model, _VISION_DEFAULT_MODELS.get(wire, ""))
    if not model:
        return VisionChannel(error=(
            f"看图渠道选了 {provider}，但没有能替它猜的模型名。"
            "请在控制台的表情包页或 config/models.yaml 的 system_models.image 里填上 model。"
        ))
    return VisionChannel(
        base_url=base_url, api_key=api_key, model=model, provider=provider, wire=wire,
    )


def resolve_image_channel(
    slot: str,
    *,
    root: Path | None = None,
    model_override: str = "",
    hidden_env: Callable[[str], bool] | None = None,
) -> ImageChannel:
    """生图槽位的完整解析。工具和提示词注入共用这一份，判定才不会打架；hidden_env 同 _EnvView。

    model 不跟着主渠道借：那边配的是聊天模型，生图端点认不了。
    """
    spec = IMAGE_SLOTS.get((slot or "").strip().lower())
    if spec is None:
        return ImageChannel()
    channel = Channel(slot, root=root, hidden_env=hidden_env)
    # 槽位和主渠道都写在 models.yaml 里，读不了就别只凭环境变量拼一条渠道去发计费请求。
    if channel.models_problem:
        return ImageChannel(error=channel.models_problem)
    main = channel.main_for(*spec.providers)
    api_key = channel.pick(
        "api_key", "API_KEY", *(channel.env(name) for name in spec.key_envs), main.api_key,
    )
    base_url = channel.pick(
        "base_url", "BASE_URL", *(channel.env(name) for name in spec.url_envs), main.base_url,
    )
    base_url = _wire_module().normalize_base_url(spec.wire, base_url or spec.default_base_url)
    model = str(model_override or "").strip() or channel.pick("model", "MODEL", spec.default_model)
    return ImageChannel(api_key=api_key, base_url=base_url, model=model)


def image_default_channel(*, root: Path | None = None) -> str:
    """控制台选的默认生图工具名：models.yaml 的 image_gen.default_channel。没选或读不了是空串，名字由调用方核对。"""
    base = Path(root) if root is not None else Path.cwd()
    block = _models_document(base).data.get("image_gen")
    if not isinstance(block, dict):
        return ""
    return str(block.get("default_channel") or "").strip()

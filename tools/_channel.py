"""工具子进程共用的渠道解析。

工具是独立子进程、只吃标准库，导不了 clonoth_runtime，所以在这里放一份而不是各抄一遍。
密钥只能从 .env 文件读：safe_subprocess_env() 会把所有 *_API_KEY 从环境里剥掉。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

# 这几家收 OpenAI 格式的 /chat/completions；anthropic 与 gemini 原生走自己的端点和请求体。
OPENAI_COMPATIBLE = frozenset({"openai", "deepseek"})

_META_KEYS = frozenset({"version", "provider", "fallbacks", "node_fallbacks", "system_models"})


class MainChannel(NamedTuple):
    """data/config.yaml 里当前活跃的那个渠道块。字段可能为空。"""

    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


def _read_dotenv(root: Path) -> dict[str, str]:
    path = root / ".env"
    if not path.exists():
        return {}
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip().strip("'\"")
    return pairs


def _read_config(root: Path) -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    path = root / "data" / "config.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_provider_block(value: object) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("base_url", "api_key", "model"))


class Channel:
    """一个 system_models 槽位的配置视图。"""

    def __init__(self, slot: str, *, root: Path | None = None) -> None:
        # root 默认 cwd：工具是以工作区为 cwd 拉起的子进程。主进程里的调用方
        # （提示词注入）cwd 不一定在工作区，得把 root 显式传进来。
        base = Path(root) if root is not None else Path.cwd()
        self.slot = (slot or "").strip().lower()
        self._dotenv = _read_dotenv(base)
        self._config = _read_config(base)
        block = (self._config.get("system_models") or {}) if isinstance(self._config, dict) else {}
        block = block.get(self.slot) if isinstance(block, dict) else None
        self._block = block if isinstance(block, dict) else {}

    def env(self, key: str) -> str:
        return (os.environ.get(key, "") or self._dotenv.get(key, "")).strip()

    def first_env(self, names: str) -> str:
        """取第一个有值的变量。仓库的节点文件在用 $ENV{NEW|OLD} 这种回退写法。"""
        for name in str(names or "").split("|"):
            value = self.env(name.strip())
            if value:
                return value
        return ""

    def _deref(self, value: object) -> str:
        text = str(value or "").strip()
        for head in ("${", "$ENV{"):
            if text.startswith(head) and text.endswith("}") and len(text) > len(head) + 1:
                return self.first_env(text[len(head):-1].strip())
        return text

    def own(self, field: str, env_suffix: str) -> str:
        """只看这一槽自己配了什么：config.yaml 的槽位块 > 槽位专属环境变量。"""
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

    def main(self) -> MainChannel:
        """当前活跃渠道。块名同时是 provider 类型，决定请求格式。"""
        if not isinstance(self._config, dict):
            return MainChannel()
        name = str(self._config.get("provider") or "").strip() or "openai"
        block = self._config.get(name)
        if not _is_provider_block(block):
            name, block = "openai", self._config.get("openai")
        if not _is_provider_block(block):
            return MainChannel()
        return MainChannel(
            provider=name,
            base_url=self._deref(block.get("base_url")),
            api_key=self._deref(block.get("api_key")),
            model=self._deref(block.get("model")),
        )

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
    strip_v1_suffix: bool = False


IMAGE_SLOTS: dict[str, ImageSlotSpec] = {
    "image_gpt": ImageSlotSpec(
        providers=tuple(sorted(OPENAI_COMPATIBLE)),
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
        strip_v1_suffix=True,
    ),
}


class ImageChannel(NamedTuple):
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url)


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
    """加载同目录的 _vision_wire。

    这个文件有三种被加载的方式 —— 包内 `tools._channel`、工具子进程里的裸 `_channel`、
    以及 clonoth_runtime 按路径加载 —— 模块级 import 会在后两种下炸掉整个模块。
    """
    try:
        from . import _vision_wire  # type: ignore[no-redef]
        return _vision_wire
    except ImportError:
        pass
    import sys
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import _vision_wire  # type: ignore[no-redef]
    return _vision_wire


def resolve_vision_channel(*, root: Path | None = None) -> VisionChannel:
    """看图渠道的完整解析。读图工具和表情包打标共用这一份，判定才不会打架。

    先看 system_models.image；没配就跟随主渠道 —— 常见部署是同一个中转站换个模型名。
    模型名不跟随：主渠道多半正是那个看不了图的纯文本模型。
    """
    wire_mod = _wire_module()
    channel = Channel("image", root=root)
    main = channel.main()

    base_url = channel.own("base_url", "BASE_URL")
    # 地址和格式是绑死的一对：这一槽自己指了地址，就不能假定对面还说主渠道那套话。
    # 没明说是哪家时按中转站的最小公约数算，只有整条跟随主渠道时才连格式一起跟。
    provider = (
        channel.own("provider", "PROVIDER").strip().lower()
        or ("openai" if base_url else main.provider)
    )
    if not base_url:
        base_url = main.base_url or channel.env("OPENAI_BASE_URL")
        api_key = channel.pick(
            "api_key", "API_KEY", main.api_key,
            channel.env("GEMINI_API_KEY"), channel.env("OPENAI_API_KEY"),
        )
    else:
        api_key = channel.pick(
            "api_key", "API_KEY",
            channel.env("GEMINI_API_KEY"), channel.env("OPENAI_API_KEY"),
        )
    if not api_key:
        # 与同目录其它工具同一句：排障时按这句话搜得到所有缺 key 的场景。
        return VisionChannel(error="No API key found in config.yaml / env / .env file")

    wire = wire_mod.wire_for(provider)
    if not base_url:
        base_url = _WIRE_DEFAULT_URLS.get(wire, "")
        if not base_url:
            # 连主渠道都没地址，退到那个谁都收的兼容端点，渠道类型也跟着退。
            base_url, wire, provider = _VISION_FALLBACK_URL, _VISION_FALLBACK_WIRE, "gemini"
    base_url = wire_mod.normalize_base_url(wire, base_url)

    model = channel.pick("model", "MODEL", _VISION_DEFAULT_MODELS.get(wire, ""))
    if not model:
        return VisionChannel(error=(
            f"看图渠道选了 {provider}，但没有能替它猜的模型名。"
            "请在控制台的表情包页或 data/config.yaml 的 system_models.image 里填上 model。"
        ))
    return VisionChannel(
        base_url=base_url, api_key=api_key, model=model, provider=provider, wire=wire,
    )


def resolve_image_channel(
    slot: str, *, root: Path | None = None, model_override: str = "",
) -> ImageChannel:
    """生图槽位的完整解析。工具和提示词注入共用这一份，判定才不会打架。

    model 不跟着主渠道借：那边配的是聊天模型，生图端点认不了。
    """
    spec = IMAGE_SLOTS.get((slot or "").strip().lower())
    if spec is None:
        return ImageChannel()
    channel = Channel(slot, root=root)
    main = channel.main_for(*spec.providers)
    api_key = channel.pick(
        "api_key", "API_KEY", *(channel.env(name) for name in spec.key_envs), main.api_key,
    )
    base_url = channel.pick(
        "base_url", "BASE_URL", *(channel.env(name) for name in spec.url_envs), main.base_url,
    ).rstrip("/")
    if spec.strip_v1_suffix and base_url.endswith("/v1"):
        base_url = base_url[:-3]
    model = str(model_override or "").strip() or channel.pick("model", "MODEL", spec.default_model)
    return ImageChannel(
        api_key=api_key, base_url=base_url or spec.default_base_url, model=model,
    )

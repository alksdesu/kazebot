from __future__ import annotations

import contextlib
import copy
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from clonoth_runtime import (
    CONFIG_META_KEYS,
    IMAGE_TOOL_SLOTS,
    SYSTEM_MODEL_SLOTS,
    image_tool_available,
    is_channel_block,
    resolve_env_ref,
)
import config_layout
from engine.node import canonical_node_id, known_node_ids

from .types import (
    ActiveProviderSecret,
    AppConfigPublic,
    AppConfigSecret,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
)

_log = logging.getLogger(__name__)

# 文件不存在或是空的时候，按这份起步。
_EMPTY_CONFIG: dict[str, Any] = {"version": 1, "provider": "openai"}


class ConfigUnreadable(ValueError):
    """config/models.yaml 现在解析不了。改写被拒：拿默认值盖上去，等于把整个共享组的渠道一起清空。"""


def _resolve_env_value(value: str) -> str:
    # 与节点 yaml 共用一套解析：$ENV{NEW|OLD} 的回退写法在 models.yaml 里也得认，
    # 否则照抄节点的写法会静默解析成空模型名。
    return resolve_env_ref((value or "").strip())


def _tristate(value: Any) -> bool | None:
    """yaml 里的可选布尔。没配返回 None —— 和「配了个 false」不是一回事。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        return text in {"1", "true", "yes", "on"} if text else None
    return bool(value)


def _redact_api_key(api_key: str) -> tuple[bool, str]:
    k = (api_key or "").strip()
    if not k:
        return False, ""
    # Environment reference (recommended)
    if k.startswith("${") and k.endswith("}") and len(k) > 3:
        var = k[2:-1].strip()
        return True, f"<env:{var}>"
    if len(k) <= 4:
        return True, "****"
    return True, "****" + k[-4:]


class ConfigStore:
    """YAML-backed config store.

    - Canonical file: config/models.yaml
    - Contains provider selection + OpenAI settings.

    注意：api_key 属于敏感信息，config/ 整个不进版本库，新建时只给属主读写。
    """

    def __init__(self, *, path: Path, workspace_root: Path | None = None):
        self.path = path
        # 共享组里 config/ 是软链，从 path 往上数两级数到的是组目录，得由调用方给准。
        self._root = Path(workspace_root) if workspace_root is not None else path.parent.parent
        self._lock = threading.RLock()
        self._config: AppConfigSecret = AppConfigSecret()
        # 最近一次解析成功的原文，读接口都从这里取；文件写坏时它和 _config 一起停在上一份能用的配置上。
        self._raw: dict[str, Any] = dict(_EMPTY_CONFIG)
        # 已经按哪个文件版本读过，None 是文件不存在。
        self._stamp: tuple[int, int, int] | None = None
        self._error = ""

        self._ensure_file()
        self.reload()

    # ---------------------------------------------------------------- 读

    def _read_file(self) -> dict[str, Any]:
        """文件现状解析成 dict。不存在或是空的按空配置算；解析不了、顶层不是映射抛 ConfigUnreadable。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return dict(_EMPTY_CONFIG)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigUnreadable(f"读不了 {config_layout.MODELS_FILE}：{exc}") from exc
        if not text.strip():
            return dict(_EMPTY_CONFIG)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigUnreadable(f"{config_layout.MODELS_FILE} 解析失败：{exc}") from exc
        if data is None:
            return dict(_EMPTY_CONFIG)
        if not isinstance(data, dict):
            raise ConfigUnreadable(f"{config_layout.MODELS_FILE} 顶层必须是映射（key: value）")
        return data

    def _load_locked(self) -> None:
        """按文件现状重读，从不写回。解析不了或字段不合法时留着上一份能用的，文件原样不动，等人去修。"""
        stamp = config_layout.file_stamp(self.path)
        try:
            data = self._read_file()
            config = AppConfigSecret.model_validate(data)
        except (ConfigUnreadable, ValidationError) as exc:
            if stamp != self._stamp or not self._error:
                _log.warning("%s 用不了，继续用上一份能用的配置：%s", config_layout.MODELS_FILE, exc)
            self._stamp = stamp
            self._error = str(exc)
            return
        self._raw = data
        self._config = config
        self._stamp = stamp
        self._error = ""

    @contextlib.contextmanager
    def _current(self) -> Iterator[None]:
        """读之前对一下文件：共享组里别的实例、控制台的原文编辑器都会直接改它。"""
        with self._lock:
            if config_layout.file_stamp(self.path) != self._stamp:
                self._load_locked()
            yield

    def reload(self) -> AppConfigSecret:
        with self._lock:
            self._load_locked()
            return self._config.model_copy(deep=True)

    @property
    def load_error(self) -> str:
        """文件现在为什么用不了；空串表示用的就是文件里那份。"""
        with self._current():
            return self._error

    def get_secret(self) -> AppConfigSecret:
        with self._current():
            return self._config.model_copy(deep=True)

    # ---------------------------------------------------------------- 写

    def _ensure_file(self) -> None:
        """文件还不存在时建一份默认值，原文编辑器才有东西可开。已有的一个字节都不动。"""
        if self.path.exists():
            return
        try:
            config_layout.write_config_text(
                self.path,
                self._dump(AppConfigSecret().model_dump(mode="json")),
                base_hash="",
                mode=config_layout.private_mode_for(self._root, self.path),
            )
        except config_layout.ConfigConflict:
            # 共享组里别的实例刚好先建好了。
            pass

    @staticmethod
    def _dump(data: dict[str, Any]) -> str:
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    @contextlib.contextmanager
    def _editing(self) -> Iterator[dict[str, Any]]:
        """改一份模型配置：拿着文件锁读最新的原文交给调用方改，改完原地写回再重读。

        读和写之间一直拿着锁：共享组里几个实例的控制台可能同时在改这一份，各读各写会丢掉对方的改动。
        调用方中途抛错就什么都不写。文件解析不了时抛 ConfigUnreadable，不拿默认值盖上去。
        """
        with self._lock, config_layout.locked(self.path):
            self._load_locked()
            if self._error:
                raise ConfigUnreadable(f"{self._error}。先在原文编辑器里修好再改")
            data = copy.deepcopy(self._raw)
            yield data
            # 没改动就不写：写一次就把手写的注释和排版洗掉一次，组里别的实例也会跟着重读。
            if data == self._raw:
                return
            try:
                AppConfigSecret.model_validate(data)
            except ValidationError as exc:
                raise ValueError(f"改完的配置不合法，没有写入：{exc}") from exc
            config_layout.write_locked(
                self.path, self._dump(data), mode=config_layout.private_mode_for(self._root, self.path),
            )
            self._load_locked()

    def _openai_public(self, openai: OpenAIConfigSecret) -> OpenAIConfigPublic:
        # present 按展开后判断：${MISSING} 引用得到的是一把用不了的钥匙。
        present, _ = _redact_api_key(_resolve_env_value(openai.api_key))
        return OpenAIConfigPublic(
            base_url=_resolve_env_value(openai.base_url),
            model=_resolve_env_value(openai.model),
            api_key_present=present,
        )

    def get_public(self) -> AppConfigPublic:
        with self._current():
            return self._active_public(self._active_name())

    def _active_name(self) -> str:
        """真正在用的渠道名。

        指向一个不存在的块时回落 openai 并如实报回落后的名字 —— 报着 anthropic
        却给 openai 的地址，engine 会拿错的请求格式去打。
        """
        name = (self._config.provider or "").strip() or "openai"
        if name != "openai" and is_channel_block(self._load_raw().get(name)):
            return name
        return "openai"

    def _block_of(self, name: str) -> OpenAIConfigSecret:
        if name == "openai":
            return self._config.openai.model_copy(deep=True)
        block = self._load_raw().get(name) or {}
        return OpenAIConfigSecret(
            base_url=str(block.get("base_url") or "").strip(),
            api_key=str(block.get("api_key") or "").strip(),
            model=str(block.get("model") or "").strip(),
        )

    def _active_public(self, name: str) -> AppConfigPublic:
        return AppConfigPublic(
            version=self._config.version, provider=name,
            openai=self._openai_public(self._block_of(name)),
        )

    def get_openai_secret(self) -> ActiveProviderSecret:
        with self._current():
            name = self._active_name()
            cfg = self._block_of(name)
            return ActiveProviderSecret(
                # engine 拿它去 registry 取实现类，只能给线格式，不能给渠道名。
                provider=self._type_of(name),
                base_url=_resolve_env_value(cfg.base_url),
                api_key=_resolve_env_value(cfg.api_key),
                model=_resolve_env_value(cfg.model),
            )

    def get_openai_public(self) -> OpenAIConfigPublic:
        with self._current():
            return self._openai_public(self._block_of(self._active_name()))

    def update_openai(self, update: OpenAIConfigUpdateIn) -> AppConfigPublic:
        """改当前活跃渠道的三项。QQ 里的「/切换模型」走这条。"""
        fields = {
            "base_url": update.base_url,
            "api_key": update.api_key,
            "model": update.model,
        }
        with self._editing() as data:
            name = self._active_name()
            block = data.get(name)
            if not isinstance(block, dict):
                block = {}
                data[name] = block
            for field, value in fields.items():
                if value is not None:
                    block[field] = value.strip()
        with self._current():
            return self._active_public(name)

    # ================================================================
    #  Multi-provider CRUD (operates on raw YAML dict)
    # ================================================================

    def _load_raw(self) -> dict[str, Any]:
        """最近一次解析成功的原文，给调用方一份自己的副本。"""
        with self._current():
            return copy.deepcopy(self._raw)

    def _type_of(self, name: str, data: dict[str, Any] | None = None) -> str:
        """这个渠道说哪种线格式。

        块名从此只是个名字，谁都能叫；决定请求怎么发的是块内的 type。没写 type 就
        拿块名当 type —— 老配置里块名本来就是家族名，于是原样继续可用。
        """
        block = (self._load_raw() if data is None else data).get(name)
        if isinstance(block, dict):
            declared = str(block.get("type") or "").strip().lower()
            if declared:
                return declared
        return (name or "").strip().lower() or "openai"

    def _public_block(self, val: dict[str, Any], name: str = "") -> dict[str, Any]:
        # raw 一并给出去：编辑器回填展开值再保存，就把 ${VAR} 这层间接烧死在 models.yaml 里了。
        # api_key 没有 raw —— 那等于用明文换掉旁边的脱敏串。
        present, redacted = _redact_api_key(_resolve_env_value(val.get("api_key", "")))
        declared = str(val.get("type") or "").strip().lower()
        return {
            "base_url": _resolve_env_value(val.get("base_url", "")),
            "model": _resolve_env_value(val.get("model", "")),
            "base_url_raw": str(val.get("base_url", "") or "").strip(),
            "model_raw": str(val.get("model", "") or "").strip(),
            "api_key_present": present,
            "api_key_redacted": redacted,
            # 没配就是 None：带图路由要区分「显式说了不收图」和「没说，按这家默认算」。
            "supports_vision": _tristate(val.get("supports_vision")),
            "type": declared or (name or "").strip().lower(),
            # 没写 type 的老块，界面上不该显示成「显式选过 gemini」。
            "type_explicit": bool(declared),
            "label": str(val.get("label") or "").strip(),
        }

    @staticmethod
    def _public_options(provider_name: str, raw: Any) -> dict[str, Any]:
        """只公布这个 provider 声明过的参数。

        options 是自由 dict，手写进去的任何东西都会原样躺在里面。逐键按 OPTIONS
        过滤，界面就只看得见它认识的项；没被公布的键在保存时靠按条目合并保住。
        """
        if not isinstance(raw, dict) or not raw:
            return {}
        try:
            from providers import registry as provider_registry
            provider_cls = provider_registry.get(provider_name)
            declared = {spec.key for spec in getattr(provider_cls, "OPTIONS", ())}
        except Exception:
            return {}
        return {key: value for key, value in raw.items() if key in declared}

    def _public_fallback(self, entry: Any) -> dict[str, Any]:
        """按白名单挑字段，把备选条目脱敏成和 provider 块同构的形状。

        备选条目允许写自己的 api_key（见 fallback_provider._resolve_fallback_entry），
        原样返回就是把密钥交给前端。白名单而不是剔除 api_key：配置里任何没被引擎
        读到的键都没有暴露的理由。
        """
        if not isinstance(entry, dict):
            return {"provider": str(entry or "")}
        provider_name = str(entry.get("provider", "") or "").strip().lower()
        public = self._public_block(entry, provider_name)
        # 备选条目用 provider 指渠道，没有自己的 type/label；漏出去编辑器会原样回写。
        for derived in ("type", "type_explicit", "label"):
            public.pop(derived, None)
        public["provider"] = provider_name
        # 这两项引擎真在读：漏了它们，编辑器存一次就会把「支持图片」和请求参数抹掉。
        public["supports_vision"] = bool(entry.get("supports_vision", False))
        # 条目指的可能是个自定义渠道名，得先问出它是哪家才知道有哪些参数可公布。
        public["options"] = self._public_options(self._type_of(provider_name), entry.get("options"))
        return public

    @staticmethod
    def _check_channel_name(name: str) -> None:
        # 渠道块和这几段配置同在 config/models.yaml 顶层，重名就会把那一段当成渠道块改写或整段删掉。
        if name in CONFIG_META_KEYS:
            raise ValueError(f"'{name}' 是 config/models.yaml 里的保留段名，不能当渠道名")

    def get_providers_public(self) -> dict[str, Any]:
        """Return all providers with redacted api_keys, plus active_provider and fallbacks."""
        with self._current():
            data = self._load_raw()
            active = data.get("provider", "openai")
            fallbacks = data.get("fallbacks", []) or []

            providers: dict[str, dict[str, Any]] = {}
            for key, val in data.items():
                if key in CONFIG_META_KEYS:
                    continue
                if is_channel_block(val):
                    providers[key] = self._public_block(val, key)

            raw_chains = data.get("node_fallbacks")
            node_chains: dict[str, list[dict[str, Any]]] = {}
            if isinstance(raw_chains, dict):
                for node_id in raw_chains:
                    # 旧 id 下的链挂到节点现在的名下，控制台看到的就是实际生效的那条。
                    current = canonical_node_id(self._workspace_root, str(node_id))
                    owner = self._node_chain_key(raw_chains, current)
                    # 空列表要原样保留：它表示「这个节点禁用 fallback」，
                    # 和「没配过」是两个意思。
                    if owner is not None and current not in node_chains:
                        node_chains[current] = [self._public_fallback(fb) for fb in raw_chains[owner]]

            return {
                "active_provider": active,
                "providers": providers,
                "fallbacks": [self._public_fallback(fb) for fb in fallbacks],
                "node_fallbacks": node_chains,
                # 新渠道起名时要避开的名字，控制台拿它提前拦下，不用等保存被拒。
                "reserved_names": sorted(CONFIG_META_KEYS),
            }

    def wire_of(self, name: str) -> str:
        """这个渠道按哪种线格式发请求。查 providers 注册表前都得先过这一层。"""
        with self._current():
            return self._type_of((name or "").strip() or self._active_name())

    def channel_names(self) -> list[str]:
        """配置里有块的那些渠道名。大小写敏感 —— 节点和备选链是按原名查块的。"""
        with self._current():
            data = self._load_raw()
        return [
            key for key, val in data.items()
            if key not in CONFIG_META_KEYS and is_channel_block(val)
        ]

    def resolve_provider_credentials(self, name: str) -> tuple[str, str]:
        """按渠道名取展开后的 (base_url, api_key)。只给服务端自己发请求用。

        绝不能进任何响应体：调用方要么拿它去请求上游，要么什么都不做。
        """
        with self._current():
            block = self._load_raw().get(name)
        if not isinstance(block, dict):
            return "", ""
        return (
            _resolve_env_value(str(block.get("base_url") or "")),
            _resolve_env_value(str(block.get("api_key") or "")),
        )

    def resolve_slot_credentials(self, slot: str) -> tuple[str, str]:
        """按系统槽位取展开后的 (base_url, api_key)。和上面那个一样，只出不进。"""
        known = {spec.key for spec in SYSTEM_MODEL_SLOTS}
        if slot not in known:
            return "", ""
        with self._current():
            blocks = self._load_raw().get("system_models")
            block = blocks.get(slot) if isinstance(blocks, dict) else None
        if not isinstance(block, dict):
            return "", ""
        return (
            _resolve_env_value(str(block.get("base_url") or "")),
            _resolve_env_value(str(block.get("api_key") or "")),
        )

    def supports_vision(self, name: str = "") -> bool:
        """这个渠道收不收图片。不给名字就问当前活跃的那个。

        块里显式写了就听它的；没写按这家 provider 的默认算 —— 带图消息靠它决定
        直接走主模型还是绕去视觉节点。
        """
        with self._current():
            data = self._load_raw()
            block_name = (name or "").strip() or self._active_name()
            block = data.get(block_name)
            explicit = _tristate(block.get("supports_vision") if isinstance(block, dict) else None)
            wire = self._type_of(block_name, data)
        if explicit is not None:
            return explicit
        try:
            from providers import registry as provider_registry
            # 默认值是按家族定的，自定义渠道名在这张表里查不到。
            return provider_registry.default_vision_support().get(wire, True)
        except Exception:
            return True

    def upsert_provider(self, name: str, *, base_url: str | None = None,
                        api_key: str | None = None, model: str | None = None,
                        supports_vision: str | None = None,
                        wire: str | None = None, label: str | None = None) -> dict[str, Any]:
        """Create or update a provider entry."""
        self._check_channel_name(name)
        with self._editing() as data:
            if name not in data or not isinstance(data.get(name), dict):
                data[name] = {}
            if wire is not None:
                # 空字符串是「回去按块名猜」，和没传这个字段不是一回事。
                cleaned = wire.strip().lower()
                if cleaned:
                    data[name]["type"] = cleaned
                else:
                    data[name].pop("type", None)
            if label is not None:
                cleaned = label.strip()
                if cleaned:
                    data[name]["label"] = cleaned
                else:
                    data[name].pop("label", None)
            if base_url is not None:
                data[name]["base_url"] = base_url.strip()
            if api_key is not None:
                data[name]["api_key"] = api_key.strip()
            if model is not None:
                data[name]["model"] = model.strip()
            if supports_vision is not None:
                # auto 是删掉这个键，让它回去跟随这家的默认，和「写了个 false」不是一回事。
                if supports_vision == "auto":
                    data[name].pop("supports_vision", None)
                else:
                    data[name]["supports_vision"] = supports_vision == "yes"
        return self.get_providers_public()

    def get_system_models_public(self) -> dict[str, Any]:
        """系统槽位的对外形状。密钥同样只报有无。"""
        with self._current():
            configured = self._load_raw().get("system_models")
            configured = configured if isinstance(configured, dict) else {}
            slots = []
            for spec in SYSTEM_MODEL_SLOTS:
                block = configured.get(spec.key)
                block = block if isinstance(block, dict) else {}
                present, redacted = _redact_api_key(_resolve_env_value(str(block.get("api_key") or "")))
                slots.append({
                    "key": spec.key,
                    "label": spec.label,
                    "desc": spec.desc,
                    "supports_provider": spec.supports_provider,
                    "env_prefix": spec.env_prefix,
                    "model": _resolve_env_value(str(block.get("model") or "")),
                    "base_url": _resolve_env_value(str(block.get("base_url") or "")),
                    "model_raw": str(block.get("model") or "").strip(),
                    "base_url_raw": str(block.get("base_url") or "").strip(),
                    "provider": str(block.get("provider") or "").strip().lower(),
                    "api_key_present": present,
                    "api_key_redacted": redacted,
                })
            return {
                "slots": slots,
                "image_tools": self._image_tools_public(),
                "image_default_channel": self._read_image_default_channel(),
            }

    @property
    def _workspace_root(self) -> Path:
        return self._root

    def _image_tools_public(self) -> list[dict[str, Any]]:
        """生图工具的真实可用性。含主渠道回退，跟工具子进程同源。"""
        root = self._workspace_root
        return [
            {"name": name, "slot": slot, "available": image_tool_available(root, name)}
            for name, slot in IMAGE_TOOL_SLOTS.items()
        ]

    def _read_image_default_channel(self) -> str:
        block = self._load_raw().get("image_gen")
        if not isinstance(block, dict):
            return ""
        name = str(block.get("default_channel") or "").strip()
        return name if name in IMAGE_TOOL_SLOTS else ""

    def set_image_default_channel(self, name: str) -> dict[str, Any]:
        """配了多个生图渠道时用哪个。空串 = 交给模型按用途判断。"""
        text = str(name or "").strip()
        if text and text not in IMAGE_TOOL_SLOTS:
            raise ValueError(f"Unknown image tool '{text}'")
        with self._editing() as data:
            block = data.get("image_gen")
            if not isinstance(block, dict):
                block = {}
            if text:
                block["default_channel"] = text
                data["image_gen"] = block
            else:
                block.pop("default_channel", None)
                # 空块留在 yaml 里只会让人以为配过什么。
                if block:
                    data["image_gen"] = block
                else:
                    data.pop("image_gen", None)
        return self.get_system_models_public()

    def update_system_model(
        self, slot: str, *, base_url: str | None = None, api_key: str | None = None,
        model: str | None = None, provider: str | None = None,
    ) -> dict[str, Any]:
        """改一个系统槽位。空串表示删掉这一项（回到跟随主渠道）。"""
        known = {spec.key for spec in SYSTEM_MODEL_SLOTS}
        if slot not in known:
            raise ValueError(f"Unknown system model slot '{slot}'")
        with self._editing() as data:
            blocks = data.get("system_models")
            if not isinstance(blocks, dict):
                blocks = {}
                data["system_models"] = blocks
            block = blocks.get(slot)
            if not isinstance(block, dict):
                block = {}
                blocks[slot] = block
            for field, value in (
                ("base_url", base_url), ("api_key", api_key),
                ("model", model), ("provider", provider),
            ):
                if value is None:
                    continue
                text = value.strip()
                if text:
                    block[field] = text
                else:
                    # 留一个空串在 yaml 里会被当成「配了个空值」，不如整键删掉。
                    block.pop(field, None)
            if not block:
                blocks.pop(slot, None)
            if not blocks:
                data.pop("system_models", None)
        return self.get_system_models_public()

    def set_active_provider(self, name: str) -> dict[str, Any]:
        """Switch the active provider."""
        self._check_channel_name(name)
        with self._editing() as data:
            if name not in data or not is_channel_block(data.get(name)):
                raise ValueError(f"Provider '{name}' not found in config")
            data["provider"] = name
        return self.get_providers_public()

    # 读接口派生出来给编辑器用的字段。原样回写会在 yaml 里留下引擎不认识的键。
    # 备选条目认 provider 不认 type：那两个键是渠道块的东西，跟着读接口漏过来的。
    _DERIVED_KEYS = frozenset({
        "model_raw", "base_url_raw", "api_key_present", "api_key_redacted",
        "type", "type_explicit", "label",
    })

    # 编辑器用来指认「这一条是原来第几条」的传输字段，不写进 yaml。
    _ORIGIN_KEY = "_origin"

    def _merge_chain(self, incoming: list[Any], previous: list[Any]) -> list[Any]:
        """按条目合并，而不是整条链覆盖。

        编辑器只提交它认识的字段：api_key 不回传（回传的只是脱敏串），手写进
        yaml 的未知键它也看不见。整条替换会把这些一起抹掉，所以这里以原条目为
        底，只盖上真正提交的键。显式给 null 才表示删掉某个键。
        """
        merged: list[Any] = []
        for entry in incoming:
            if not isinstance(entry, dict):
                merged.append(entry)
                continue
            origin = entry.get(self._ORIGIN_KEY)
            base: dict[str, Any] = {}
            if isinstance(origin, int) and 0 <= origin < len(previous):
                if isinstance(previous[origin], dict):
                    base = dict(previous[origin])
            for key, value in entry.items():
                if key in self._DERIVED_KEYS or key == self._ORIGIN_KEY:
                    continue
                if value is None:
                    base.pop(key, None)
                else:
                    base[key] = value
            # options 同样按键合并：界面只公布了声明过的项，直接整块替换会
            # 丢掉手写的其余键。
            old_options = previous[origin].get("options") if (
                isinstance(origin, int) and 0 <= origin < len(previous)
                and isinstance(previous[origin], dict)
            ) else None
            new_options = entry.get("options")
            if isinstance(old_options, dict) and isinstance(new_options, dict):
                combined = dict(old_options)
                for key, value in new_options.items():
                    if value is None:
                        combined.pop(key, None)
                    else:
                        combined[key] = value
                base["options"] = combined
            if isinstance(base.get("options"), dict) and not base["options"]:
                base.pop("options")
            merged.append(base)
        return merged

    def update_fallbacks(self, fallbacks: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the fallback chain."""
        with self._editing() as data:
            data["fallbacks"] = self._merge_chain(fallbacks, data.get("fallbacks") or [])
        return self.get_providers_public()

    def update_node_fallbacks(self, node_id: str, fallbacks: list[dict[str, Any]] | None) -> dict[str, Any]:
        """按节点的专属备选链。传 None 表示删掉这条，让该节点回退到全局链。

        空列表和「没配」是两回事：空列表表示这个节点禁用一切 fallback
        （fallback_provider._select_fallbacks_for_node 就是这么读的）。
        """
        node = (node_id or "").strip()
        if not node:
            raise ValueError("node id is empty")
        with self._editing() as data:
            chains = data.get("node_fallbacks")
            if not isinstance(chains, dict):
                chains = {}
            current = canonical_node_id(self._workspace_root, node)
            owner = self._node_chain_key(chains, current)
            previous = chains.get(owner) if owner is not None else None
            # 旧 id 下的链一并收掉：只删新键的话，旧的那条会接着生效。
            for key in known_node_ids(self._workspace_root, current):
                chains.pop(key, None)
            if fallbacks is not None:
                chains[current] = self._merge_chain(
                    fallbacks, previous if isinstance(previous, list) else [],
                )
            if chains:
                data["node_fallbacks"] = chains
            else:
                data.pop("node_fallbacks", None)
        return self.get_providers_public()

    def _node_chain_key(self, chains: dict[str, Any], node_id: str) -> str | None:
        """节点实际生效的链记在哪个键下：先认现在的 id，再按声明顺序认旧 id，和引擎查的顺序一样。"""
        for key in known_node_ids(self._workspace_root, node_id):
            if isinstance(chains.get(key), list):
                return key
        return None

    def delete_provider(self, name: str) -> dict[str, Any]:
        """Delete a provider entry."""
        self._check_channel_name(name)
        with self._editing() as data:
            if name in data:
                if data.get("provider") == name:
                    raise ValueError(f"Cannot delete active provider '{name}'; switch to another first")
                del data[name]
                fb = data.get("fallbacks", []) or []
                data["fallbacks"] = [f for f in fb if f.get("provider") != name]
                # 节点链也得跟着清：漏在这里的条目会拿着已经不存在的渠道名去查配置，
                # 继承到一份空的 url+key，然后在真的降级时才炸。
                chains = data.get("node_fallbacks")
                if isinstance(chains, dict):
                    for node_id, chain in list(chains.items()):
                        if isinstance(chain, list):
                            chains[node_id] = [f for f in chain if f.get("provider") != name]
        return self.get_providers_public()

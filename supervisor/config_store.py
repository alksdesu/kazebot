from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml
from clonoth_runtime import (
    IMAGE_TOOL_SLOTS,
    SYSTEM_MODEL_SLOTS,
    image_tool_available,
    resolve_env_ref,
)

from .types import (
    ActiveProviderSecret,
    AppConfigPublic,
    AppConfigSecret,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
)


def _resolve_env_value(value: str) -> str:
    # 与节点 yaml 共用一套解析：$ENV{NEW|OLD} 的回退写法在 config.yaml 里也得认，
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

    - Canonical file: data/config.yaml
    - Contains provider selection + OpenAI settings.

    注意：api_key 属于敏感信息，本仓库会将 data/config.yaml 加入 .gitignore。
    """

    def __init__(self, *, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._config: AppConfigSecret = AppConfigSecret()
        self._extra_keys: dict[str, Any] = {}  # preserve unknown top-level keys

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    def _load_yaml_dict(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None

        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return None

        data = yaml.safe_load(text)
        if data is None:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_yaml(self, cfg: AppConfigSecret) -> None:
        # [2026-05-24] Preserve unknown top-level keys (e.g. fallbacks) that
        # plugins may have added to config.yaml but are not part of the
        # AppConfigSecret Pydantic model. Without this, reload() would strip
        # any key not declared in the model.
        data = cfg.model_dump(mode="json")
        if self._extra_keys:
            data.update(self._extra_keys)
        text = yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
        )
        self.path.write_text(text, encoding="utf-8")

    def reload(self) -> AppConfigSecret:
        with self._lock:
            data = self._load_yaml_dict()
            if data is None:
                self._config = AppConfigSecret()
                self._save_yaml(self._config)
                return self._config.model_copy(deep=True)

            # Capture top-level keys not in the Pydantic model
            _known_keys = set(AppConfigSecret.model_fields.keys())
            self._extra_keys = {k: v for k, v in data.items() if k not in _known_keys}
            try:
                self._config = AppConfigSecret.model_validate(data)
            except Exception:
                # 备份坏配置，写回默认值
                try:
                    bak = self.path.with_suffix(self.path.suffix + ".bak")
                    if self.path.exists() and not bak.exists():
                        self.path.replace(bak)
                except Exception:
                    pass

                self._config = AppConfigSecret()
                self._save_yaml(self._config)
                return self._config.model_copy(deep=True)

            # 写回一次，确保字段齐全且格式统一
            self._save_yaml(self._config)
            return self._config.model_copy(deep=True)

    def get_secret(self) -> AppConfigSecret:
        with self._lock:
            return self._config.model_copy(deep=True)

    def _openai_public(self, openai: OpenAIConfigSecret) -> OpenAIConfigPublic:
        # present 按展开后判断：${MISSING} 引用得到的是一把用不了的钥匙。
        present, _ = _redact_api_key(_resolve_env_value(openai.api_key))
        return OpenAIConfigPublic(
            base_url=_resolve_env_value(openai.base_url),
            model=_resolve_env_value(openai.model),
            api_key_present=present,
        )

    def get_public(self) -> AppConfigPublic:
        with self._lock:
            return self._active_public(self._active_name())

    def _active_name(self) -> str:
        """真正在用的渠道名。

        指向一个不存在的块时回落 openai 并如实报回落后的名字 —— 报着 anthropic
        却给 openai 的地址，engine 会拿错的请求格式去打。
        """
        name = (self._config.provider or "").strip() or "openai"
        if name != "openai" and self._is_provider_block(self._load_raw().get(name)):
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
        with self._lock:
            name = self._active_name()
            cfg = self._block_of(name)
            return ActiveProviderSecret(
                provider=name,
                base_url=_resolve_env_value(cfg.base_url),
                api_key=_resolve_env_value(cfg.api_key),
                model=_resolve_env_value(cfg.model),
            )

    def get_openai_public(self) -> OpenAIConfigPublic:
        with self._lock:
            return self._openai_public(self._block_of(self._active_name()))

    def update_openai(self, update: OpenAIConfigUpdateIn) -> AppConfigPublic:
        """改当前活跃渠道的三项。QQ 里的「/切换模型」走这条。"""
        with self._lock:
            name = self._active_name()
            fields = {
                "base_url": update.base_url,
                "api_key": update.api_key,
                "model": update.model,
            }
            if name == "openai":
                cfg = self._config
                for field, value in fields.items():
                    if value is not None:
                        setattr(cfg.openai, field, value.strip())
                self._save_yaml(cfg)
                self._config = cfg
            else:
                data = self._load_raw()
                for field, value in fields.items():
                    if value is not None:
                        data[name][field] = value.strip()
                self._save_raw(data)
                self.reload()

            return self._active_public(name)

    # ================================================================
    #  Multi-provider CRUD (operates on raw YAML dict)
    # ================================================================

    # [2026-07-16] node_fallbacks / system_models 是可选配置块，不是 provider
    # 块，列为 meta key 避免被当成 provider 展示/删除。
    _META_KEYS = frozenset({
        "version", "provider", "fallbacks", "node_fallbacks", "system_models", "image_gen",
    })

    def _load_raw(self) -> dict[str, Any]:
        """Load raw YAML dict from disk."""
        return self._load_yaml_dict() or {"version": 1, "provider": "openai"}

    def _save_raw(self, data: dict[str, Any]) -> None:
        """Write raw dict to YAML file."""
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        self.path.write_text(text, encoding="utf-8")

    def _is_provider_block(self, val: Any) -> bool:
        return isinstance(val, dict) and any(k in val for k in ("base_url", "api_key", "model"))

    def _public_block(self, val: dict[str, Any]) -> dict[str, Any]:
        # raw 一并给出去：编辑器回填展开值再保存，就把 ${VAR} 这层间接烧死在 config.yaml 里了。
        # api_key 没有 raw —— 那等于用明文换掉旁边的脱敏串。
        present, redacted = _redact_api_key(_resolve_env_value(val.get("api_key", "")))
        return {
            "base_url": _resolve_env_value(val.get("base_url", "")),
            "model": _resolve_env_value(val.get("model", "")),
            "base_url_raw": str(val.get("base_url", "") or "").strip(),
            "model_raw": str(val.get("model", "") or "").strip(),
            "api_key_present": present,
            "api_key_redacted": redacted,
            # 没配就是 None：带图路由要区分「显式说了不收图」和「没说，按这家默认算」。
            "supports_vision": _tristate(val.get("supports_vision")),
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
        public = self._public_block(entry)
        public["provider"] = provider_name
        # 这两项引擎真在读：漏了它们，编辑器存一次就会把「支持图片」和请求参数抹掉。
        public["supports_vision"] = bool(entry.get("supports_vision", False))
        public["options"] = self._public_options(provider_name, entry.get("options"))
        return public

    def get_providers_public(self) -> dict[str, Any]:
        """Return all providers with redacted api_keys, plus active_provider and fallbacks."""
        with self._lock:
            data = self._load_raw()
            active = data.get("provider", "openai")
            fallbacks = data.get("fallbacks", []) or []

            providers: dict[str, dict[str, Any]] = {}
            for key, val in data.items():
                if key in self._META_KEYS:
                    continue
                if self._is_provider_block(val):
                    providers[key] = self._public_block(val)

            raw_chains = data.get("node_fallbacks")
            node_chains: dict[str, list[dict[str, Any]]] = {}
            if isinstance(raw_chains, dict):
                for node_id, chain in raw_chains.items():
                    # 空列表要原样保留：它表示「这个节点禁用 fallback」，
                    # 和「没配过」是两个意思。
                    if isinstance(chain, list):
                        node_chains[str(node_id)] = [self._public_fallback(fb) for fb in chain]

            return {
                "active_provider": active,
                "providers": providers,
                "fallbacks": [self._public_fallback(fb) for fb in fallbacks],
                "node_fallbacks": node_chains,
            }

    def resolve_provider_credentials(self, name: str) -> tuple[str, str]:
        """按渠道名取展开后的 (base_url, api_key)。只给服务端自己发请求用。

        绝不能进任何响应体：调用方要么拿它去请求上游，要么什么都不做。
        """
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            block_name = (name or "").strip() or self._active_name()
            block = self._load_raw().get(block_name)
            explicit = _tristate(block.get("supports_vision") if isinstance(block, dict) else None)
        if explicit is not None:
            return explicit
        try:
            from providers import registry as provider_registry
            return provider_registry.default_vision_support().get(block_name, True)
        except Exception:
            return True

    def upsert_provider(self, name: str, *, base_url: str | None = None,
                        api_key: str | None = None, model: str | None = None,
                        supports_vision: str | None = None) -> dict[str, Any]:
        """Create or update a provider entry."""
        with self._lock:
            data = self._load_raw()
            if name not in data or not isinstance(data.get(name), dict):
                data[name] = {}
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
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def get_system_models_public(self) -> dict[str, Any]:
        """系统槽位的对外形状。密钥同样只报有无。"""
        with self._lock:
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
        """config.yaml 固定在 <workspace>/data/ 下。"""
        return self.path.parent.parent

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
        with self._lock:
            data = self._load_raw()
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
            self._save_raw(data)
            self.reload()
            return self.get_system_models_public()

    def update_system_model(
        self, slot: str, *, base_url: str | None = None, api_key: str | None = None,
        model: str | None = None, provider: str | None = None,
    ) -> dict[str, Any]:
        """改一个系统槽位。空串表示删掉这一项（回到跟随主渠道）。"""
        known = {spec.key for spec in SYSTEM_MODEL_SLOTS}
        if slot not in known:
            raise ValueError(f"Unknown system model slot '{slot}'")
        with self._lock:
            data = self._load_raw()
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
            self._save_raw(data)
            self.reload()
            return self.get_system_models_public()

    def set_active_provider(self, name: str) -> dict[str, Any]:
        """Switch the active provider."""
        with self._lock:
            data = self._load_raw()
            if name not in data or not self._is_provider_block(data.get(name)):
                raise ValueError(f"Provider '{name}' not found in config")
            data["provider"] = name
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    # 读接口派生出来给编辑器用的字段。原样回写会在 yaml 里留下引擎不认识的键。
    _DERIVED_KEYS = frozenset({"model_raw", "base_url_raw", "api_key_present", "api_key_redacted"})

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
        with self._lock:
            data = self._load_raw()
            data["fallbacks"] = self._merge_chain(fallbacks, data.get("fallbacks") or [])
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def update_node_fallbacks(self, node_id: str, fallbacks: list[dict[str, Any]] | None) -> dict[str, Any]:
        """按节点的专属备选链。传 None 表示删掉这条，让该节点回退到全局链。

        空列表和「没配」是两回事：空列表表示这个节点禁用一切 fallback
        （fallback_provider._select_fallbacks_for_node 就是这么读的）。
        """
        node = (node_id or "").strip()
        if not node:
            raise ValueError("node id is empty")
        with self._lock:
            data = self._load_raw()
            chains = data.get("node_fallbacks")
            if not isinstance(chains, dict):
                chains = {}
            if fallbacks is None:
                chains.pop(node, None)
            else:
                previous = chains.get(node)
                chains[node] = self._merge_chain(
                    fallbacks, previous if isinstance(previous, list) else [],
                )
            if chains:
                data["node_fallbacks"] = chains
            else:
                data.pop("node_fallbacks", None)
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def delete_provider(self, name: str) -> dict[str, Any]:
        """Delete a provider entry."""
        with self._lock:
            data = self._load_raw()
            if name not in data:
                return self.get_providers_public()
            if data.get("provider") == name:
                raise ValueError(f"Cannot delete active provider '{name}'; switch to another first")
            del data[name]
            fb = data.get("fallbacks", []) or []
            data["fallbacks"] = [f for f in fb if f.get("provider") != name]
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

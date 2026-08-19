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


def _read_dotenv() -> dict[str, str]:
    path = Path.cwd() / ".env"
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


def _read_config() -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    path = Path.cwd() / "data" / "config.yaml"
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

    def __init__(self, slot: str) -> None:
        self.slot = (slot or "").strip().lower()
        self._dotenv = _read_dotenv()
        self._config = _read_config()
        block = (self._config.get("system_models") or {}) if isinstance(self._config, dict) else {}
        block = block.get(self.slot) if isinstance(block, dict) else None
        self._block = block if isinstance(block, dict) else {}

    def env(self, key: str) -> str:
        return (os.environ.get(key, "") or self._dotenv.get(key, "")).strip()

    def _deref(self, value: object) -> str:
        text = str(value or "").strip()
        for head in ("${", "$ENV{"):
            if text.startswith(head) and text.endswith("}") and len(text) > len(head) + 1:
                return self.env(text[len(head):-1].strip())
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

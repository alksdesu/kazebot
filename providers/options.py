"""provider 可调参数的声明与读取。

控制台按这份清单渲染表单，provider 按它翻译成各家的请求字段。
配错的值一律回落到默认值，不让一个手滑的 yaml 打死整条推理链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BOOL = "bool"
INT = "int"
FLOAT = "float"
ENUM = "enum"
TEXT = "text"

_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True)
class OptionSpec:
    """一个可调参数。key 是 provider_options 里的键名，同时是控制台控件的 id。"""

    key: str
    label: str
    kind: str
    default: Any
    desc: str = ""
    # (值, 显示名)。kind=ENUM 时必填，控制台按顺序摆按钮。
    choices: tuple[tuple[str, str], ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    # 另一项取到指定值时这项才有意义，控制台据此收起控件。
    depends_on: tuple[str, Any] | None = None

    def values(self) -> tuple[str, ...]:
        return tuple(value for value, _ in self.choices)

    def coerce(self, value: Any) -> Any:
        """把配置里读到的值收成合法值。不合法一律回落 default，绝不抛。"""
        if value is None:
            return self.default
        if self.kind == BOOL:
            return self._coerce_bool(value)
        if self.kind in (INT, FLOAT):
            return self._coerce_number(value)
        if self.kind == ENUM:
            # YAML 1.1 把裸 off/on/no/yes 读成布尔，于是 `safety_threshold: off`
            # 到这里是 False，str() 得到 "false" 匹配不上任何档位，静默回落成默认 ——
            # 界面上写着关掉了过滤，实际全开着。选项里有同名档就按用户的字面意思还原。
            if isinstance(value, bool):
                spelled = "on" if value else "off"
                if spelled in self.values():
                    return spelled
            text = str(value).strip().lower()
            return text if text in self.values() else self.default
        # 只认标量。给文本项写个 dict 时 str() 会得到 "{'a': 1}" 并原样发出去，
        # 那比回落默认值更难查。
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return self.default
        text = str(value).strip()
        return text or self.default

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        return bool(self.default)

    def _coerce_number(self, value: Any) -> int | float | None:
        # bool 是 int 的子类，"thinking_budget: true" 不该变成 1。
        if isinstance(value, bool):
            return self.default
        try:
            number: int | float = float(value)
        except (TypeError, ValueError):
            return self.default
        if self.minimum is not None:
            number = max(number, self.minimum)
        if self.maximum is not None:
            number = min(number, self.maximum)
        return int(number) if self.kind == INT else number

    def publish(self) -> dict[str, Any]:
        """给 /v1/config/provider-options 用的可序列化形态。"""
        item: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "desc": self.desc,
        }
        if self.choices:
            item["choices"] = [{"value": value, "label": label} for value, label in self.choices]
        if self.minimum is not None:
            item["minimum"] = self.minimum
        if self.maximum is not None:
            item["maximum"] = self.maximum
        if self.depends_on is not None:
            item["depends_on"] = {"key": self.depends_on[0], "value": self.depends_on[1]}
        return item


class OptionSet:
    """一个 provider 实例的生效参数。"""

    def __init__(self, specs: tuple[OptionSpec, ...], raw: dict[str, Any] | None) -> None:
        self._specs = {spec.key: spec for spec in specs}
        self._raw = dict(raw) if isinstance(raw, dict) else {}

    def get(self, key: str) -> Any:
        spec = self._specs.get(key)
        if spec is None:
            return self._raw.get(key)
        return spec.coerce(self._raw.get(key))

    def is_set(self, key: str) -> bool:
        """用户显式配过吗。用来区分「没配」和「配成了和默认值一样」。

        采样参数就靠它：最新 Claude 只要请求体里出现 temperature 这个键就 400，
        所以没配的时候必须整个键都不发，而不是发一个默认值。
        """
        return self._raw.get(key) is not None

    def override(self, key: str, value: Any) -> bool:
        """记住一次修正，返回是否真的改了。

        降级重试用。写回这一层而不是改请求体，是为了让修正对之后每一次请求都生效——
        否则同一个参数互斥每轮都要先撞一次 400 再重试，白付一个来回。
        """
        if self._raw.get(key) == value:
            return False
        self._raw[key] = value
        return True

    def unknown_keys(self) -> tuple[str, ...]:
        """配了但这个 provider 不认识的键。启动时告警，避免改了半天没生效还找不到原因。"""
        return tuple(sorted(key for key in self._raw if key not in self._specs))

    def effective(self) -> dict[str, Any]:
        return {key: self.get(key) for key in self._specs}


def catalog(specs: tuple[OptionSpec, ...]) -> list[dict[str, Any]]:
    return [spec.publish() for spec in specs]

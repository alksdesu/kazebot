"""在没有 NoneBot 的环境里加载 QQ 插件本体的测试夹具。

stub 只覆盖插件 import 期实际触碰的接口。共享一份，避免多个测试文件各存一套
stub 后行为漂移。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

_ADAPTER_ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot"


def install_nonebot_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    class Matcher:
        def handle(self, *args: Any, **kwargs: Any):
            return lambda func: func

        def append_handler(self, *args: Any, **kwargs: Any):
            return lambda func: func

    class Driver:
        def on_startup(self, func=None):
            return (lambda value: value) if func is None else func

        def on_shutdown(self, func=None):
            return (lambda value: value) if func is None else func

        def on_bot_connect(self, func=None):
            return (lambda value: value) if func is None else func

    nonebot = types.ModuleType("nonebot")
    nonebot.get_bot = lambda *a, **k: None
    nonebot.get_driver = lambda: Driver()
    nonebot.on_message = lambda *a, **k: Matcher()
    nonebot.on_notice = lambda *a, **k: Matcher()

    class Segment(dict):
        @classmethod
        def image(cls, file: str):
            return cls(type="image", data={"file": file})

        @classmethod
        def text(cls, text: str):
            return cls(type="text", data={"text": text})

        @classmethod
        def reply(cls, message_id: Any):
            return cls(type="reply", data={"id": message_id})

        @classmethod
        def at(cls, user_id: Any):
            return cls(type="at", data={"qq": user_id})

        @property
        def type(self) -> str:
            return str(self.get("type") or "")

        @property
        def data(self) -> dict:
            return self.get("data") or {}

        def __add__(self, other: Any):
            return Message([self]) + other

    class Message(list):
        def __init__(self, value: Any = None):
            if value is None:
                value = []
            elif isinstance(value, (str, dict)):
                value = [value]
            super().__init__(value)

        def __add__(self, other: Any):
            return Message(list(self) + (list(other) if isinstance(other, list) else [other]))

    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    for name, value in {
        "Bot": object, "Event": object, "GroupMessageEvent": object,
        "GroupUploadNoticeEvent": object, "PrivateMessageEvent": object,
        "Message": Message, "MessageSegment": Segment,
    }.items():
        setattr(v11, name, value)
    exception = types.ModuleType("nonebot.adapters.onebot.v11.exception")
    exception.ActionFailed = type("ActionFailed", (RuntimeError,), {})
    rule = types.ModuleType("nonebot.rule")
    rule.Rule = lambda *a, **k: object()

    modules = {
        "nonebot": nonebot,
        "nonebot.adapters": types.ModuleType("nonebot.adapters"),
        "nonebot.adapters.onebot": types.ModuleType("nonebot.adapters.onebot"),
        "nonebot.adapters.onebot.v11": v11,
        "nonebot.adapters.onebot.v11.exception": exception,
        "nonebot.rule": rule,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def load_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """加载一份独立的插件实例，工作区指向 tmp_path。"""
    install_nonebot_stubs(monkeypatch)
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    name = f"_onebot_runtime_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(
        name, _ADAPTER_ROOT / "__init__.py", submodule_search_locations=[str(_ADAPTER_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def load_live_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str):
    """只加载 config + live_config 两个模块，不拉起插件本体，也不需要 nonebot stub。"""
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pkg_name = f"_onebot_live_{tmp_path.name}"
    package = types.ModuleType(pkg_name)
    package.__path__ = [str(_ADAPTER_ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, pkg_name, package)
    for name in ("config", "capability", "live_config"):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{name}", _ADAPTER_ROOT / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, f"{pkg_name}.{name}", module)
        spec.loader.exec_module(module)
    return sys.modules[f"{pkg_name}.live_config"]


def live_config_module(runtime: Any):
    """这份 runtime 自己那个 live_config 实例。每次 load_runtime 都是新的一份。"""
    return sys.modules[f"{runtime.__name__}.live_config"]


def write_live_config(module: Any, **values: Any) -> None:
    """把热载键写进这个 live_config 实例读的 qq.yaml 并让它立刻生效。

    走真实文件与真实写入路径，而不是直接改内存快照：键名写错、值类型不合法、点分路径
    漂移这几类问题会在测试里当场暴露，而改快照会把它们全部绕过。多次调用累加。
    """
    module.save(module.document_from(values, base=module.raw_document()))


def set_live_config(runtime: Any, **values: Any) -> None:
    """load_runtime 拿到的插件实例上的便捷入口。"""
    write_live_config(live_config_module(runtime), **values)

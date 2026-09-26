"""QQ 适配器的进程内状态全部挂在 OneBotAdapter 实例上，入口和 napcat 本体的模块级只剩常量和 NoneBot 注册。

回退成模块级容器或 global 重新赋值，拆文件时状态会散落到各个模块里，
FeatureGateway 也会重新伸手去摸插件的私有名字。
"""
from __future__ import annotations

import ast
from pathlib import Path

from _module_state_guards import factory_name, module_level_mutables, rebound_globals

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
_PLUGIN = _ADAPTER_DIR / "onebot_plugin.py"
_BODY = [_PLUGIN, *sorted((_ADAPTER_DIR / "napcat").glob("*.py"))]
# resolver 注入点和收藏表情缓存按连接分，同样不能落回模块级。
_EMOJI = _ADAPTER_DIR / "emoji_handler.py"
# NoneBot 要求在模块级注册 matcher；_adapter 是那唯一的实例本身。
_ALLOWED_CALLS = frozenset({"on_message", "on_notice", "get_driver", "OneBotAdapter"})


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _BODY)


def _tree() -> ast.Module:
    return ast.parse(_source())


def test_the_plugin_never_rebinds_module_globals() -> None:
    assert rebound_globals(_tree()) == []


def test_the_plugin_keeps_no_mutable_state_at_module_level() -> None:
    assert module_level_mutables(_tree(), allowed_calls=_ALLOWED_CALLS) == []


def test_emoji_rendering_keeps_its_state_on_the_renderer() -> None:
    tree = ast.parse(_EMOJI.read_text(encoding="utf-8"))

    assert rebound_globals(tree) == []
    assert module_level_mutables(tree) == []


def test_there_is_exactly_one_adapter_instance() -> None:
    created = [
        node for node in _tree().body
        if isinstance(node, ast.Assign) and factory_name(node.value) == "OneBotAdapter"
    ]
    assert [ast.unparse(node.targets[0]) for node in created] == ["_adapter"]


def test_the_feature_gateway_only_talks_to_its_host_interface() -> None:
    source = (_ADAPTER_DIR / "feature_gateway.py").read_text(encoding="utf-8")
    assert "runtime" not in source
    assert "sys.modules" not in _source()


def test_every_host_member_is_implemented(monkeypatch, tmp_path) -> None:
    from _onebot_harness import load_runtime

    runtime = load_runtime(monkeypatch, tmp_path)
    try:
        protocol = __import__(f"{runtime.__name__}.feature_gateway", fromlist=["FeatureHost"]).FeatureHost
        wanted = {
            name for name in (*vars(protocol), *getattr(protocol, "__annotations__", {}))
            if not name.startswith("_")
        }
        host = runtime.state._adapter.features.host
        missing = sorted(name for name in wanted if not hasattr(host, name))
        assert missing == []
    finally:
        runtime.state._adapter.outbound_idempotency.close()


def test_each_load_gets_its_own_state(monkeypatch, tmp_path) -> None:
    from _onebot_harness import load_runtime

    first = load_runtime(monkeypatch, tmp_path / "a")
    second = load_runtime(monkeypatch, tmp_path / "b")
    try:
        first.state._adapter.group_names["1"] = "猫猫群"
        first.state._adapter.emoji.custom_face_cache["10000"] = (0.0, [])
        assert first.state._adapter is not second.state._adapter
        assert second.state._adapter.group_names == {}
        assert second.state._adapter.emoji.custom_face_cache == {}
    finally:
        first.state._adapter.outbound_idempotency.close()
        second.state._adapter.outbound_idempotency.close()

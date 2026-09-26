"""QQ 适配器的进程内状态全部挂在 OneBotAdapter 实例上，入口和 napcat 本体的模块级只剩常量和 NoneBot 注册。

回退成模块级容器或 global 重新赋值，拆文件时状态会散落到各个模块里，
FeatureGateway 也会重新伸手去摸插件的私有名字。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
_PLUGIN = _ADAPTER_DIR / "onebot_plugin.py"
_BODY = [_PLUGIN, *sorted((_ADAPTER_DIR / "napcat").glob("*.py"))]
# resolver 注入点和收藏表情缓存按连接分，同样不能落回模块级。
_EMOJI = _ADAPTER_DIR / "emoji_handler.py"
_MUTABLE_LITERALS = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
_MUTABLE_FACTORIES = {
    "dict", "list", "set", "deque", "defaultdict", "OrderedDict", "Lock", "Condition", "ContextVar",
    "CooldownState", "CombatTracker", "TwoPhaseIdempotencyStore", "FeatureGateway",
}
# NoneBot 要求在模块级注册 matcher；_adapter 是那唯一的实例本身。
_ALLOWED_CALLS = {"on_message", "on_notice", "get_driver", "OneBotAdapter"}


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _BODY)


def _tree() -> ast.Module:
    return ast.parse(_source())


def _factory_name(value: ast.AST) -> str:
    if not isinstance(value, ast.Call):
        return ""
    func = value.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _rebound_globals(tree: ast.Module) -> list[str]:
    return sorted({name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names})


def _module_level_mutables(tree: ast.Module) -> list[str]:
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        factory = _factory_name(value)
        if factory in _ALLOWED_CALLS:
            continue
        if isinstance(value, _MUTABLE_LITERALS) or factory in _MUTABLE_FACTORIES:
            offenders.extend(ast.unparse(target) for target in targets)
    return offenders


def test_the_plugin_never_rebinds_module_globals() -> None:
    assert _rebound_globals(_tree()) == []


def test_the_plugin_keeps_no_mutable_state_at_module_level() -> None:
    assert _module_level_mutables(_tree()) == []


def test_emoji_rendering_keeps_its_state_on_the_renderer() -> None:
    tree = ast.parse(_EMOJI.read_text(encoding="utf-8"))

    assert _rebound_globals(tree) == []
    assert _module_level_mutables(tree) == []


def test_there_is_exactly_one_adapter_instance() -> None:
    created = [
        node for node in _tree().body
        if isinstance(node, ast.Assign) and _factory_name(node.value) == "OneBotAdapter"
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

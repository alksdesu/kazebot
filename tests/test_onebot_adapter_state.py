"""QQ 插件的进程内状态全部挂在 OneBotAdapter 实例上，模块级只剩常量和 NoneBot 注册。

回退成模块级容器或 global 重新赋值，拆文件时状态会散落到各个模块里，
FeatureGateway 也会重新伸手去摸插件的私有名字。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
_PLUGIN = _ADAPTER_DIR / "onebot_plugin.py"
_MUTABLE_LITERALS = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
_MUTABLE_FACTORIES = {
    "dict", "list", "set", "deque", "defaultdict", "OrderedDict", "Lock", "Condition", "ContextVar",
    "CooldownState", "CombatTracker", "TwoPhaseIdempotencyStore", "FeatureGateway",
}
# NoneBot 要求在模块级注册 matcher；_adapter 是那唯一的实例本身。
_ALLOWED_CALLS = {"on_message", "on_notice", "get_driver", "OneBotAdapter"}


def _tree() -> ast.Module:
    return ast.parse(_PLUGIN.read_text(encoding="utf-8"))


def _factory_name(value: ast.AST) -> str:
    if not isinstance(value, ast.Call):
        return ""
    func = value.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_the_plugin_never_rebinds_module_globals() -> None:
    offenders = sorted({name for node in ast.walk(_tree()) if isinstance(node, ast.Global) for name in node.names})
    assert offenders == []


def test_the_plugin_keeps_no_mutable_state_at_module_level() -> None:
    offenders = []
    for node in _tree().body:
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
    assert offenders == []


def test_there_is_exactly_one_adapter_instance() -> None:
    created = [
        node for node in _tree().body
        if isinstance(node, ast.Assign) and _factory_name(node.value) == "OneBotAdapter"
    ]
    assert [ast.unparse(node.targets[0]) for node in created] == ["_adapter"]


def test_the_feature_gateway_only_talks_to_its_host_interface() -> None:
    source = (_ADAPTER_DIR / "feature_gateway.py").read_text(encoding="utf-8")
    assert "runtime" not in source
    assert "sys.modules" not in _PLUGIN.read_text(encoding="utf-8")


def test_every_host_member_is_implemented(monkeypatch, tmp_path) -> None:
    from _onebot_harness import load_runtime

    runtime = load_runtime(monkeypatch, tmp_path)
    try:
        protocol = __import__(f"{runtime.__name__}.feature_gateway", fromlist=["FeatureHost"]).FeatureHost
        wanted = {
            name for name in (*vars(protocol), *getattr(protocol, "__annotations__", {}))
            if not name.startswith("_")
        }
        host = runtime._adapter.features.host
        missing = sorted(name for name in wanted if not hasattr(host, name))
        assert missing == []
    finally:
        runtime._adapter.outbound_idempotency.close()


def test_each_load_gets_its_own_state(monkeypatch, tmp_path) -> None:
    from _onebot_harness import load_runtime

    first = load_runtime(monkeypatch, tmp_path / "a")
    second = load_runtime(monkeypatch, tmp_path / "b")
    try:
        first._adapter.group_names["1"] = "猫猫群"
        assert first._adapter is not second._adapter
        assert second._adapter.group_names == {}
    finally:
        first._adapter.outbound_idempotency.close()
        second._adapter.outbound_idempotency.close()

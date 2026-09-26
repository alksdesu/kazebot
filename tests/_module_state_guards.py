"""模块级状态的静态检查：global 重新赋值、模块顶层直接建出来的可变容器和有状态对象。"""
from __future__ import annotations

import ast

MUTABLE_LITERALS = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
MUTABLE_FACTORIES = frozenset({
    "dict", "list", "set", "deque", "defaultdict", "OrderedDict", "Lock", "Condition", "ContextVar",
    "CooldownState", "CombatTracker", "TwoPhaseIdempotencyStore", "OneBotIdempotencyStore", "FeatureGateway",
    "AliasBook", "HistoryWindows", "ClearBarriers", "ConversationQueue", "ConversationCoordinator", "BurstStore",
})


def factory_name(value: ast.AST) -> str:
    if not isinstance(value, ast.Call):
        return ""
    func = value.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def rebound_globals(tree: ast.Module) -> list[str]:
    return sorted({name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names})


def module_level_mutables(tree: ast.Module, *, allowed_calls: frozenset[str] = frozenset()) -> list[str]:
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        factory = factory_name(value)
        if factory in allowed_calls:
            continue
        if isinstance(value, MUTABLE_LITERALS) or factory in MUTABLE_FACTORIES:
            offenders.extend(ast.unparse(target) for target in targets)
    return offenders

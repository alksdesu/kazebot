"""在 supervisor 侧复用 QQ 触发判定，供控制台的「试听」调用。

判定逻辑只有 adapters/onebot/trigger_policy.py 一份 —— 前端不重写、supervisor 也不重写，
否则试听说会回、实际不回，比没有试听更糟。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

# 按文件路径加载而不是 import adapters.onebot.trigger_policy：那个包的 __init__ 会
# get_driver()，在没有 NoneBot 的 supervisor 进程里直接抛。trigger_policy 自己零相对
# 导入，所以单独加载是安全的 —— 有测试盯着这个前提。
_MODULE_NAME = "_clonoth_qq_trigger_policy"
_cache: dict[str, ModuleType] = {}


def load_policy(workspace_root: Path) -> ModuleType:
    path = (workspace_root / "adapters" / "onebot" / "trigger_policy.py").resolve()
    key = str(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(f"{_MODULE_NAME}_{abs(hash(key))}", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"trigger policy not found at {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器会按 cls.__module__ 回查 sys.modules，先注册再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _cache[key] = module
    return module


def config_from_values(policy: ModuleType, values: dict[str, Any]) -> Any:
    """用 bot 公布的生效值装配 TriggerConfig。

    from_live 只做属性读取，所以扁平的键值对包一层 SimpleNamespace 就能直接喂进去 ——
    三态开关回落旧 group_mode 的那套推导因此也只有一份实现。
    """
    return policy.TriggerConfig.from_live(SimpleNamespace(**values))


def _as_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def dry_run(
    workspace_root: Path,
    messages: list[dict[str, Any]],
    values: dict[str, Any],
    *,
    apply_cooldown: bool = True,
) -> dict[str, Any]:
    """把一批模拟消息按顺序过一遍判定。

    冷却按顺序累积：第一条命中之后，后面几条才可能被冷却挡住 —— 试听要能看出这个因果，
    所以不是每条独立判定。
    """
    policy = load_policy(workspace_root)
    config = config_from_values(policy, values)
    cooldown = policy.CooldownState() if apply_cooldown else None

    results: list[dict[str, Any]] = []
    for index, raw in enumerate(messages):
        item = raw if isinstance(raw, dict) else {}
        inp = policy.TriggerInput(
            text=str(item.get("text") or ""),
            group_id=int(item.get("group_id") or 0),
            user_id=int(item.get("user_id") or 0),
            at_me=_as_bool(item.get("at_me")),
            reply_to_bot=_as_bool(item.get("reply_to_bot")),
            # 默认「每条间隔一秒依次到达」，冷却窗口的效果才看得出来。
            now=float(item.get("at_sec") if item.get("at_sec") is not None else index),
        )
        # 随机与 LLM 意愿判断报成「取决于运行时」，不伪造答案。
        decision = policy.evaluate(inp, config, cooldown, resolve_random=False)
        if decision.triggered and cooldown is not None:
            cooldown.record(inp)
        results.append({"index": index, "text": inp.text, **decision.as_dict()})

    return {
        "enabled_signals": list(config.enabled_signals()),
        "cooldown_applied": apply_cooldown,
        "results": results,
    }

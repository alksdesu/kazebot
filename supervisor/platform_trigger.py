"""在 supervisor 侧复用平台的触发判定，供控制台的「试听」调用。

判定逻辑只有平台适配器里那一份，由平台描述的 trigger_policy 钩子指过来 ——
前端不重写、supervisor 也不重写，否则试听说会回、实际不回，比没有试听更糟。
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

from clonoth_runtime import parse_bool
from platforms.descriptor import HOOK_TRIGGER_POLICY, PlatformDescriptor
from platforms.ids import normalize_id
from platforms.registry import platform_hook


def load_policy(platform: PlatformDescriptor) -> ModuleType:
    return platform_hook(platform, HOOK_TRIGGER_POLICY)


def config_from_values(policy: ModuleType, values: dict[str, Any]) -> Any:
    """用 bot 公布的生效值装配 TriggerConfig。

    from_live 只做属性读取，所以扁平的键值对包一层 SimpleNamespace 就能直接喂进去 ——
    三态开关回落旧 group_mode 的那套推导因此也只有一份实现。
    """
    return policy.TriggerConfig.from_live(SimpleNamespace(**values))


def dry_run(
    platform: PlatformDescriptor,
    messages: list[dict[str, Any]],
    values: dict[str, Any],
    *,
    apply_cooldown: bool = True,
) -> dict[str, Any]:
    """把一批模拟消息按 at_sec 时间线依次过一遍判定。

    冷却、在场、安静等待和主动回复密度都按顺序累积：前面有人叫了 bot，后面几条才在场、才会被合并，
    试听要能看出这个因果，所以不是每条独立判定。安静等待在下一条到来前、或时间线走完时按到点时刻结算。
    随机、概率门和模型判断报成「看运行时」，不伪造答案；bot 自己发言也算进场，但它发不发、什么时候发
    看运行时，这里只按被直接叫到算进场。
    """
    policy = load_policy(platform)
    config = config_from_values(policy, values)
    cooldown = policy.CooldownState() if apply_cooldown else None
    presence = policy.PresenceState()

    results: list[dict[str, Any]] = []
    # 群 → 这个群当前安静等待里排在最后的那一条，到点时由它交给模型。
    latest: dict[str, int] = {}

    def settle(group_id: str, now: float) -> None:
        wait = presence.quiet(group_id)
        if wait is None or now < wait.deadline:
            return
        presence.close_quiet(group_id, wait.cycle)
        owner = latest.pop(group_id, None)
        if owner is not None:
            results[owner].update({
                "quiet": policy.QUIET_DUE,
                "quiet_due_at": round(wait.deadline, 3),
                "quiet_merged": wait.count,
            })

    for index, raw in enumerate(messages):
        item = raw if isinstance(raw, dict) else {}
        inp = policy.TriggerInput(
            text=str(item.get("text") or ""),
            group_id=normalize_id(item.get("group_id")),
            user_id=normalize_id(item.get("user_id")),
            at_me=bool(parse_bool(item.get("at_me"), False)),
            reply_to_bot=bool(parse_bool(item.get("reply_to_bot"), False)),
            # 默认「每条间隔一秒依次到达」，冷却窗口的效果才看得出来。
            now=float(item.get("at_sec") if item.get("at_sec") is not None else index),
        )
        settle(inp.group_id, inp.now)
        # 随机、概率门与 LLM 意愿判断报成「取决于运行时」，不伪造答案。
        decision = policy.evaluate(inp, config, cooldown, presence=presence, resolve_random=False)
        policy.observe(inp, decision, presence)
        entry: dict[str, Any] = {
            "index": index, "text": inp.text, "at_sec": inp.now,
            **decision.as_dict(), **decision.status_dict(),
        }
        if decision.triggered and not decision.undetermined:
            owner = latest.pop(inp.group_id, None)
            if owner is not None and presence.quiet(inp.group_id) is not None:
                # 这条直接触发了，在等的那一轮由它的回复一并接过去。
                results[owner]["quiet"] = policy.QUIET_CANCELLED
            policy.accept(inp, decision, cooldown=cooldown, presence=presence)
        if decision.awaits_quiet():
            wait = presence.join_quiet(
                inp.group_id, inp.now, wait_sec=config.quiet_wait_sec, max_wait_sec=config.quiet_max_wait_sec,
            )
            owner = latest.get(inp.group_id)
            if owner is not None:
                results[owner]["quiet"] = policy.QUIET_MERGED
            latest[inp.group_id] = index
            entry["quiet_due_at"] = round(wait.deadline, 3)
            entry["quiet_merged"] = wait.count
        until = presence.present_until(inp.group_id, config.presence_window_sec)
        entry["present_until"] = None if until is None else round(until, 3)
        results.append(entry)

    for group_id in list(latest):
        wait = presence.quiet(group_id)
        if wait is not None:
            settle(group_id, wait.deadline)

    return {
        "enabled_signals": list(config.enabled_signals()),
        "cooldown_applied": apply_cooldown,
        "presence": {
            "active": config.presence_active,
            "window_sec": config.presence_window_sec,
            "absent_judge_probability": config.absent_judge_probability,
            "quiet_wait_sec": config.quiet_wait_sec,
            "quiet_max_wait_sec": config.quiet_max_wait_sec,
            "density_limit": config.proactive_max_replies,
            "density_window_sec": config.proactive_window_sec,
        },
        "results": results,
    }

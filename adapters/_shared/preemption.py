"""新消息能不能接进同会话正在跑的入口任务：只做判定，注入和善后由适配器做。"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar

Task = TypeVar("Task")
Trigger = TypeVar("Trigger")


def route_hints_of(trigger: Any) -> Mapping[str, Any]:
    if not trigger:
        return {}
    return trigger.platform_data.get("_route_hints") or {}


def preempt_candidates(
    running_tasks: Iterable[Task],
    *,
    trigger_of: Callable[[Task], Trigger | None],
    is_dm: bool,
    same_sender: Callable[[Trigger], bool],
    route_hints: Mapping[str, Any],
) -> Iterator[tuple[Task, Trigger]]:
    """按原顺序给出可以接过这条新消息的入口任务，连同它登记的触发。

    trigger_of 只能给这条任务自己登记的触发：查不到就不接，拿会话里别的触发顶上等于把消息注进别人的任务。
    群聊里多人共用一个会话键：确认不了是同一个人发的就不接，免得一个人打断另一个人的任务。
    响应方式变了也不接，那是另一件事。
    """
    current_action = route_hints.get("response_action", "reply")
    for task in running_tasks:
        if not task.task_id or not task.is_user_entry:
            continue
        trigger = trigger_of(task)
        if trigger is None or (not is_dm and not same_sender(trigger)):
            continue
        previous = route_hints_of(trigger)
        if previous.get("response_action", "reply") != current_action:
            continue
        yield task, trigger

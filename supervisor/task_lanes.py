"""任务分道：接话判断走快道插队，用户在等的走交互道，压缩、记忆提取、轮摘要、dream、执行计划步骤走后台道。

道在建任务时定下、随快照落盘；后台道所有 worker 合计限并发，worker 只剩保留给交互道的空位时不领后台道的任务。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from clonoth_runtime import get_int


class TaskLane(str, Enum):
    interactive = "interactive"
    fast = "fast"
    background = "background"


# 领任务时按这个次序挑，同一道里按排队先后
CLAIM_ORDER: tuple[TaskLane, ...] = (TaskLane.fast, TaskLane.interactive, TaskLane.background)

# 建任务时就写进 input 的系统流程标记，旧快照里也都留着
_BACKGROUND_INPUT_KEYS = (
    "_system_task",
    "_compact_async",
    "_turn_summary_dispatch",
    "_memory_extract_intent_id",
    "execution_plan_id",
)


def parse_lane(value: Any) -> TaskLane | None:
    """认得的道名换成 TaskLane，认不出的是 None。"""
    if isinstance(value, TaskLane):
        return value
    try:
        return TaskLane(str(value or ""))
    except ValueError:
        return None


def derive_lane(task_input: Any, *, inherited: TaskLane | None = None) -> TaskLane:
    """建任务和补旧快照共用的唯一规则。

    任务自己带的标记优先：接话判断是快道，系统内部流程是后台道。其余的随它替之干活的那个任务
    （同步派发的 caller、异步派发的发起任务），只有后台道往下传，插队只给接话判断本身。都没有就是交互道。
    """
    data = task_input if isinstance(task_input, Mapping) else {}
    if data.get("_reply_intent"):
        return TaskLane.fast
    context = data.get("task_context")
    if any(data.get(key) for key in _BACKGROUND_INPUT_KEYS) or (
        isinstance(context, Mapping) and context.get("is_system_task")
    ):
        return TaskLane.background
    return TaskLane.background if inherited is TaskLane.background else TaskLane.interactive


@dataclass(frozen=True)
class LaneLimits:
    """runtime.yaml 里的分道上限。supervisor 每次领任务时现读，改了不用重启。"""

    background_max_running: int = 2
    interactive_reserve: int = 1

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "LaneLimits":
        data = cfg if isinstance(cfg, dict) else {}
        return cls(
            background_max_running=get_int(
                data, "engine.lanes.background_max_running", 2, min_value=0, max_value=1024,
            ),
            interactive_reserve=get_int(data, "engine.lanes.interactive_reserve", 1, min_value=0, max_value=1024),
        )

    def worker_takes_background(self, free_slots: int | None, capacity: int | None) -> bool:
        """worker 报来的空位够不够领后台道的任务。

        空位多于保留数才给；整个空着的 worker 照样给，免得 max_workers 不大于保留数时后台道永远轮不上。
        不报空位的老 engine 不做保留。
        """
        if free_slots is None:
            return True
        if free_slots > self.interactive_reserve:
            return True
        return capacity is not None and free_slots >= capacity

    def background_has_room(self, running: int) -> bool:
        """所有 worker 合计正在跑 running 个后台道任务时，还能不能再放一个。0 表示不限。"""
        return self.background_max_running <= 0 or running < self.background_max_running

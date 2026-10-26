"""任务一段运行的预算：步数跨挂起恢复累计，时长到运行上限或总寿命之前留出收尾的余量。

总寿命从续跑链第一个任务创建时算，supervisor 回收和 engine 收尾用同一个起点。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from clonoth_runtime import get_float

# 单次运行按领取时刻计时，挂起再恢复会重新起算；总寿命再按运行上限的这个倍数兜底，防止派发、恢复无限循环
TASK_LIFETIME_FACTOR = 6

STEPS_USED_KEY = "_steps_used"
STEP_COUNT_RESULT_KEY = "_step_count"
AUTO_CONTINUE_KEY = "_auto_continue"
CHAIN_STARTED_AT_KEY = "chain_started_at"

STEP_LIMIT_FLAG = "step_limit_reached"
TIME_LIMIT_FLAG = "time_limit_reached"
# 收尾文本末尾的说明；supervisor 自动续跑时换成续跑说明
STEP_LIMIT_NOTICE = "（已达到最大步数，任务到此停止）"
TIME_LIMIT_NOTICE = "（已接近运行时长上限，任务到此停止）"
WRAPUP_KEY = "wrapup"
WRAPUP_STEPS = "steps"
WRAPUP_TIME = "time"

WRAPUP_TIME_RATIO_KEY = "engine.wrapup.time_ratio"
DEFAULT_WRAPUP_TIME_RATIO = 0.9
_MIN_WRAPUP_TIME_RATIO = 0.5
_MAX_WRAPUP_TIME_RATIO = 0.99


def parse_timestamp(value: Any) -> datetime | None:
    """ISO 时间串或 datetime 换成带时区的 datetime；没写时区的按 UTC，读不出来给 None。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def lifetime_origin(created_at: datetime, task_input: Mapping[str, Any] | None) -> datetime:
    """总寿命从哪一刻算：续跑任务从续跑链第一个任务创建时算，其余从自己创建时算。"""
    created = parse_timestamp(created_at) or datetime.now(timezone.utc)
    chain = task_input.get(AUTO_CONTINUE_KEY) if isinstance(task_input, Mapping) else None
    started = parse_timestamp(chain.get(CHAIN_STARTED_AT_KEY)) if isinstance(chain, Mapping) else None
    if started is None or started > created:
        return created
    return started


def lifetime_limit_seconds(max_running_sec: float) -> float:
    return float(max_running_sec) * TASK_LIFETIME_FACTOR


def wrapup_time_ratio(runtime_cfg: Any) -> float:
    """单段运行用到上限的多大比例就收尾，留下的那段时间给最后一步交代进度。"""
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return get_float(
        cfg, WRAPUP_TIME_RATIO_KEY, DEFAULT_WRAPUP_TIME_RATIO,
        min_value=_MIN_WRAPUP_TIME_RATIO, max_value=_MAX_WRAPUP_TIME_RATIO,
    )


def wrapup_reserve_seconds(max_running_sec: float, ratio: float) -> float:
    return max(0.0, (1.0 - float(ratio)) * float(max_running_sec))


def steps_used(task_input: Mapping[str, Any] | None) -> int:
    """前几段已经用掉的步数；没有或读不出来按 0。"""
    if not isinstance(task_input, Mapping):
        return 0
    try:
        return max(0, int(task_input.get(STEPS_USED_KEY) or 0))
    except (TypeError, ValueError):
        return 0


def reported_step_count(result: Mapping[str, Any] | None) -> int:
    """engine 回报的累计步数（含挂起前各段）；没有或读不出来按 0。"""
    if not isinstance(result, Mapping):
        return 0
    try:
        return max(0, int(result.get(STEP_COUNT_RESULT_KEY) or 0))
    except (TypeError, ValueError):
        return 0


def wrapup_info(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """finish 结果里的收尾说明：到了步数或运行时长上限才有，reason 只会是 steps 或 time。"""
    if not isinstance(result, Mapping):
        return None
    time_reached = result.get(TIME_LIMIT_FLAG) is True
    steps_reached = result.get(STEP_LIMIT_FLAG) is True
    if not (time_reached or steps_reached):
        return None
    raw = result.get(WRAPUP_KEY)
    info = dict(raw) if isinstance(raw, Mapping) else {}
    reason = str(info.get("reason") or "").strip()
    if reason not in (WRAPUP_STEPS, WRAPUP_TIME):
        reason = WRAPUP_TIME if time_reached else WRAPUP_STEPS
    info["reason"] = reason
    return info


@dataclass(frozen=True)
class RunBudget:
    """这一段运行什么时候该收尾。时刻按 clock 计，engine 用单调时钟，不受系统时间回拨影响。"""

    deadline: float
    started: float
    clock: Callable[[], float] = time.monotonic

    def due(self) -> bool:
        return self.clock() >= self.deadline

    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started)

    @classmethod
    def for_task(
        cls,
        task: Mapping[str, Any],
        *,
        max_running_sec: float,
        ratio: float,
        now: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "RunBudget":
        """按 supervisor 记的领取、创建时刻算：运行上限和总寿命先到的那个，再往前让出收尾的余量。"""
        current = parse_timestamp(now) or datetime.now(timezone.utc)
        mark = clock()
        claimed = parse_timestamp(task.get("claimed_at")) or current
        created = parse_timestamp(task.get("created_at")) or claimed
        task_input = task.get("input") if isinstance(task.get("input"), Mapping) else {}
        origin = lifetime_origin(created, task_input)
        run_end = claimed + timedelta(seconds=float(max_running_sec))
        life_end = origin + timedelta(seconds=lifetime_limit_seconds(max_running_sec))
        wrap_at = min(run_end, life_end) - timedelta(seconds=wrapup_reserve_seconds(max_running_sec, ratio))
        return cls(
            deadline=mark + (wrap_at - current).total_seconds(),
            started=mark - max(0.0, (current - claimed).total_seconds()),
            clock=clock,
        )

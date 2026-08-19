"""压缩熔断器。

连续失败 N 次后停一段冷却，不再为同一个 session 反复调压缩模型。计数只放在
supervisor 里：压缩模型调用、摘要应用、应急截断全在这一侧，而 engine 是多个独立
worker 进程，谁也读不到别人的计数器。

只管软阈值的后台压缩。硬阈值同步压缩不受熔断影响 —— 那条路不压就会顶爆上下文窗口，
且它的失败分支本身会用 TaskRecord 摘要做应急截断，不需要再调模型。
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 600.0
MAX_TRACKED_SESSIONS = 512


class CompactBreaker:
    """按 target_session_id 记账的失败计数 + 冷却窗口。

    自带容量上限而不是靠外部在 session 销毁时通知：清理点有十来处，漏一处就是泄漏，
    而这里存的只是一个整数和一个时间戳，按插入序淘汰最旧的即可。
    """

    def __init__(
        self,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        max_tracked: int = MAX_TRACKED_SESSIONS,
    ) -> None:
        self._max_failures = max_failures
        self._cooldown = cooldown_seconds
        self._max_tracked = max_tracked
        self._state: "OrderedDict[str, tuple[int, float]]" = OrderedDict()

    def record_failure(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        count, open_until = self._state.get(sid, (0, 0.0))
        count += 1
        if count >= self._max_failures:
            open_until = time.monotonic() + self._cooldown
            log.warning(
                "compact breaker open for session %s after %d consecutive failures, "
                "cooling down %.0fs",
                sid, count, self._cooldown,
            )
        self._state[sid] = (count, open_until)
        self._state.move_to_end(sid)
        while len(self._state) > self._max_tracked:
            self._state.popitem(last=False)

    def record_success(self, session_id: str) -> None:
        self._state.pop(str(session_id or "").strip(), None)

    def is_open(self, session_id: str) -> bool:
        """冷却期内返回 True。过期即自动关闭并清零，不需要外部定时清理。"""
        sid = str(session_id or "").strip()
        entry = self._state.get(sid) if sid else None
        if entry is None or entry[1] <= 0.0:
            return False
        if time.monotonic() < entry[1]:
            return True
        # 冷却结束：给一次干净的重试机会，而不是挂着旧计数一失败就立刻再熔断。
        self._state.pop(sid, None)
        return False

    def failure_count(self, session_id: str) -> int:
        entry = self._state.get(str(session_id or "").strip())
        return entry[0] if entry else 0

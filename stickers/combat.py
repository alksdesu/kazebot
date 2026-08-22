"""群里刷图时跟着接一张。

只做判定，不碰网络也不碰磁盘 —— 状态机被消息流驱动，出错的样子必须能在单测里复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 群数量无上限，状态得能被淘汰，否则进程越跑越占内存。
MAX_TRACKED_GROUPS = 256


@dataclass(frozen=True)
class CombatConfig:
    enabled: bool = False
    # 连着这么多张图才算刷起来了。太小会把「两个人各发一张」也当成斗图。
    battle_threshold: int = 5
    battle_window_sec: float = 45.0
    battle_cooldown_sec: float = 90.0
    # 至少这么多人参与，一个人连发五张是自娱自乐。
    battle_min_users: int = 2
    burst_probability: float = 0.0
    burst_extra: int = 1
    burst_cooldown_sec: float = 60.0


@dataclass
class GroupState:
    """一个群的连图streak。清空比记错更安全，所以断流条件给得宽。"""

    stamps: list[float] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    # 负无穷表示"从没触发过"。用 0 的话，单调时钟从小数开始的机器上，
    # 开机头一个冷却周期内会被判成"刚触发完"，一次都发不出去。
    last_battle_at: float = float("-inf")
    last_burst_at: float = float("-inf")
    touched_at: float = 0.0

    def reset_streak(self) -> None:
        self.stamps.clear()
        self.users.clear()


class CombatTracker:
    """按群跟踪连图。同一个实例被消息线程和发送路径共用，但都在事件循环里，不加锁。"""

    def __init__(self, *, max_groups: int = MAX_TRACKED_GROUPS) -> None:
        self._groups: dict[str, GroupState] = {}
        self._max_groups = max(int(max_groups), 1)

    def state(self, group: str, now: float = 0.0) -> GroupState:
        found = self._groups.get(group)
        if found is None:
            # 建好就带上时间：淘汰按 touched_at 排，留 0 会让刚建的这个先被踢掉。
            found = GroupState(touched_at=now)
            self._groups[group] = found
            self._evict()
        return found

    def _evict(self) -> None:
        if len(self._groups) <= self._max_groups:
            return
        # 最久没动的先走。斗图是当下的事，冷群的 streak 留着也没用。
        stale = sorted(self._groups.items(), key=lambda item: item[1].touched_at)
        for key, _ in stale[: len(self._groups) - self._max_groups]:
            self._groups.pop(key, None)

    def forget(self, group: str) -> None:
        self._groups.pop(group, None)

    def clear(self) -> None:
        self._groups.clear()

    def on_image(self, group: str, *, user: str, now: float, config: CombatConfig) -> None:
        state = self.state(group, now)
        state.touched_at = now
        cutoff = now - max(config.battle_window_sec, 0.0)
        keep = [index for index, stamp in enumerate(state.stamps) if stamp >= cutoff]
        state.stamps = [state.stamps[index] for index in keep]
        state.users = [state.users[index] for index in keep]
        state.stamps.append(now)
        state.users.append(str(user))

    def on_text(self, group: str, *, now: float) -> None:
        """有人说了句话，刷图就断了。接一句话之后再发图是回应，不是斗图。"""
        state = self.state(group, now)
        state.touched_at = now
        state.reset_streak()

    def on_self_send(self, group: str, *, now: float) -> None:
        """bot 自己发完图整组清空 —— 不清就会把自己发的那张算进 streak，自激起来没完。"""
        state = self.state(group, now)
        state.touched_at = now
        state.reset_streak()

    def should_battle(self, group: str, *, now: float, config: CombatConfig) -> bool:
        if not config.enabled or config.battle_threshold <= 0:
            return False
        state = self.state(group, now)
        if now - state.last_battle_at < config.battle_cooldown_sec:
            return False
        cutoff = now - max(config.battle_window_sec, 0.0)
        fresh = [index for index, stamp in enumerate(state.stamps) if stamp >= cutoff]
        if len(fresh) < config.battle_threshold:
            return False
        return len({state.users[index] for index in fresh}) >= max(config.battle_min_users, 1)

    def mark_battled(self, group: str, *, now: float) -> None:
        state = self.state(group, now)
        state.last_battle_at = now
        state.touched_at = now
        state.reset_streak()

    def should_burst(self, group: str, *, now: float, config: CombatConfig, roll: float) -> bool:
        """bot 刚发完一张，要不要再补几张。roll 由调用方给，判定才可复现。"""
        if not config.enabled or config.burst_probability <= 0 or config.burst_extra <= 0:
            return False
        state = self.state(group, now)
        if now - state.last_burst_at < config.burst_cooldown_sec:
            return False
        return roll < config.burst_probability

    def mark_burst(self, group: str, *, now: float) -> None:
        state = self.state(group, now)
        state.last_burst_at = now
        state.touched_at = now

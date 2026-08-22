"""群里刷图时的接梗判定。

最要紧的是自激：bot 自己发的图如果算进 streak，它会一直跟自己斗下去。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers.combat import CombatConfig, CombatTracker


def _config(**overrides) -> CombatConfig:
    base = {
        "enabled": True, "battle_threshold": 3, "battle_window_sec": 30.0,
        "battle_cooldown_sec": 60.0, "battle_min_users": 2,
    }
    base.update(overrides)
    return CombatConfig(**base)


def _flood(tracker: CombatTracker, config: CombatConfig, *, count: int, users, start: float = 100.0):
    for index in range(count):
        tracker.on_image("g", user=users[index % len(users)], now=start + index, config=config)
    return start + count - 1


# ── 触发 ──

def test_enough_images_from_enough_people_triggers(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    assert tracker.should_battle("g", now=now, config=config)


def test_one_person_flooding_is_not_a_battle(tracker_and_config) -> None:
    # 一个人连发五张是自娱自乐，跟着接反而尴尬。
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=5, users=["a"])
    assert not tracker.should_battle("g", now=now, config=config)


def test_too_few_images_does_not_trigger(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=2, users=["a", "b"])
    assert not tracker.should_battle("g", now=now, config=config)


def test_images_outside_the_window_do_not_count(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    tracker.on_image("g", user="a", now=0.0, config=config)
    tracker.on_image("g", user="b", now=1.0, config=config)
    tracker.on_image("g", user="c", now=1000.0, config=config)
    assert not tracker.should_battle("g", now=1000.0, config=config)


def test_disabled_never_triggers(tracker_and_config) -> None:
    tracker, _config_on = tracker_and_config
    off = _config(enabled=False)
    now = _flood(tracker, off, count=5, users=["a", "b", "c"])
    assert not tracker.should_battle("g", now=now, config=off)


# ── 断流 ──

def test_a_plain_message_breaks_the_streak(tracker_and_config) -> None:
    # 中间有人说了句话，后面的图是回应而不是斗图。
    tracker, config = tracker_and_config
    _flood(tracker, config, count=2, users=["a", "b"])
    tracker.on_text("g", now=103.0)
    tracker.on_image("g", user="c", now=104.0, config=config)
    assert not tracker.should_battle("g", now=104.0, config=config)


def test_the_bot_sending_clears_everything(tracker_and_config) -> None:
    # 不清空的话 bot 会把自己发的图算进 streak，一路自激下去。
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    tracker.on_self_send("g", now=now)
    assert not tracker.should_battle("g", now=now, config=config)


def test_marking_a_battle_clears_the_streak(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    tracker.mark_battled("g", now=now)
    assert not tracker.should_battle("g", now=now, config=config)


# ── 冷却 ──

def test_cooldown_blocks_a_second_battle(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    tracker.mark_battled("g", now=now)
    later = _flood(tracker, config, count=3, users=["a", "b", "c"], start=now + 5)
    assert not tracker.should_battle("g", now=later, config=config)


def test_battle_resumes_after_the_cooldown(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    tracker.mark_battled("g", now=now)
    later = _flood(
        tracker, config, count=3, users=["a", "b", "c"],
        start=now + config.battle_cooldown_sec + 1,
    )
    assert tracker.should_battle("g", now=later, config=config)


# ── 群之间互不影响 ──

def test_groups_are_independent(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    assert tracker.should_battle("g", now=now, config=config)
    assert not tracker.should_battle("other", now=now, config=config)


def test_forget_drops_one_group_only(tracker_and_config) -> None:
    tracker, config = tracker_and_config
    now = _flood(tracker, config, count=3, users=["a", "b", "c"])
    tracker.forget("g")
    assert not tracker.should_battle("g", now=now, config=config)


def test_tracked_groups_are_capped() -> None:
    # 群数量没有上限，状态不淘汰就是慢性内存泄漏。
    tracker = CombatTracker(max_groups=4)
    config = _config()
    for index in range(20):
        tracker.on_image(f"g{index}", user="a", now=float(index), config=config)
    assert len(tracker._groups) <= 4


def test_eviction_keeps_the_most_recent(tracker_and_config) -> None:
    tracker = CombatTracker(max_groups=2)
    config = _config()
    tracker.on_image("old", user="a", now=1.0, config=config)
    tracker.on_image("mid", user="a", now=2.0, config=config)
    tracker.on_image("new", user="a", now=3.0, config=config)
    assert "new" in tracker._groups


# ── 连发 ──

def test_burst_is_off_by_default() -> None:
    tracker = CombatTracker()
    assert not tracker.should_burst("g", now=1.0, config=_config(), roll=0.0)


def test_burst_fires_when_the_roll_lands() -> None:
    tracker = CombatTracker()
    config = _config(burst_probability=0.3)
    assert tracker.should_burst("g", now=1.0, config=config, roll=0.1)
    assert not tracker.should_burst("g", now=1.0, config=config, roll=0.9)


def test_burst_respects_its_cooldown() -> None:
    tracker = CombatTracker()
    config = _config(burst_probability=1.0, burst_cooldown_sec=60.0)
    tracker.mark_burst("g", now=100.0)
    assert not tracker.should_burst("g", now=120.0, config=config, roll=0.0)
    assert tracker.should_burst("g", now=200.0, config=config, roll=0.0)


def test_burst_needs_a_positive_extra_count() -> None:
    tracker = CombatTracker()
    config = _config(burst_probability=1.0, burst_extra=0)
    assert not tracker.should_burst("g", now=1.0, config=config, roll=0.0)


@pytest.fixture()
def tracker_and_config():
    return CombatTracker(), _config()

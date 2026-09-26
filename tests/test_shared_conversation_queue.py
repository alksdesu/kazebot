"""共享层的会话排队与抢占判定。"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.conversation_queue import ConversationQueue  # noqa: E402
from adapters._shared.preemption import preempt_candidates  # noqa: E402


@dataclass
class _Item:
    texts: list[str] = field(default_factory=list)


def _item(text: str) -> _Item:
    return _Item([text])


def _merge(existing: _Item, item: _Item) -> None:
    existing.texts.extend(item.texts)


def _texts(queue: ConversationQueue[_Item]) -> list[list[str]]:
    return [item.texts for item in queue]


def _as(identity: str | None) -> Callable[[_Item], str | None]:
    return lambda item: identity


async def _put(queue: ConversationQueue[_Item], key: str, item: _Item, identity: str | None = None) -> None:
    await queue.put(key, item, identity_of=_as(identity), merge=_merge)


class TestPut:
    def test_the_same_identity_merges_into_the_waiting_item(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> None:
            await _put(queue, "room", _item("a1"), "alice")
            await _put(queue, "room", _item("a2"), "alice")

        asyncio.run(scenario())

        assert _texts(queue) == [["a1", "a2"]]

    def test_a_different_identity_queues_behind_and_takes_over_merging(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()
        later = _item("b")

        async def scenario() -> None:
            await _put(queue, "room", _item("a1"), "alice")
            await _put(queue, "room", later, "bob")
            await _put(queue, "room", _item("a2"), "alice")

        asyncio.run(scenario())

        # 换人以后合并入口指向最新那件：a2 不能越过 b 合进 a1，否则到达顺序就乱了。
        assert _texts(queue) == [["a1"], ["b"], ["a2"]]
        assert queue.open_item("room") is not later

    def test_an_unknown_identity_never_merges(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> None:
            await _put(queue, "room", _item("x"))
            await _put(queue, "room", _item("y"))

        asyncio.run(scenario())

        assert len(queue) == 2

    def test_the_identity_is_read_on_every_put_including_merges(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()
        seen: list[str] = []

        def identity_of(item: _Item) -> str:
            seen.append(item.texts[0])
            return "alice"

        async def scenario() -> None:
            await queue.put("room", _item("a1"), identity_of=identity_of, merge=_merge)
            await queue.put("room", _item("a2"), identity_of=identity_of, merge=_merge)

        asyncio.run(scenario())

        assert seen == ["a1", "a2"]
        assert _texts(queue) == [["a1", "a2"]]


class TestTake:
    def test_a_busy_conversation_is_skipped_until_released(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> list[Any]:
            await _put(queue, "room-a", _item("a1"))
            await _put(queue, "room-a", _item("a2"))
            await _put(queue, "room-b", _item("b1"))
            first = await queue.take(lambda: False)
            second = await queue.take(lambda: False)
            assert queue.active_keys() == {"room-a", "room-b"}
            waiting = asyncio.create_task(queue.take(lambda: False))
            await asyncio.sleep(0)
            assert not waiting.done()
            await queue.release("room-a")
            third = await asyncio.wait_for(waiting, 1)
            return [first, second, third]

        taken = asyncio.run(scenario())

        assert [(key, item.texts) for key, item in taken] == [
            ("room-a", ["a1"]), ("room-b", ["b1"]), ("room-a", ["a2"]),
        ]

    def test_taking_the_merge_target_closes_it(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> None:
            await _put(queue, "room", _item("a1"), "alice")
            await queue.take(lambda: False)
            await _put(queue, "room", _item("a2"), "alice")

        asyncio.run(scenario())

        # 已经开始处理的那件不能再往里塞：a2 另起一件。
        assert _texts(queue) == [["a2"]]

    def test_taking_an_older_item_keeps_the_newer_merge_target(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()
        newer = _item("b")

        async def scenario() -> None:
            await _put(queue, "room", _item("a"), "alice")
            await _put(queue, "room", newer, "bob")
            await queue.take(lambda: False)

        asyncio.run(scenario())

        assert queue.open_item("room") is newer

    def test_should_exit_is_checked_before_waiting(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        assert asyncio.run(queue.take(lambda: True)) is None

    def test_wake_all_lets_idle_workers_exit(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()
        stop = SimpleNamespace(now=False)

        async def scenario() -> Any:
            waiting = asyncio.create_task(queue.take(lambda: stop.now))
            await asyncio.sleep(0)
            stop.now = True
            await queue.wake_all()
            return await asyncio.wait_for(waiting, 1)

        assert asyncio.run(scenario()) is None


def _task(task_id: str = "t1", *, entry: bool = True) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, is_user_entry=entry)


def _trigger(user: int | None = 1, **hints: str) -> SimpleNamespace:
    return SimpleNamespace(user=user, platform_data={"_route_hints": hints} if hints else {})


def _candidates(tasks: list[Any], triggers: dict[str, Any], *, is_dm: bool = False, user: int = 1, **hints: str) -> list[str]:
    return [
        task.task_id for task, _ in preempt_candidates(
            tasks,
            trigger_of=lambda task: triggers.get(task.task_id),
            is_dm=is_dm,
            same_sender=lambda trigger: trigger.user == user,
            route_hints=hints,
        )
    ]


class TestPreemptCandidates:
    def test_only_user_entry_tasks_with_an_id_qualify(self) -> None:
        tasks = [_task(""), _task("child", entry=False), _task("t1")]

        assert _candidates(tasks, {"t1": _trigger()}) == ["t1"]

    def test_group_tasks_need_the_same_sender(self) -> None:
        tasks = [_task("mine"), _task("theirs"), _task("unknown")]
        triggers = {"mine": _trigger(1), "theirs": _trigger(2)}

        assert _candidates(tasks, triggers) == ["mine"]

    def test_private_chats_skip_the_sender_check(self) -> None:
        assert _candidates([_task("t1")], {}, is_dm=True) == ["t1"]

    @pytest.mark.parametrize(
        ("previous", "current", "accepted"),
        [
            ({}, {}, True),
            ({"topic_id": "tp1"}, {"topic_id": "tp1"}, True),
            ({"topic_id": "tp1"}, {"topic_id": "tp2"}, False),
            ({}, {"response_action": "task"}, False),
            ({"response_action": "task"}, {"response_action": "task"}, True),
        ],
    )
    def test_the_topic_and_response_action_must_match(
        self, previous: dict[str, str], current: dict[str, str], accepted: bool,
    ) -> None:
        result = _candidates([_task("t1")], {"t1": _trigger(1, **previous)}, **current)

        assert result == (["t1"] if accepted else [])

    def test_candidates_are_yielded_lazily_in_order(self) -> None:
        looked_up: list[str] = []

        def trigger_of(task: Any) -> Any:
            looked_up.append(task.task_id)
            return _trigger()

        candidates = preempt_candidates(
            [_task("a"), _task("b")], trigger_of=trigger_of, is_dm=False,
            same_sender=lambda trigger: True, route_hints={},
        )

        assert next(candidates)[0].task_id == "a"
        assert looked_up == ["a"]

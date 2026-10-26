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

    def test_put_reports_true_for_queued_and_merged_items(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> list[bool]:
            return [
                await queue.put("room", _item("a1"), identity_of=_as("alice"), merge=_merge),
                await queue.put("room", _item("a2"), identity_of=_as("alice"), merge=_merge),
            ]

        assert asyncio.run(scenario()) == [True, True]


class TestDiscard:
    def test_a_discarded_item_is_neither_queued_nor_merged(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> list[bool]:
            kept = await queue.put("room", _item("request"), identity_of=_as("alice"), merge=_merge)
            dropped = await queue.put(
                "room", _item("chat"), identity_of=_as("alice"), merge=_merge,
                discard=lambda queued, item: True,
            )
            return [kept, dropped]

        assert asyncio.run(scenario()) == [True, False]
        assert _texts(queue) == [["request"]]

    def test_only_items_of_the_same_conversation_are_weighed(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()
        weighed: list[tuple[str, str]] = []

        def discard(queued: _Item, item: _Item) -> bool:
            weighed.append((queued.texts[0], item.texts[0]))
            return True

        async def scenario() -> bool:
            await _put(queue, "room-a", _item("a"))
            return await queue.put("room-b", _item("b"), identity_of=_as(None), merge=_merge, discard=discard)

        assert asyncio.run(scenario()) is True
        assert weighed == []
        assert _texts(queue) == [["a"], ["b"]]

    def test_every_waiting_item_is_weighed_not_only_the_merge_target(self) -> None:
        # 正式请求后面又排了别人的一件，合并入口已经换成那件；插话照样得让给排在前面的正式请求。
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> bool:
            await _put(queue, "room", _item("request"), "alice")
            await _put(queue, "room", _item("other"), "bob")
            return await queue.put(
                "room", _item("chat"), identity_of=_as("bob"), merge=_merge,
                discard=lambda queued, item: queued.texts == ["request"],
            )

        assert asyncio.run(scenario()) is False
        assert _texts(queue) == [["request"], ["other"]]

    def test_an_item_already_taken_no_longer_counts(self) -> None:
        queue: ConversationQueue[_Item] = ConversationQueue()

        async def scenario() -> bool:
            await _put(queue, "room", _item("request"))
            await queue.take(lambda: False)
            return await queue.put(
                "room", _item("chat"), identity_of=_as(None), merge=_merge,
                discard=lambda queued, item: True,
            )

        assert asyncio.run(scenario()) is True
        assert _texts(queue) == [["chat"]]


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
        assert _candidates([_task("t1")], {"t1": _trigger(2)}, is_dm=True, user=1) == ["t1"]

    @pytest.mark.parametrize("is_dm", [True, False])
    def test_a_task_without_its_own_trigger_is_never_taken_over(self, is_dm: bool) -> None:
        # 拿会话里别的触发顶上，等于把这条消息注进别人的任务。
        assert _candidates([_task("t1")], {}, is_dm=is_dm) == []

    @pytest.mark.parametrize(
        ("previous", "current", "accepted"),
        [
            ({}, {}, True),
            ({"topic_id": "tp1"}, {"topic_id": "tp1"}, True),
            ({"topic_id": "tp1"}, {"topic_id": "tp2"}, True),
            ({}, {"response_action": "task"}, False),
            ({"response_action": "task"}, {"response_action": "task"}, True),
        ],
    )
    def test_response_action_must_match_but_legacy_topics_are_ignored(
        self, previous: dict[str, str], current: dict[str, str], accepted: bool,
    ) -> None:
        result = _candidates([_task("t1")], {"t1": _trigger(1, **previous)}, **current)

        assert result == (["t1"] if accepted else [])

    @pytest.mark.parametrize(
        ("previous", "current", "accepted"),
        [
            ({"delivery_purpose": "ambient"}, {}, False),
            ({"delivery_purpose": "ambient"}, {"delivery_purpose": "direct"}, False),
            ({"delivery_purpose": "ambient"}, {"delivery_purpose": "ambient"}, True),
            ({"delivery_purpose": "direct"}, {"delivery_purpose": "ambient"}, True),
            ({}, {"delivery_purpose": "direct"}, True),
        ],
    )
    def test_a_running_interjection_never_takes_a_direct_request(
        self, previous: dict[str, str], current: dict[str, str], accepted: bool,
    ) -> None:
        # 那一轮 engine 仍当它是插话，可能选择不发言，接进去的正式请求就没人回了。
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

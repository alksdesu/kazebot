"""适配器契约：任何平台都要过的一组断言，QQ 与只在测试里存在的 fakechat 各跑一遍。

只断言平台无关的事：提交字段与身份、外部平台硬规则、发送防重、审批、上下文重置、提醒回执。文案与协议细节归各平台自己的测试。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _contract_drivers import DRIVERS, ContractDriver, open_driver, registered_fake_platform  # noqa: E402
from _fake_platform import FAULT_HANG, FAULT_LOST_ACK, FAULT_REFUSED  # noqa: E402
from adapters._shared.feature_gateway import FeatureHost, QuietDeliveryDeferred  # noqa: E402
from adapters._shared.send_guard import SendError  # noqa: E402
from clonoth_sdk.callbacks import AdapterCallbacks  # noqa: E402
from clonoth_sdk.state import TriggerInfo  # noqa: E402
from clonoth_sdk.types import DeliveryContext  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_OTHER, CHANNEL_PRIVATE  # noqa: E402
from platforms.sources import classify_operation_source  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_DIGEST = re.compile(r"[0-9a-f]{24}")
APPROVAL_ID = "5f3c2a10-8d4e-4b7a-9c21-0e6f1d2b3a4c"
REMINDER_BINDING = "/v1/reminders/delivery-messages"


@pytest.fixture(params=DRIVERS)
def driver(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ContractDriver]:
    with registered_fake_platform():
        # QQ 插件实例按工作区目录名起模块名，必须是每个用例独有的 tmp_path，否则子模块会串用上一个用例的状态。
        opened = open_driver(request.param, monkeypatch, tmp_path)
        try:
            yield opened
        finally:
            opened.close()


def _protocol_members(protocol: type) -> list[str]:
    return sorted(name for name in vars(protocol) if not name.startswith("_"))


def _decisions(driver: ContractDriver) -> list[tuple[str, str]]:
    return [(decision["approval_id"], decision["decision"]) for decision in driver.client.decisions]


def _texts(driver: ContractDriver, kind: str, target: str) -> list[str]:
    return [delivery.text.strip() for delivery in driver.delivered_to(kind, target)]


async def _group_trigger(driver: ContractDriver, text: str = "a question", *, message_id: str = "101") -> TriggerInfo:
    await driver.group_message(text, sender=driver.member, message_id=message_id, mention_bot=True)
    trigger = driver.session_state.get_trigger(len(driver.client.submissions))
    assert trigger is not None, "a submitted group message registers its trigger"
    return trigger


class TestShape:
    def test_callbacks_implement_every_sdk_callback(self, driver: ContractDriver) -> None:
        missing = [name for name in _protocol_members(AdapterCallbacks) if not callable(getattr(driver.callbacks, name, None))]

        assert missing == []

    def test_the_feature_host_answers_everything_the_gateway_asks(self, driver: ContractDriver) -> None:
        missing = [name for name in _protocol_members(FeatureHost) if not hasattr(driver.feature_host, name)]

        assert missing == []

    def test_the_platform_declares_group_private_and_fallback_channels(self, driver: ContractDriver) -> None:
        hashed_kinds = {channel.kind for channel in driver.platform.channels if channel.hashed}

        assert {CHANNEL_GROUP, CHANNEL_PRIVATE, CHANNEL_OTHER} <= hashed_kinds


class TestInboundSubmission:
    def test_a_group_mention_carries_the_platform_fields(self, driver: ContractDriver) -> None:
        asyncio.run(driver.group_message("hello there", sender=driver.member, message_id="101", mention_bot=True))

        [call] = driver.client.submissions
        channel = driver.channel(CHANNEL_GROUP)
        prefix, _, digest = call["conversation_key"].partition(":")
        assert (call["channel"], prefix) == (channel, channel)
        assert _DIGEST.fullmatch(digest)
        assert call["message_id"] == "101"
        assert call.get("entry_node_pinned", False) is False
        assert "hello there" in call["text"]
        auth = call["platform_auth"]
        assert auth == {
            **auth,
            "platform": driver.platform.id,
            "user_id": driver.member,
            "is_admin": False,
            "group_role": "member",
            "group_scope": call["conversation_key"],
            "bot_scope": driver.bot_account,
        }
        hints = call["route_hints"]
        assert (hints["platform"], hints["channel"], hints["target_type"]) == (driver.platform.id, channel, CHANNEL_GROUP)

    def test_a_private_message_from_an_admin_is_submitted_as_admin(self, driver: ContractDriver) -> None:
        asyncio.run(driver.private_message("hi bot", sender=driver.admin, message_id="102"))

        [call] = driver.client.submissions
        channel = driver.channel(CHANNEL_PRIVATE)
        assert call["channel"] == channel
        assert call["conversation_key"].startswith(f"{channel}:")
        auth = call["platform_auth"]
        assert (auth["platform"], auth["user_id"], auth["is_admin"]) == (driver.platform.id, driver.admin, True)
        assert (auth["group_role"], auth["group_scope"]) == ("", "")
        assert call["route_hints"]["target_type"] == CHANNEL_PRIVATE

    def test_a_group_message_without_a_trigger_is_not_submitted(self, driver: ContractDriver) -> None:
        asyncio.run(driver.group_message("just chatting", sender=driver.member, message_id="101"))

        assert driver.client.submissions == []

    def test_conversation_keys_are_stable_per_chat_and_distinct_across_chats(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await driver.group_message("one", sender=driver.member, message_id="101", mention_bot=True)
            await driver.group_message("two", sender=driver.admin, message_id="102", mention_bot=True)
            await driver.group_message("three", sender=driver.member, message_id="103", room=driver.other_room, mention_bot=True)
            await driver.private_message("four", sender=driver.admin, message_id="104")

        asyncio.run(scenario())

        same_room, same_room_again, other_room, private = (call["conversation_key"] for call in driver.client.submissions)
        assert same_room == same_room_again
        assert len({same_room, other_room, private}) == 3

    def test_earlier_room_messages_reach_the_model_as_context(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await driver.group_message("marker-alpha", sender=driver.admin, message_id="101")
            await driver.group_message("what did they say", sender=driver.member, message_id="102", mention_bot=True)

        asyncio.run(scenario())

        [call] = driver.client.submissions
        assert "marker-alpha" in call["text"]
        assert call["text"].count("what did they say") == 1

    def test_the_model_never_sees_real_ids(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await driver.group_message(f"I am {driver.admin}", sender=driver.admin, message_id="101")
            await driver.group_message(
                f"what did {driver.admin} say in {driver.room}", sender=driver.member, message_id="102", mention_bot=True,
            )

        asyncio.run(scenario())

        [call] = driver.client.submissions
        visible = "\n".join([call["text"], call["conversation_key"], *call["memory_hints"]["subjects"]])
        leaked = sorted(real for real in (driver.admin, driver.member, driver.room) if real in visible)
        assert leaked == []

    def test_memory_hints_name_the_sender_first_by_alias(self, driver: ContractDriver) -> None:
        asyncio.run(driver.group_message("hello", sender=driver.member, message_id="101", mention_bot=True))

        [call] = driver.client.submissions
        subjects = call["memory_hints"]["subjects"]
        assert subjects and subjects[0] not in driver.private_ids
        assert re.fullmatch(r"User[A-Z]+", subjects[0])


class TestExternalPlatformRules:
    @staticmethod
    def _verdict(workspace: Path, call: dict[str, Any], op: str, parameters: dict[str, Any]) -> Any:
        root = workspace / "supervisor"
        root.mkdir(parents=True, exist_ok=True)
        state = SupervisorState(
            workspace_root=root,
            eventlog=EventLog(root / "data" / "events.jsonl", run_id="run-contract"),
            policy=PolicyEngine(workspace_root=root),
        )
        session_id = state.get_or_create_session(channel=call["channel"], conversation_key=call["conversation_key"])
        return state.request_operation(
            session_id=session_id, op=op, parameters={**parameters, "platform_auth": call["platform_auth"]},
        )

    def test_a_submission_is_classified_as_coming_from_its_platform(self, driver: ContractDriver) -> None:
        asyncio.run(driver.group_message("hello", sender=driver.member, message_id="101", mention_bot=True))

        [call] = driver.client.submissions
        source = classify_operation_source(
            conversation_key=call["conversation_key"], channel=call["channel"], platform=call["platform_auth"]["platform"],
        )
        assert (source.external, source.label, source.declared) == (True, driver.platform.id, True)

    @pytest.mark.parametrize("op,parameters", [
        ("execute_command", {"command": "python -c 'print(1)'"}),
        ("write_file", {"path": "notes.txt"}),
        ("read_file", {"path": "README.md"}),
        ("restart", {}),
    ])
    def test_a_member_is_held_to_the_hard_rules(
        self, driver: ContractDriver, tmp_path: Path, op: str, parameters: dict[str, Any],
    ) -> None:
        asyncio.run(driver.group_message("do it", sender=driver.member, message_id="101", mention_bot=True))

        [call] = driver.client.submissions
        verdict = self._verdict(tmp_path, call, op, parameters)
        assert verdict.safety_level == SafetyLevel.deny
        assert verdict.reason.startswith(f"{driver.platform.id} non-admin users")

    def test_an_admin_runs_ordinary_commands_without_approval(self, driver: ContractDriver, tmp_path: Path) -> None:
        asyncio.run(driver.private_message("run it", sender=driver.admin, message_id="101"))

        [call] = driver.client.submissions
        verdict = self._verdict(tmp_path, call, "execute_command", {"command": "echo hi"})
        assert verdict.safety_level == SafetyLevel.auto


class TestSendGuard:
    @staticmethod
    def _context(trigger: TriggerInfo, key: str) -> DeliveryContext:
        return DeliveryContext(
            idempotency_key=key, conversation_key=trigger.conversation_key, source_inbound_seq=trigger.inbound_seq,
        )

    def test_a_lost_acknowledgement_is_never_resent(self, driver: ContractDriver) -> None:
        async def scenario() -> tuple[SendError, SendError]:
            trigger = await _group_trigger(driver)
            context = self._context(trigger, "reply-lost-ack")
            driver.fail_next_send(FAULT_LOST_ACK)
            with pytest.raises(SendError) as first:
                await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            with pytest.raises(SendError) as retry:
                await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            return first.value, retry.value

        first, retry = asyncio.run(scenario())

        # 回执不明就进死信等人确认：自动重试同样报不明，不能赌一把再发一条。
        assert [(error.retryable, error.ambiguous_ack) for error in (first, retry)] == [(False, True), (False, True)]
        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["the answer"]

    def test_a_send_cut_off_by_shutdown_is_never_resent(self, driver: ContractDriver) -> None:
        async def cut_off() -> tuple[TriggerInfo, DeliveryContext]:
            trigger = await _group_trigger(driver)
            context = self._context(trigger, "reply-cut-off")
            driver.fail_next_send(FAULT_HANG)
            asyncio.get_running_loop().create_task(
                driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context),
            )
            for _ in range(1000):
                if driver.delivered_to(CHANNEL_GROUP, driver.room):
                    break
                await asyncio.sleep(0)
            return trigger, context

        # 事件循环在平台还没回话时收尾，相当于进程在领取之后、确认之前被关停。
        trigger, context = asyncio.run(cut_off())

        async def after_restart() -> SendError:
            with pytest.raises(SendError) as retry:
                await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            return retry.value

        retry = asyncio.run(after_restart())

        assert (retry.retryable, retry.ambiguous_ack) == (False, True)
        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["the answer"]

    def test_a_send_that_never_reached_the_platform_goes_out_on_retry(self, driver: ContractDriver) -> None:
        async def scenario() -> SendError:
            trigger = await _group_trigger(driver)
            context = self._context(trigger, "reply-refused")
            driver.fail_next_send(FAULT_REFUSED)
            with pytest.raises(SendError) as first:
                await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            return first.value

        failure = asyncio.run(scenario())

        assert (failure.retryable, failure.definitely_not_sent) == (True, True)
        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["the answer"]

    def test_a_replayed_delivery_is_not_sent_twice(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver)
            context = self._context(trigger, "reply-replayed")
            await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)
            await driver.callbacks.send_reply(trigger, "the answer", [], delivery_context=context)

        asyncio.run(scenario())

        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["the answer"]

    def test_distinct_deliveries_of_the_same_text_both_go_out(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver)
            await driver.callbacks.send_reply(trigger, "ok", [], delivery_context=self._context(trigger, "reply-a"))
            await driver.callbacks.send_reply(trigger, "ok", [], delivery_context=self._context(trigger, "reply-b"))

        asyncio.run(scenario())

        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["ok", "ok"]


class TestApprovalFlow:
    @staticmethod
    async def _request(driver: ContractDriver) -> str:
        await driver.group_message("please run it", sender=driver.member, message_id="101", mention_bot=True)
        conversation_key = driver.client.submissions[-1]["conversation_key"]
        await driver.callbacks.show_approval_ui(
            APPROVAL_ID, "execute_command", {"command": "ls"}, conversation_key=conversation_key, session_id="session-1",
        )
        return conversation_key

    def test_every_admin_gets_a_card_and_the_chat_hears_it_is_pending(self, driver: ContractDriver) -> None:
        asyncio.run(self._request(driver))

        cards = driver.delivered_to(CHANNEL_PRIVATE, driver.admin)
        assert len(cards) == 1 and APPROVAL_ID in cards[0].text
        assert driver.delivered_to(CHANNEL_PRIVATE, driver.member) == []
        assert driver.delivered_to(CHANNEL_GROUP, driver.room)
        assert driver.client.decisions == []

    def test_an_admin_command_decides_exactly_once(self, driver: ContractDriver) -> None:
        async def scenario() -> tuple[list[str], list[str]]:
            await self._request(driver)
            command = f"/approval approve {APPROVAL_ID}"
            first = await driver.private_message(command, sender=driver.admin, message_id="201")
            again = await driver.private_message(command, sender=driver.admin, message_id="202")
            return first, again

        first, again = asyncio.run(scenario())

        assert _decisions(driver) == [(APPROVAL_ID, "allow")]
        assert any(APPROVAL_ID in text for text in first)
        assert again, "a second decision is answered, not silently dropped"

    def test_a_bare_verb_quoting_the_card_decides(self, driver: ContractDriver) -> None:
        async def scenario() -> list[str]:
            await self._request(driver)
            [card] = driver.delivered_to(CHANNEL_PRIVATE, driver.admin)
            return await driver.private_message("deny", sender=driver.admin, message_id="201", reply_to=card.message_id)

        replies = asyncio.run(scenario())

        assert _decisions(driver) == [(APPROVAL_ID, "deny")]
        assert replies

    def test_a_member_cannot_decide(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await self._request(driver)
            await driver.private_message(f"/approval approve {APPROVAL_ID}", sender=driver.member, message_id="201")

        asyncio.run(scenario())

        assert driver.client.decisions == []

    def test_a_rejected_submission_leaves_the_approval_open_for_a_retry(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await self._request(driver)
            command = f"/approval approve {APPROVAL_ID}"
            driver.client.approve_result = False
            await driver.private_message(command, sender=driver.admin, message_id="201")
            driver.client.approve_result = True
            await driver.private_message(command, sender=driver.admin, message_id="202")

        asyncio.run(scenario())

        assert _decisions(driver) == [(APPROVAL_ID, "allow"), (APPROVAL_ID, "allow")]

    def test_without_admins_the_request_is_denied_at_once(self, driver: ContractDriver) -> None:
        driver.remove_admins()

        asyncio.run(self._request(driver))

        assert _decisions(driver) == [(APPROVAL_ID, "deny")]
        assert driver.delivered_to(CHANNEL_PRIVATE, driver.admin) == []


class TestContextReset:
    def test_clear_drops_the_old_conversation_from_the_next_prompt(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await driver.group_message("marker-alpha", sender=driver.admin, message_id="101")
            await driver.group_message("first question", sender=driver.member, message_id="102", mention_bot=True)
            conversation_key = driver.client.submissions[-1]["conversation_key"]
            cleaned = driver.session_state.cleanup_for_context_reset(conversation_key)
            await driver.callbacks.on_context_reset(conversation_key, "clear", cleaned)
            await driver.group_message("second question", sender=driver.member, message_id="103", mention_bot=True)

        asyncio.run(scenario())

        first, second = (call["text"] for call in driver.client.submissions)
        assert "marker-alpha" in first
        assert "marker-alpha" not in second and "first question" not in second
        assert "second question" in second

    def test_compact_resends_the_recent_window(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            await driver.group_message("marker-alpha", sender=driver.admin, message_id="101")
            await driver.group_message("first question", sender=driver.member, message_id="102", mention_bot=True)
            driver.session_state.accept_watermark(1)
            await driver.group_message("second question", sender=driver.member, message_id="103", mention_bot=True)
            driver.session_state.accept_watermark(2)
            conversation_key = driver.client.submissions[-1]["conversation_key"]
            await driver.callbacks.on_context_reset(conversation_key, "compact", [])
            await driver.group_message("third question", sender=driver.member, message_id="104", mention_bot=True)

        asyncio.run(scenario())

        first, second, third = (call["text"] for call in driver.client.submissions)
        assert "marker-alpha" in first
        assert "marker-alpha" not in second, "delivered lines are not sent twice"
        assert "marker-alpha" in third and "second question" in third

    def test_a_delivered_reply_becomes_part_of_the_next_prompt(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver, "first question", message_id="101")
            context = DeliveryContext(
                idempotency_key="fresh-answer", conversation_key=trigger.conversation_key,
                source_inbound_seq=trigger.inbound_seq,
            )
            await driver.callbacks.send_reply(trigger, "fresh answer", [], delivery_context=context)
            await driver.group_message("second question", sender=driver.member, message_id="102", mention_bot=True)

        asyncio.run(scenario())

        assert "fresh answer" in driver.client.submissions[-1]["text"]

    def test_a_reply_in_flight_at_clear_is_delivered_but_not_remembered(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver, "first question", message_id="101")
            conversation_key = trigger.conversation_key
            cleaned = driver.session_state.cleanup_for_context_reset(conversation_key)
            await driver.callbacks.on_context_reset(conversation_key, "clear", cleaned)
            late = DeliveryContext(
                idempotency_key="late-answer", conversation_key=conversation_key,
                source_inbound_seq=trigger.inbound_seq,
            )
            await driver.callbacks.send_to_channel(conversation_key, "late answer", [], delivery_context=late)
            await driver.group_message("second question", sender=driver.member, message_id="102", mention_bot=True)

        asyncio.run(scenario())

        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["late answer"]
        assert "late answer" not in driver.client.submissions[-1]["text"]


class TestReminderDelivery:
    @staticmethod
    def _reminder(conversation_key: str, *, event: str = "reminder-event-1") -> DeliveryContext:
        return DeliveryContext(
            idempotency_key=event, conversation_key=conversation_key, purpose="ambient",
            feature_delivery_id="reminder:d-1",
        )

    def test_a_reminder_is_sent_once_and_bound_to_its_message(self, driver: ContractDriver) -> None:
        async def scenario() -> str:
            trigger = await _group_trigger(driver, "remind me later")
            context = self._reminder(trigger.conversation_key)
            await driver.callbacks.send_to_channel(trigger.conversation_key, "time to stretch", [], delivery_context=context)
            await driver.callbacks.send_to_channel(trigger.conversation_key, "time to stretch", [], delivery_context=context)
            return trigger.conversation_key

        conversation_key = asyncio.run(scenario())

        [sent] = driver.delivered_to(CHANNEL_GROUP, driver.room)
        assert sent.text.strip() == "time to stretch"
        binding = {
            "delivery_id": "reminder:d-1", "scope": conversation_key,
            "bot_scope": driver.bot_account, "message_id": sent.message_id,
        }
        assert [request["body"] for request in driver.client.requests_to(REMINDER_BINDING)] == [binding, binding]

    def test_an_ordinary_delivery_binds_nothing(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver)
            context = DeliveryContext(idempotency_key="plain-1", conversation_key=trigger.conversation_key)
            await driver.callbacks.send_to_channel(trigger.conversation_key, "plain text", [], delivery_context=context)

        asyncio.run(scenario())

        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["plain text"]
        assert driver.client.requests_to(REMINDER_BINDING) == []

    def test_a_quiet_room_defers_the_reminder_but_not_a_direct_reply(self, driver: ContractDriver) -> None:
        async def scenario() -> None:
            trigger = await _group_trigger(driver)
            driver.client.quiet = {"mode": "quiet", "expires_at": time.time() + 600}
            with pytest.raises(QuietDeliveryDeferred):
                await driver.callbacks.send_to_channel(
                    trigger.conversation_key, "time to stretch", [],
                    delivery_context=self._reminder(trigger.conversation_key),
                )
            direct = DeliveryContext(idempotency_key="direct-1", conversation_key=trigger.conversation_key)
            await driver.callbacks.send_to_channel(trigger.conversation_key, "direct answer", [], delivery_context=direct)

        asyncio.run(scenario())

        assert _texts(driver, CHANNEL_GROUP, driver.room) == ["direct answer"]
        assert driver.client.requests_to(REMINDER_BINDING) == []

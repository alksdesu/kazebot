from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class PendingBurst:
    item: Any
    future: asyncio.Future
    started: float
    last: float
    deadline: float
    wakeup: asyncio.Event
    identity: Any


class ConversationCoordinator:
    def __init__(self):
        self.messages: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self.policies: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.bursts: dict[str, PendingBurst] = {}
        self.replies: dict[str, deque[float]] = defaultdict(deque)

    async def policy(self, client, actor, *, refresh=False):
        scope = actor["scope"]
        cached = self.policies.get(scope)
        if cached and not refresh and time.monotonic() - cached[0] < 3:
            return cached[1]
        state = await client.request_feature("GET", "/v1/community/state", actor=actor)
        self.policies[scope] = (time.monotonic(), state)
        self.policies.move_to_end(scope)
        while len(self.policies) > 512:
            self.policies.popitem(last=False)
        return state

    def invalidate(self, scope):
        self.policies.pop(scope, None)

    def clear_context(self, scope):
        for key in [key for key in self.messages if key[0] == scope]:
            self.messages.pop(key, None)
        burst = self.bursts.pop(scope, None)
        if burst is not None:
            if not burst.future.done():
                burst.future.set_result(False)
            burst.wakeup.set()

    @staticmethod
    def quiet_blocks(policy: dict, *, direct=False, control=False) -> bool:
        quiet = policy.get("quiet") or {}
        if control or float(quiet.get("expires_at", 0)) <= time.time():
            return False
        return quiet.get("mode") == "silent" or not direct

    def metadata(self, actor):
        return self.messages.get((actor["scope"], str(actor.get("message_id") or "")), {})

    async def prepare(self, client, actor, text, *, reply_ref="", sender_name=""):
        key = (actor["scope"], str(actor.get("message_id") or ""))
        if key in self.messages:
            return self.messages[key]
        policy = await self.policy(client, actor)
        topic = {}
        if policy.get("settings", {}).get("topic_enabled"):
            topic = await client.request_feature("POST", "/v1/community/messages", actor=actor, body={
                "text": text, "message_id": actor.get("message_id"), "reply_ref": reply_ref, "sender_name": sender_name,
            })
        result = {"policy": policy, "topic": topic, "reply_ref": reply_ref, "action": "reply", "direct": False}
        self.messages[key] = result
        while len(self.messages) > 1024:
            self.messages.popitem(last=False)
        burst = self.bursts.get(actor["scope"])
        if burst and str(getattr(burst.item.event, "user_id", "")) != str(actor.get("user_id", "")):
            burst.deadline = time.monotonic()
            burst.wakeup.set()
        return result

    def can_continue(self, actor, reply_ref="") -> bool:
        burst = self.bursts.get(actor["scope"])
        if not burst or str(getattr(burst.item.event, "user_id", "")) != str(actor.get("user_id", "")):
            return False
        refs = burst.item.platform_updates.get("_route_hints", {}).get("source_message_refs", [])
        return not reply_ref or reply_ref in {str(ref) for ref in refs} | {str(getattr(burst.item.event, "message_id", ""))}

    def allow_response(self, scope, settings, *, direct=False):
        if direct or not settings.get("response_policy_enabled"):
            return True
        now = time.monotonic()
        times = self.replies[scope]
        while times and now - times[0] >= 60:
            times.popleft()
        if len(times) >= settings.get("reply_budget_per_minute", 6):
            return False
        times.append(now)
        return True

    async def submit(self, item, settings, identity, submit: Callable[[Any], Awaitable[bool]], merge: Callable[[Any, Any], None]):
        window = float(settings.get("merge_window_sec", 0))
        if window <= 0 or not identity or item.entry_node_id or item.user_text.lstrip().startswith(("/", "／")):
            return await submit(item)
        scope = item.stable_conversation_key
        while previous := self.bursts.get(scope):
            now = time.monotonic()
            if previous.identity == identity and now < previous.deadline:
                merge(previous.item, item)
                previous.last = now
                previous.deadline = min(now + window, previous.started + float(settings.get("merge_max_wait_sec", 4)))
                previous.wakeup.set()
                return await asyncio.shield(previous.future)
            previous.deadline = now
            previous.wakeup.set()
            await asyncio.shield(previous.future)
        now = time.monotonic()
        future = asyncio.get_running_loop().create_future()
        burst = PendingBurst(item, future, now, now, now + window, asyncio.Event(), identity)
        self.bursts[scope] = burst

        async def flush():
            try:
                while True:
                    if future.done():
                        return
                    remaining = burst.deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    burst.wakeup.clear()
                    try:
                        await asyncio.wait_for(burst.wakeup.wait(), remaining)
                    except asyncio.TimeoutError:
                        break
                if self.bursts.get(scope) is burst:
                    self.bursts.pop(scope, None)
                future.set_result(await submit(burst.item))
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)
            finally:
                if self.bursts.get(scope) is burst:
                    self.bursts.pop(scope, None)

        asyncio.create_task(flush())
        return await asyncio.shield(future)

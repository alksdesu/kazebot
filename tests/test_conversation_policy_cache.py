from __future__ import annotations

import asyncio
import gc
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


MODULE_NAME = "_conversation_policy_cache_test"
SPEC = importlib.util.spec_from_file_location(
    MODULE_NAME,
    Path(__file__).resolve().parents[1] / "adapters/onebot/conversation_coordinator.py",
)
coordinator_module = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = coordinator_module
SPEC.loader.exec_module(coordinator_module)
ConversationCoordinator = coordinator_module.ConversationCoordinator


class ControlledClient:
    def __init__(self):
        self.requests = []

    async def request_feature(self, method, path, *, actor):
        assert method == "GET"
        assert path == "/v1/community/state"
        future = asyncio.get_running_loop().create_future()
        self.requests.append((dict(actor), future))
        return await future

    async def wait_requests(self, count):
        for _ in range(20):
            if len(self.requests) >= count:
                return
            await asyncio.sleep(0)
        assert len(self.requests) >= count


def test_concurrent_refreshes_share_one_request():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        first = asyncio.create_task(coordinator.policy(client, actor, refresh=True))
        await client.wait_requests(1)
        second = asyncio.create_task(coordinator.policy(client, actor, refresh=True))
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(client.requests) == 1
        state = {"settings": {"topic_enabled": True}, "quiet": {}}
        client.requests[0][1].set_result(state)
        assert await first == await second == state

    asyncio.run(scenario())


def test_invalidated_request_is_refetched_before_returning_to_waiter():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(1)
        coordinator.invalidate(actor["scope"])
        client.requests[0][1].set_result({"settings": {"topic_enabled": False}, "quiet": {}})
        await client.wait_requests(2)
        assert actor["scope"] not in coordinator.policies
        state = {"settings": {"topic_enabled": True}, "quiet": {}}
        client.requests[1][1].set_result(state)
        assert await waiter == state
        assert coordinator.policies[actor["scope"]][1] == state

    asyncio.run(scenario())


def test_cache_ttl_expires_at_three_seconds(monkeypatch):
    async def scenario():
        now = [100.0]
        monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
        coordinator = ConversationCoordinator()
        client = SimpleNamespace(request_feature=AsyncMock(side_effect=[{"version": 1}, {"version": 2}]))
        actor = {"scope": "qq_private:one"}
        assert await coordinator.policy(client, actor) == {"version": 1}
        now[0] = 102.999
        assert await coordinator.policy(client, actor) == {"version": 1}
        assert client.request_feature.await_count == 1
        now[0] = 103.0
        assert await coordinator.policy(client, actor) == {"version": 2}
        assert client.request_feature.await_count == 2

    asyncio.run(scenario())


def test_refresh_bypasses_fresh_cache_and_preserves_state_shape():
    async def scenario():
        coordinator = ConversationCoordinator()
        states = [
            {"settings": {"topic_enabled": False}, "quiet": {"mode": "silent", "expires_at": 1000}},
            {"settings": {"topic_enabled": True}, "quiet": {}, "guide": {"rules": "test"}},
        ]
        client = SimpleNamespace(request_feature=AsyncMock(side_effect=states))
        actor = {"scope": "qq_group:one"}
        assert await coordinator.policy(client, actor) is states[0]
        assert await coordinator.policy(client, actor, refresh=True) is states[1]
        assert await coordinator.policy(client, actor) is states[1]
        assert client.request_feature.await_count == 2

    asyncio.run(scenario())


def test_unexpired_cache_is_available_while_refresh_is_pending():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        coordinator.policies[actor["scope"]] = (time.monotonic(), {"version": 1})
        refreshed = asyncio.create_task(coordinator.policy(client, actor, refresh=True))
        await client.wait_requests(1)
        assert await coordinator.policy(client, actor) == {"version": 1}
        assert len(client.requests) == 1
        client.requests[0][1].set_result({"version": 2})
        assert await refreshed == {"version": 2}

    asyncio.run(scenario())


def test_invalidated_waiters_share_only_one_replacement_request():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiters = [asyncio.create_task(coordinator.policy(client, actor, refresh=True)) for _ in range(30)]
        await client.wait_requests(1)
        for _ in range(10):
            coordinator.invalidate(actor["scope"])
        assert len(client.requests) == 1
        client.requests[0][1].set_result({"version": 1})
        await client.wait_requests(2)
        assert len(client.requests) == 2
        client.requests[1][1].set_result({"version": 2})
        assert await asyncio.gather(*waiters) == [{"version": 2}] * 30
        assert not coordinator._policy_requests

    asyncio.run(scenario())


def test_repeated_invalidation_fails_after_one_replacement_without_stale_cache():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        for index in range(2):
            await client.wait_requests(index + 1)
            coordinator.invalidate(actor["scope"])
            client.requests[index][1].set_result({"version": index})
        with pytest.raises(RuntimeError, match="changed during refresh"):
            await waiter
        assert not coordinator.policies
        assert not coordinator._policy_requests
        assert len(client.requests) == 2
        retry = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(3)
        client.requests[2][1].set_result({"version": 3})
        assert await retry == {"version": 3}

    asyncio.run(scenario())


def test_invalidate_after_request_finishes_before_waiter_resumes():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(1)
        client.requests[0][1].set_result({"version": 1})
        await asyncio.sleep(0)
        assert actor["scope"] in coordinator.policies
        assert actor["scope"] not in coordinator._policy_requests
        assert not waiter.done()
        coordinator.invalidate(actor["scope"])
        await client.wait_requests(2)
        client.requests[1][1].set_result({"version": 2})
        assert await waiter == {"version": 2}

    asyncio.run(scenario())


def test_invalidation_is_scope_local_with_independent_requests():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        first = asyncio.create_task(coordinator.policy(client, {"scope": "qq_group:one"}))
        second = asyncio.create_task(coordinator.policy(client, {"scope": "qq_private:two"}))
        await client.wait_requests(2)
        coordinator.invalidate("qq_group:one")
        client.requests[1][1].set_result({"scope": "two"})
        assert await second == {"scope": "two"}
        client.requests[0][1].set_result({"scope": "one", "version": 1})
        await client.wait_requests(3)
        assert client.requests[2][0]["scope"] == "qq_group:one"
        client.requests[2][1].set_result({"scope": "one", "version": 2})
        assert await first == {"scope": "one", "version": 2}
        assert await coordinator.policy(client, {"scope": "qq_private:two"}) == {"scope": "two"}

    asyncio.run(scenario())


def test_identical_scopes_do_not_share_cache_between_coordinators():
    async def scenario():
        first = ConversationCoordinator()
        second = ConversationCoordinator()
        client = SimpleNamespace(request_feature=AsyncMock(side_effect=[{"instance": 1}, {"instance": 2}]))
        actor = {"scope": "qq_group:one"}
        assert await first.policy(client, actor) == {"instance": 1}
        assert await second.policy(client, actor) == {"instance": 2}
        first.invalidate(actor["scope"])
        assert await second.policy(client, actor) == {"instance": 2}
        assert not first.policies

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidated", [False, True])
def test_failed_shared_request_propagates_without_automatic_retry(invalidated):
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiters = [asyncio.create_task(coordinator.policy(client, actor)) for _ in range(2)]
        await client.wait_requests(1)
        if invalidated:
            coordinator.invalidate(actor["scope"])
        error = ValueError("service unavailable")
        client.requests[0][1].set_exception(error)
        assert await asyncio.gather(*waiters, return_exceptions=True) == [error, error]
        assert not coordinator.policies
        assert not coordinator._policy_requests
        assert len(client.requests) == 1
        retry = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(2)
        client.requests[1][1].set_result({"version": 2})
        assert await retry == {"version": 2}

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidated", [False, True])
def test_failed_refresh_never_repopulates_an_invalidated_cached_policy(invalidated):
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        coordinator.policies[actor["scope"]] = (time.monotonic(), {"version": 1})
        waiter = asyncio.create_task(coordinator.policy(client, actor, refresh=True))
        await client.wait_requests(1)
        if invalidated:
            coordinator.invalidate(actor["scope"])
        client.requests[0][1].set_exception(ValueError("unavailable"))
        with pytest.raises(ValueError, match="unavailable"):
            await waiter
        if invalidated:
            assert actor["scope"] not in coordinator.policies
        else:
            assert await coordinator.policy(client, actor) == {"version": 1}
        assert not coordinator._policy_requests

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_request():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        cancelled = asyncio.create_task(coordinator.policy(client, actor))
        surviving = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not client.requests[0][1].cancelled()
        client.requests[0][1].set_result({"version": 1})
        assert await surviving == {"version": 1}
        assert not coordinator._policy_requests

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_request_settles_without_unhandled_exception_when_all_waiters_cancel(failed):
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        request_task = coordinator._policy_requests[actor["scope"]].task
        if failed:
            client.requests[0][1].set_exception(ValueError("service unavailable"))
        else:
            client.requests[0][1].set_result({"version": 1})
        for _ in range(5):
            await asyncio.sleep(0)
        assert request_task.done()
        assert not coordinator._policy_requests
        del request_task
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
        assert (actor["scope"] in coordinator.policies) is not failed

    asyncio.run(scenario())


def test_cancelled_underlying_request_cleans_scope_and_allows_next_attempt():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one"}
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(1)
        coordinator._policy_requests[actor["scope"]].task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not coordinator.policies
        assert not coordinator._policy_requests
        retry = asyncio.create_task(coordinator.policy(client, actor))
        await client.wait_requests(2)
        client.requests[1][1].set_result({"version": 2})
        assert await retry == {"version": 2}

    asyncio.run(scenario())


def test_cache_is_bounded_and_recently_read_entries_are_retained():
    async def scenario():
        coordinator = ConversationCoordinator()

        async def request(_method, _path, *, actor):
            return {"scope": actor["scope"]}

        client = SimpleNamespace(request_feature=request)
        for index in range(512):
            await coordinator.policy(client, {"scope": str(index)})
        await coordinator.policy(client, {"scope": "0"})
        await coordinator.policy(client, {"scope": "512"})
        assert len(coordinator.policies) == 512
        assert "0" in coordinator.policies
        assert "1" not in coordinator.policies
        assert not coordinator._policy_requests
        for index in range(1024):
            coordinator.invalidate(str(index))
        assert not coordinator.policies
        assert not coordinator._policy_requests

    asyncio.run(scenario())


def test_active_request_limit_rejects_new_scopes_without_evicting_inflight_work():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        waiters = [asyncio.create_task(coordinator.policy(client, {"scope": str(index)})) for index in range(512)]
        await client.wait_requests(512)
        with pytest.raises(RuntimeError, match="Too many"):
            await coordinator.policy(client, {"scope": "overflow"})
        assert len(client.requests) == len(coordinator._policy_requests) == 512
        same_scope = asyncio.create_task(coordinator.policy(client, {"scope": "0"}, refresh=True))
        client.requests[0][1].set_result({"scope": "0"})
        assert await same_scope == await waiters[0] == {"scope": "0"}
        next_scope = asyncio.create_task(coordinator.policy(client, {"scope": "overflow"}))
        await client.wait_requests(513)
        assert len(coordinator._policy_requests) == 512
        for actor, response in client.requests[1:]:
            response.set_result({"scope": actor["scope"]})
        await asyncio.gather(*waiters[1:], next_scope)
        assert len(coordinator.policies) == 512
        assert not coordinator._policy_requests

    asyncio.run(scenario())


def test_actor_is_snapshotted_before_shared_fetch_starts():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        actor = {"scope": "qq_group:one", "user_id": "original"}
        waiter = asyncio.create_task(coordinator.policy(client, actor))
        await asyncio.sleep(0)
        actor["scope"] = "qq_group:two"
        actor["user_id"] = "changed"
        await client.wait_requests(1)
        assert client.requests[0][0] == {"scope": "qq_group:one", "user_id": "original"}
        client.requests[0][1].set_result({"version": 1})
        assert await waiter == {"version": 1}
        assert "qq_group:one" in coordinator.policies
        assert "qq_group:two" not in coordinator.policies

    asyncio.run(scenario())


def test_eviction_of_old_cache_does_not_remove_an_active_refresh():
    async def scenario():
        coordinator = ConversationCoordinator()
        client = ControlledClient()
        now = time.monotonic()
        for index in range(512):
            coordinator.policies[str(index)] = (now, {"scope": str(index), "version": 1})
        refreshing = asyncio.create_task(coordinator.policy(client, {"scope": "0"}, refresh=True))
        added = asyncio.create_task(coordinator.policy(client, {"scope": "512"}))
        await client.wait_requests(2)
        client.requests[1][1].set_result({"scope": "512"})
        assert await added == {"scope": "512"}
        assert "0" not in coordinator.policies
        assert "0" in coordinator._policy_requests
        joined = asyncio.create_task(coordinator.policy(client, {"scope": "0"}))
        client.requests[0][1].set_result({"scope": "0", "version": 2})
        assert await refreshing == await joined == {"scope": "0", "version": 2}
        assert len(client.requests) == 2
        assert len(coordinator.policies) == 512
        assert not coordinator._policy_requests

    asyncio.run(scenario())

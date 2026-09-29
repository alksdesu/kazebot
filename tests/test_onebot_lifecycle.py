"""实际登记的生命周期回调必须恢复旧状态并管理后台任务。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402


def test_registered_callbacks_restore_state_and_manage_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "data/cache/onebot_reply_attachments.json"
    cache_path.parent.mkdir(parents=True)
    attachment = tmp_path / "data/attachments/qq_group_saved/history.png"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"historical attachment")
    old_time = time.time() - 2 * 86400
    os.utime(attachment, (old_time, old_time))
    cached = {
        "saved_message": {
            "conversation_key": "qq_group:saved", "sender_id": "10002",
            "created_at": old_time,
            "attachments": [{"type": "image", "path": attachment.relative_to(tmp_path).as_posix()}],
        },
    }
    cache_path.write_text(json.dumps({"version": 1, "reply_attachments": cached}), encoding="utf-8")
    (tmp_path / "data/onebot_plugin_state.json").write_text(json.dumps({
        "real_conversation_keys": {"qq_group:saved": "qq_group:10001"},
        "session_targets": {"saved_session": {"type": "group", "group_id": 10001, "conversation_key": "qq_group:saved"}},
    }), encoding="utf-8")
    (tmp_path / "data/onebot_anon_map.json").write_text(json.dumps({
        "version": 1, "user_next": 1, "group_next": 1,
        "users": {"10002": {"alias": "UserA", "last_seen": old_time}},
        "groups": {"10001": {"alias": "GroupA", "last_seen": old_time}},
    }), encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/qq_user_profiles.yaml").write_text(
        'users:\n  "10002":\n    display_name: 张三\n', encoding="utf-8",
    )
    runtime = load_runtime(monkeypatch, tmp_path, ONEBOT_IMAGE_CACHE_TTL_SECONDS=str(30 * 86400))
    set_live_config(runtime, enable_queue=True, queue_workers=1, enable_forward_bridge=False)
    driver = runtime.common.driver
    assert driver.startup_hooks == [runtime.lifecycle._startup]
    assert driver.bot_connect_hooks == [runtime.lifecycle._on_bot_connect]
    assert driver.shutdown_hooks == [runtime.lifecycle._shutdown]

    async def wait_until_cancelled(*args, **kwargs):
        await asyncio.Event().wait()

    background = {}
    for owner, name in (
        (runtime.lifecycle.EventRouter, "run"),
        (runtime.emotes, "_sticker_receipts_forever"),
        (runtime.lifecycle, "_live_config_reconcile_forever"),
        (runtime.lifecycle, "_attachment_cleanup_forever"),
        (runtime.state._adapter.features, "poll"),
    ):
        background[name] = AsyncMock(side_effect=wait_until_cancelled)
        monkeypatch.setattr(owner, name, background[name])
    monkeypatch.setattr(runtime.emotes, "_reconcile_sticker_receipts", AsyncMock())

    async def exercise() -> None:
        adapter = runtime.state._adapter
        router = None
        tasks = []
        try:
            for callback in driver.startup_hooks:
                await callback()
            await asyncio.sleep(0)

            assert adapter.reply_attachment_cache == cached
            assert adapter.qq_user_profiles == {"10002": {"display_name": "张三"}}
            assert adapter.real_conversation_keys == {"qq_group:saved": "qq_group:10001"}
            assert adapter.session_state.get_session_id("qq_group:saved") == "saved_session"
            assert adapter.aliases.users.alias_of("10002") == "UserA"
            assert adapter.aliases.groups.alias_of("10001") == "GroupA"
            assert runtime.anonymize._resolve_at_alias_to_real("UserA") == "10002"
            assert isinstance(adapter.client, runtime.lifecycle.ClonothClient)
            assert isinstance(adapter.event_router, runtime.lifecycle.EventRouter)
            assert isinstance(adapter.callbacks, runtime.callbacks.TangQiuCallbacks)
            router = adapter.event_router
            client = adapter.client
            close_client = AsyncMock(wraps=client.close)
            monkeypatch.setattr(client, "close", close_client)
            tasks = [
                adapter.router_task, adapter.live_reconcile_task, adapter.attachment_cleanup_task,
                adapter.sticker_receipt_task, adapter.features.poll_task, *adapter.qq_queue_tasks.values(),
            ]
            assert len(tasks) == 6 and all(task is not None and not task.done() for task in tasks)
            for mock in background.values():
                mock.assert_awaited_once()

            for callback in driver.startup_hooks:
                await callback()
            assert adapter.client is client and adapter.event_router is router
            assert adapter.router_task is tasks[0]
            for mock in background.values():
                mock.assert_awaited_once()

            for callback in driver.bot_connect_hooks:
                await callback(SimpleNamespace(self_id="12345"))
            assert adapter.active_bot_scope == "12345"
            assert adapter.reply_attachment_cache == cached
            await runtime.routing._save_reply_attachment_cache()
            assert json.loads(cache_path.read_text(encoding="utf-8"))["reply_attachments"] == cached
            assert attachment.read_bytes() == b"historical attachment"
            assert attachment.stat().st_mtime == old_time
        finally:
            for callback in driver.shutdown_hooks:
                await callback()
            if router is not None:
                router._outbound_store.close()

        close_client.assert_awaited_once()
        assert all(task.done() for task in tasks)
        assert adapter.client is None and adapter.event_router is None and adapter.callbacks is None
        assert adapter.router_task is None and adapter.live_reconcile_task is None
        assert adapter.attachment_cleanup_task is None and adapter.sticker_receipt_task is None
        assert adapter.features.poll_task is None and not adapter.qq_queue_tasks

    asyncio.run(exercise())

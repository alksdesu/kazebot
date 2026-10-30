"""附件缓存 TTL 的下限与清理判定。

清理按 mtime 判定，TTL 太小会把刚下载还没进 prompt 的图删掉，所以最小值夹在 60 秒。
下限只有一份、放在拥有它的 config 里；放宽它又不改清理逻辑就会咬默认配置。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime  # noqa: E402


def _attachment(tmp_path: Path, name: str = "shot.png") -> Path:
    conv_dir = tmp_path / "data" / "attachments" / "qq_group_12345"
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / name
    path.write_bytes(b"png-bytes")
    return path


class TestTheTtlHasAFloor:
    def test_a_zero_ttl_env_is_clamped_to_the_floor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """TTL=0 会被夹到下限；死分支就是靠这个下限死的。"""
        monkeypatch.setenv("ONEBOT_IMAGE_CACHE_TTL_SECONDS", "0")
        runtime = load_runtime(monkeypatch, tmp_path)

        assert runtime.routing.IMAGE_CACHE_TTL_SECONDS == 60


class TestCleanupRespectsTheFloor:
    def test_a_fresh_attachment_survives_a_cleanup_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """哪怕运营者把 TTL 配成 0，刚下载的图也不能在下一轮清理里被删。"""
        monkeypatch.setenv("ONEBOT_IMAGE_CACHE_TTL_SECONDS", "0")
        runtime = load_runtime(monkeypatch, tmp_path)
        path = _attachment(tmp_path)
        fresh = time.time() - 5
        os.utime(path, (fresh, fresh))

        runtime.state._adapter.last_attachment_cleanup_at = 0.0
        runtime.media._cleanup_old_qq_attachments()

        assert path.exists()

    def test_a_file_past_the_ttl_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """超过 TTL 的文件仍然要清掉，否则把清理关掉也能让上一条变绿。"""
        monkeypatch.setenv("ONEBOT_IMAGE_CACHE_TTL_SECONDS", "60")
        runtime = load_runtime(monkeypatch, tmp_path)
        path = _attachment(tmp_path)
        stale = time.time() - 120
        os.utime(path, (stale, stale))

        runtime.state._adapter.last_attachment_cleanup_at = 0.0
        runtime.media._cleanup_old_qq_attachments()

        assert not path.exists()


class TestCleanupRunsOffTheEventLoop:
    def test_inbound_rounds_walk_the_folders_in_a_thread_and_only_one_of_them_cleans(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("ONEBOT_IMAGE_CACHE_TTL_SECONDS", "60")
        runtime = load_runtime(monkeypatch, tmp_path)
        path = _attachment(tmp_path)
        stale = time.time() - 120
        os.utime(path, (stale, stale))
        loop_thread = threading.get_ident()
        walks: list[int] = []
        real_remove = runtime.media._remove_expired_qq_attachments

        def recorded(now: float) -> None:
            walks.append(threading.get_ident())
            real_remove(now)

        monkeypatch.setattr(runtime.media, "_remove_expired_qq_attachments", recorded)
        runtime.state._adapter.last_attachment_cleanup_at = 0.0

        async def two_inbound_rounds() -> None:
            await asyncio.gather(
                runtime.media._cleanup_old_qq_attachments_in_thread(),
                runtime.media._cleanup_old_qq_attachments_in_thread(),
            )

        asyncio.run(two_inbound_rounds())

        assert not path.exists()
        assert len(walks) == 1 and loop_thread not in walks

    def test_the_hourly_cleanup_task_walks_the_folders_in_a_thread(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = load_runtime(monkeypatch, tmp_path)
        loop_thread = threading.get_ident()
        walks: list[int] = []
        monkeypatch.setattr(
            runtime.media, "_remove_expired_qq_attachments", lambda now: walks.append(threading.get_ident()),
        )
        runtime.state._adapter.last_attachment_cleanup_at = 0.0

        async def first_round() -> None:
            task = asyncio.create_task(runtime.lifecycle._attachment_cleanup_forever())
            for _ in range(500):
                if walks:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(first_round())

        assert walks and loop_thread not in walks

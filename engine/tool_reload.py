"""engine worker 跟上 supervisor 的工具重载序号：脚本目录变了当场重扫，MCP 配置变了或有人显式重载就在后台重列。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from toolbox.reload_seqs import ReloadSeqs

logger = logging.getLogger(__name__)

# 每轮主循环都问一次：supervisor 半天不回时宁可这一轮不跟，也不能拖住领任务
_FETCH_TIMEOUT_SEC = 5.0


async def fetch_reload_seqs(http: Any, supervisor_url: str) -> ReloadSeqs | None:
    """问一次现在的序号；问不到、应答不对给 None。"""
    try:
        response = await http.get(f"{supervisor_url}/v1/tools/reload-seq", timeout=_FETCH_TIMEOUT_SEC)
        if response.status_code != 200:
            return None
        return ReloadSeqs.from_payload(response.json())
    except Exception:
        return None


class ToolReloadFollower:
    """主循环每轮调一次 poll()。

    脚本序号变了当场重扫脚本。MCP 序号变了交给后台任务对齐，同一时间只跑一个：跑着的时候又来的变化，等它跑完下一轮接着做。
    看到的序号先记下再动手，做失败了只记一次日志，等下一次变化再来，不每轮重试。
    """

    def __init__(self, registry: Any, http: Any, supervisor_url: str, *, seen: ReloadSeqs | None) -> None:
        self._registry = registry
        self._http = http
        self._supervisor_url = supervisor_url
        self._seen = seen
        self._mcp_wanted: ReloadSeqs | None = None
        self._mcp_task: asyncio.Task[None] | None = None

    async def poll(self) -> int | None:
        """问一次序号并照着做。重扫了脚本返回现在的工具数，否则返回 None。"""
        latest = await fetch_reload_seqs(self._http, self._supervisor_url)
        if latest is None:
            self._start_mcp_sync()
            return None
        seen, self._seen = self._seen, latest
        count: int | None = None
        if latest.scripts_changed_since(seen):
            try:
                count = self._registry.reload()
            except Exception:
                logger.warning("script tools could not be rescanned after reload sequence %s", latest, exc_info=True)
        if latest.mcp_changed_since(seen):
            self._mcp_wanted = latest
        self._start_mcp_sync()
        return count

    def _start_mcp_sync(self) -> None:
        if self._mcp_wanted is None or (self._mcp_task is not None and not self._mcp_task.done()):
            return
        target, self._mcp_wanted = self._mcp_wanted, None
        self._mcp_task = asyncio.create_task(self._sync_mcp(target), name="mcp-tool-sync")

    async def _sync_mcp(self, seqs: ReloadSeqs) -> None:
        try:
            changed = await self._registry.sync_mcp_tools(seqs)
            count = self._registry.mcp_tool_count() if changed else None
        except Exception:
            logger.warning("MCP tools could not follow reload sequence %s", seqs, exc_info=True)
            return
        if count is not None:
            logger.info("MCP tools relisted for reload sequence %s: %d tools", seqs, count)

    async def aclose(self) -> None:
        """退出时收掉还在跑的对齐；被取消的重列一样都不换上，工具表停在原样。"""
        task, self._mcp_task = self._mcp_task, None
        self._mcp_wanted = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

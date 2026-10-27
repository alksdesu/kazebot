"""工具重载序号：supervisor 发、engine 跟。脚本目录、config/mcp.yaml、显式重载各记各的，同一次 supervisor 运行里只增不减。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _counter(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@dataclass(frozen=True)
class ReloadSeqs:
    """run_id 换了说明 supervisor 重启过：序号从头数，两边比大小比不出先后。"""

    run_id: str = ""
    scripts: int = 0
    mcp_config: int = 0
    mcp_reload: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> ReloadSeqs | None:
        """GET /v1/tools/reload-seq 和 POST /v1/tools/reload 的应答；读不出脚本序号给 None。

        只有 seq 的应答也认，run_id 和两个 MCP 序号按空串和 0 算。
        """
        if not isinstance(payload, Mapping):
            return None
        scripts = _counter(payload.get("seq"))
        if scripts is None:
            return None
        return cls(
            run_id=str(payload.get("run_id") or ""),
            scripts=scripts,
            mcp_config=_counter(payload.get("mcp_config_seq")) or 0,
            mcp_reload=_counter(payload.get("mcp_reload_seq")) or 0,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.scripts,
            "mcp_config_seq": self.mcp_config,
            "mcp_reload_seq": self.mcp_reload,
        }

    def scripts_changed_since(self, seen: ReloadSeqs | None) -> bool:
        return seen is None or self.run_id != seen.run_id or self.scripts != seen.scripts

    def mcp_changed_since(self, seen: ReloadSeqs | None) -> bool:
        if seen is None or self.run_id != seen.run_id:
            return True
        return (self.mcp_config, self.mcp_reload) != (seen.mcp_config, seen.mcp_reload)

    def mcp_covered_by(self, applied: ReloadSeqs | None) -> bool:
        """applied 已经对齐过的 MCP 改动包括了这一组：同一次运行里两个 MCP 序号都不比它大。"""
        if applied is None or self.run_id != applied.run_id:
            return False
        return self.mcp_config <= applied.mcp_config and self.mcp_reload <= applied.mcp_reload

    def mcp_reconnect_after(self, applied: ReloadSeqs | None) -> bool:
        """对齐到这一组要不要把 MCP 会话全部重开：applied 之后有过显式重载，或者历史接不上。"""
        if applied is None or self.run_id != applied.run_id:
            return True
        return self.mcp_reload > applied.mcp_reload

    def merged_with(self, other: ReloadSeqs | None) -> ReloadSeqs:
        """两组里新的那些：同一次运行逐项取大，不是同一次运行以 self 为准。"""
        if other is None or other.run_id != self.run_id:
            return self
        return ReloadSeqs(
            run_id=self.run_id,
            scripts=max(self.scripts, other.scripts),
            mcp_config=max(self.mcp_config, other.mcp_config),
            mcp_reload=max(self.mcp_reload, other.mcp_reload),
        )

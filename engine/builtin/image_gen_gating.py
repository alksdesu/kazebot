"""生图渠道门控：跑不通的渠道，工具从模型的可用列表里彻底拿掉。

两个渠道都跑不通时，draw.image_gen 也不参与委派 —— orchestrator 压根看不到它。

判定调 clonoth_runtime.image_tool_available，与工具子进程、提示词注入同源。各判各的
就会出现「提示词说能用，工具却不在列表里」这种对不上的局面。
"""
from __future__ import annotations

from pathlib import Path

from clonoth_runtime import IMAGE_TOOL_SLOTS, image_tool_available

# 受门控的生图工具名集合
IMAGE_GEN_TOOLS = frozenset(IMAGE_TOOL_SLOTS)
# 受门控的委派目标节点
IMAGE_GEN_NODE_ID = "draw.image_gen"

# 对外仍按 gpt / gemini 这两个短名报状态。
_SHORT_NAMES = {"gpt": "gpt_image_2", "gemini": "gemini_image"}


def image_channel_availability(workspace_root: Path | str) -> dict[str, bool]:
    """返回 {'gpt': bool, 'gemini': bool}。"""
    root = Path(workspace_root)
    return {short: image_tool_available(root, tool) for short, tool in _SHORT_NAMES.items()}


def disabled_image_tools(workspace_root: Path | str) -> set[str]:
    """应当从可用工具列表里移除的生图工具名。"""
    root = Path(workspace_root)
    return {tool for tool in IMAGE_GEN_TOOLS if not image_tool_available(root, tool)}


def any_image_channel_enabled(workspace_root: Path | str) -> bool:
    root = Path(workspace_root)
    return any(image_tool_available(root, tool) for tool in IMAGE_GEN_TOOLS)

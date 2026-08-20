"""绘图门控：跑不通的渠道，工具从模型的可用列表里彻底拿掉，节点也不参与委派。

判定调 clonoth_runtime，与工具子进程、提示词注入同源。各判各的就会出现「提示词说能用，
工具却不在列表里」这种对不上的局面。

NovelAI 只挡委派、不挡工具：draw.novelai_planner 的 finish_requires_tool 要求必须调到
nai_generate* 才能收尾，而 /生图 是直达这个节点的 —— 摘了工具就把直达的人送进一个
「必须调工具但没有工具」的死角，不如让工具照常报缺 key。
"""
from __future__ import annotations

from pathlib import Path

from clonoth_runtime import IMAGE_TOOL_SLOTS, image_tool_available, novelai_available

# 受门控的生图工具名集合
IMAGE_GEN_TOOLS = frozenset(IMAGE_TOOL_SLOTS)
# 受门控的委派目标节点
IMAGE_GEN_NODE_ID = "draw.image_gen"
NOVELAI_NODE_ID = "draw.novelai_planner"

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


def disabled_draw_nodes(workspace_root: Path | str) -> set[str]:
    """一张图都画不出来的绘图节点，不该出现在委派目标里。

    留着的话模型会认真规划一轮，再撞上工具报缺 key —— 白烧一轮还给用户一个失败。
    """
    root = Path(workspace_root)
    disabled: set[str] = set()
    if not any_image_channel_enabled(root):
        disabled.add(IMAGE_GEN_NODE_ID)
    if not novelai_available(root):
        disabled.add(NOVELAI_NODE_ID)
    return disabled

"""绘图门控：确定没配的渠道，工具从模型的可用列表里彻底拿掉，节点也不参与委派。

判定调 clonoth_runtime，与工具子进程、提示词注入同源。各判各的就会出现「提示词说能用，
工具却不在列表里」这种对不上的局面。配置读不了或判定本身出错时判断不了，一律留着，让工具报出原因。

NovelAI 只挡委派、不挡工具：draw.novelai_planner 的 finish_requires_tool 要求必须调到
nai_generate* 才能收尾，而 /生图 是直达这个节点的 —— 摘了工具就把直达的人送进一个
「必须调工具但没有工具」的死角，不如让工具照常报缺 key。
"""
from __future__ import annotations

from pathlib import Path

from clonoth_runtime import IMAGE_TOOL_SLOTS, image_tool_status, novelai_status

# 受门控的生图工具名集合
IMAGE_GEN_TOOLS = frozenset(IMAGE_TOOL_SLOTS)
# 受门控的委派目标节点
IMAGE_GEN_NODE_ID = "draw.image_gen"
NOVELAI_NODE_ID = "draw.novelai_planner"


def disabled_image_tools(workspace_root: Path | str) -> set[str]:
    """应当从可用工具列表里移除的生图工具名：确定没配的那几个。"""
    root = Path(workspace_root)
    return {tool for tool in IMAGE_GEN_TOOLS if image_tool_status(root, tool).unconfigured}


def disabled_draw_nodes(workspace_root: Path | str) -> set[str]:
    """确定一个渠道都没配的绘图节点，不该出现在委派目标里；判断不了的留着。

    留着的话模型会认真规划一轮，再撞上工具报缺 key —— 白烧一轮还给用户一个失败。
    """
    root = Path(workspace_root)
    disabled: set[str] = set()
    if disabled_image_tools(root) == IMAGE_GEN_TOOLS:
        disabled.add(IMAGE_GEN_NODE_ID)
    if novelai_status(root).unconfigured:
        disabled.add(NOVELAI_NODE_ID)
    return disabled

"""按实际配好的渠道裁剪 draw.image_gen 的提示词。

未配置的生图工具，连同它的参数文档和选型建议一起从提示词里拿掉 —— 只标一句
「不可用」的话，模型仍会读到十几行教它怎么用那个工具，照样往上撞。

可用性判定直接调 tools/_channel.py 的 resolve_image_channel，跟工具本身用同一份
回退链；各算各的必然出现「工具能跑但提示词说没配」这类对不上的情况。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_TARGET_NODE_ID = "draw.image_gen"
_PLACEHOLDER = "<<IMAGE_TOOLS_AVAILABILITY>>"
_SLOT_BY_TOOL = {"gpt_image_2": "image_gpt", "gemini_image": "image_gemini"}
_TOOL_BLURB = {
    "gpt_image_2": "写实/照片/图片编辑/精确分辨率生图",
    "gemini_image": "二次元/插画/风格化/按宽高比生图",
}

# 条件段整行匹配，标记行本身不进正文。
_CONDITION_RE = re.compile(
    r"^[ \t]*<<IF:([A-Za-z0-9_]+)>>[ \t]*\r?\n(.*?)^[ \t]*<<ENDIF>>[ \t]*\r?\n?",
    re.DOTALL | re.MULTILINE,
)

def _available_tools(root: Path) -> dict[str, bool]:
    try:
        from clonoth_runtime import image_tool_available
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_inject: 取不到渠道判定，提示词保持原样: %s", exc)
        return {}
    return {tool: image_tool_available(root, tool) for tool in _SLOT_BY_TOOL}


def _read_default_channel(root: Path) -> str:
    """控制台选的默认生图工具。只在配了多个渠道时才有意义。"""
    try:
        import yaml  # type: ignore
    except Exception:
        return ""
    path = root / "data" / "config.yaml"
    if not path.exists():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_inject: 读取 config.yaml 失败: %s", exc)
        return ""
    block = data.get("image_gen") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return ""
    name = str(block.get("default_channel") or "").strip()
    return name if name in _SLOT_BY_TOOL else ""


def build_flags(available: Mapping[str, bool]) -> dict[str, bool]:
    """条件段的开关表。每个工具名一个，另加三个描述整体局面的。"""
    live = [tool for tool, ok in available.items() if ok]
    flags = {tool: bool(ok) for tool, ok in available.items()}
    flags["MULTI"] = len(live) > 1
    flags["SINGLE"] = len(live) == 1
    flags["NONE"] = not live
    return flags


def render_conditions(text: str, flags: Mapping[str, bool]) -> str:
    """按 flags 保留或删掉 <<IF:X>>…<<ENDIF>> 段。

    条件名不认识就原样留着：提示词里标记写错时，多发一段远好过静默吞掉整块说明。
    """
    def _replace(match: re.Match[str]) -> str:
        name, body = match.group(1), match.group(2)
        if name not in flags:
            logger.warning("image_gen_inject: 未知条件 %s，保留原文", name)
            return match.group(0)
        return body if flags[name] else ""

    return _CONDITION_RE.sub(_replace, text)


def build_availability_text(available: Mapping[str, bool], default_channel: str = "") -> str:
    live = [tool for tool, ok in available.items() if ok]
    if not live:
        return (
            "【生图渠道】当前一个都没配。不要调用任何生图工具，直接用 finish 告诉用户：\n"
            "生图功能尚未配置，需要在控制台的渠道设置里填 system_models.image_gpt 或 \n"
            "system_models.image_gemini 的 model/base_url/api_key。"
        )
    lines = ["【可用的生图渠道】"]
    for tool in live:
        lines.append("- `%s`：%s。" % (tool, _TOOL_BLURB.get(tool, "生图")))
    if len(live) == 1:
        lines.append("只有这一个渠道，所有生图需求都用它，不要提别的工具。")
    elif default_channel in live:
        lines.append("拿不准用哪个时默认 `%s`。" % default_channel)
    return "\n".join(lines)


def _rewrite(text: str, availability: str, flags: Mapping[str, bool]) -> str:
    return render_conditions(text.replace(_PLACEHOLDER, availability), flags)


class ImageGenAvailabilityInjector:
    """在 draw.image_gen 节点按实际渠道裁剪提示词。"""

    name = "image_gen_inject"
    priority = 45  # 与 draw_character_inject(40) 相近，独立注入互不影响

    async def handle(self, ctx: Any) -> Any | None:
        node = getattr(ctx, "node", None)
        rctx = getattr(ctx, "rctx", None)
        if node is None or rctx is None:
            return None
        if str(getattr(node, "id", "") or "") != _TARGET_NODE_ID:
            return None

        workspace_root = getattr(rctx, "workspace_root", None)
        if workspace_root is None:
            return None
        root = Path(workspace_root)

        available = _available_tools(root)
        if not available:
            # 判定本身失败了。这时说"一个都没配"是在撒谎，原样发出去让工具自己报错。
            return None
        flags = build_flags(available)
        availability = build_availability_text(available, _read_default_channel(root))

        replaced = False
        for message in ctx.messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                rewritten = _rewrite(content, availability, flags)
                if rewritten != content:
                    message["content"] = rewritten
                    replaced = True
            elif isinstance(content, list):
                for part in content:
                    if not (isinstance(part, dict) and part.get("type") == "text"):
                        continue
                    original = part.get("text")
                    if not isinstance(original, str):
                        continue
                    rewritten = _rewrite(original, availability, flags)
                    if rewritten != original:
                        part["text"] = rewritten
                        replaced = True

        if not replaced:
            # 占位符和条件段都不在（prompt 被改过等）：不强行注入，避免污染。
            return None

        logger.info(
            "image_gen_inject: 裁剪生图提示词 %s node=%s",
            " ".join("%s=%s" % (tool, ok) for tool, ok in available.items()), _TARGET_NODE_ID,
        )
        return _hook_result(modified=True)


def _hook_result(*, modified: bool = False):
    """构造一个 HookResult 兼容对象（避免强依赖 hooks 包内部结构）。"""
    try:
        from engine.hooks.types import HookResult  # type: ignore

        return HookResult(modified=modified)
    except Exception:  # noqa: BLE001
        class _R:  # 最小 duck-typed 兜底
            def __init__(self, modified: bool) -> None:
                self.block = False
                self.skip_step = False
                self.action = None
                self.reason = ""
                self.error_message = ""
                self.modified = modified

        return _R(modified)


PLUGIN_META = {
    "name": "image-gen-inject",
    "version": "2.0.0",
    "description": "按实际配好的渠道裁剪 draw.image_gen 提示词，未配置的工具连文档一起不发。",
    "author": "Clonoth",
    "handler_class": "ImageGenAvailabilityInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 45,
    "hooks": ["before_prompt_build"],
}

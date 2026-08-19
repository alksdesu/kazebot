"""绘图节点角色库自动注入插件。

目的：当用户请求命中 character_tags.yaml 中的角色时，在 draw 节点的 system
提示里自动附上该角色的 danbooru / appearance / outfits，供 AI 直接参考，
而不再依赖 AI 主动调用 draw_context。这样即便 AI 不调工具，也能拿到正确
的英文 tag，避免把中文名或凭空想象的外貌写进 prompt。

做法：注册 before_prompt_build 钩子，只对 draw 节点生效。用当前 instruction
（外加最近若干条历史）匹配角色关键词，命中则把角色信息拼成一段 system
消息，插到 messages 的系统提示之后、历史之前。

原因：与内置 knowledge_inject 使用同一套 HookContext 协议，保持零侵入。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 只对这些节点注入角色库（draw 分析/生成节点）。
_TARGET_NODE_PREFIXES = ("draw.",)

# 匹配时回看的历史条数（把最近几条对话也纳入关键词匹配，覆盖“再画一张她”这类指代）。
_SCAN_HISTORY_DEPTH = 4
# 单次最多注入的命中角色数，避免上下文膨胀。
_MAX_INJECT_CHARACTERS = 8


def _drawtools_dir(workspace_root: Path) -> Path:
    return workspace_root / "tools" / "drawtools"


def _load_character_tags(workspace_root: Path) -> list[dict[str, Any]]:
    """读取 character_tags.yaml（不存在则回退 example）。"""
    base = _drawtools_dir(workspace_root)
    path = base / "character_tags.yaml"
    if not path.exists():
        path = base / "character_tags.example.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draw_character_inject: 读取角色库失败: %s", exc)
        return []
    chars = data.get("characters") if isinstance(data, dict) else []
    if not isinstance(chars, list):
        return []
    return [c for c in chars if isinstance(c, dict)]


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _character_keywords(ch: dict[str, Any]) -> list[str]:
    """收集一个角色可用于匹配的关键词：name / danbooru / aliases。"""
    keys: list[str] = []
    for key in ("name", "danbooru"):
        value = str(ch.get(key) or "").strip()
        if value:
            keys.append(value)
            keys.append(value.replace("_", " "))
    aliases = ch.get("aliases") if isinstance(ch.get("aliases"), list) else []
    keys.extend(str(a).strip() for a in aliases if str(a).strip())
    # 去重且保序
    return list(dict.fromkeys(keys))


def _matches(query_norm: str, ch: dict[str, Any]) -> bool:
    for key in _character_keywords(ch):
        k = _norm(key)
        if k and k in query_norm:
            return True
    return False


def _build_scan_text(instruction: str, history: list[dict[str, Any]]) -> str:
    parts: list[str] = [str(instruction or "")]
    if isinstance(history, list) and history:
        for msg in history[-_SCAN_HISTORY_DEPTH:]:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "")) for c in content if isinstance(c, dict)
                )
            parts.append(str(content or ""))
    return " ".join(parts)


def _format_outfits(outfits: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(outfits, list):
        return lines
    for outfit in outfits:
        if not isinstance(outfit, dict):
            continue
        name = str(outfit.get("name") or "").strip()
        tags = str(outfit.get("tags") or "").strip()
        tags = re.sub(r"\s+", " ", tags)
        if not tags:
            continue
        if name:
            lines.append(f"      * {name}: {tags}")
        else:
            lines.append(f"      * {tags}")
    return lines


def _build_character_block(matched: list[dict[str, Any]]) -> str:
    """把命中角色拼成一段 system 提示文本。"""
    blocks: list[str] = []
    for ch in matched:
        name = str(ch.get("name") or "").strip()
        danbooru = str(ch.get("danbooru") or "").strip()
        ctype = str(ch.get("type") or "").strip()
        appearance = re.sub(r"\s+", " ", str(ch.get("appearance") or "").strip())
        lines = [f"- 角色「{name}」"]
        if danbooru:
            lines.append(f"    danbooru: {danbooru}")
        else:
            lines.append("    danbooru: （无官方 tag，禁止把中文名当 tag，用下面外貌描述表现身份）")
        if ctype:
            lines.append(f"    type: {ctype}")
        if appearance:
            lines.append(f"    appearance: {appearance}")
        outfit_lines = _format_outfits(ch.get("outfits"))
        if outfit_lines:
            lines.append("    outfits:")
            lines.extend(outfit_lines)
        blocks.append("\n".join(lines))

    header = (
        "[已录入角色库 · 自动命中]\n"
        "以下角色已在角色标签库中命中当前绘图请求，请务必优先使用这里给出的"
        " danbooru / appearance / outfits 英文 tag 来表现角色身份与服装，"
        "从 outfits 中挑选最贴合场景的一套（可微调，不要机械堆叠所有服装）。\n"
        "严禁把中文角色名直接写进任何 prompt / character 字段；danbooru 为空时，"
        "靠 appearance 里的英文特征 tag 体现身份。\n\n"
    )
    return header + "\n\n".join(blocks)


class DrawCharacterInjector:
    """在 draw 节点自动注入命中角色库信息。"""

    name = "draw_character_inject"
    priority = 40  # 早于 knowledge_inject(50) 前跑无所谓，注入内容独立

    async def handle(self, ctx: Any) -> Any | None:
        node = getattr(ctx, "node", None)
        rctx = getattr(ctx, "rctx", None)
        if node is None or rctx is None:
            return None

        node_id = str(getattr(node, "id", "") or "")
        if not node_id.startswith(_TARGET_NODE_PREFIXES):
            return None

        workspace_root = getattr(rctx, "workspace_root", None)
        if workspace_root is None:
            return None
        workspace_root = Path(workspace_root)

        characters = _load_character_tags(workspace_root)
        if not characters:
            return None

        instruction = str(ctx.extra.get("instruction_text") or "")
        history = ctx.extra.get("history") or []
        scan_text = _build_scan_text(instruction, history)
        qn = _norm(scan_text)
        if not qn:
            return None

        matched: list[dict[str, Any]] = []
        for ch in characters:
            if _matches(qn, ch):
                matched.append(ch)
                if len(matched) >= _MAX_INJECT_CHARACTERS:
                    break

        if not matched:
            return None

        block_text = _build_character_block(matched)
        message = {"role": "system", "content": block_text}

        # 把角色库消息插到最后一条 system 提示之后（历史之前）。
        messages = ctx.messages
        insert_at = 0
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                insert_at = idx + 1
            else:
                break
        messages.insert(insert_at, message)

        names = ", ".join(str(c.get("name") or "") for c in matched)
        logger.info("draw_character_inject: 命中并注入角色 [%s] node=%s", names, node_id)

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
    "name": "draw-character-inject",
    "version": "1.0.0",
    "description": "为 draw 节点自动注入命中的角色库标签（无需 AI 调用 draw_context）。",
    "author": "Clonoth",
    "handler_class": "DrawCharacterInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 40,
    "hooks": ["before_prompt_build"],
}

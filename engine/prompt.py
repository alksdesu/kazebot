from __future__ import annotations

import locale
import platform
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import config_layout

from .node import Node

DEFAULT_PROMPT = "你是一个 AI 节点。请根据指令完成处理。\n"

_INCLUDE_RE = re.compile(r'\{\{include:([^}]+)\}\}')
_DRAWTOOLS_PREFIX = "drawtools/"


def _include_name(raw: str) -> str | None:
    """include 里写的相对名字。绝对路径、盘符、带 .. 的一律不认：片段只能从节点目录和绘图配置里取。"""
    text = raw.strip().replace("\\", "/")
    if not text or text.startswith("/") or text[1:2] == ":":
        return None
    parts = PurePosixPath(text).parts
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _include_file(workspace_root: Path, filename: str) -> Path | None:
    """drawtools/ 开头的取实例的绘图配置，没有就取仓库模板；其余取节点片段，实例改过的优先，再退到仓库默认和模板。"""
    if filename.startswith(_DRAWTOOLS_PREFIX):
        path = config_layout.drawtools_effective(workspace_root, filename[len(_DRAWTOOLS_PREFIX):])
        return path if path.is_file() else None
    return config_layout.fragment_file(workspace_root, filename)


def _expand_includes(text: str, workspace_root: Path, *, depth: int = 0) -> str:
    """展开 {{include:filename}} 指令，把节点片段或绘图配置的内容内联进来。

    最多递归 3 层，防止循环引用。
    """
    if depth > 3:
        return text

    def _repl(m: re.Match) -> str:
        filename = _include_name(m.group(1))
        if filename is None:
            return m.group(0)
        fp = _include_file(workspace_root, filename)
        if fp is None:
            return m.group(0)
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return m.group(0)
        # 递归展开被包含文件中的 include
        return _expand_includes(content, workspace_root, depth=depth + 1)

    return _INCLUDE_RE.sub(_repl, text)


def _render_variables(text: str, variables: dict[str, str]) -> str:
    """替换 {{key}} 占位符。未匹配的保留原样。"""
    def _repl(m: re.Match) -> str:
        key = m.group(1).strip()
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{(\w+)\}\}', _repl, text)


def _build_variables(
    workspace_root: Path,
    node: Node,
    variables: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build merged template variable dict for prompt rendering."""
    merged: dict[str, str] = {}
    if variables:
        merged.update(variables)
    merged.setdefault("node_id", node.id)
    merged.setdefault("node_name", node.name)
    merged.setdefault("now", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    merged.setdefault("default_language", "zh-CN")
    merged.setdefault("workspace_root", str(workspace_root.resolve()))
    merged.setdefault("os_name", platform.system())
    merged.setdefault("os_version", platform.version())
    merged.setdefault("timezone", str(datetime.now().astimezone().tzinfo or "UTC"))
    merged.setdefault("user_language", locale.getdefaultlocale()[0] or "en_US")
    return merged


def _assemble_block_prompt(
    workspace_root: Path,
    node: Node,
    blocks: list,
    variables: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Process prompt in block list mode.

    Each block: {role, content?, depth?}
    Special role "history" marks the conversation history insertion point.
    Returns processed block list for ai_step to consume.
    """
    merged = _build_variables(workspace_root, node, variables)

    result: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue

        # Skip disabled blocks
        if block.get("enabled") is False:
            continue

        role = str(block.get("role") or "").strip()
        if not role:
            continue

        # History marker — no content processing
        if role == "history":
            result.append({"role": "history"})
            continue

        content = str(block.get("content") or "").strip()
        if not content:
            continue

        # Expand includes and render variables
        content = _expand_includes(content, workspace_root)
        content = _render_variables(content, merged)

        entry: dict[str, Any] = {"role": role, "content": content}

        # Optional depth (parsed for forward-compat; used by ai_step)
        raw_depth = block.get("depth")
        if raw_depth is not None:
            try:
                entry["depth"] = int(raw_depth)
            except (ValueError, TypeError):
                pass

        result.append(entry)

    # Ensure a history marker exists; if user didn't specify one, append at end
    if not any(isinstance(b, dict) and b.get("role") == "history" for b in result):
        result.append({"role": "history"})

    return result if result else [{"role": "system", "content": DEFAULT_PROMPT}]


def assemble_prompt(
    workspace_root: Path,
    node: Node,
    *,
    variables: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """渲染节点的 prompt 模板，返回消息列表。

    字符串模式（prompt 为 str）：
      返回 list[{"role": "system", "content": str}]（1-2 条）。
      使用 # %%DYNAMIC%% 标记拆分静态/动态段。

    列表模式（prompt 为 list）：
      返回 block 列表，可含 {"role": "history"}（历史展开标记）等。
      不使用 %%DYNAMIC%%，由块排列控制缓存边界。
    """
    raw = node.prompt

    # Block list mode
    if isinstance(raw, list):
        return _assemble_block_prompt(workspace_root, node, raw, variables)

    # String mode (existing behavior)
    raw_str = raw.strip() if raw else ""
    if not raw_str:
        return [{"role": "system", "content": DEFAULT_PROMPT}]

    expanded = _expand_includes(raw_str, workspace_root)

    merged = _build_variables(workspace_root, node, variables)

    # 切分在渲染之前：渲染后再找标记，等于让任何变量值（{{instruction}} 就是用户原话）
    # 自带一行标记就能把系统提示词从任意位置劈开，把它上方的规则挤进"动态段"。
    marker = "# %%DYNAMIC%%"
    if marker in expanded:
        idx = expanded.index(marker)
        static_part = _render_variables(expanded[:idx], merged).strip()
        dynamic_part = _render_variables(expanded[idx + len(marker):], merged).strip()
        msgs: list[dict[str, Any]] = []
        if static_part:
            msgs.append({"role": "system", "content": static_part})
        if dynamic_part:
            msgs.append({"role": "system", "content": dynamic_part})
        if msgs:
            return msgs

    # 没有标记，整体作为一条 system 消息
    return [{"role": "system", "content": _render_variables(expanded, merged)}]

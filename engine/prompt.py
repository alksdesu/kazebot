from __future__ import annotations

import locale
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .node import Node

DEFAULT_PROMPT = "你是一个 AI 节点。请根据指令完成处理。\n"

_INCLUDE_RE = re.compile(r'\{\{include:([^}]+)\}\}')


def _expand_includes(text: str, nodes_dir: Path, *, depth: int = 0) -> str:
    """展开 {{include:filename}} 指令，读取 config/nodes/ 下的文件并内联。

    最多递归 3 层，防止循环引用。
    """
    if depth > 3:
        return text

    def _repl(m: re.Match) -> str:
        filename = m.group(1).strip()
        if not filename:
            return m.group(0)
        if filename.startswith("drawtools/"):
            workspace_root = nodes_dir.parent.parent
            rel = filename[len("drawtools/"):].lstrip("/")
            fp = (workspace_root / "tools" / "drawtools" / rel).resolve()
            allowed_root = (workspace_root / "tools" / "drawtools").resolve()
            try:
                fp.relative_to(allowed_root)
            except ValueError:
                return m.group(0)
            if not fp.exists() and fp.suffix:
                example = fp.with_name(f"{fp.stem}.example{fp.suffix}")
                if example.exists():
                    fp = example
        else:
            fp = (nodes_dir / filename).resolve()
            # 安全检查：不能逃逸出 nodes_dir
            try:
                fp.relative_to(nodes_dir.resolve())
            except ValueError:
                return m.group(0)
            # 缺失时回退同名 .example，让 _persona.md 这类用户覆盖文件可选存在
            if not fp.exists() and fp.suffix:
                example = fp.with_name(f"{fp.stem}.example{fp.suffix}")
                if example.exists():
                    fp = example
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return m.group(0)
        # 递归展开被包含文件中的 include
        return _expand_includes(content, nodes_dir, depth=depth + 1)

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
    nodes_dir = workspace_root / "config" / "nodes"
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
        content = _expand_includes(content, nodes_dir)
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

    nodes_dir = workspace_root / "config" / "nodes"
    expanded = _expand_includes(raw_str, nodes_dir)

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

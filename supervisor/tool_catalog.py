"""工具目录：supervisor 侧「有哪些工具」的唯一答案。

真正的注册表只活在 engine worker 进程里，这边按它的三个来源各自重建一遍。
手抄一份名字集合迟早和注册表漂移 —— 界面上少一个工具，等于那个工具没人管得到。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ToolSource = Literal["builtin", "plugin", "external"]


@dataclass(frozen=True)
class ToolEntry:
    name: str
    source: ToolSource
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    file: str | None = None
    timeout_sec: float | None = None
    has_spec: bool = True

    @property
    def editable(self) -> bool:
        """只有外部脚本工具落在 tools/ 下有源码，内置和插件工具改不了。"""
        return self.source == "external"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "source": self.source,
            "editable": self.editable,
            "description": self.description,
            "input_schema": self.input_schema,
            "has_spec": self.has_spec,
        }
        if self.file is not None:
            out["file"] = self.file
        if self.timeout_sec is not None:
            out["timeout_sec"] = self.timeout_sec
        return out


def builtin_entries(workspace_root: Path) -> list[ToolEntry]:
    """注册表自带的内置工具。"""
    from toolbox.registry import ToolRegistry

    registry = ToolRegistry(
        workspace_root=workspace_root,
        tools_dir=workspace_root / "tools",
        load_external=False,
    )
    return [_from_spec(spec, "builtin") for spec in registry.builtin_specs()]


def plugin_entries() -> list[ToolEntry]:
    """engine/builtin 下插件在 PLUGIN_META 里自带的工具。"""
    from engine.builtin.loader import iter_plugin_tool_meta

    return [_from_spec(meta, "plugin") for meta in iter_plugin_tool_meta()]


def external_entries(workspace_root: Path) -> list[ToolEntry]:
    """tools/ 下的脚本工具。"""
    from toolbox.registry import extract_tool_spec, iter_external_tool_files

    tools_dir = workspace_root / "tools"
    if not tools_dir.is_dir():
        return []

    out: list[ToolEntry] = []
    for path in iter_external_tool_files(tools_dir):
        rel = path.relative_to(workspace_root).as_posix()
        spec, timeout_sec = extract_tool_spec(path)
        name = ""
        if isinstance(spec, dict):
            name = str(spec.get("name") or "").strip()
        if not name:
            # SPEC 写坏了也要列出来，否则唯一能修它的入口——源码编辑器——打不开。
            if path.parent != tools_dir:
                continue
            out.append(ToolEntry(name=path.stem, source="external", file=rel, has_spec=False))
            continue
        out.append(_from_spec(spec, "external", file=rel, timeout_sec=timeout_sec))
    return out


def tool_catalog(workspace_root: Path) -> list[ToolEntry]:
    """三类合一，按名字排序。重名时外部工具让位，和注册表 reload 的取舍一致。"""
    entries: dict[str, ToolEntry] = {}
    for entry in builtin_entries(workspace_root) + plugin_entries():
        entries[entry.name] = entry
    for entry in external_entries(workspace_root):
        entries.setdefault(entry.name, entry)
    return sorted(entries.values(), key=lambda item: item.name)


def tool_names(workspace_root: Path) -> list[str]:
    return [entry.name for entry in tool_catalog(workspace_root)]


def _from_spec(
    spec: dict[str, Any],
    source: ToolSource,
    *,
    file: str | None = None,
    timeout_sec: float | None = None,
) -> ToolEntry:
    schema = spec.get("input_schema")
    return ToolEntry(
        name=str(spec.get("name") or "").strip(),
        source=source,
        description=str(spec.get("description") or ""),
        input_schema=schema if isinstance(schema, dict) else {},
        file=file,
        timeout_sec=timeout_sec,
    )

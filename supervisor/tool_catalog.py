"""工具目录：supervisor 侧「有哪些工具」的唯一答案。

真正的注册表只活在 engine worker 进程里，这边按它的三个来源各自重建一遍。
手抄一份名字集合迟早和注册表漂移 —— 界面上少一个工具，等于那个工具没人管得到。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import config_layout

if TYPE_CHECKING:
    from toolbox.registry import ToolOrigin

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
    # 外部脚本才有：实例自己加的、改过仓库那份的、仓库原样的。
    origin: ToolOrigin | None = None
    # 外部脚本在所属工具目录里的相对路径，改仓库那份时按它落到 config/tools/ 的同一位置。
    rel: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None

    @property
    def editable(self) -> bool:
        """只有外部脚本工具有源码，内置和插件工具改不了。仓库那份改了落成 config/tools/ 里的同路径文件。"""
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
        if self.origin is not None:
            out["origin"] = self.origin
        if self.plugin_id:
            out["plugin_id"] = self.plugin_id
            out["plugin_version"] = self.plugin_version
        return out


def builtin_entries(workspace_root: Path) -> list[ToolEntry]:
    """注册表自带的内置工具。"""
    from toolbox.registry import ToolRegistry

    registry = ToolRegistry(
        workspace_root=workspace_root,
        tools_dir=config_layout.workspace_tools_dir(workspace_root),
        load_external=False,
    )
    return [_from_spec(spec, "builtin") for spec in registry.builtin_specs()]


def plugin_entries() -> list[ToolEntry]:
    """engine/builtin 下插件在 PLUGIN_META 里自带的工具。"""
    from engine.builtin.loader import iter_plugin_tool_meta

    return [_from_spec(meta, "plugin") for meta in iter_plugin_tool_meta()]


def external_entries(workspace_root: Path) -> list[ToolEntry]:
    """config/tools/ 和仓库 tools/ 下的脚本工具，同一相对路径只列生效的那份。"""
    from toolbox.registry import external_tool_files, extract_tool_spec

    out: list[ToolEntry] = []
    for item in external_tool_files(workspace_root):
        rel = config_layout.logical_path(workspace_root, item.path)
        spec, timeout_sec = extract_tool_spec(item.path)
        name = ""
        if isinstance(spec, dict):
            name = str(spec.get("name") or "").strip()
        if not name:
            # SPEC 写坏了也要列出来，否则唯一能修它的入口——源码编辑器——打不开。
            if "/" in item.rel:
                continue
            out.append(ToolEntry(
                name=item.path.stem, source="external", file=rel, has_spec=False, origin=item.origin, rel=item.rel,
            ))
            continue
        out.append(_from_spec(spec, "external", file=rel, timeout_sec=timeout_sec, origin=item.origin, rel=item.rel))
    return out


def tool_catalog(workspace_root: Path) -> list[ToolEntry]:
    """三类合一，按名字排序。重名时外部工具让位，外部工具之间先出现的算数，和注册表 reload 的取舍一致。"""
    entries: dict[str, ToolEntry] = {}
    for entry in builtin_entries(workspace_root) + plugin_entries():
        entries[entry.name] = entry
    for entry in external_entries(workspace_root):
        entries.setdefault(entry.name, entry)
    for plugin_id, revision, declaration in application_tool_declarations(workspace_root):
        spec = declaration.get("spec", {})
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        from toolbox.tool_names import is_engine_owned_name
        if is_engine_owned_name(name) or (name in entries and not declaration.get("replace")):
            continue
        entries[name] = _from_spec(spec, "plugin", plugin_id=plugin_id, plugin_version=revision)
    return sorted(entries.values(), key=lambda item: item.name)


def application_tool_declarations(workspace_root: Path) -> list[tuple[str, str, dict]]:
    from .plugin_api import runtime_status

    latest: dict[str, tuple[str, dict]] = {}
    for report in runtime_status(workspace_root):
        if not report.get("alive") or report.get("process") != "engine":
            continue
        stamp = str(report.get("updated_at") or "")
        for plugin in report.get("plugins", []):
            plugin_id = str(plugin.get("id") or "")
            if plugin.get("status") not in {"loaded", "error"} or not plugin_id:
                continue
            if plugin_id not in latest or stamp > latest[plugin_id][0]:
                latest[plugin_id] = (stamp, plugin)
    result = []
    ordered = sorted(latest.items(), key=lambda pair: (-int(pair[1][1].get("priority", 100)), pair[0]), reverse=True)
    for plugin_id, (_, plugin) in ordered:
        for declaration in plugin.get("tools", []):
            if isinstance(declaration, dict) and isinstance(declaration.get("spec"), dict):
                result.append((plugin_id, str(plugin.get("desired_hash") or ""), declaration))
    return result


def tool_names(workspace_root: Path) -> list[str]:
    return [entry.name for entry in tool_catalog(workspace_root)]


def _from_spec(
    spec: dict[str, Any],
    source: ToolSource,
    *,
    file: str | None = None,
    timeout_sec: float | None = None,
    origin: ToolOrigin | None = None,
    rel: str | None = None,
    plugin_id: str | None = None,
    plugin_version: str | None = None,
) -> ToolEntry:
    schema = spec.get("input_schema")
    return ToolEntry(
        name=str(spec.get("name") or "").strip(),
        source=source,
        description=str(spec.get("description") or ""),
        input_schema=schema if isinstance(schema, dict) else {},
        file=file,
        timeout_sec=timeout_sec,
        origin=origin,
        rel=rel,
        plugin_id=plugin_id,
        plugin_version=plugin_version,
    )

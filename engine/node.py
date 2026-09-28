from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import config_layout
from clonoth_runtime import load_yaml_dict, load_runtime_config, resolve_env_ref

logger = logging.getLogger(__name__)


@dataclass
class ToolAccess:
    mode: str = "none"  # "none" | "all" | "allowlist"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class SkillAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class MemoryAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class Node:
    id: str
    type: str  # "ai" | "tool"
    name: str = ""
    description: str = ""
    model: str = ""          # 模型名称，空 = 使用全局默认
    api_key: str = ""        # 可选独立 API key（支持 $ENV{} 语法）
    base_url: str = ""       # 可选独立 base URL（支持 $ENV{} 语法）
    prompt: str | list = ""  # system prompt：字符串模式或 block 列表模式
    tool_access: ToolAccess = field(default_factory=ToolAccess)
    skill_access: SkillAccess = field(default_factory=SkillAccess)
    memory_access: MemoryAccess = field(default_factory=MemoryAccess)
    # [2026-05-01] 默认工具模式改为 fake-native。
    # 原因：旧 native 实际是文本化 fake-native；未显式配置的节点应继续旧行为。
    tool_mode: str = "fake-native"  # "fake-native" | "native" | "json"
    # hybrid output_mode: 纯文本输出不再 reject 重试，直接作为隐式 finish 投递给用户。
    # tool_only = 现有行为（强制 finish）；hybrid = 允许纯文本直接投递。
    output_mode: str = "tool_only"  # "tool_only" | "hybrid"
    # models.yaml 里的渠道名，或直接写线格式名。空表示跟随全局活跃渠道。
    provider: str = ""
    provider_options: dict = field(default_factory=dict)  # 透传给 provider 的参数（thinking/reasoning 等）
    delegate_targets: list[str] = field(default_factory=list)
    # 改名前的 id：会话、定时任务、配置里还写着旧 id 的照样找得到这个节点
    aliases: list[str] = field(default_factory=list)
    # [2026-05-27] persistent 标记：声明该节点为持久节点。
    # 为什么：子节点在 accumulate 模式下复用 child session，但缺少显式配置标记
    # 来告知引擎该 session 需要接入自动压缩。
    # 怎么改：新增 bool 字段，默认 False。
    # 目的：persistent=true 的节点的 child session 能触发自动上下文压缩，
    # 同时 dispatch 该节点时默认使用 accumulate context_mode。
    persistent: bool = False
    # [2026-05-27] extra: 收集节点 yaml 中 core 不认识的字段，供插件层零 IO 读取。
    # 为什么：memory_book 等插件业务配置不应定义在 Node dataclass 上，
    # 但插件每次从 yaml 文件读取也浪费 IO。
    # 怎么改：load_node 把 yaml 中不属于 core 已知字段的 key-value 全部收进 extra。
    # 目的：插件通过 ToolContext._node_extra 零 IO 拿到自己的配置。
    extra: dict = field(default_factory=dict)


_NODE_SUFFIX = ".yaml"
# 按旧 id 找节点要把几个节点目录都过一遍，解析出的别名按 (文件, 修改时间) 缓存
_ALIAS_CACHE: dict[str, tuple[int, tuple[str, ...]]] = {}
_WARNED_ALIAS_CONFLICTS: set[tuple[str, ...]] = set()


def _node_dirs(workspace_root: Path) -> tuple[Path, ...]:
    return config_layout.node_search_dirs(workspace_root)


def _is_node_name(name: str) -> bool:
    # id 直接拼成文件名：带分隔符、盘符或 .. 就能把节点目录外的 yaml 当节点加载
    return bool(name) and name not in {".", ".."} and not any(ch in name for ch in "/\\:\0")


def _exact_node_file(workspace_root: Path, name: str) -> Path | None:
    for nodes_dir in _node_dirs(workspace_root):
        path = nodes_dir / f"{name}{_NODE_SUFFIX}"
        if path.is_file():
            return path
    return None


def _parse_aliases(data: dict[str, Any], stem: str) -> tuple[str, ...]:
    raw = data.get("aliases")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    names: list[str] = []
    for item in items:
        name = item.strip() if isinstance(item, str) else ""
        if _is_node_name(name) and name != stem and name not in names:
            names.append(name)
    return tuple(names)


def _declared_aliases(path: Path) -> tuple[str, ...]:
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return ()
    key = str(path.resolve())
    cached = _ALIAS_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    aliases = _parse_aliases(load_yaml_dict(path) or {}, path.stem)
    _ALIAS_CACHE[key] = (mtime, aliases)
    return aliases


def node_files(workspace_root: Path) -> list[Path]:
    """能按 id 派发到的节点文件：系统目录、实例目录、仓库默认依次排，同一目录按文件名排序。"""
    files: list[Path] = []
    for nodes_dir in _node_dirs(workspace_root):
        if not nodes_dir.is_dir():
            continue
        for path in sorted(nodes_dir.glob(f"*{_NODE_SUFFIX}")):
            # 模板和示例不会被派发到；被排在前面的同名文件顶掉的那份永远加载不到
            if path.name.startswith("_") or ".example" in path.name:
                continue
            if _exact_node_file(workspace_root, path.stem) == path:
                files.append(path)
    return files


def _alias_owner(workspace_root: Path, name: str) -> Path | None:
    owners = [path for path in node_files(workspace_root) if name in _declared_aliases(path)]
    if len(owners) > 1:
        _warn_alias_conflict(name, owners)
    return owners[0] if owners else None


def _warn_alias_conflict(name: str, owners: list[Path]) -> None:
    key = (name, *(str(path) for path in owners))
    if key in _WARNED_ALIAS_CONFLICTS:
        return
    _WARNED_ALIAS_CONFLICTS.add(key)
    logger.warning(
        "node alias %s is declared by %s; %s wins",
        name, ", ".join(path.name for path in owners), owners[0].name,
    )


def _effective_aliases(workspace_root: Path, path: Path, declared: Iterable[str]) -> tuple[str, ...]:
    # 有同名文件、或被排在前面的节点先认领了的旧 id 找不到这里，不算这个节点的
    return tuple(name for name in declared if node_file(workspace_root, name) == path)


def node_file(workspace_root: Path, node_id: str) -> Path | None:
    """节点 id 对应的文件：先按文件名找，找不到再看哪个节点把它声明成了旧 id。"""
    name = (node_id or "").strip()
    if not _is_node_name(name):
        return None
    return _exact_node_file(workspace_root, name) or _alias_owner(workspace_root, name)


def canonical_node_id(workspace_root: Path, node_id: str) -> str:
    """节点现在的 id，即文件名。旧 id 换成现在的，找不到节点就原样返回。"""
    name = (node_id or "").strip()
    path = node_file(workspace_root, name)
    return path.stem if path is not None else name


def known_node_ids(workspace_root: Path, node_id: str) -> tuple[str, ...]:
    """节点认的全部 id：现在的在前，旧的按声明顺序在后。按 id 查设置时依次试。"""
    name = (node_id or "").strip()
    path = node_file(workspace_root, name)
    if path is None:
        return (name,) if name else ()
    return (path.stem, *_effective_aliases(workspace_root, path, _declared_aliases(path)))


def load_node(workspace_root: Path, node_id: str) -> Node | None:
    node_path = node_file(workspace_root, node_id)
    if node_path is None:
        return None
    data = load_yaml_dict(node_path)
    if not isinstance(data, dict):
        return None
    nid = node_path.stem
    # kind 默认 "node"，允许省略
    if str(data.get("kind") or "node").strip() != "node":
        return None
    node_type = str(data.get("type") or "ai").strip().lower()
    if node_type not in {"ai", "tool"}:
        return None

    # prompt：字符串→字符串模式，列表→block 列表模式
    raw_prompt = data.get("prompt")
    if isinstance(raw_prompt, list):
        prompt = raw_prompt
    elif isinstance(raw_prompt, str):
        prompt = raw_prompt.strip()
    else:
        prompt = ""

    # model：直接值或 $ENV{} 引用
    raw_model = str(data.get("model") or "").strip()
    model = resolve_env_ref(raw_model) if raw_model else ""

    # 可选独立 api_key / base_url
    raw_api_key = str(data.get("api_key") or "").strip()
    api_key = resolve_env_ref(raw_api_key) if raw_api_key else ""
    raw_base_url = str(data.get("base_url") or "").strip()
    base_url = resolve_env_ref(raw_base_url) if raw_base_url else ""

    ta_raw = data.get("tool_access")
    if isinstance(ta_raw, str):
        m = ta_raw.strip().lower()
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none")
    elif isinstance(ta_raw, dict):
        m = str(ta_raw.get("mode") or "none").strip().lower()
        allow = [
            str(x).strip()
            for x in (ta_raw.get("allow") or [])
            if isinstance(x, str) and x.strip()
        ]
        deny = [
            str(x).strip()
            for x in (ta_raw.get("deny") or [])
            if isinstance(x, str) and x.strip()
        ]
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none", allow=allow, deny=deny)
    else:
        ta = ToolAccess()

    sa_raw = data.get("skills")
    if isinstance(sa_raw, str):
        sm = sa_raw.strip().lower()
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all")
    elif isinstance(sa_raw, dict):
        sm = str(sa_raw.get("mode") or "all").strip().lower()
        sa_allow = [str(x).strip() for x in (sa_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all", allow=sa_allow)
    else:
        sa = SkillAccess()

    ma_raw = data.get("memories")
    if isinstance(ma_raw, str):
        mm = ma_raw.strip().lower()
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all")
    elif isinstance(ma_raw, dict):
        mm = str(ma_raw.get("mode") or "all").strip().lower()
        ma_allow = [str(x).strip() for x in (ma_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all", allow=ma_allow)
    else:
        ma = MemoryAccess()

    # tool_mode — 节点 yaml 优先，否则用 runtime.yaml engine.tool_mode 全局默认。
    # [2026-05-01] 支持 fake-native / native / json 三种值，并兼容旧写法 fake_native。
    # 空值或非法值回退到 fake-native，目的：不把旧节点静默切到新的真 native。
    _node_tm = data.get("tool_mode")
    if _node_tm is not None:
        raw_tool_mode = str(_node_tm).strip().lower().replace("_", "-")
    else:
        _rt = load_runtime_config(workspace_root)
        raw_tool_mode = str((_rt.get("engine") or {}).get("tool_mode") or "fake-native").strip().lower().replace("_", "-")
    tool_mode = raw_tool_mode if raw_tool_mode in {"fake-native", "native", "json"} else "fake-native"

    # output_mode — 节点 yaml 优先，否则用 runtime.yaml engine.output_mode 全局默认
    # hybrid 模式下纯文本输出直接投递给用户，不 reject 不重试（RFC: rfc_hybrid_output_mode.md）
    _node_om = data.get("output_mode")
    if _node_om is not None:
        raw_output_mode = str(_node_om).strip().lower()
    else:
        _rt_om = load_runtime_config(workspace_root)
        raw_output_mode = str((_rt_om.get("engine") or {}).get("output_mode") or "tool_only").strip().lower()
    output_mode = raw_output_mode if raw_output_mode in {"tool_only", "hybrid"} else "tool_only"

    # provider — 渠道名或线格式名，空表示跟随全局活跃渠道。
    # 不在这里校验也不转小写：认得的只有线格式，models.yaml 里的自定义渠道名会被
    # 判成非法而静默清空，而渠道名是区分大小写的。真解析在 model.resolve_provider。
    provider = str(data.get("provider") or "").strip()

    # provider_options — 透传给 provider 的参数（如 Anthropic thinking、OpenAI reasoning 等）
    raw_po = data.get("provider_options")
    provider_options = dict(raw_po) if isinstance(raw_po, dict) else {}

    # delegate_targets：统一换成现在的 id，派发工具名、白名单、子会话键都按它算
    dt_raw = data.get("delegate_targets")
    delegate_targets: list[str] = list(dict.fromkeys(
        canonical_node_id(workspace_root, x) for x in dt_raw if isinstance(x, str) and x.strip()
    )) if isinstance(dt_raw, list) else []

    # [2026-05-27] persistent 字段解析：从 node yaml 读取 persistent 布尔值。
    # 为什么：支持节点级别的持久化声明，用于控制 context_mode 默认值和自动压缩。
    # 怎么改：解析 yaml 中的 persistent 字段，非布尔值均视为 False。
    persistent = bool(data.get("persistent", False))

    # [2026-05-27] extra dict：收集 yaml 中 core 不认识的字段（如 memory_book），
    # 供插件层通过 ToolContext._node_extra 零 IO 读取。
    _KNOWN_KEYS = {
        "kind", "id", "type", "name", "description", "model", "api_key",
        "base_url", "prompt", "tool_access", "skills", "memories",
        "tool_mode", "output_mode", "provider", "provider_options",
        "delegate_targets", "aliases", "persistent",
    }
    extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    return Node(
        id=str(data.get("id") or nid).strip(),
        type=node_type,
        name=str(data.get("name") or nid).strip(),
        description=str(data.get("description") or "").strip(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        prompt=prompt,
        tool_access=ta,
        skill_access=sa,
        memory_access=ma,
        tool_mode=tool_mode,
        output_mode=output_mode,
        provider=provider,
        provider_options=provider_options,
        delegate_targets=delegate_targets,
        aliases=list(_effective_aliases(workspace_root, node_path, _parse_aliases(data, nid))),
        persistent=persistent,
        extra=extra,
    )

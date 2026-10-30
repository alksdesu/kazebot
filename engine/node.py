from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

import config_layout
from clonoth_runtime import load_runtime_config, parse_bool, resolve_env_ref

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
_NODE_TYPES = frozenset({"ai", "tool"})
# Windows、macOS 的文件系统不分大小写，改了大小写的 id 照样打开示例文件，判定也不能分
_EXAMPLE_MARK = re.compile(re.escape(".example"), re.IGNORECASE)
# 按旧 id 找节点要把几个节点目录都过一遍，解析出的别名按 (文件, 修改时间) 缓存
_ALIAS_CACHE: dict[str, tuple[tuple[int, int, int], tuple[str, ...]]] = {}
_WARNED_ALIAS_CONFLICTS: set[tuple[str, ...]] = set()
_WARNED_TEMPLATE_TARGETS: set[tuple[str, str]] = set()
# 节点每加载一次都会解析，写的 id 和文件名不一致的提醒按 (文件, 写的 id, 文件版本) 只记一次
_WARNED_DECLARED_IDS: set[tuple[str, str, tuple[int, int, int]]] = set()


def _node_dirs(workspace_root: Path) -> tuple[Path, ...]:
    return config_layout.node_search_dirs(workspace_root)


def _is_node_name(name: str) -> bool:
    # id 直接拼成文件名：带分隔符、盘符或 .. 就能把节点目录外的 yaml 当节点加载
    return bool(name) and name not in {".", ".."} and not any(ch in name for ch in "/\\:\0")


def example_node_name(name: str) -> bool:
    """示例文件的名字（不带后缀）：带 .example 的是照着改的参考，运行期不当节点用。"""
    return _EXAMPLE_MARK.search(name) is not None


def example_base_name(name: str) -> str:
    """示例去掉 .example 之后的名字，即照着它建正式文件该叫的名字；不是示例的原样返回。"""
    return _EXAMPLE_MARK.sub("", name)


def template_node_name(name: str) -> bool:
    """模板或示例的名字（不带 .yaml）：_ 开头的是模板，带 .example 的是示例，运行期都不按 id 当节点。"""
    return name.startswith("_") or example_node_name(name)


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


def declared_id(data: dict[str, Any], stem: str) -> str:
    """yaml 里写的、和文件名不同的 id；没写或和文件名一样给空串。

    节点的 id 一律是文件名。写的这个 id 以前当过节点 id，会话、配置、定时任务里可能存着它，所以当旧 id 认。
    """
    raw = data.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return ""
    name = str(raw).strip()
    return name if _is_node_name(name) and name != stem else ""


def _old_ids(data: dict[str, Any], stem: str) -> tuple[str, ...]:
    """文件名之外节点还认的 id：写的 id 和文件名不同时排最前，然后是 aliases 里声明的。"""
    return tuple(dict.fromkeys(name for name in (declared_id(data, stem), *_parse_aliases(data, stem)) if name))


def _declared_aliases(path: Path) -> tuple[str, ...]:
    stamp = config_layout.file_stamp(path)
    if stamp is None:
        return ()
    key = str(path.resolve())
    cached = _ALIAS_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    # kind、type 写错的定义也认它的旧 id：按旧 id 找过来，报的才是「不是能用的节点定义」而不是「找不到」。
    data, _problem = read_node_definition(path)
    aliases = _old_ids(data or {}, path.stem)
    _ALIAS_CACHE[key] = (stamp, aliases)
    return aliases


def _warn_declared_id(path: Path, declared: str) -> None:
    stamp = config_layout.file_stamp(path)
    key = (str(path), declared, stamp or (0, 0, 0))
    if key in _WARNED_DECLARED_IDS:
        return
    _WARNED_DECLARED_IDS.add(key)
    logger.warning(
        "%s declares id %s; a node's id is its file name %s, so %s is only kept as an old id",
        path.name, declared, path.stem, declared,
    )


def node_files(workspace_root: Path) -> list[Path]:
    """能按 id 派发到的节点文件：系统目录、实例目录、仓库默认依次排，同一目录按文件名排序。"""
    files: list[Path] = []
    for nodes_dir in _node_dirs(workspace_root):
        if not nodes_dir.is_dir():
            continue
        for path in sorted(nodes_dir.glob(f"*{_NODE_SUFFIX}")):
            # 模板和示例不会被派发到；被排在前面的同名文件顶掉的那份永远加载不到
            if template_node_name(path.stem):
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
    """节点 id 对应的文件：先按文件名找，找不到再看哪个节点把它声明成了旧 id。模板和示例的名字一律找不到。"""
    name = (node_id or "").strip()
    if not _is_node_name(name) or template_node_name(name):
        return None
    return _exact_node_file(workspace_root, name) or _alias_owner(workspace_root, name)


def template_node_file(workspace_root: Path, node_id: str) -> Path | None:
    """模板或示例的文件，给拿它当底稿复制的地方用；名字不是模板或示例的返回 None，按 id 找节点用 node_file。"""
    name = (node_id or "").strip()
    if not _is_node_name(name) or not template_node_name(name):
        return None
    return _exact_node_file(workspace_root, name)


def template_node_reason(workspace_root: Path, node_id: str) -> str:
    """点名的是模板或示例时说清楚它不当节点用；不是的给空串。"""
    name = (node_id or "").strip()
    if not _is_node_name(name) or not template_node_name(name):
        return ""
    path = _exact_node_file(workspace_root, name)
    if path is None:
        return f"{name} 是模板或示例的名字，不当节点用：节点名不能以 _ 开头，也不能带 .example"
    return (
        f"{name} 是模板或示例（{config_layout.logical_path(workspace_root, path)}），不当节点用；"
        f"要用就复制到 {config_layout.NODES_DIR}/ 下，换一个不以 _ 开头、不带 .example 的名字"
    )


def missing_node_reason(workspace_root: Path, node_id: str) -> str:
    """load_node 拿不到节点时给人看的原因：点名的是模板或示例、文件在但不是能用的节点定义，还是根本没有这个节点。"""
    name = (node_id or "").strip()
    reason = template_node_reason(workspace_root, name)
    if reason:
        return reason
    path = node_file(workspace_root, name)
    if path is None:
        return f"节点未找到：{name}"
    where = config_layout.logical_path(workspace_root, path)
    _data, problem = read_node_definition(path)
    if not problem:
        return f"节点 {name}（{where}）这次没读成，文件可能正被改写，稍后再试"
    return f"节点 {name}（{where}）不是能用的节点定义：{problem}"


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


def effective_node_type(data: dict[str, Any]) -> str:
    """节点定义实际按哪种节点跑：type 没写是 ai，大小写不论。"""
    return str(data.get("type") or "ai").strip().lower()


def _definition_problem(data: dict[str, Any]) -> str:
    """节点定义为什么用不了，能用时给空串。kind 默认 node、type 默认 ai，都允许省略。"""
    kind = str(data.get("kind") or "node").strip()
    if kind != "node":
        return f"kind 写的是 {kind}，节点只认 node"
    node_type = effective_node_type(data)
    if node_type not in _NODE_TYPES:
        return f"type 写的是 {node_type}，只认 ai、tool"
    return ""


def read_node_definition(path: Path) -> tuple[dict[str, Any] | None, str]:
    """读一份节点文件，返回 (定义, 用不了的原因)。能用时原因是空串。

    读不出来、顶层不是映射时定义是 None；读出来了却不是能用的节点定义（kind、type 写错）时定义照给，原因不空。
    原因只有位置和问题，不带原文：节点能单配 api_key，原因会进日志、控制台和交给模型的结果。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "文件不见了，可能刚被删掉或改名"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"读不了：{config_layout.config_error_text(exc)}"
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        return None, f"YAML 读不出来：{config_layout.yaml_error_text(exc)}"
    if data is None:
        return None, "文件是空的"
    if not isinstance(data, dict):
        return None, "顶层不是 key: value 的映射"
    return data, _definition_problem(data)


def listed_targets(raw: Any) -> list[str]:
    """delegate_targets 里写的名字：去掉空项和重复，顺序不变。"""
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(item.strip() for item in raw if isinstance(item, str) and item.strip()))


def _delegate_targets(workspace_root: Path, node_path: Path, raw: Any) -> list[str]:
    """统一换成现在的 id，派发工具名、白名单、子会话键都按它算；模板和示例剔掉，还不存在的节点照留。"""
    targets: list[str] = []
    for name in listed_targets(raw):
        if template_node_name(name):
            _warn_template_target(node_path, name)
        else:
            targets.append(canonical_node_id(workspace_root, name))
    return list(dict.fromkeys(targets))


def _warn_template_target(node_path: Path, name: str) -> None:
    key = (str(node_path), name)
    if key in _WARNED_TEMPLATE_TARGETS:
        return
    _WARNED_TEMPLATE_TARGETS.add(key)
    logger.warning(
        "%s lists %s in delegate_targets; templates and examples are never dispatched to, so it is left out",
        node_path.name, name,
    )


def template_reference_warnings(data: dict[str, Any]) -> list[str]:
    """节点定义里点名模板或示例的地方，控制台保存时随结果提醒：委派清单里的会被剔掉，旧 id 不会生效。"""
    warnings = [
        f"delegate_targets 里的 {name} 是模板或示例，不会派发过去"
        for name in listed_targets(data.get("delegate_targets"))
        if template_node_name(name)
    ]
    written_id = declared_id(data, stem="")
    if written_id and template_node_name(written_id):
        warnings.append(f"id 写的 {written_id} 是模板或示例的名字，按它找不到这个节点")
    warnings.extend(
        f"aliases 里的 {name} 是模板或示例的名字，按它找不到这个节点"
        for name in _parse_aliases(data, stem="")
        if template_node_name(name)
    )
    return warnings


def load_node(workspace_root: Path, node_id: str) -> Node | None:
    node_path = node_file(workspace_root, node_id)
    if node_path is None:
        return None
    return load_node_file(workspace_root, node_path)


def load_node_file(workspace_root: Path, node_path: Path) -> Node | None:
    """按一份节点文件解析出节点，不管它能不能按 id 找到：控制台给模板、示例报实际工具时也要解析它们。"""
    data, problem = read_node_definition(node_path)
    if data is None or problem:
        return None
    nid = node_path.stem
    node_type = effective_node_type(data)

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

    delegate_targets = _delegate_targets(workspace_root, node_path, data.get("delegate_targets"))

    # 决定 dispatch 默认的 context_mode；手写成 "false"、no 的也算关，和控制台的读法一致
    persistent = bool(parse_bool(data.get("persistent"), False))

    # [2026-05-27] extra dict：收集 yaml 中 core 不认识的字段（如 memory_book），
    # 供插件层通过 ToolContext._node_extra 零 IO 读取。
    _KNOWN_KEYS = {
        "kind", "id", "type", "name", "description", "model", "api_key",
        "base_url", "prompt", "tool_access", "skills", "memories",
        "tool_mode", "output_mode", "provider", "provider_options",
        "delegate_targets", "aliases", "persistent",
    }
    extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    # 委派名单、子会话键、自派判断都按文件名认节点，id 跟着它走；写的不同 id 只当旧 id
    written_id = declared_id(data, nid)
    if written_id and not template_node_name(nid):
        _warn_declared_id(node_path, written_id)

    return Node(
        id=nid,
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
        aliases=list(_effective_aliases(workspace_root, node_path, _old_ids(data, nid))),
        persistent=persistent,
        extra=extra,
    )


def internal_node(node: Node) -> bool:
    """internal 节点回的是给程序看的判定值，switch_node 枚举、终端界面和网页的入口列表都不列它；手写的 "no"、"false" 也算关。"""
    return declares_internal(node.extra or {})


def declares_internal(data: dict[str, Any]) -> bool:
    """节点定义里 internal 开关的读法。控制台列节点时手上只有 YAML，照这一条判，和 internal_node 是同一个结论。"""
    return bool(parse_bool(data.get("internal"), False))


def entry_node_problem(node: Node) -> str:
    """这个节点接不接用户对话（当会话入口、被 switch_node 切过去）：接就给空串，不接给原因。网页按同样三条筛。"""
    if node.type != "ai":
        return f"{node.id} 是工具节点，不接对话"
    if node.id.startswith("system."):
        return f"{node.id} 是系统节点，只跑引擎自己发起的后台任务，不接对话"
    if internal_node(node):
        return f"{node.id} 是 internal 节点，回的是给程序看的判定值，不接对话"
    return ""

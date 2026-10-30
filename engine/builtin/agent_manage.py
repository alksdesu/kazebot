from __future__ import annotations

"""Built-in plugin: create_agent tool for persistent sub-node provisioning.

[2026-05-28] 为什么新建此模块：手动复制 yaml + 修改字段 + 更新 delegate_targets 繁琐且易出错。
怎么做：注册为 engine.builtin 内置工具，通过 PLUGIN_META.tools 声明，与 knowledge_inject.py
中的 save_memory / create_or_update_skill 采用相同的注册路径。
目的：一条工具调用完成子节点创建、记忆目录初始化、委派关系注册。
"""

import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

import yaml

import config_layout
from clonoth_runtime import SwitchValueError, switch_arg
from clonoth_sdk.refusals import error_detail
from engine.memory_namespace import InvalidMemoryNamespace, memory_book_error, memory_dir
from engine.node import (
    missing_node_reason,
    node_file,
    read_node_definition,
    template_node_file,
    template_node_name,
)
from toolbox._common import request_guard
from toolbox.context import ToolContext
from toolbox.redaction import mask_sensitive
from toolbox.text_files import normalize_newlines

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  PLUGIN_META — loader auto-discovery 需要的元数据声明
# ---------------------------------------------------------------------------
# hook_points 为空列表：本模块只提供工具，不注册任何 hook handler。
# loader 会正常实例化 handler_class 并注册 tools，只是不绑定任何 hook。
PLUGIN_META = {
    "handler_class": "AgentManageHandler",
    "hook_points": [],
    "priority": 10,
    "tools": [],  # 在文件末尾由工具函数定义后填充
}


class AgentManageHandler:
    """Minimal handler class required by loader auto-discovery.

    [2026-05-28] 为什么需要这个类：loader.py 的 _instantiate_handler 要求
    PLUGIN_META.handler_class 指向一个可实例化的类。本模块没有 hook 行为，
    只需要一个空壳让 loader 走完流程，tools 声明才能被注册到 ToolRegistry。
    """
    name = "agent_manage"
    priority = 10


# ---------------------------------------------------------------------------
#  Validation
# ---------------------------------------------------------------------------
# [2026-05-28] 节点 ID 校验：与 knowledge_inject.SKILL_NAME_RE 对齐，
# 但节点 ID 允许点号（如 bootstrap.coder），所以单独定义。
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# 审批卡上的预览截这么多字，content_len 照报全长。
_PREVIEW_CHARS = 200


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: create_agent returns several structured paths and
    # flags but should expose data.result as its readable transcript. How: put all
    # previous fields under data with the summary. Purpose: align plugin tools with
    # the unified ok/data/error response schema.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: create_agent validation failures should be readable
    # through data.result. How: wrap the error once. Purpose: avoid legacy plugin
    # error payloads during migration.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


def _guard_err(err: Any) -> dict[str, Any]:
    """策略拒绝 / 用户驳回 / 任务取消。cancelled 要透传，调用方靠它区分「被拒」和「没跑完」。"""
    text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
    out = _err(text)
    if isinstance(err, dict) and err.get("cancelled"):
        out["cancelled"] = True
    return out


def _yaml_preview(definition: dict[str, Any]) -> str:
    """节点定义送审时的预览：密钥键下的值换成 [已隐藏]，其余照原样。

    模板和调用方节点是模型读不到的文件，审批 details 却会进事件流、控制台和调试工具。
    """
    masked = mask_sensitive(definition)
    # 键序和落盘那份一致，审批人对照着看
    return yaml.safe_dump(masked, default_flow_style=False, allow_unicode=True, sort_keys=False)[:_PREVIEW_CHARS]


class _CallerUpdate(NamedTuple):
    """往调用方 delegate_targets 里加新节点的结果。updated 为真时调用方派得到新节点。"""

    updated: bool = False
    already_listed: bool = False
    denied: str = ""
    error: str = ""


_DELEGATE_TARGETS = "delegate_targets"
_NULL_TAG = "tag:yaml.org,2002:null"


class _TargetsUneditable(ValueError):
    """delegate_targets 没法只改这一项、其余原文不动地加上新名字。消息只有位置和问题，不带原文。"""


def _node_text(path: Path) -> str:
    """节点文件原文：严格按 UTF-8 解码，开头的 BOM 留着，CRLF 统一成 LF（写回时 write_config_text 照原文件的换行写）。"""
    try:
        text = normalize_newlines(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise _TargetsUneditable(f"读不了：{config_layout.config_error_text(exc)}") from None
    if "\r" in text:
        raise _TargetsUneditable("文件里有单独的回车符，按行改不准")
    return text


def _rendered_target(name: str) -> str:
    """name 写成 YAML 标量的样子：123、yes、on 这类会被读成别的类型的名字带上引号。"""
    return yaml.safe_dump([name], allow_unicode=True, default_flow_style=False)[len("- "):].rstrip("\n")


def _list_indent(root: yaml.MappingNode) -> int:
    """文件里顶层块列表的「- 」比键多缩进几格，新写的列表照着来；一个都没有按两格。"""
    for key, value in root.value:
        if isinstance(value, yaml.SequenceNode) and not value.flow_style and value.value:
            return max(0, value.start_mark.column - key.start_mark.column)
    return 2


def _line_end(text: str, index: int) -> int:
    end = text.find("\n", index)
    return len(text) if end == -1 else end


def _appended_targets(text: str, root: yaml.MappingNode, item: str) -> str:
    """没写 delegate_targets：在顶层映射末尾补上这个键和一项，放在文档结束记号之前。"""
    margin = " " * root.value[0][0].start_mark.column
    block = f"{margin}{_DELEGATE_TARGETS}:\n{margin}{' ' * _list_indent(root)}- {item}\n"
    position = root.end_mark.index
    if position and text[position - 1] != "\n":
        block = "\n" + block
    return text[:position] + block + text[position:]


def _filled_targets(text: str, root: yaml.MappingNode, key: yaml.Node, value: yaml.Node, item: str) -> str:
    """delegate_targets 空着或写的 ~、null：键那一行去掉空值、留着行尾注释，下一行接上新的一项。"""
    line_start = text.rfind("\n", 0, key.start_mark.index) + 1
    line_end = _line_end(text, key.end_mark.index)
    if value.end_mark.index > line_end:
        raise _TargetsUneditable("delegate_targets 的空值没写在键那一行")
    line = (text[line_start:value.start_mark.index].rstrip() + text[value.end_mark.index:line_end]).rstrip()
    margin = " " * (key.start_mark.column + _list_indent(root))
    return text[:line_start] + line + f"\n{margin}- {item}" + text[line_end:]


def _block_target_added(text: str, value: yaml.SequenceNode, item: str) -> str:
    """逐行写的列表：新的一项接在最后一项后面，「- 」对齐第一项；最后一项后面的空行和注释留给下一个键。"""
    margin = " " * value.start_mark.column
    position = value.value[-1].end_mark.index
    if text[position - 1] == "\n":
        # 最后一项是块标量，结尾已经换过行
        return text[:position] + f"{margin}- {item}\n" + text[position:]
    end = _line_end(text, position)
    return text[:end] + f"\n{margin}- {item}" + text[end:]


def _flow_target_added(text: str, value: yaml.SequenceNode, item: str) -> str:
    """一行写完的 [...]：补在收尾的 ] 前面。跨行的不改：中间夹着注释时补的位置拿不准。"""
    if value.start_mark.line != value.end_mark.line:
        raise _TargetsUneditable("delegate_targets 写成了跨行的 [...]")
    close = value.end_mark.index - 1
    if text[close] != "]":
        raise _TargetsUneditable("delegate_targets 的 [...] 不是以 ] 收尾")
    if not value.value:
        opening = text.rfind("[", value.start_mark.index, close)
        if opening == -1:
            raise _TargetsUneditable("delegate_targets 的 [...] 找不到开头")
        return text[:opening + 1] + item + text[close:]
    position = value.value[-1].end_mark.index
    tail = text[position:close].strip()
    if tail == ",":
        comma = text.index(",", position, close)
        return text[:comma + 1] + " " + item + text[comma + 1:]
    if tail:
        raise _TargetsUneditable("delegate_targets 的 [...] 末尾有认不出的内容")
    return text[:position] + ", " + item + text[position:]


def _inserted_target(text: str, root: yaml.MappingNode, key: yaml.Node, value: yaml.Node, item: str) -> str:
    if isinstance(value, yaml.ScalarNode) and value.tag == _NULL_TAG:
        return _filled_targets(text, root, key, value, item)
    if not isinstance(value, yaml.SequenceNode):
        raise _TargetsUneditable("delegate_targets 不是列表")
    if value.flow_style:
        return _flow_target_added(text, value, item)
    return _block_target_added(text, value, item)


def _with_delegate_target(text: str, data: dict[str, Any], name: str) -> str:
    """只在 delegate_targets 里多出 name 的原文：注释、空行、键的顺序和别的值逐字不动。

    data 是 read_node_definition 读到的定义，原文解析出来和它不一样（读的时候被改过）就不改。
    改完重新解析，必须正好是 data 加上这一项，锚点、别名、合并键让别处跟着变的都不改。改不了抛 _TargetsUneditable。
    """
    try:
        current = config_layout.load_yaml_text(text)
        root = yaml.compose(text, Loader=config_layout.ConfigLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise _TargetsUneditable(f"读不出来：{config_layout.config_error_text(exc)}") from None
    if current != data:
        raise _TargetsUneditable("读的时候文件被改过")
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise _TargetsUneditable("整份节点写成了一行的 {...}")
    pairs = [
        (key, value) for key, value in root.value
        if isinstance(key, yaml.ScalarNode) and key.value == _DELEGATE_TARGETS
    ]
    if len(pairs) > 1:
        raise _TargetsUneditable("delegate_targets 写了不止一次")
    item = _rendered_target(name)
    updated = _inserted_target(text, root, *pairs[0], item) if pairs else _appended_targets(text, root, item)
    expected = {**data, _DELEGATE_TARGETS: [*(data.get(_DELEGATE_TARGETS) or []), name]}
    try:
        written = config_layout.load_yaml_text(updated)
    except yaml.YAMLError:
        written = None
    if written != expected:
        raise _TargetsUneditable("改完对不上原来的内容，可能用了锚点、别名或合并键")
    return updated


async def _add_delegate_target(ctx: ToolContext, caller_node_id: str, name: str) -> _CallerUpdate:
    """把新节点加进调用方的 delegate_targets。改不成的原因只带位置和问题：节点文件里可能写着 api_key。"""
    workspace_root = ctx.workspace_root
    # 读的是调用者实际加载的那份文件：被系统节点顶掉的同名用户文件改了也不生效
    caller_path = node_file(workspace_root, caller_node_id)
    if caller_path is None:
        return _CallerUpdate(error=missing_node_reason(workspace_root, caller_node_id))
    where = config_layout.logical_path(workspace_root, caller_path)
    caller_target = config_layout.node_edit_target(workspace_root, caller_path)
    caller_rel = config_layout.logical_path(workspace_root, caller_target)
    # 先取指纹再读：送审要等人，等的时候文件被改过就不写，免得盖掉那次改动。
    base_hash = config_layout.file_hash(caller_target)
    caller_data, problem = read_node_definition(caller_path)
    if caller_data is None:
        return _CallerUpdate(error=f"调用方节点 {caller_node_id}（{where}）：{problem}")
    if problem:
        return _CallerUpdate(error=f"调用方节点 {caller_node_id}（{where}）不是能用的节点定义：{problem}")
    targets = caller_data.get(_DELEGATE_TARGETS)
    if targets is None:
        targets = []
    if not isinstance(targets, list):
        return _CallerUpdate(
            error=f"调用方节点 {caller_node_id}（{where}）的 delegate_targets 不是列表，引擎按没写算，改成列表再加；没有改它",
        )
    if name in targets:
        return _CallerUpdate(updated=True, already_listed=True)
    try:
        # 只动这一项：整份 safe_dump 回去会洗掉注释、按字母重排键
        caller_yaml = _with_delegate_target(_node_text(caller_path), caller_data, name)
    except _TargetsUneditable as exc:
        return _CallerUpdate(
            error=f"调用方节点 {caller_node_id}（{where}）的 delegate_targets 没法只改这一项：{exc}；没有改它",
        )
    caller_data[_DELEGATE_TARGETS] = [*targets, name]
    try:
        # 改的是别人的委派清单，等于给调用者开一条新的下派路径，
        # 和写节点定义一样送策略，不能因为「只加一行」就免检。
        _op, err = await request_guard(
            ctx,
            "write_file",
            {
                "path": caller_rel,
                "content_preview": _yaml_preview(caller_data),
                "content_len": len(caller_yaml),
                "delegate_target_added": name,
            },
        )
    except Exception as exc:
        # 新节点已经落盘：异常往外抛，模型会以为整个没建成。
        log.warning("create_agent: policy check for %s failed", caller_rel, exc_info=True)
        return _CallerUpdate(error=f"{caller_rel} 送审没走完：{error_detail(exc) or type(exc).__name__}")
    if err is not None:
        return _CallerUpdate(denied=str(err.get("error", "denied")) if isinstance(err, dict) else str(err))
    try:
        config_layout.write_config_text(
            caller_target, caller_yaml, base_hash=base_hash,
            mode=config_layout.private_mode_for(workspace_root, caller_target),
        )
    except config_layout.ConfigConflict:
        return _CallerUpdate(error=f"{caller_rel} 在等审批的时候被改过，没有写，免得盖掉那次改动")
    except OSError as exc:
        return _CallerUpdate(error=f"{caller_rel} 写不进去：{config_layout.config_error_text(exc)}")
    return _CallerUpdate(updated=True)


# ---------------------------------------------------------------------------
#  create_agent tool
# ---------------------------------------------------------------------------

async def create_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new persistent sub-node from an existing node yaml template.

    [2026-05-28] 工作流程：
    1. 按节点 id 找模板 yaml（和 load_node 同一个查法：系统目录、config/nodes/、仓库默认依次找，也认旧 id）
    2. 替换 id / name / description / persistent / memory_book / provider 字段
    3. 写入 config/nodes/{name}.yaml
    4. 可选：追加到 caller_node_id 的 delegate_targets
    5. 创建 data/memory/{memory_book}/ 目录
    """
    name = str(args.get("name", "")).strip()
    template = str(args.get("template", "")).strip()

    if not name:
        return _err("name is required")
    if not template:
        return _err("template is required")
    if not _NODE_ID_RE.fullmatch(name):
        return _err(f"invalid node id: {name!r} — only [A-Za-z0-9][A-Za-z0-9_.\\-]{{0,63}} allowed")
    if template_node_name(name):
        return _err(
            f"invalid node id: {name!r} — names starting with _ or containing .example are templates "
            "and examples, which are never loaded as nodes"
        )
    try:
        persistent = switch_arg(args, "persistent", True)
    except SwitchValueError as exc:
        return _err(exc)

    display_name = str(args.get("display_name", "")).strip() or name
    description = str(args.get("description", "")).strip()
    memory_book = str(args.get("memory_book", "")).strip() or name
    provider = str(args.get("provider", "")).strip()
    caller_node_id = str(args.get("caller_node_id", "")).strip()

    workspace_root = ctx.workspace_root

    # memory_book 会拼成 data/memory 下的目录名，记忆工具在那里读写、整本覆盖或删除；
    # 落盘任何东西之前先挡掉，免得留下一个指着坏目录的节点。
    memory_book_reason = memory_book_error(memory_book)
    if memory_book_reason:
        return _err(f"invalid memory_book {memory_book!r}: {memory_book_reason}")
    try:
        mem_dir = memory_dir(workspace_root, memory_book)
    except InvalidMemoryNamespace as exc:
        return _err(str(exc))

    # Step 1: 检查节点 id 是否已被占用，避免误覆盖。同名系统节点、把它声明成旧 id 的节点
    # 都会让新文件永远加载不到；后者还会把写着旧 id 的会话和配置悄悄引到新节点上。
    target_path = config_layout.nodes_dir(workspace_root) / f"{name}.yaml"
    taken_by = node_file(workspace_root, name)
    if taken_by is not None:
        return _err(f"node id already taken: {name} resolves to {config_layout.logical_path(workspace_root, taken_by)}")

    # Step 2: 查找模板 yaml。模板名是模型给的参数，只认节点目录里能按 id 找到的文件；
    # _ 开头、带 .example 的模板和示例运行期不当节点，但本来就是给这里当底稿的。
    tmpl_path = node_file(workspace_root, template) or template_node_file(workspace_root, template)
    if tmpl_path is None:
        return _err(
            f"template not found: {template} (checked engine/system_nodes/, config/nodes/ and defaults/nodes/)"
        )

    # Step 3: 解析模板 yaml。节点文件里可能写着 api_key：交给模型的原因只带位置和问题。
    tmpl_where = config_layout.logical_path(workspace_root, tmpl_path)
    tmpl_data, tmpl_problem = read_node_definition(tmpl_path)
    if tmpl_data is None:
        return _err(f"failed to read template {tmpl_where}: {tmpl_problem}")
    if tmpl_problem:
        # kind、type 写错的模板抄出来的节点一样加载不了。
        return _err(f"template {tmpl_where} is not a usable node definition: {tmpl_problem}")

    # Step 4: 替换核心字段
    tmpl_data["id"] = name
    tmpl_data["name"] = display_name
    if description:
        tmpl_data["description"] = description
    tmpl_data["persistent"] = persistent
    # memory_book 作为 extra 字段，会被 Node.load_node 收进 extra dict，
    # 供 save_memory 等插件通过 ToolContext._node_extra 零 IO 读取
    tmpl_data["memory_book"] = memory_book
    if provider:
        tmpl_data["provider"] = provider

    # Step 5: 写入新节点 yaml
    # 节点定义里有 prompt、模型和 tool_access，落一份就等于加一条新的执行路径，
    # 和 write_file 写 config/nodes/** 是同一件事，走同一道策略。
    _node_yaml = yaml.safe_dump(tmpl_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    _rel_target = f"{config_layout.NODES_DIR}/{name}.yaml"
    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": _rel_target,
            "content_preview": _yaml_preview(tmpl_data),
            "content_len": len(_node_yaml),
            "create_agent": True,
            # 预览里的密钥打过码，审批人靠这一项知道抄的是哪份文件。
            "template": tmpl_where,
            # 预览只截前 200 字，memory_book 常落在后面；审批人得看见新节点会读写哪个记忆库。
            "memory_book": memory_book,
        },
    )
    if err is not None:
        return _guard_err(err)

    # 节点 YAML 可以单配 api_key，新建就只给属主
    config_layout.write_config_text(
        target_path, _node_yaml, mode=config_layout.private_mode_for(workspace_root, target_path),
    )

    # Step 6: 更新调用者的 delegate_targets（如果指定了 caller_node_id）
    # 追加新节点到调用者的委派列表，使调用者可以 dispatch 到新节点
    caller = await _add_delegate_target(ctx, caller_node_id, name) if caller_node_id else _CallerUpdate()

    # Step 7: 创建记忆 namespace 目录（mkdir -p）
    mem_dir.mkdir(parents=True, exist_ok=True)

    summary = f"Agent created: {name}"
    if caller.already_listed:
        summary += f"; {caller_node_id} already lists it in delegate_targets"
    elif caller_node_id and not caller.updated:
        summary += f"; delegate_targets of {caller_node_id} not updated: {caller.denied or caller.error}"
    return _ok(
        summary,
        node_id=name,
        config_path=_rel_target,
        memory_book=memory_book,
        memory_dir=f"data/memory/{memory_book}/",
        persistent=persistent,
        caller_updated=caller.updated,
        # 委派清单没改成要说出来，否则模型以为能 dispatch 到新节点，实际派不过去。
        caller_denied=caller.denied,
        caller_error=caller.error,
        template=template,
    )


# ---------------------------------------------------------------------------
#  PLUGIN_META tool declarations
# ---------------------------------------------------------------------------
# [2026-05-28] 与 knowledge_inject.py 中 save_memory 等工具的注册方式一致：
# 在函数定义后将其引用填入 PLUGIN_META["tools"]，由 loader._register_declared_tools
# 注册到 ToolRegistry。
PLUGIN_META["tools"] = [
    {
        "name": "create_agent",
        "description": (
            "从现有节点 yaml 模板创建新的持久化子节点。"
            "读取模板节点配置，替换 id/name/description 等字段后写入 config/nodes/ 目录，"
            "并可选地将新节点追加到调用者的 delegate_targets 中。"
            "约定：每个长期方向（研究领域/项目/专属助手）应各自新建一个 persistent 节点，"
            "并使用独立的 memory_book（记忆库物理隔离），不要用同一个节点靠 context_key 分叉多个方向。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "新节点 ID，如 bob",
                },
                "display_name": {
                    "type": "string",
                    "description": "显示名，不填则用 name",
                },
                "template": {
                    "type": "string",
                    "description": "模板节点 ID，如 bootstrap.coder。依次查 engine/system_nodes/、config/nodes/、defaults/nodes/",
                },
                "description": {
                    "type": "string",
                    "description": "节点描述",
                },
                "memory_book": {
                    "type": "string",
                    "description": "记忆 namespace（记忆物理落在 data/memory/{memory_book}/，与其他节点隔离），不填则默认等于 name。每个长期方向应有独立的 memory_book。",
                },
                "persistent": {
                    "type": "boolean",
                    "description": "是否持久化，默认 true。persistent=true 的节点被 dispatch 时默认 context_mode=accumulate（长期累积上下文）；一次性/工具型节点应设为 false，dispatch 时默认 fresh。",
                },
                "provider": {
                    "type": "string",
                    "description": "覆盖 provider",
                },
                "caller_node_id": {
                    "type": "string",
                    "description": "调用者节点 ID，用于自动追加 delegate_targets",
                },
            },
            "required": ["name", "template"],
        },
        "func": create_agent,
    },
]

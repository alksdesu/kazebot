"""伪工具 spec 构建器和工具列表 helpers。

从 ai_step.py 中拆出，纯数据结构构建。伪工具叫什么名字由 toolbox/tool_names.py 统一声明，
注册表拒收同名外部工具也看那一份。
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.tool_names import DISPATCH_TOOL_PREFIX

from ..dispatch_batch import (
    AWAIT_ALL,
    AWAIT_ANY,
    ISOLATION_KEY,
    ISOLATION_WORKTREE,
    MAX_AWAIT_HANDLES,
    MAX_DISPATCH_BATCH_ITEMS,
    MIN_TIMEOUT_SEC,
    OWN_SESSION_MODES,
    REPO_KEY,
    DispatchTimeouts,
)
from ..todo_list import MAX_TODO_CONTENT_CHARS, MAX_TODO_ITEMS, TODO_STATUSES, TODO_TOOL_NAME
from ..worktree_report import MERGE_TOOL

if TYPE_CHECKING:
    from ..node import Node

logger = logging.getLogger(__name__)


import re as _re

_TOOL_NAME_SANITIZE_RE = _re.compile(r'[^a-zA-Z0-9_-]')
# Reverse mapping: sanitized tool name suffix → original node id
_DISPATCH_TOOL_REVERSE: dict[str, str] = {}


def _sanitize_node_id_for_tool(node_id: str) -> str:
    """Sanitize a node id to comply with OpenAI tool name pattern [a-zA-Z0-9_-]."""
    return _TOOL_NAME_SANITIZE_RE.sub('_', str(node_id or '').strip())


def _dispatch_tool_name(target_id: str) -> str:
    """Build the public per-target dispatch tool name."""
    raw = str(target_id or '').strip()
    sanitized = _sanitize_node_id_for_tool(raw)
    name = f"{DISPATCH_TOOL_PREFIX}{sanitized}"
    _DISPATCH_TOOL_REVERSE[sanitized] = raw
    return name


def _dispatch_target_from_tool_name(tool_name: str) -> str:
    """Extract the original target id from a dispatch-{sanitized_id} tool name."""
    name = str(tool_name or "").strip()
    if not name.startswith(DISPATCH_TOOL_PREFIX):
        return ""
    sanitized_suffix = name[len(DISPATCH_TOOL_PREFIX):].strip()
    # Reverse lookup: sanitized → original node id
    return _DISPATCH_TOOL_REVERSE.get(sanitized_suffix, sanitized_suffix)

# [2026-05-07] 控制流伪工具集合。
# 原因：finish 只表示“结束当前任务并提交结果”，不是可长期回放的业务工具。
# 做法：把控制流工具名集中声明，供运行期、存储层和 L2 历史构造层共用。
# 目的：普通工具仍保留 provider 所需配对，finish 不再污染长期历史。
# [AutoC 2026-05-31] Why: ask has the same terminal control-flow shape as
# finish in Phase 0. How: classify both names as control tools for shared history
# repair and future routing helpers. Purpose: keep control-flow bookkeeping
# consistent when ask appears in stored tool turns.
CONTROL_TOOL_NAMES = frozenset({"finish", "ask"})


def _is_control_tool_name(name: str) -> bool:
    """Return whether ``name`` is an engine control-flow tool."""
    return str(name or "").strip() in CONTROL_TOOL_NAMES


# ---------------------------------------------------------------------------
#  v3 伪工具 spec 构建
# ---------------------------------------------------------------------------

AWAIT_DISPATCHES_TOOL_NAME = "await_dispatches"
LIST_DISPATCHES_TOOL_NAME = "list_dispatches"
CANCEL_DISPATCH_TOOL_NAME = "cancel_dispatch"
DISPATCH_CONTROL_TOOL_NAMES = frozenset({
    AWAIT_DISPATCHES_TOOL_NAME, LIST_DISPATCHES_TOOL_NAME, CANCEL_DISPATCH_TOOL_NAME,
})

_REPO_DESCRIPTION = (
    f"只在 {ISOLATION_KEY}={ISOLATION_WORKTREE} 时有效：要隔离的仓库，填它的绝对路径；不填就是工作区所在的仓库。"
    "只能是工作区所在的仓库，或者策略 extra_roots 下的 git 仓库。"
)


def _isolation_description(*, can_merge: bool) -> str:
    if can_merge:
        after = f"做完后改动按 diff 随结果交回，你审过之后用 {MERGE_TOOL} 合回或放弃。"
    else:
        after = f"做完后改动按 diff 随结果交回；这个节点不能调用 {MERGE_TOOL}，审过的改动交给上级或用户去合回。"
    return (
        f"{ISOLATION_WORKTREE}=让目标节点在自己的 git worktree（隔离副本）里改代码、跑命令，不动原仓库；"
        f"副本从原仓库当前的提交（HEAD）检出，原仓库里还没提交的改动不在里面。{after}"
        "几个子任务要同时改同一个仓库，或者改动要先审再进仓库时用；只读、查询、生图这类任务不要用。"
        f"用 continue_from 接着一个隔离过的子 agent 时，沿用它原来的副本，不用再填 {ISOLATION_KEY}。"
    )


def _isolation_review(*, can_merge: bool) -> str:
    """可隔离目标的委派工具说明里多的那一段：交回的副本怎么审、怎么合回或放弃。"""
    head = (
        f"带 {ISOLATION_KEY}={ISOLATION_WORKTREE} 派出的，结果里附有副本的改动清单、diff 和完整补丁的位置：先看清单和 diff，"
        "补丁文件和副本目录都在工作区外，读它们要审批。"
    )
    if can_merge:
        return head + (
            f"满意就调用 {MERGE_TOOL} 合回，不要就用它带上 abandon=true 放弃；合回冲突时如实说明冲突在哪，"
            "不要绕开合回把改动手抄进原仓库；既不合回也不放弃的副本过了保留期会被清理。"
        )
    return head + (
        f"这个节点不能调用 {MERGE_TOOL}，审过的改动交给上级或用户去合回，不要自己把改动手抄进原仓库；"
        "副本过了保留期会被清理。"
    )


# [2026-05-04] Shared parameter schema for dynamic per-target dispatch tools.
# Why: dispatch:{target_id} fixes the target in the tool name, so exposing a
# target argument would let model output disagree with the registered tool.
# How: reuse the legacy dispatch parameters except for target, and document the
# default context behavior in schema. Purpose: keep execution compatible with the
# existing supervisor dispatch API while making each delegate target visible as a
# first-class tool.
def _dispatch_delegate_parameters(
    *,
    allow_wait: bool = True,
    timeouts: DispatchTimeouts | None = None,
    isolation: bool = False,
    can_merge: bool = True,
    to_self: bool = False,
) -> dict[str, Any]:
    """isolation=True 时多带 isolation、repo 两个参数，其余参数一字不变。

    to_self=True 是委派给调用方自己这个节点：context_mode 只有 fresh、fork，也没有只对 accumulate 有效的 context_key。
    """
    limits = timeouts or DispatchTimeouts()
    if to_self:
        context_mode_description = (
            "子节点上下文模式。fresh=从零无历史（不填就是它）；fork=继承你当前的对话历史。"
            "委派给自己时不能沿用会话（accumulate）：同时派出的几份、以及它们再派出的，会落进同一个会话；"
            "要接着之前派出的那一份，用 continue_from。"
        )
    else:
        context_mode_description = (
            "子节点上下文模式。fresh=每次从零无历史（一次性搜索/一次性工程执行/审查/debug 推荐）；"
            "fork=继承父节点对话历史（需要当前上下文但不污染长期子节点时用）；"
            "accumulate=首次从零、后续恢复自己的长期上下文（长期人格/专属助手/立项长期维护 agent 用）。"
            "不填时按目标节点是否为持久节点自动选择：持久节点默认 accumulate，其余默认 fresh。"
        )
        if allow_wait:
            context_mode_description += (
                "wait=true 时子节点的会话随本任务结束清理，accumulate 只在这次任务里延续；要跨任务长期累积，用 wait=false。"
            )
    properties: dict[str, Any] = {
        "instruction": {
            "type": "string",
            "description": "给目标节点的清晰、具体、可执行的指令。说明要做什么、为什么、你已经知道什么。",
        },
        "context_mode": {
            "type": "string",
            "enum": list(OWN_SESSION_MODES) if to_self else [*OWN_SESSION_MODES, "accumulate"],
            "description": context_mode_description,
        },
        "context_key": {
            "type": "string",
            "description": "上下文继承标识（仅 accumulate 模式有效）。同一 context_key 的历次任务共享同一条上下文链，不同 context_key 之间文件隔离——用于长期研究员按主题/项目分开积累。不填则按目标节点 ID 查找。",
        },
        "attachment_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
        },
        "continue_from": {
            "type": "string",
            "description": (
                "接着上一个子 agent 干：填它的委派结果里给的句柄（task_id@session_id），目标节点要和上次一样。"
                f"子 agent 带着自己原来的对话历史继续，{'context_mode' if to_self else 'context_mode 和 context_key'} 不再生效；"
                "当时用 wait 派的要用 wait 接着派，异步派的要异步接着派。"
            ),
        },
    }
    if to_self:
        del properties["context_key"]
    if allow_wait:
        properties["wait"] = {
            "type": "boolean",
            "description": (
                "true：挂起当前任务，等目标节点做完、把结果交回来再继续；同一轮发出的多个 wait 委派并行执行，"
                "全部完成后结果一起在下一条消息里给出。false（默认）：异步派出，立即返回句柄（handle），"
                "结果稍后作为一条新消息送达。"
            ),
        }
        properties["timeout_sec"] = {
            "type": "number",
            "description": (
                f"只对 wait=true 有效：最多等这个子节点多少秒（{MIN_TIMEOUT_SEC}-{limits.maximum}，超出按上限算）。"
                "到点还没做完就按失败交回：它会被停下，会话保留，之后可以用 continue_from 带上它的句柄接着做；"
                "同一批的其它委派照常收齐。不填就一直等到它做完。"
            ),
        }
    if isolation:
        properties[ISOLATION_KEY] = {
            "type": "string",
            "enum": [ISOLATION_WORKTREE],
            "description": _isolation_description(can_merge=can_merge),
        }
        properties[REPO_KEY] = {
            "type": "string",
            "description": _REPO_DESCRIPTION,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["instruction"],
    }


def _dispatch_delegate_spec(
    target_id: str,
    info: Mapping[str, Any] | None = None,
    *,
    allow_wait: bool = True,
    timeouts: DispatchTimeouts | None = None,
    isolation: bool = False,
    can_merge: bool = True,
    to_self: bool = False,
) -> dict:
    """Build one dispatch_to_<target> pseudo-tool spec; allow_wait=False leaves out the wait parameter.

    isolation=True 给可以隔离的目标：多 isolation、repo 两个参数，说明里多一段交回的副本怎么审、怎么合回。
    to_self=True 给调用方自己这个节点：派出去的是另一份它，不能沿用会话。
    """
    target = str(target_id or "").strip()
    node_name = str((info or {}).get("name") or target).strip() or target
    node_description = str((info or {}).get("description") or node_name or target).strip()
    desc_parts = [
        f"将任务委派给固定目标节点 {node_name}（{target}）。",
        node_description,
        "",
        "目标节点在自己的会话里执行。参数中不再提供 target，该工具名已经固定了目标节点。",
    ]
    if to_self:
        desc_parts.append("目标就是你自己这个节点：派出去的是另一份你，在它自己的会话里做。")
    if allow_wait:
        desc_parts += [
            "两种用法：",
            "- wait: true（等结果）：当前任务挂起，目标节点做完后把结果交回来，你接着往下做。"
            "后续步骤要用到它的结果时用这种，例如要看命令输出、要拿读到的内容继续处理。"
            "本工具当轮只返回「已派出」，结果在下一条消息里给出；同一轮的多个 wait 委派并行执行，全部完成后一起返回。"
            "可以用 timeout_sec 给某一项设等待上限。",
            "- wait: false（默认，异步）：立即返回句柄（handle），不含执行结果；结果稍后作为一条新消息送达，那时再向用户汇报。"
            "不需要结果，或者要跑很久（例如生图）时用这种。本轮不要臆测结果，也不要承诺完成时间。"
            f"之后想在本任务里拿结果，用 {AWAIT_DISPATCHES_TOOL_NAME} 带上句柄等它；"
            f"用 {LIST_DISPATCHES_TOOL_NAME} 查看进度，用 {CANCEL_DISPATCH_TOOL_NAME} 取消。",
            f"同一轮最多 {MAX_DISPATCH_BATCH_ITEMS} 个 wait 委派。和 wait 委派同一轮调用的 finish/ask 不会执行，"
            "结果回来之后再单独调用 finish。",
        ]
    else:
        desc_parts += [
            "本工具立即返回句柄（handle），不含执行结果。",
            "结果稍后会作为一条新消息送达，那时再向用户汇报；本轮不要臆测结果，也不要承诺完成时间。",
        ]
    if isolation:
        desc_parts.append(_isolation_review(can_merge=can_merge))
    desc_parts.append("需要并行委派时，可以在同一轮调用多个委派工具。")
    return {
        "type": "function",
        "function": {
            "name": _dispatch_tool_name(target),
            "description": "\n".join(part for part in desc_parts if part is not None),
            "parameters": _dispatch_delegate_parameters(
                allow_wait=allow_wait, timeouts=timeouts, isolation=isolation, can_merge=can_merge, to_self=to_self,
            ),
        },
    }


def _info_by_id(downstream_info: list[dict[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    return {
        str(info.get("id") or "").strip(): info
        for info in (downstream_info or [])
        if isinstance(info, Mapping) and str(info.get("id") or "").strip()
    }


def _dispatch_delegate_specs(
    targets: list[str],
    downstream_info: list[dict[str, Any]] | None = None,
    *,
    allow_wait: bool = True,
    timeouts: DispatchTimeouts | None = None,
    isolating: Collection[str] = (),
    can_merge: bool = True,
    caller_id: str = "",
) -> list[dict]:
    """Build dynamic dispatch pseudo-tools for all delegate targets; targets in isolating get isolation and repo.

    caller_id 是调用方自己的节点 id：委派清单里列着它自己时，那个工具按委派给自己来建。
    """
    info_by_id = _info_by_id(downstream_info)
    offered = set(isolating)
    result: list[dict] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        result.append(_dispatch_delegate_spec(
            target, info_by_id.get(target), allow_wait=allow_wait, timeouts=timeouts,
            isolation=target in offered, can_merge=can_merge, to_self=bool(caller_id) and target == caller_id,
        ))
    return result


def isolating_targets(
    targets: list[str], downstream_info: list[dict[str, Any]] | None, *, in_isolation: bool,
) -> list[str]:
    """委派时给 isolation、repo 的目标：节点信息说它可以隔离的（isolatable，节点 YAML 写了 isolation: worktree）。

    任务自己已在隔离副本里时一个都不给：子任务只能沿用这个副本，嵌套的副本会从原仓库的 HEAD 起步、
    合回原仓库，绕开外层副本收尾时的审。
    """
    if in_isolation:
        return []
    info_by_id = _info_by_id(downstream_info)
    picked: list[str] = []
    for raw_target in targets:
        target = str(raw_target or "").strip()
        info = info_by_id.get(target)
        if target and target not in picked and info is not None and info.get("isolatable") is True:
            picked.append(target)
    return picked


def _await_dispatches_spec(timeouts: DispatchTimeouts | None = None) -> dict:
    """伪工具：挂起当前任务，等之前异步派出的委派交回结果。"""
    limits = timeouts or DispatchTimeouts()
    return {
        "type": "function",
        "function": {
            "name": AWAIT_DISPATCHES_TOOL_NAME,
            "description": (
                "等之前异步（wait=false）委派出去的子任务交回结果。当前任务挂起，等到这些句柄的结果全部回来"
                f"（mode={AWAIT_ALL}，默认），或者其中任意一个回来（mode={AWAIT_ANY}），或者到了 timeout_sec，"
                "就带着结果恢复；结果在下一条消息里给出，每个句柄一项，失败、取消的如实标出。\n\n"
                "- 在调用之前就已经结束的委派也直接给出结果；如果它的结果早先已经作为一条单独的消息交回过，"
                "会注明，不要重复汇报。\n"
                "- 等到时还没做完的标成「还在运行」，它的结果留给你：之后再调用本工具收取；"
                "本任务结束时还没收取的，会作为单独的消息交回。\n"
                "- 句柄写错、或者不属于这段对话的，会注明拿不到结果，不影响其余的。\n"
                "- 本工具当轮只返回「正在等待」。和它同一轮调用的 finish/ask 不会执行；"
                "也不能和 wait=true 的委派放在同一轮，同一轮只能调用一次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_AWAIT_HANDLES,
                        "description": (
                            f"要等的委派句柄（task_id@session_id），照抄异步委派返回的 handle，一次最多 {MAX_AWAIT_HANDLES} 个。"
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": [AWAIT_ALL, AWAIT_ANY],
                        "description": (
                            f"{AWAIT_ALL}=全部结束再恢复（默认）；{AWAIT_ANY}=任意一个结束就恢复，其余的之后还能再收。"
                        ),
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": (
                            f"最多等多少秒，不填就等 {limits.await_default} 秒（{MIN_TIMEOUT_SEC}-{limits.maximum}，"
                            "超出按上限算）。到点时已经结束的照常给出结果，没结束的标成还在运行。"
                        ),
                    },
                },
                "required": ["handles"],
            },
        },
    }


def _list_dispatches_spec() -> dict:
    """伪工具：查看这段对话里的异步委派。"""
    return {
        "type": "function",
        "function": {
            "name": LIST_DISPATCHES_TOOL_NAME,
            "description": (
                "列出这段对话里异步（wait=false）委派出去的子任务：还没结束的（排队、运行中、正在等它自己的子任务）"
                "全部列出，再加上最近结束的几个。每项给出句柄、目标节点、状态、指令摘要和已经过了多久；"
                "结束的还给出结局、摘要，以及结果交到了哪里。只查看，不改变任何东西。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _cancel_dispatch_spec() -> dict:
    """伪工具：取消一个异步委派和它派出去的子任务。"""
    return {
        "type": "function",
        "function": {
            "name": CANCEL_DISPATCH_TOOL_NAME,
            "description": (
                "取消一个还在运行的异步（wait=false）委派，连同它派出去的全部子任务。正在执行的会在当前这一步做完后停下，"
                f"被取消的委派不再交回结果，正在用 {AWAIT_DISPATCHES_TOOL_NAME} 等它的会收到「已取消」。"
                "已经结束的委派不用取消。只能取消这段对话里的委派，不能取消自己或派出自己的任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "要取消的委派句柄（task_id@session_id），照抄异步委派返回的 handle。",
                    },
                },
                "required": ["handle"],
            },
        },
    }


def _dispatch_control_specs(*, allow_await: bool, timeouts: DispatchTimeouts | None = None) -> list[dict]:
    """有委派目标的节点才给；系统任务不能挂起，不给 await_dispatches。"""
    specs = [_await_dispatches_spec(timeouts)] if allow_await else []
    specs += [_list_dispatches_spec(), _cancel_dispatch_spec()]
    return specs


def _finish_spec() -> dict:
    """Build the finish pseudo-tool spec.

    【Fix 4】description 重写：
    - 原文 "describe what HAS BEEN done in text" 措辞误导模型把 text 写成「已完成 X」
      之类的状态汇报，把真正的交付载荷（如 compactor 的 <analysis><summary>）
      留在自由正文里，配合旧的隐式兜底导致泄漏。
    - 新版强调 text 是「实际交付内容」，不是状态描述；并明确自由正文不会送达用户/调用方，
      只有 reply()/finish() 的 text 才会到达。
    """
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. If you have other tools to call (like execute_command or save_memory), you MUST wait for them to complete (`ok`) before calling finish.\n\n"
                "⚠️ finish MUST be called ALONE — never in the same turn as other tools (except reply). "
                "If you call finish alongside execute_command, save_memory, a dispatch tool, or any other tool, those other tools still run but finish is REJECTED and does not end the node: read their results, then call finish again on its own. "
                "Always: execute tools first → wait for results → then call finish separately.\n\n"
                "Do NOT send your final report via `reply` while waiting for tools. Just call the tools silently, wait for the next turn, and put your ENTIRE final report in the `finish` tool's text.\n\n"
                "The `text` parameter is the ACTUAL DELIVERABLE — the content the caller "
                "or user will receive. Put your real output here (full answer, full data, "
                "full summary), not a status report of what you did. If the caller expects "
                "a structured payload, put the entire structured payload in `text`.\n\n"
                "Free prose outside of tool calls is NOT delivered to the caller or user. "
                "Only `reply()` and `finish()` reach the recipient.\n\n"
                "Use cases:\n"
                "- Task completed: put the deliverable content in `text`.\n"
                "- Need more information: put your specific question in `text`. "
                "The caller or user will see it and respond.\n\n"
                "Parameters:\n"
                "- text: The actual deliverable content, or a question if you need more info.\n"
                "- summary: Brief summary (optional) for the upstream node.\n"
                "- attachment_paths: File paths to attach (optional).\n\n"
                "Do not include internal protocol markers or debug info in text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "最终结果文本，或需要补充信息时的具体问题。",
                    },
                    "summary": {
                        "type": "string",
                        "description": "简要摘要（可选），帮助上游快速了解结果。",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "附带的文件路径列表（可选，仅在有图片等附件时使用）。",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _ask_spec() -> dict:
    """Build the ask pseudo-tool spec.

    [AutoC 2026-05-31] Why: Phase 0 needs an explicit ask exit even though the
    supervisor still routes it like finish. How: expose a finish-shaped terminal
    tool without attachments and with clarification-oriented text. Purpose: let
    nodes signal “need more input” now, while preserving action="ask" for Phase 1
    topology routing.
    """
    return {
        "type": "function",
        "function": {
            "name": "ask",
            "description": (
                "Ask the upstream (caller or user) for additional information or clarification. "
                "This terminates the current task, similar to finish, but signals that you need "
                "more input before you can complete your work.\n\n"
                "Use cases:\n"
                "- You need clarification on requirements before proceeding.\n"
                "- You've hit a blocker and need the caller's decision.\n"
                "- You want to confirm your approach before investing effort.\n\n"
                "The upstream will see your question and can respond, which will create a new "
                "task for you to continue working.\n\n"
                "Call ask on its own (reply may accompany it). If you call ask in the same turn as other tools, "
                "including a dispatch tool, those other tools still run but ask is ignored and does not end the node: "
                "read their results first, then call ask again on its own.\n\n"
                "Parameters:\n"
                "- text: Your specific question or request for information.\n"
                "- summary: Brief summary (optional) for the upstream node."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Your question or request for additional information.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief summary for the upstream node.",
                    },
                },
                "required": ["text"],
            },
        },
    }



def _reply_spec() -> dict:
    """Build the reply pseudo-tool spec (non-terminating).

    【Fix 4】description 重写：
    - 显式声明 `text` 是 user-facing 的消息内容。
    - 显式声明自由正文不会送达用户，只有 reply()/finish() 才会到达，
      避免模型把想说的话写在 tool_call 之外的自由正文里。
    """
    return {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "Send an interim message to the user WITHOUT terminating this node.\n\n"
                "The node keeps running after this call — you can continue calling tools "
                "or do more work in subsequent turns.\n\n"
                "The `text` parameter is the message content the user will read. "
                "Anything you want the user to see mid-task must go here. "
                "Do NOT rely on free prose output — free prose is not delivered to the user; "
                "only reply() and finish() reach the user.\n\n"
                "When to use:\n"
                "- You have a partial result or progress update to share, but more work remains.\n"
                "- The user asked for multiple things and you want to respond to one "
                "while continuing to work on the rest.\n\n"
                "When NOT to use:\n"
                "- You are done with all work → use finish instead.\n"
                "- You need more information from the user → use finish instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message content the user will read.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional attachment paths produced by previous tools to send with this interim reply.",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _compact_context_spec() -> dict:
    """伪工具：手动触发上下文压缩。"""
    return {
        "type": "function",
        "function": {
            "name": "compact_context",
            "description": (
                "Compress the current conversation context by summarizing older messages.\n\n"
                "Use when context is getting very long and you want to free up token budget. "
                "Recent messages are preserved; older messages are replaced with a structured summary.\n\n"
                "This is non-terminating — the node continues running after compression.\n"
                "The compression happens immediately and takes effect for subsequent LLM calls in this session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "description": "Number of recent conversation rounds to preserve (default uses system config, typically 6).",
                        "type": "integer",
                    },
                },
                "required": [],
            },
        },
    }


def _preempt_task_spec() -> dict:
    """伪工具：向正在运行的子任务注入追加指令。"""
    return {
        "type": "function",
        "function": {
            "name": "preempt_task",
            "description": (
                "Inject an additional instruction into a running child task. The child task will "
                "finish its current atomic operation (e.g. tool call), receive your message as a new "
                "user instruction, and continue working with the updated direction.\n\n"
                "This is NOT for cancelling tasks — use cancel_active_tasks for that.\n\n"
                "Use cases:\n"
                "- You need to add corrections or supplementary instructions to a running child.\n"
                "- The user provided new information that the child task needs to incorporate.\n"
                "- You want to steer an ongoing task in a different direction without restarting it.\n\n"
                "This is non-terminating — your node keeps running after the call. "
                "The child task continues with the injected message and returns results via the normal dispatch callback.\n\n"
                "Reachable tasks: those of the conversation this turn belongs to (entry branches included) and every "
                "task dispatched from it, directly or through other children, including persistent agents running in "
                "their own agent: sessions. Tasks of other conversations are never found. Tool tasks, system tasks and "
                "tasks suspended while waiting for their own children cannot be steered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "要追加指令的任务：一般填子节点名（如 bob），也可以填完整任务 ID 或至少 8 位的 ID 前缀。"
                            "节点名对上多个任务时优先选你自己派出去的那个；仍分不出来、或 ID 前缀对上多个时会报错并列出"
                            "候选 ID 前缀，换成其中一个再调用。"
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "追加指令文本。子任务会将此消息作为新的用户指令注入并继续执行。",
                    },
                },
                "required": ["task_id", "message"],
            },
        },
    }


STAY_SILENT_TOOL_NAME = "stay_silent"
# 不发言结束的任务在结果里带这个标记：supervisor 照它把出站消息标成不发言，适配层见到就什么也不发。
NO_REPLY_MARKER = "no_reply"


def _stay_silent_spec() -> dict:
    """伪工具：这一轮不发言。只注入给声明了 stay_silent: true 的节点，只在主动插话的任务里生效。"""
    return {
        "type": "function",
        "function": {
            "name": STAY_SILENT_TOOL_NAME,
            "description": (
                "这一轮不发言，直接结束，群里什么也不会收到。\n\n"
                "只在没人叫你、是你自己看到群聊在想要不要插话的时候用：话题和你无关、插进去显得突兀、"
                "刚说过话不想刷屏，就调用它。\n"
                "有人 @ 你、回复你、点你的名或者用前缀叫你的时候不能用，调用会被拒绝，照常回答。\n"
                "调用它就不要再调 finish；想说话就用 finish 正常回复。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "一句话说明为什么不接，只记进日志，不会发出去。",
                    },
                },
                "required": [],
            },
        },
    }


def _update_todos_spec() -> dict:
    """伪工具：维护本任务的待办清单，只注入给声明了 todo: true 的节点。"""
    statuses = " / ".join(TODO_STATUSES)
    return {
        "type": "function",
        "function": {
            "name": TODO_TOOL_NAME,
            "description": (
                "维护本任务的待办清单，帮你跟踪多步任务的进度。每次传入完整清单，会整体替换上一份。\n\n"
                "用法：\n"
                "- 多步任务开始时先列出要做的步骤，全部是 pending。\n"
                "- 开始做某一项时把它标成 in_progress，同一时间一般只有一项 in_progress；做完立刻标成 completed。\n"
                "- 中途发现新步骤就加进去，不再需要的删掉；传空数组表示清空清单。\n"
                "- 一两步就能做完的简单任务不用建清单。\n\n"
                f"最多 {MAX_TODO_ITEMS} 项，每项不超过 {MAX_TODO_CONTENT_CHARS} 字，status 取 {statuses}。"
                "清单上还有没做完的项时，系统每步都会提醒你。\n"
                "本工具不结束任务：清单全部完成后另起一步单独调用 finish，和 finish 同一轮调用时 finish 不会执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "完整的待办清单，按执行顺序排列。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "这一项要做什么，一句话说清楚。",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": list(TODO_STATUSES),
                                    "description": "pending=还没开始；in_progress=正在做；completed=已经做完。",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    }


def _switch_node_spec(targets: list[str], switch_info: list[dict[str, str]] | None = None, *, current_node_id: str = "", current_node_name: str = "") -> dict:
    """构建 switch_node 伪工具定义——切换 session 的对话节点。"""
    desc_parts = [
        "切换当前会话的对话节点。调用后当前节点 finish，用户的下一条消息将由目标节点处理。",
        "",
        "使用场景：",
        "- 用户明确要求切换到某个节点（如'切到编程节点'）",
        "- 当前节点能力不足，判断应该由其他节点持续对话",
        "",
        "注意：",
        "- 调用后当前节点立即终止，不会再执行后续工具调用",
        "- 目标节点能看到之前的对话历史",
        "- 传空字符串的 target 可恢复为默认入口节点",
    ]
    if switch_info:
        desc_parts.append("")
        desc_parts.append("可切换到的节点：")
        for info in switch_info:
            desc_parts.append(f"- {info.get('name', info['id'])}（{info['id']}）：{info.get('description', '')}")
    # [2026-05-06] switch_node 的枚举必须与 Gemini 的 schema 规则兼容。
    # Why: target="" 是运行时恢复默认入口节点的约定，但 Gemini 会拒绝 enum
    # 中的空字符串成员。How: 只把非空目标节点 ID 去重后写入 enum，没有可选
    # 目标时直接省略 enum。Purpose: 保留 handler 对空 target 的兼容，同时避免
    # 模型请求因工具声明无效而在进入推理前失败。
    enum_targets: list[str] = []
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if target and target not in enum_targets:
            enum_targets.append(target)
    target_prop: dict = {
        "type": "string",
        "description": "目标节点 ID。传空字符串恢复为默认入口节点。",
    }
    if enum_targets:
        target_prop["enum"] = enum_targets
    return {
        "type": "function",
        "function": {
            "name": "switch_node",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target_prop,
                    "text": {
                        "type": "string",
                        "description": "给用户的回复文本（如'已切换到编程节点'）。",
                    },
                },
                "required": ["target", "text"],
            },
        },
    }


# ---------------------------------------------------------------------------
#  工具 spec 相关
# ---------------------------------------------------------------------------

_ASYNC_TOOL_HINT = (
    "\n\n⚡ This is an async tool. It runs in the background and its result arrives later as a new message, "
    "to this node or, if this node cannot receive messages, to the node that dispatched it. "
    "You can continue working or finish — no need to wait."
)


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    # [2026-04-23] 恢复 async_mode 支持：遍历 spec 列表，对 async_mode=True 的工具
    # 在 description 末尾追加 _ASYNC_TOOL_HINT，使模型知道该工具为异步执行。
    result = []
    for s in specs:
        desc = s.get("description", "")
        if s.get("async_mode"):
            desc += _ASYNC_TOOL_HINT
        result.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": desc,
                "parameters": s.get("input_schema", {"type": "object", "properties": {}, "required": []}),
            },
        })
    return result


def _normalized_mode(value: Any) -> str:
    return str(value or "").strip().lower()


def edit_mode_satisfied(current: Any, required: Any) -> bool:
    """节点写的 edit_mode 满不满足工具要求的 requires_edit_mode；工具没要求时总是满足。"""
    wanted = _normalized_mode(required)
    return not wanted or _normalized_mode(current) == wanted


def edit_mode_matches(node: "Node", spec: dict[str, Any] | None) -> bool:
    """spec 声明了 requires_edit_mode 时，只有节点 extra 里的 edit_mode 与它相同才给这个工具。"""
    required = spec.get("requires_edit_mode") if isinstance(spec, dict) else ""
    extra = getattr(node, "extra", None)
    current = extra.get("edit_mode") if isinstance(extra, dict) else ""
    return edit_mode_satisfied(current, required)


def tool_allowed(node: "Node", name: str, spec: dict[str, Any] | None = None) -> bool:
    """这个节点能不能调这个工具。

    授权语义只此一份：列表过滤和单名校验都走它，不然两处迟早漂移成两套规则。
    未知 mode 落 none —— 配错了宁可什么都不给。手里有 spec 的调用方要把它传进来，requires_edit_mode 也在这里判。
    合回工具不归 tool_access 放行：推理循环按 merge_tool_offered 另给，别的入口（独立工具任务、执行计划）一律不给。
    """
    if name == MERGE_TOOL:
        return False
    if not edit_mode_matches(node, spec):
        return False
    mode = (node.tool_access.mode or "none").lower()
    if mode == "all":
        return name not in set(node.tool_access.deny)
    if mode == "allowlist":
        return name in set(node.tool_access.allow)
    return False


def _filter_tool_specs(node: "Node", all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in all_specs if tool_allowed(node, s.get("name") or "", s)]


def isolatable_node(node: "Node") -> bool:
    """委派这个节点时能不能要求隔离：节点 YAML 写了 isolation: worktree 才算，取值和委派时填的是同一个。"""
    extra = getattr(node, "extra", None)
    raw = extra.get(ISOLATION_KEY) if isinstance(extra, dict) else None
    if raw is None:
        return False
    if isinstance(raw, str) and raw.strip().lower() == ISOLATION_WORKTREE:
        return True
    logger.warning(
        "node %s has an invalid %s %r; only %s is recognized, dispatches to it cannot ask for isolation",
        getattr(node, "id", "?"), ISOLATION_KEY, raw, ISOLATION_WORKTREE,
    )
    return False


def merge_tool_offered(node: "Node", isolating: Collection[str]) -> bool:
    """推理循环给不给合回工具：委派目标里有能要求隔离的（任务已在隔离副本里时一个都没有），而且 tool_access 没有 deny 它。

    白名单里写没写它都不看；mode: all 的节点没有能隔离的目标也不给。
    """
    return bool(isolating) and MERGE_TOOL not in set(node.tool_access.deny)


@dataclass(frozen=True)
class Delegation:
    """这个任务能派给谁、派哪些时能要求隔离、工具表里有没有合回工具。"""

    targets: tuple[str, ...]
    isolating: tuple[str, ...]
    merge_spec: dict[str, Any] | None

    @property
    def can_merge(self) -> bool:
        return self.merge_spec is not None


def plan_delegation(
    node: "Node",
    workspace_root: Path | str,
    downstream_info: list[dict[str, Any]] | None,
    registry_specs: list[dict[str, Any]],
    *,
    in_isolation: bool,
) -> Delegation:
    """工具表、委派工具的说明和子任务交回结果里的合回提示都按这一份，不然会出现提示让调一个工具表里没有的工具。"""
    targets = list(node.delegate_targets)
    # 一张图都画不出来的绘图节点不参与委派：留着只会让模型认真规划一轮，再撞上工具报缺 key。
    try:
        from ..builtin.image_gen_gating import disabled_draw_nodes
        disabled = disabled_draw_nodes(workspace_root) & set(targets)
        if disabled:
            targets = [target for target in targets if target not in disabled]
            logger.info(
                "image_gen_gating: removed delegate targets %s (no channel configured, node=%s)", sorted(disabled), node.id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_gating delegate filter skipped: %s", exc)
    # 隔离副本里的任务不给 isolation、repo，也不给合回工具：它派出的子任务只能沿用这个副本
    isolating = isolating_targets(targets, downstream_info, in_isolation=in_isolation)
    # 合回工具不看 tool_access，像 await_dispatches 一样跟着委派目标走：有能隔离的目标才给
    merge_spec = (
        next((spec for spec in registry_specs if spec.get("name") == MERGE_TOOL), None)
        if merge_tool_offered(node, isolating) else None
    )
    return Delegation(tuple(targets), tuple(isolating), merge_spec)

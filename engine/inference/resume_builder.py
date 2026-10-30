"""恢复消息构建和附件筛选。

从 ai_step.py 中拆出。依赖 engine.attachments 中的 build_multimodal_content。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import config_layout
from real_paths import real_path

from ..attachments import build_multimodal_content, payload_attachments
from ..conversation_store import MessageType
from ..interrupted_calls import INTERRUPTED_RESUME_TYPE
from ..run_budget import WRAPUP_TIME
from ..tool_outputs import ToolOutputLimits, inline_tool_output, mentioned_refs
from ..worktree_report import worktree_lines
from .loop_state import TOOL_RESULT_ATTACHMENT_TYPE


# ---------------------------------------------------------------------------
#  恢复消息构建（同时兼容 v1 和 v2 格式）
# ---------------------------------------------------------------------------

_CHILD_RESUME_TYPES = frozenset(
    {"child_result", "child_ask", "child_failed", "child_cancelled", "child_preempted"}
)

# 和异步委派回调落盘时用同一个类型，前端按它画成子任务回调卡片
DISPATCH_RESULT_TYPE = "dispatch_result"

# 批次里子节点的结局对上单个子任务恢复时的类型，读图失败判定之类按 resume_type 认结果的逻辑才认得出
_BATCH_NODE_RESUME_TYPES = {
    "": "child_result",
    "finish": "child_result",
    "ask": "child_ask",
    "fail": "child_failed",
    "cancelled": "child_cancelled",
    "preempted": "child_preempted",
}
_BATCH_INSTRUCTION_PREVIEW = 200
# 这两种恢复数据把子任务交回的结果原样拼进上下文，超长的要先落盘
RESUME_RESULT_TYPES = frozenset({"tool_results", "batch_results"})

# await_dispatches 交回的项里还没结束的和拿不到结果的，取值要和 supervisor 写进恢复数据的一致
AWAIT_RUNNING = "running"
AWAIT_UNAVAILABLE = "unavailable"
_RUNNING_KEPT_NOTE = (
    "它的结果会留给你：之后再调用 await_dispatches 收取；本任务结束时还没收取的，会和其他留着的结果合成一条消息交回。"
)
_DELIVERED_EARLIER_NOTES = {
    "inbound": "这个结果之前已经作为消息交回过，那一轮可能已经处理或汇报过它，不要重复汇报。",
    "join": "这个结果之前已经由另一次 await_dispatches 收到过。",
}

_INTERRUPTED_NOTICE = (
    "[SYSTEM] 这个任务刚才被服务重启打断了，现在接着做。中断那一步的工具调用已经标成「中断，结果未知」："
    "它们可能没执行、只执行了一部分，也可能已经执行完。先核对实际状态（重新读相关文件、查看命令的产物），"
    "再决定要不要重做，然后继续完成任务。"
)


def resume_outcome(message: Any) -> tuple[str, str]:
    """(resume_type, from_node)，不是下游节点的恢复消息返回两个空串。"""
    meta = message.get("_meta") if isinstance(message, dict) else None
    if not isinstance(meta, dict):
        return "", ""
    return str(meta.get("resume_type") or ""), str(meta.get("resume_from_node") or "")


def _build_resume_messages(resume_data: dict[str, Any], *, can_merge: bool = True) -> list[dict[str, Any]]:
    """从 resume_data / resume_event 构建恢复消息。

    v2 格式:
      - child_result:    下级节点完成
      - child_failed:    下级节点失败
      - child_cancelled: 下级节点被取消
    v1 兼容:
      - tool_results:    工具调用结果
      - handoff_result:  子链返回结果

    can_merge 是这个任务的工具表里有没有合回工具，子任务交回的隔离副本说明按它写怎么合回。
    """
    messages = _resume_messages(resume_data, can_merge=can_merge)
    rtype = _single_resume_type(resume_data)
    if rtype in _CHILD_RESUME_TYPES:
        # 下游结果的判定读这里，不去匹配中文正文；_meta 发给模型前会被剥掉。
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        for message in messages:
            message.setdefault("_meta", {}).update(
                {"resume_type": rtype, "resume_from_node": from_node}
            )
    return messages


def _child_message(content: Any, from_node: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"message_type": DISPATCH_RESULT_TYPE}
    if from_node:
        meta["child_node_id"] = from_node
    return {"role": "user", "content": content, "_meta": meta}


def _tool_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content, "_meta": {"message_type": MessageType.TOOL_RESULT}}


def _attachments_message(text: str, attachments: list[Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": build_multimodal_content(text, attachments),
        "_meta": {"message_type": TOOL_RESULT_ATTACHMENT_TYPE},
    }


def _preview(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _continue_lines(source: dict[str, Any], node: str, status: str = "") -> list[str]:
    """子任务的句柄；它的会话能用 continue_from 接着做时，按它这一程怎么停下的说清怎么接着派。"""
    handle = str(source.get("handle") or "").strip()
    if not handle:
        return []
    lines = [f"句柄：{handle}"]
    if not source.get("continuable"):
        return lines
    again = f"再委派给 {node or '同一个节点'}，带上 continue_from 填这个句柄"
    wrapup = str(source.get("wrapup") or "").strip()
    outcome = status or "finish"
    if outcome == "ask":
        lines.append(f"补齐它要的信息后，{again}，它会从停下的地方接着做；不带句柄就是从零开始的另一份。")
    elif outcome == "preempted":
        lines.append(f"它的会话为接着做留着；要它从打断的地方接着做，就{again}。")
    elif wrapup:
        limit = "运行时长" if wrapup == WRAPUP_TIME else "步数"
        lines.append(f"它是到了{limit}上限停下的，还没做完；要它接着做，就{again}。")
    elif outcome == "finish":
        lines.append(f"它的会话留着；要它带着这段历史接着做，就{again}。")
    else:
        lines.append(f"要它从停下的地方接着做，就{again}。")
    return lines


def _kept_for_continuing(source: dict[str, Any]) -> bool:
    return bool(source.get("continuable") and str(source.get("handle") or "").strip())


def _ask_closing(source: dict[str, Any]) -> list[str]:
    """ask 交回的子任务会话没为接着做留着时，说清补齐信息后只能重新委派。"""
    if _kept_for_continuing(source):
        return []
    return ["它这一程已经结束，会话没有为接着做留着；补齐信息后要重新委派，那是从零开始的另一份。"]


def _preempted_closing(source: dict[str, Any]) -> list[str]:
    """被打断的子任务会话没为接着做留着时，说清要做完只能重新委派。"""
    if _kept_for_continuing(source):
        return []
    return ["它的会话没有为接着做留着；要做完得重新委派。"]


def _single_resume_type(resume_data: dict[str, Any]) -> str:
    rtype = str(resume_data.get("type") or "").strip()
    # 单个委派的 ask 和 finish 走同一条路交回，靠 action_type 分开：要补充信息的不能说成已完成
    if rtype == "child_result" and str(resume_data.get("action_type") or "").strip() == "ask":
        return "child_ask"
    return rtype


def _unavailable_message(entry: dict[str, Any]) -> dict[str, Any]:
    handle = str(entry.get("handle") or "").strip() or "（空）"
    error = str(entry.get("error") or "").strip() or "认不出这个句柄。"
    return {
        "role": "user",
        "content": f"句柄 {handle} 拿不到结果：{error}",
        "_meta": {"message_type": DISPATCH_RESULT_TYPE, "handle": handle},
    }


def _running_lines(entry: dict[str, Any], node: str, where: str) -> list[str]:
    """await_dispatches 恢复时还没结束的异步委派。"""
    if entry.get("lifetime_capped"):
        head = f"子节点 {node}{where}还没做完：本任务的总运行时长快到上限了，没有再等下去，它仍在运行。"
    elif entry.get("timed_out"):
        waited = entry.get("waited_sec")
        head = f"子节点 {node}{where}等了 {waited} 秒还没做完，仍在运行。" if waited else f"子节点 {node}{where}到点还没做完，仍在运行。"
    else:
        head = f"子节点 {node}{where}还在运行。"
    return [head, _RUNNING_KEPT_NOTE]


def _batch_node_message(entry: dict[str, Any], *, can_merge: bool = True) -> dict[str, Any]:
    """批次里一个子节点的结果：先说清对应哪次调用、哪条指令、结局如何，失败、取消、打断都明说没有结果。

    await_dispatches 交回的项还会是还在运行（running）或拿不到结果（unavailable）。
    """
    node = str(entry.get("node_id") or "unknown")
    status = str(entry.get("status") or "").strip()
    if status == AWAIT_UNAVAILABLE:
        return _unavailable_message(entry)
    text = str(entry.get("text") or "")
    summary = str(entry.get("summary") or "").strip()
    error = str(entry.get("error") or "").strip()
    labels: list[str] = []
    call_id = str(entry.get("call_id") or "").strip()
    if call_id:
        labels.append(f"调用 {call_id}")
    instruction = _preview(str(entry.get("instruction") or ""), _BATCH_INSTRUCTION_PREVIEW)
    if instruction:
        labels.append(f"指令：{instruction}")
    where = f"（{'；'.join(labels)}）" if labels else " "

    if status in ("", "finish"):
        lines = [f"子节点 {node}{where}已完成。"]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append(f"结果：\n{text}")
        if not summary and not text:
            lines.append("它没有返回结果文本。")
    elif status == "ask":
        lines = [
            f"子节点 {node}{where}没有做完，需要补充信息：",
            text.strip() or summary or "（它没有说明需要什么）",
            *_ask_closing(entry),
        ]
    elif status == "fail":
        lines = [f"子节点 {node}{where}执行失败：{error or '未知错误'}", "它没有给出结果，不要臆测它的输出。"]
    elif status == "cancelled":
        lines = [f"子节点 {node}{where}已被取消，没有结果。"]
        if error:
            lines.append(f"原因：{error}")
    elif status == "preempted":
        lines = [f"子节点 {node}{where}被打断，没有做完，也没有结果。"]
        if summary:
            lines.append(f"打断前的摘要：{summary}")
        lines.extend(_preempted_closing(entry))
    elif status == AWAIT_RUNNING:
        lines = _running_lines(entry, node, where)
    else:
        lines = [f"子节点 {node}{where}结束了，但没有拿到结果（状态：{status}）。"]
        if error:
            lines.append(f"原因：{error}")
    earlier = _DELIVERED_EARLIER_NOTES.get(str(entry.get("delivered_earlier") or "").strip())
    if earlier:
        lines.append(earlier)
    lines.extend(_continue_lines(entry, node, status))
    lines.extend(worktree_lines(entry.get("worktree"), can_merge=can_merge))

    meta: dict[str, Any] = {"message_type": DISPATCH_RESULT_TYPE, "child_node_id": node}
    resume_type = _BATCH_NODE_RESUME_TYPES.get(status)
    if resume_type:
        meta.update({"resume_type": resume_type, "resume_from_node": node})
    if summary:
        meta["summary"] = summary
    for key in ("child_task_id", "child_session_id", "handle"):
        value = str(entry.get(key) or "").strip()
        if value:
            meta[key] = value
    return {"role": "user", "content": "\n".join(lines), "_meta": meta}


def _resume_messages(resume_data: dict[str, Any], *, can_merge: bool = True) -> list[dict[str, Any]]:
    rtype = _single_resume_type(resume_data)
    worktree = worktree_lines(resume_data.get("worktree"), can_merge=can_merge)

    # v2: child_result
    if rtype == "child_result":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        summary = str(result.get("summary") or "")
        text = str(result.get("text") or "")
        child_atts = payload_attachments(result)
        lines = [f"下游节点 {from_node} 已完成。" if from_node else "下游节点已完成。"]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append("结果：")
            lines.append(text)
        lines.extend(_continue_lines(resume_data, from_node, "finish"))
        lines.extend(worktree)
        content_text = "\n".join(lines).strip()
        if isinstance(child_atts, list) and child_atts:
            return [_child_message(build_multimodal_content(content_text, child_atts), from_node)]
        return [_child_message(content_text, from_node)]

    # v2: child_ask
    if rtype == "child_ask":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        text = str(result.get("text") or "").strip()
        if from_node and text:
            content = f"下游节点 {from_node} 需要补充信息：{text}"
        elif text:
            content = f"下游节点需要补充信息：{text}"
        elif from_node:
            content = f"下游节点 {from_node} 需要补充信息。"
        else:
            content = "下游节点需要补充信息。"
        return [_child_message("\n".join([
            content,
            *_ask_closing(resume_data),
            *_continue_lines(resume_data, from_node, "ask"),
            *worktree,
        ]), from_node)]

    # v2: child_failed
    if rtype == "child_failed":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        error = str(resume_data.get("error") or "未知错误")
        prefix = f"下游节点 {from_node} 执行失败：" if from_node else "下游节点执行失败："
        return [_child_message("\n".join([
            f"{prefix}{error}",
            *_continue_lines(resume_data, from_node, "fail"),
            *worktree,
        ]), from_node)]

    # v2: child_cancelled
    if rtype == "child_cancelled":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        text = f"下游节点 {from_node} 已被取消。" if from_node else "下游节点已被取消。"
        return [_child_message("\n".join([text, *worktree]), from_node)]

    # v2: child_preempted
    if rtype == "child_preempted":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        prefix = f"下游节点 {from_node} 被打断，没有做完，也没有结果。" if from_node else "下游节点被打断，没有做完，也没有结果。"
        return [_child_message("\n".join([
            prefix,
            *_preempted_closing(resume_data),
            *_continue_lines(resume_data, from_node, "preempted"),
            *worktree,
        ]), from_node)]

    # 服务重启打断后重排的任务：缺结果的工具调用 runner 已补成「中断，结果未知」，这里只提醒先核对再接着做
    if rtype == INTERRUPTED_RESUME_TYPE:
        return [{"role": "user", "content": _INTERRUPTED_NOTICE, "_meta": {"message_type": MessageType.SYSTEM}}]

    # v2: compact_done (compactor 子 task 完成后恢复)
    if rtype == "compact_done":
        # 压缩只动上方旧消息，当前 task 消息链完整保留，LLM 无需感知。
        # 注入假 user 消息反而会破坏工具调用的角色链。
        return []

    # v1: tool_results
    if rtype == "tool_results":
        entries = resume_data.get("tool_results")
        if not isinstance(entries, list):
            entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = []
            all_atts: list[dict[str, Any]] = []
            for e in entries:
                _name = e.get("name", "unknown")
                _raw = e.get("raw_inline", "")
                msgs.append(_tool_message(f'Tool result for "{_name}":\n{_raw}'))
                atts = payload_attachments(e)
                if atts:
                    all_atts.extend(atts)
            if all_atts:
                msgs.append(_attachments_message("以上工具执行产生了以下图片结果：", all_atts))
            return msgs
        return []

    # v3: batch_results（统一批量返回，node 和 tool 共用）
    if rtype == "batch_results":
        entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs, all_atts = _batch_entry_messages(entries, can_merge=can_merge)
            capped = sum(
                1 for e in entries
                if isinstance(e, dict) and e.get("lifetime_capped") and str(e.get("status") or "") == "fail"
            )
            if capped:
                msgs.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM] 这一批有 {capped} 项是因为本任务的总运行时长快到上限被提前收的；"
                        "剩下的时间不多，先把已有的结果整理交回。"
                    ),
                    "_meta": {"message_type": MessageType.SYSTEM},
                })
            if all_atts:
                msgs.append(_attachments_message(
                    "以上子任务产生了以下附件；finish 时不写 attachment_paths，它们会随最终回复一起发出；"
                    "只写其中几个就只发那几个：", all_atts,
                ))
            return msgs
        return []

    return []


def _batch_entry_messages(
    entries: list[Any], *, can_merge: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """批次里每一项的结果消息，以及各项带回来的附件。"""
    msgs: list[dict[str, Any]] = []
    all_atts: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        _kind = str(e.get("kind") or "node")
        _status = str(e.get("status") or "")

        if _kind == "tool":
            _name = e.get("name", "unknown")
            _raw = e.get("raw_inline", "")
            if _status == "fail":
                msgs.append(_tool_message(f'Tool "{_name}" 执行失败：{e.get("error", "")}'))
            elif _status == "cancelled":
                _error = str(e.get("error") or "").strip()
                msgs.append(_tool_message(f'Tool "{_name}" 已取消，没有结果。' + (f"原因：{_error}" if _error else "")))
            else:
                msgs.append(_tool_message(f'Tool result for "{_name}":\n{_raw}'))
        else:
            msgs.append(_batch_node_message(e, can_merge=can_merge))

        atts = payload_attachments(e)
        if atts:
            all_atts.extend(atts)
    return msgs, all_atts


def stopped_batch_messages(entries: list[Any], *, reason: str, unsent: bool = False) -> list[dict[str, Any]]:
    """中间层挂起等一批子任务时被叫停（unsent：交回了这批委派、还没派出去就被叫停）：那一批写成和恢复时一样的结果消息，
    补进它自己的会话历史。

    它不会再恢复，子任务的附件不会随它的回复自动发出，只列出路径；隔离副本按没有合回工具的说法写。
    """
    msgs, attachments = _batch_entry_messages(entries, can_merge=False)
    cause = str(reason or "").strip().rstrip("。.")
    because = f"：{cause}" if cause else ""
    if unsent:
        lines = [
            f"[SYSTEM] 这一轮交回了上面这批委派，还没派出去就被停下了{because}。",
            "这些子任务一个都没有派出，不会有结果。接着做时以这些为准，需要的话重新派。",
        ]
    else:
        lines = [
            f"[SYSTEM] 这一轮在等上面这批子任务时被停下了{because}。",
            "已经交回的结果照上面写的；没交回的已被叫停，不会再有结果。接着做时以这些为准，需要的话重新派。",
        ]
    paths = [str(item.get("path")) for item in attachments if isinstance(item, dict) and item.get("path")]
    if paths:
        lines.append(f"子任务留下的附件：{'，'.join(paths)}。要发出去，就在 finish 的 attachment_paths 里点名。")
    msgs.append({"role": "user", "content": "\n".join(lines), "_meta": {"message_type": MessageType.SYSTEM}})
    return msgs


def bound_dispatch_text(
    workspace_root: Path | str | None, session_id: str, node_id: str, text: str, limits: ToolOutputLimits,
) -> str:
    """一个子节点交回的正文进上下文的样子：没超 max_inline_chars 原样返回，超了全文落盘换成带 ref 的预览。

    预览里没带出来、全文里提到的 ref 另起一行列出。存在 session_id 名下，read_tool_output 按同一个键读回。阻塞 I/O。
    """
    if not isinstance(text, str) or len(text) <= limits.max_inline_chars:
        return text
    preview = inline_tool_output(workspace_root, session_id, f"dispatch_to_{node_id or 'node'}", text, limits)
    shown = set(mentioned_refs(preview))
    dropped = [ref for ref in mentioned_refs(text) if ref not in shown]
    if dropped:
        preview += (
            "\n[全文里还提到这些存档，预览里没显示："
            + "，".join(f"ref={ref}" for ref in dropped)
            + "，同样用 read_tool_output 读]"
        )
    return preview


def bound_resume_data(
    resume_data: dict[str, Any], *, workspace_root: Path | str | None, session_id: str, limits: ToolOutputLimits,
) -> dict[str, Any]:
    """恢复数据里超长的子任务结果落盘换成带 ref 的预览，只给拼上下文用：工具项换 raw_inline，节点项换 text 和 error。

    原数据不改（收附件、循环状态还要用），没有要换的原样返回同一个对象。阻塞 I/O，调用方放到线程里。
    """
    if str(resume_data.get("type") or "").strip() not in RESUME_RESULT_TYPES:
        return resume_data
    copied: dict[str, Any] = dict(resume_data)
    changed = False
    for key in ("tool_results", "entries"):
        entries = resume_data.get(key)
        if not isinstance(entries, list):
            continue
        bounded = [
            _bounded_entry(entry, workspace_root=workspace_root, session_id=session_id, limits=limits)
            for entry in entries
        ]
        changed = changed or any(new is not old for new, old in zip(bounded, entries))
        copied[key] = bounded
    return copied if changed else resume_data


def _bounded_entry(
    entry: Any, *, workspace_root: Path | str | None, session_id: str, limits: ToolOutputLimits,
) -> Any:
    if not isinstance(entry, dict):
        return entry
    replaced: dict[str, Any] = {}
    if str(entry.get("kind") or "tool") == "tool":
        raw = entry.get("raw_inline")
        if isinstance(raw, str) and len(raw) > limits.max_inline_chars:
            replaced["raw_inline"] = inline_tool_output(
                workspace_root, session_id, str(entry.get("name") or ""), raw, limits,
            )
    else:
        node_id = str(entry.get("node_id") or "")
        for field in ("text", "error"):
            value = entry.get(field)
            if isinstance(value, str) and len(value) > limits.max_inline_chars:
                replaced[field] = bound_dispatch_text(workspace_root, session_id, node_id, value, limits)
    return {**entry, **replaced} if replaced else entry


# ---------------------------------------------------------------------------
#  附件筛选
# ---------------------------------------------------------------------------

def _select_attachments(
    collected: list[dict[str, Any]],
    selected_paths: Any,
    workspace_root: "Path | None" = None,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Select attachments by path from collected, or read from disk as fallback.

    Returns only what was named and found; an empty or unmatched selection gives [].
    Disk fallback is restricted to paths under workspace_root for security.
    It still reads any file in the workspace, so model-supplied paths must go
    through screen_model_attachment_paths first.
    """
    if not isinstance(selected_paths, list) or not selected_paths:
        return []

    path_set = {str(p).strip() for p in selected_paths if isinstance(p, str) and str(p).strip()}
    selected = [a for a in collected if a.get("path") in path_set]
    found_paths = {a.get("path") for a in selected}

    if workspace_root:
        from ..attachments import save_attachment
        for raw in sorted(path_set - found_paths):
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = workspace_root / p
            # 只读工作区里的文件，和 screen_model_attachment_paths 放行的范围一致：受信链接后面的也算。
            try:
                resolved = real_path(p)
            except (OSError, RuntimeError):
                continue
            if config_layout.workspace_logical_path(workspace_root, resolved) is None:
                continue
            if not p.is_file():
                continue
            try:
                data_bytes = p.read_bytes()
            except Exception:
                continue
            att = save_attachment(workspace_root, session_id, data_bytes, filename=p.name)
            selected.append(att)

    # 点名的一个都没找到就是什么都不发，不能退回把收集到的附件全发出去
    return selected

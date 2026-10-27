"""恢复消息构建和附件筛选。

从 ai_step.py 中拆出。依赖 engine.attachments 中的 build_multimodal_content。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import config_layout

from ..attachments import build_multimodal_content, payload_attachments
from ..conversation_store import MessageType
from ..interrupted_calls import INTERRUPTED_RESUME_TYPE
from ..run_budget import WRAPUP_TIME
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

# await_dispatches 交回的项里还没结束的和拿不到结果的，取值要和 supervisor 写进恢复数据的一致
AWAIT_RUNNING = "running"
AWAIT_UNAVAILABLE = "unavailable"
_RUNNING_KEPT_NOTE = (
    "它的结果会留给你：之后再调用 await_dispatches 收取；本任务结束时还没收取的，会作为单独的消息交回。"
)
_DELIVERED_EARLIER_NOTES = {
    "inbound": "这个结果之前已经作为一条单独的消息交回过，那一轮可能已经处理或汇报过它，不要重复汇报。",
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


def _build_resume_messages(resume_data: dict[str, Any]) -> list[dict[str, Any]]:
    """从 resume_data / resume_event 构建恢复消息。

    v2 格式:
      - child_result:    下级节点完成
      - child_failed:    下级节点失败
      - child_cancelled: 下级节点被取消
    v1 兼容:
      - tool_results:    工具调用结果
      - handoff_result:  子链返回结果
    """
    messages = _resume_messages(resume_data)
    rtype = str(resume_data.get("type") or "").strip()
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


def _continue_lines(source: dict[str, Any], node: str) -> list[str]:
    """子任务的句柄；它的会话为接着做留着时，再说清怎么用 continue_from 接着派。"""
    handle = str(source.get("handle") or "").strip()
    if not handle:
        return []
    lines = [f"句柄：{handle}"]
    if not source.get("continuable"):
        return lines
    target = node or "同一个节点"
    wrapup = str(source.get("wrapup") or "").strip()
    if wrapup:
        limit = "运行时长" if wrapup == WRAPUP_TIME else "步数"
        lines.append(f"它是到了{limit}上限停下的，还没做完；要它接着做，就再委派给 {target}，带上 continue_from 填这个句柄。")
    else:
        lines.append(f"要它从停下的地方接着做，就再委派给 {target}，带上 continue_from 填这个句柄。")
    return lines


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


def _batch_node_message(entry: dict[str, Any]) -> dict[str, Any]:
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
            "它这一程已经结束；补齐信息后要重新委派。",
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
    elif status == AWAIT_RUNNING:
        lines = _running_lines(entry, node, where)
    else:
        lines = [f"子节点 {node}{where}结束了，但没有拿到结果（状态：{status}）。"]
        if error:
            lines.append(f"原因：{error}")
    earlier = _DELIVERED_EARLIER_NOTES.get(str(entry.get("delivered_earlier") or "").strip())
    if earlier:
        lines.append(earlier)
    lines.extend(_continue_lines(entry, node))

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


def _resume_messages(resume_data: dict[str, Any]) -> list[dict[str, Any]]:
    rtype = str(resume_data.get("type") or "").strip()

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
        lines.extend(_continue_lines(resume_data, from_node))
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
        return [_child_message(content, from_node)]

    # v2: child_failed
    if rtype == "child_failed":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        error = str(resume_data.get("error") or "未知错误")
        prefix = f"下游节点 {from_node} 执行失败：" if from_node else "下游节点执行失败："
        return [_child_message("\n".join([f"{prefix}{error}", *_continue_lines(resume_data, from_node)]), from_node)]

    # v2: child_cancelled
    if rtype == "child_cancelled":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        text = f"下游节点 {from_node} 已被取消。" if from_node else "下游节点已被取消。"
        return [_child_message(text, from_node)]

    # v2: child_preempted
    if rtype == "child_preempted":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        ctx_ref = str(resume_data.get("context_ref") or "")
        prefix = f"下游节点 {from_node} 被打断，上下文已保存。" if from_node else "下游节点被打断，上下文已保存。"
        if ctx_ref:
            prefix += f"（context_ref: {ctx_ref}）"
        return [_child_message(prefix, from_node)]

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
                    else:
                        msgs.append(_tool_message(f'Tool result for "{_name}":\n{_raw}'))
                else:
                    msgs.append(_batch_node_message(e))

                atts = payload_attachments(e)
                if atts:
                    all_atts.extend(atts)
            if all_atts:
                msgs.append(_attachments_message(
                    "以上子任务产生了以下附件；finish 时不写 attachment_paths，它们会随最终回复一起发出：", all_atts,
                ))
            return msgs
        return []

    return []


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
                resolved = p.resolve()
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

"""任务内进度摘要：单个任务自己跑得很长时，把本任务较早的完整轮次换成一条进展摘要。

只换请求视图、持久历史不动；摘要连同被覆盖消息的指纹存在 loop state 和 data/node_contexts/task_digests/，指纹全对上才用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import get_float, get_int
from plugin_runtime import PluginExecutionError
from providers.base import content_text

from .builtin.usage_tracker import accumulate_usage
from .compact import estimate_message_tokens, is_tool_result_message, total_content_chars
from .compaction_plan import record_fingerprint, record_task_id
from .tool_history import (
    COMMAND_TOOL,
    READ_TOOL,
    REWRITE_TOOLS,
    ReadEntry,
    command_text,
    message_meta,
    read_entries,
    resolve_tool_results,
    rewritten_paths,
    tool_calls_of,
    tool_status,
)
from .tool_outputs import excerpt_text
from .tool_step import TOOL_CANCELLED, TOOL_ERROR, TOOL_SUCCESS

logger = logging.getLogger(__name__)

DIGEST_PREFIX = "[本任务前面步骤的进展摘要]"
DIGEST_MESSAGE_TYPE = "task_digest"
THRESHOLD_REASON = "threshold"
OVERFLOW_REASON = "context_overflow"
DIGEST_EVENT = "task_digest"

_VERSION = 1
_STORE_PARTS = ("data", "node_contexts", "task_digests")
_UNSAFE_STEM_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# 摘要请求的输入取软阈值的四分之一并夹在这两个数之间：这一次调用不该比正常推理还贵
_INPUT_SHARE_OF_THRESHOLD = 0.25
_MIN_INPUT_TOKENS = 4_000
_MAX_INPUT_TOKENS = 32_000
_MAX_RESULT_CHARS = 4_000
_MIN_RESULT_CHARS = 400
_MAX_ARGUMENT_CHARS = 600
_MAX_INSTRUCTION_CHARS = 2_000
_MAX_NARRATIVE_CHARS = 8_000
_MAX_LIST_ITEMS = 40
_MAX_COMMAND_CHARS = 200

_STATUS_LABELS = {
    TOOL_SUCCESS: "成功",
    TOOL_ERROR: "出错",
    TOOL_CANCELLED: "已取消",
    "blocked": "被拦下，没有执行",
    "async_started": "已转入后台",
}

_DIGEST_INSTRUCTIONS = (
    "你在给一个还在进行中的任务写进度摘要。摘要会替换掉下面这些已经完成的步骤的原文，"
    "接着干活的还是同一个助手，它只能靠这份摘要记起前面做过什么，所以要具体、准确。\n"
    "按下面几节写，每节一个标题，没有内容的写「无」：\n"
    "1. 目标：任务要达成什么，用户中途追加的要求也写进来。\n"
    "2. 已完成的步骤和结论：按顺序，一步一行。\n"
    "3. 关键发现：具体的值、文件路径、行号、函数名、报错原文，照抄，不要概括。\n"
    "4. 改过的文件：每个文件一行，写改动要点。\n"
    "5. 没完成的事项。\n"
    "6. 下一步：接下来该做什么。\n"
    "只依据给出的材料，不要编造；给了上一份进展摘要时，把它和新完成的步骤合并成一份完整的摘要。\n"
    "直接输出摘要正文，不要调用工具，篇幅控制在 2000 字以内。"
)


class DigestOutcome(Enum):
    SKIPPED = "skipped"
    CREATED = "created"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DigestSettings:
    trigger_ratio: float
    keep_recent_tokens: int

    @property
    def enabled(self) -> bool:
        return self.trigger_ratio > 0

    def trigger_tokens(self, soft_threshold: int) -> int:
        return int(soft_threshold * self.trigger_ratio) if soft_threshold > 0 and self.enabled else 0

    def keep_tokens(self, soft_threshold: int) -> int:
        trigger = self.trigger_tokens(soft_threshold)
        # 保留量压在触发线一半以下：否则摘要完剩下的原文还在线上，下一步又要摘要一次
        return min(self.keep_recent_tokens, trigger // 2) if trigger > 0 else self.keep_recent_tokens


def digest_settings(runtime_cfg: Any) -> DigestSettings:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return DigestSettings(
        trigger_ratio=get_float(cfg, "engine.compact.task_digest_trigger_ratio", 0.5, min_value=0.0, max_value=0.95),
        keep_recent_tokens=get_int(cfg, "engine.compact.task_keep_recent_tokens", 20_000, min_value=0, max_value=1_000_000),
    )


@dataclass(frozen=True)
class TaskDigest:
    task_id: str
    text: str
    narrative: str
    fingerprints: tuple[str, ...]
    covered_units: int
    covered_rounds: int
    covered_chars: int
    generated: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "task_id": self.task_id,
            "text": self.text,
            "narrative": self.narrative,
            "fingerprints": list(self.fingerprints),
            "covered_units": self.covered_units,
            "covered_rounds": self.covered_rounds,
            "covered_chars": self.covered_chars,
            "generated": self.generated,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any, *, task_id: str) -> "TaskDigest | None":
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != _VERSION:
            return None
        if data.get("task_id") != task_id:
            return None
        text = data.get("text")
        fingerprints = data.get("fingerprints")
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(fingerprints, list) or not fingerprints or not all(isinstance(item, str) for item in fingerprints):
            return None
        counts = [data.get(key) for key in ("covered_units", "covered_rounds", "covered_chars")]
        if any(type(value) is not int or value < 0 for value in counts):
            return None
        narrative = data.get("narrative")
        return cls(
            task_id=task_id,
            text=text,
            narrative=narrative if isinstance(narrative, str) else "",
            fingerprints=tuple(fingerprints),
            covered_units=counts[0],
            covered_rounds=counts[1],
            covered_chars=counts[2],
            generated=bool(data.get("generated")),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass(frozen=True)
class TaskUnit:
    """本任务里不能拆开的一段：一轮工具调用连同它的全部结果，或者单独一条别的消息。

    fingerprinted 是会落盘的那几条（带本任务的 source_task_id），恢复后重载的历史里也找得到它们。
    """

    indices: tuple[int, ...]
    fingerprinted: tuple[int, ...]
    tool_round: bool
    complete: bool


@dataclass(frozen=True)
class TaskLayout:
    instruction: int | None
    units: tuple[TaskUnit, ...]


@dataclass(frozen=True)
class DigestPlan:
    layout: TaskLayout
    covered_units: int
    target_units: int
    total_tokens: int


def _owned(message: Any, task_id: str) -> bool:
    if not isinstance(message, dict) or message.get("role") == "system":
        return False
    if message.get("_dynamic") or message.get("_ephemeral"):
        return False
    return record_task_id(message) == task_id


def _is_tool_attachment(message: Any) -> bool:
    # engine.inference 导入时要经过 llm_call 再导入本模块，放到模块顶部会循环导入
    from .inference.loop_state import TOOL_RESULT_ATTACHMENT_TYPE

    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and message_meta(message).get("message_type") == TOOL_RESULT_ATTACHMENT_TYPE
    )


def _round_attachment(message: Any, task_id: str) -> bool:
    return _owned(message, task_id) and _is_tool_attachment(message)


def task_layout(messages: list[Any], task_id: str) -> TaskLayout:
    """本任务的消息怎么分段：开头那条用户指令单独拎出来，其余按轮切，一轮的调用和结果永远在一起。"""
    if not task_id:
        return TaskLayout(None, ())
    first = next((index for index, message in enumerate(messages) if _owned(message, task_id)), None)
    if first is None:
        return TaskLayout(None, ())
    head = messages[first]
    instruction = first if head.get("role") == "user" and not is_tool_result_message(head) else None
    units: list[TaskUnit] = []
    index = first + 1 if instruction is not None else first
    while index < len(messages):
        message = messages[index]
        if not _owned(message, task_id):
            index += 1
            continue
        calls = tool_calls_of(message)
        if not calls:
            units.append(TaskUnit((index,), (index,), False, True))
            index += 1
            continue
        wanted = {call.call_id for call in calls}
        members, stamped = [index], [index]
        answered: list[str] = []
        cursor = index + 1
        # 工具出图时补的那条 user 消息写在结果后面，没执行的终止调用的错误结果又可能补在它后面
        while cursor < len(messages):
            candidate = messages[cursor]
            if is_tool_result_message(candidate):
                call_id = str(candidate.get("tool_call_id") or "")
                if call_id and call_id not in wanted:
                    break
                answered.append(call_id)
            elif not _round_attachment(candidate, task_id):
                break
            members.append(cursor)
            if _owned(candidate, task_id):
                stamped.append(cursor)
            cursor += 1
        if all(wanted):
            complete = wanted <= set(answered)
        else:
            complete = len(answered) >= len(calls)
        units.append(TaskUnit(tuple(members), tuple(stamped), True, complete))
        index = cursor
    return TaskLayout(instruction, tuple(units))


def _stable_record(message: dict[str, Any]) -> dict[str, Any]:
    # 只认落盘后重载还原得一模一样的字段：meta 重载时会补 message_id、改 timestamp
    return {
        "role": message.get("role"),
        "content": message.get("content"),
        "tool_calls": [
            {"id": call.call_id, "name": call.name, "arguments": call.arguments}
            for call in tool_calls_of(message)
        ],
        "tool_call_id": str(message.get("tool_call_id") or ""),
        "name": str(message.get("name") or ""),
    }


def message_fingerprint(message: dict[str, Any]) -> str:
    return record_fingerprint(_stable_record(message))


def verified_coverage(messages: list[Any], layout: TaskLayout, digest: TaskDigest | None) -> int | None:
    """摘要覆盖了本任务开头的几段；它记的指纹和现在这些消息逐条对上才算，对不上返回 None。"""
    if digest is None or not digest.fingerprints:
        return None
    expected = list(digest.fingerprints)
    seen: list[str] = []
    for count, unit in enumerate(layout.units, start=1):
        if not unit.complete:
            return None
        seen.extend(message_fingerprint(messages[index]) for index in unit.fingerprinted)
        if len(seen) > len(expected) or seen != expected[:len(seen)]:
            return None
        if len(seen) == len(expected):
            return count
    return None


def digest_message(digest: TaskDigest) -> dict[str, Any]:
    return {
        "role": "user",
        "content": digest.text,
        "_meta": {
            "source_task_id": digest.task_id,
            "message_type": DIGEST_MESSAGE_TYPE,
            "task_digest": True,
        },
    }


def _stray(message: Any) -> bool:
    # 夹在被覆盖的轮次中间、只活在运行期的提示和临时回执，跟着那几轮一起换掉
    return isinstance(message, dict) and bool(message.get("_retry_hint") or message.get("_ephemeral"))


def apply_task_digest(
    messages: list[Any], view: list[dict[str, Any]], task_id: str, digest: TaskDigest,
) -> list[dict[str, Any]] | None:
    """把 view 里被摘要覆盖的消息换成一条摘要消息；校验在 messages 上做，view 和它逐条对应。对不上返回 None。"""
    if len(view) != len(messages) or digest.task_id != task_id:
        return None
    layout = task_layout(messages, task_id)
    count = verified_coverage(messages, layout, digest)
    if not count:
        return None
    covered = sorted(index for unit in layout.units[:count] for index in unit.indices)
    first, last = covered[0], covered[-1]
    dropped = set(covered)
    dropped.update(index for index in range(first + 1, last) if _stray(messages[index]))
    result: list[dict[str, Any]] = []
    for index, message in enumerate(view):
        if index == first:
            result.append(digest_message(digest))
        if index not in dropped:
            result.append(message)
    return result


def _unit_tokens(view: list[dict[str, Any]], unit: TaskUnit, chars_per_token: float) -> int:
    return sum(estimate_message_tokens(view[index], chars_per_token) for index in unit.indices)


def _coverable_units(layout: TaskLayout, tokens: list[int], keep_tokens: int) -> int:
    """从最近往前留 keep_tokens 以内的原文（最近一段总是留着），返回前面能摘要的段数；遇到没配齐结果的轮次就停在它前面。"""
    units = layout.units
    kept = 1
    used = tokens[-1]
    for index in range(len(units) - 2, -1, -1):
        if used + tokens[index] > keep_tokens:
            break
        used += tokens[index]
        kept += 1
    target = len(units) - kept
    return next((index for index in range(target) if not units[index].complete), target)


def plan_task_digest(
    messages: list[Any],
    view: list[dict[str, Any]],
    *,
    task_id: str,
    digest: TaskDigest | None,
    chars_per_token: float,
    trigger_tokens: int,
    keep_tokens: int,
    require_no_digest: bool = False,
) -> DigestPlan | None:
    """要不要摘要、摘要到第几段。

    本任务没被摘要覆盖的原文（含开头指令，不含摘要本身）超过 trigger_tokens、且保留区之外还有没覆盖的完整轮次才返回计划；
    只数原文，摘要完要再长出半条触发线才会再摘要。require_no_digest 时已有能用的摘要就不再摘要。
    """
    if len(view) != len(messages):
        return None
    layout = task_layout(messages, task_id)
    if len(layout.units) < 2:
        return None
    covered = verified_coverage(messages, layout, digest)
    if covered and require_no_digest:
        return None
    covered = covered or 0
    tokens = [_unit_tokens(view, unit, chars_per_token) for unit in layout.units]
    total = sum(tokens[covered:])
    if layout.instruction is not None:
        total += estimate_message_tokens(view[layout.instruction], chars_per_token)
    if total <= trigger_tokens:
        return None
    target = _coverable_units(layout, tokens, keep_tokens)
    if target <= covered:
        return None
    return DigestPlan(layout=layout, covered_units=covered, target_units=target, total_tokens=total)


def _clip(text: str, limit: int) -> str:
    return excerpt_text(text, limit)


def _one_line(text: str, limit: int) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    return line if len(line) <= limit else f"{line[:limit]}…"


def _render_arguments(arguments: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(arguments)
    return _clip(rendered, _MAX_ARGUMENT_CHARS)


def _render_message(message: dict[str, Any], result_chars: int) -> list[str]:
    role = str(message.get("role") or "")
    text = content_text(message.get("content"))
    has_image = isinstance(message.get("content"), list) and any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"]
    )
    if is_tool_result_message(message):
        status = _STATUS_LABELS.get(tool_status(message), "")
        label = f"{message.get('name') or '工具'}，{status}" if status else str(message.get("name") or "工具")
        return [f"结果（{label}）：", _clip(text, result_chars)]
    if _is_tool_attachment(message):
        return ["[这一轮的工具产出了图片]"]
    lines: list[str] = []
    if role == "assistant":
        if text.strip():
            lines.append(f"助手：{_clip(text.strip(), result_chars)}")
        lines.extend(f"调用 {call.name}：{_render_arguments(call.arguments)}" for call in tool_calls_of(message))
        return lines
    speaker = "用户" if role == "user" else role or "消息"
    if text.strip():
        lines.append(f"{speaker}：{_clip(text.strip(), result_chars)}")
    if has_image:
        lines.append(f"{speaker}：[附带图片]")
    return lines


def _render_units(messages: list[Any], units: tuple[TaskUnit, ...], *, first_round: int, budget: int) -> str:
    results = sum(1 for unit in units for index in unit.indices if is_tool_result_message(messages[index]))
    result_chars = max(_MIN_RESULT_CHARS, min(_MAX_RESULT_CHARS, budget // max(1, results * 2)))
    blocks: list[str] = []
    round_number = first_round
    for unit in units:
        lines: list[str] = []
        if unit.tool_round:
            round_number += 1
            lines.append(f"── 第 {round_number} 轮 ──")
        for index in unit.indices:
            lines.extend(_render_message(messages[index], result_chars))
        if lines:
            blocks.append("\n".join(lines))
    return _clip("\n\n".join(blocks), budget)


def _input_chars(soft_threshold: int, chars_per_token: float) -> int:
    tokens = int(soft_threshold * _INPUT_SHARE_OF_THRESHOLD) if soft_threshold > 0 else _MAX_INPUT_TOKENS
    tokens = max(_MIN_INPUT_TOKENS, min(_MAX_INPUT_TOKENS, tokens))
    return int(tokens * max(chars_per_token, 1.0))


def build_digest_request(
    messages: list[Any],
    plan: DigestPlan,
    previous: TaskDigest | None,
    *,
    input_chars: int,
) -> list[dict[str, Any]]:
    """发给模型写摘要的那两条消息：说明放 system，任务指令、上一份摘要和新覆盖的轮次放 user。"""
    layout = plan.layout
    sections: list[str] = []
    if layout.instruction is not None:
        instruction = content_text(messages[layout.instruction].get("content")).strip()
        if instruction:
            sections.append(f"【任务指令】\n{_clip(instruction, _MAX_INSTRUCTION_CHARS)}")
    if previous is not None:
        sections.append(f"【上一份进展摘要】\n{previous.text}")
    first_round = sum(1 for unit in layout.units[:plan.covered_units] if unit.tool_round)
    material = _render_units(
        messages, layout.units[plan.covered_units:plan.target_units], first_round=first_round, budget=input_chars,
    )
    sections.append(f"【新完成的步骤】\n{material}")
    return [
        {"role": "system", "content": _DIGEST_INSTRUCTIONS},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def _span_label(entry: ReadEntry) -> str:
    span = entry.known_span
    if span is None:
        return "没读到内容"
    if span.first <= 1 and span.last == math.inf:
        return "全文"
    if span.last == math.inf:
        return f"第 {span.first} 行起"
    return f"第 {span.first}–{int(span.last)} 行"


def _command_label(arguments: dict[str, Any], status: str) -> str:
    command = _one_line(command_text(arguments), _MAX_COMMAND_CHARS) or "（空命令）"
    if status in ("", TOOL_SUCCESS):
        return command
    return f"{command}（{_STATUS_LABELS.get(status, status)}）"


def _list_section(title: str, items: list[str]) -> str:
    shown = items[-_MAX_LIST_ITEMS:]
    lines = [f"【{title}】"]
    if len(items) > len(shown):
        lines.append(f"- （更早的 {len(items) - len(shown)} 项从略）")
    lines.extend(f"- {item}" for item in shown)
    return "\n".join(lines)


def operation_lists(messages: list[Any], units: tuple[TaskUnit, ...]) -> str:
    """从被覆盖轮次的工具调用参数里确定性地整理出读过的文件、改过的文件和执行过的命令。"""
    resolved = resolve_tool_results(messages)
    reads: dict[str, None] = {}
    writes: dict[str, list[str]] = {}
    commands: list[str] = []
    for unit in units:
        for index in unit.indices:
            record = resolved.get(index)
            if record is None:
                continue
            message = messages[index]
            status = tool_status(message)
            if record.name == READ_TOOL:
                for entry in read_entries(record.arguments, message.get("content")):
                    reads[f"{entry.raw_path}（{_span_label(entry)}）"] = None
            elif record.name in REWRITE_TOOLS:
                if status != TOOL_SUCCESS:
                    continue
                for path in rewritten_paths(record.name, record.arguments, message.get("content")):
                    tools = writes.setdefault(path, [])
                    if record.name not in tools:
                        tools.append(record.name)
            elif record.name == COMMAND_TOOL:
                commands.append(_command_label(record.arguments, status))
    sections: list[str] = []
    if reads:
        sections.append(_list_section("读过的文件", list(reads)))
    if writes:
        sections.append(_list_section("改过的文件", [f"{path}（{'、'.join(tools)}）" for path, tools in writes.items()]))
    if commands:
        sections.append(_list_section("执行过的命令", commands))
    return "\n\n".join(sections)


def compose_task_digest(
    messages: list[Any],
    plan: DigestPlan,
    previous: TaskDigest | None,
    *,
    task_id: str,
    narrative: str,
    generated: bool,
) -> TaskDigest:
    covered = plan.layout.units[:plan.target_units]
    rounds = sum(1 for unit in covered if unit.tool_round)
    body = narrative if generated else (previous.narrative if previous is not None else "")
    lines = [
        DIGEST_PREFIX,
        f"本任务前面 {rounds} 轮工具调用的原始内容已从上下文中省略，下面是它们的进展；"
        "需要原文里的细节时重新读取文件或重新执行命令。",
    ]
    if body:
        lines.extend(["", body])
    if not generated:
        lines.extend(["", "（这次没能生成文字摘要，下面的操作清单是从工具调用里直接整理出来的。）"])
    lists = operation_lists(messages, covered)
    if lists:
        lines.extend(["", lists])
    return TaskDigest(
        task_id=task_id,
        text="\n".join(lines),
        narrative=body,
        fingerprints=tuple(message_fingerprint(messages[index]) for unit in covered for index in unit.fingerprinted),
        covered_units=plan.target_units,
        covered_rounds=rounds,
        covered_chars=total_content_chars([messages[index] for unit in covered for index in unit.indices]),
        generated=generated,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _file_stem(task_id: str) -> str:
    readable = _UNSAFE_STEM_CHARS.sub("_", task_id).strip("._-")[:80] or "task"
    if readable == task_id:
        return readable
    digest = hashlib.sha256(task_id.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{readable}-{digest}"


def digest_path(workspace_root: Path | str, task_id: str) -> Path:
    shard = hashlib.sha256(task_id.encode("utf-8", "surrogatepass")).hexdigest()[:2]
    # 多套一层目录：supervisor 每小时删 node_contexts/<目录>/ 第一层里一小时没动过的 json，更深的只归 48 小时的 data_cleanup 管
    return Path(workspace_root).joinpath(*_STORE_PARTS, shard) / f"{_file_stem(task_id)}.json"


def save_task_digest(workspace_root: Path | str, digest: TaskDigest) -> Path:
    """原子写入摘要文件。阻塞 I/O，调用方放到线程里。"""
    path = digest_path(workspace_root, digest.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(digest.to_dict(), ensure_ascii=False, indent=2)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def load_task_digest(workspace_root: Path | str, task_id: str) -> TaskDigest | None:
    """读回这个任务存下的摘要；没有、读不了或格式不对都返回 None。阻塞 I/O，调用方放到线程里。"""
    if not task_id:
        return None
    path = digest_path(workspace_root, task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("task digest %s is unreadable; ignored", path, exc_info=True)
        return None
    digest = TaskDigest.from_dict(data, task_id=task_id)
    if digest is None:
        logger.warning("task digest %s has an unexpected shape; ignored", path)
    return digest


async def restore_task_digest(ls: Any) -> TaskDigest | None:
    """任务挂起后恢复（同一个 task_id、新的 loop state）时读回摘要，指纹和重载的历史对得上才接着用。"""
    rctx = getattr(ls, "rctx", None)
    task_id = str(getattr(rctx, "task_id", "") or "")
    workspace_root = getattr(rctx, "workspace_root", None)
    if not task_id or workspace_root is None:
        return None
    digest = await asyncio.to_thread(load_task_digest, workspace_root, task_id)
    if digest is None:
        return None
    if verified_coverage(ls.messages, task_layout(ls.messages, task_id), digest) is None:
        logger.info("task digest for %s no longer matches the reloaded history; not used", task_id)
        return None
    ls.task_digest = digest
    logger.info("task digest for %s restored: %d rounds covered", task_id, digest.covered_rounds)
    return digest


DigestCall = Callable[[list[dict[str, Any]]], Awaitable[Any]]


async def _request_narrative(ls: Any, call: DigestCall, request: list[dict[str, Any]]) -> tuple[str, bool, bool]:
    """(摘要正文, 是否生成成功, 是否被取消)。"""
    task_id = str(getattr(getattr(ls, "rctx", None), "task_id", "") or "")
    try:
        response = await call(request)
    except PluginExecutionError:
        raise
    except Exception:
        logger.warning("task digest request failed: task=%s", task_id, exc_info=True)
        return "", False, False
    if response is None:
        return "", False, True
    usage = getattr(response, "usage", None)
    total = getattr(getattr(ls, "rctx", None), "total_usage", None)
    if isinstance(usage, dict) and isinstance(total, dict):
        accumulate_usage(total, usage)
    text = (getattr(response, "text", None) or "").strip() if getattr(response, "ok", False) else ""
    if not text:
        logger.warning(
            "task digest request returned no text: task=%s error=%s", task_id, getattr(response, "error", "") or "",
        )
        return "", False, False
    return _clip(text, _MAX_NARRATIVE_CHARS), True, False


async def _persist(ls: Any, digest: TaskDigest) -> None:
    workspace_root = getattr(getattr(ls, "rctx", None), "workspace_root", None)
    if workspace_root is None:
        return
    try:
        await asyncio.to_thread(save_task_digest, workspace_root, digest)
    except OSError:
        # 落盘失败只影响挂起后恢复，当轮照样用内存里这份
        logger.warning("saving task digest failed: task=%s", digest.task_id, exc_info=True)


async def run_task_digest(
    ls: Any,
    *,
    step: int,
    view: list[dict[str, Any]],
    chars_per_token: float,
    trigger_tokens: int,
    keep_tokens: int,
    call: DigestCall,
    reason: str,
    require_no_digest: bool = False,
) -> DigestOutcome:
    """该摘要时生成一份覆盖范围更大的新摘要，写进 loop state 并落盘，发 task_digest 事件。

    view 是和 ls.messages 逐条对应、还没换入摘要的请求视图，token 按它估。
    """
    rctx = getattr(ls, "rctx", None)
    task_id = str(getattr(rctx, "task_id", "") or "")
    if not task_id:
        return DigestOutcome.SKIPPED
    messages = ls.messages
    current = getattr(ls, "task_digest", None)
    if current is not None and verified_coverage(messages, task_layout(messages, task_id), current) is None:
        ls.task_digest = current = None
    plan = plan_task_digest(
        messages, view, task_id=task_id, digest=current, chars_per_token=chars_per_token,
        trigger_tokens=trigger_tokens, keep_tokens=keep_tokens, require_no_digest=require_no_digest,
    )
    if plan is None:
        return DigestOutcome.SKIPPED
    previous = current if plan.covered_units else None
    soft_threshold = int(getattr(ls, "compact_threshold", 0) or 0)
    request = build_digest_request(
        messages, plan, previous, input_chars=_input_chars(soft_threshold, chars_per_token),
    )
    narrative, generated, cancelled = await _request_narrative(ls, call, request)
    if cancelled:
        return DigestOutcome.CANCELLED
    digest = compose_task_digest(
        messages, plan, previous, task_id=task_id, narrative=narrative, generated=generated,
    )
    ls.task_digest = digest
    await _persist(ls, digest)
    logger.info(
        "task digest for %s (%s): %d rounds / %d chars covered, generated=%s",
        task_id, reason, digest.covered_rounds, digest.covered_chars, generated,
    )
    await rctx.emit_event(DIGEST_EVENT, {
        "node_id": getattr(getattr(ls, "node", None), "id", ""),
        "task_id": task_id,
        "step": step,
        "reason": reason,
        "covered_rounds": digest.covered_rounds,
        "covered_chars": digest.covered_chars,
        "digest_chars": len(digest.text),
        "generated": generated,
        "incremental": previous is not None,
    })
    return DigestOutcome.CREATED

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from toolbox.redaction import hides_entry

from .tool_result_formatters import (
    ToolResultFormatContext,
    format_tool_result_by_structure,
    json_fallback,
)



def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "x").strip() or "x")[:80]


TOOL_SUCCESS = "success"
TOOL_ERROR = "error"
TOOL_CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolOutcome:
    status: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == TOOL_SUCCESS


def tool_outcome(result: Any) -> ToolOutcome:
    """工具结果算成功、失败还是被取消；所有判定都走这一处。"""
    if not isinstance(result, dict):
        return ToolOutcome(TOOL_SUCCESS)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    error = str(result.get("error") or "").strip()
    # registry 归一化 ok=false 的结果时会把顶层 cancelled 挪进 data
    if result.get("cancelled") or data.get("cancelled"):
        return ToolOutcome(TOOL_CANCELLED, error or "task cancelled")
    # 带 ok 的结果以 ok 为准；没有 ok 的旧格式靠 error 判
    failed = result.get("ok") is False if "ok" in result else bool(error)
    if not failed:
        return ToolOutcome(TOOL_SUCCESS)
    rendered = str(data.get("result") or "").strip()
    if rendered.upper().startswith("ERROR:"):
        rendered = rendered[6:].strip()
    return ToolOutcome(TOOL_ERROR, error or rendered or "tool returned ok=false")



def result_to_raw(
    tool_name: str,
    result: Any,
    *,
    tool_spec: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """把工具结果转为 (format, raw_text)。"""
    # [AutoC 2026-05-31] Why: tools are migrating to a unified ok/data/error
    # response shape, where data.result is the canonical human-readable transcript.
    # How: prefer data.result before the legacy structure-based registry. Purpose:
    # keep model-visible tool history stable while structured fields move under data.
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return "text", data["result"]

    # [AutoC 2026-05-31] Why: result formatting previously depended on hard-coded
    # tool-name branches, so compatible tools with the same return structure could
    # not reuse readable renderers. How: create a formatting context, route through
    # the structure-based formatter registry, and use the preserved JSON fallback
    # only when no formatter matches. Purpose: keep the old public signature while
    # allowing tool_spec to opt into explicit result_format routing.
    ctx = ToolResultFormatContext(tool_name=tool_name, tool_spec=tool_spec)
    formatted = format_tool_result_by_structure(result, ctx)
    if formatted is not None:
        return formatted
    return json_fallback(result)


def _one_line_text(value: Any) -> str:
    """Return a whitespace-normalized single-line string for progress logs."""
    # [summary-args 2026-05-19] Why: handoff_progress is displayed as one log row,
    # but commands, queries, and final text can contain newlines. How: collapse all
    # whitespace into single spaces before composing summaries. Purpose: keep every
    # summarize_result() output safe for one-line progress messages.
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _clip_one_line(value: Any, limit: int) -> str:
    """Normalize text to one line and append an ellipsis when it is too long."""
    text = _one_line_text(value)
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _summary_line(text: Any, limit: int = 120) -> str:
    """Enforce the final one-line and reasonable-length summary contract."""
    # [summary-args 2026-05-19] Why: individual argument snippets are clipped, but
    # a long path plus prefix can still exceed the desired log width. How: apply a
    # final 120-character cap after composing the message. Purpose: preserve the
    # legacy handoff_progress shape without creating overly long progress rows.
    line = _one_line_text(text)
    if len(line) > limit:
        return line[:max(0, limit - 3)] + "..."
    return line


def _brief_args(args: dict | None, *, value_limit: int = 30) -> str:
    """Build a compact fallback argument summary for tools without custom rules."""
    # [summary-args 2026-05-19] Why: the fallback rule requested by operators is
    # intentionally narrow: unknown tools should expose the first key=value only,
    # not a dump of every argument. How: take the first insertion-ordered item,
    # clip its key and value, and redact obvious secret-bearing argument names.
    # Purpose: make generic handoff_progress rows informative while preserving the
    # one-line, under-120-character summary contract.
    if not isinstance(args, dict) or not args:
        return ""
    key, value = next(iter(args.items()))
    key_text = _clip_one_line(key, 24)
    if hides_entry(key, value):
        value_text = "<redacted>"
    elif isinstance(value, (dict, list, tuple)):
        try:
            value_text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            value_text = str(value)
        value_text = _clip_one_line(value_text, value_limit)
    else:
        value_text = _clip_one_line(value, value_limit)
    return _summary_line(f"{key_text}={value_text}", 100)


def _dispatch_target(tool_name: str, args: dict) -> str:
    """Resolve the target node name for dynamic and legacy dispatch-style tools."""
    # [summary-args 2026-05-19] Why: dispatch:{target} stores the node in the tool
    # name, while older dispatch_to_* names may also carry target in arguments. How:
    # prefer the fixed tool-name target and fall back to explicit argument fields.
    # Purpose: show a stable target_node in the progress summary for both forms.
    if tool_name.startswith("dispatch:"):
        return _clip_one_line(tool_name.split(":", 1)[1], 40)
    suffix = tool_name.removeprefix("dispatch_to_") if tool_name.startswith("dispatch_to_") else ""
    return _clip_one_line(args.get("target_node") or args.get("target") or suffix, 40)


def _edit_check_suffix(result_data: dict) -> str:
    """write_file / apply_diff 写成了但改后检查没过：进度行里也得看得出来。"""
    syntax = result_data.get("syntaxCheck")
    if isinstance(syntax, dict) and syntax.get("ok") is False:
        return "，语法检查未通过"
    checks = result_data.get("externalChecks")
    if isinstance(checks, list) and any(isinstance(check, dict) and check.get("ok") is False for check in checks):
        return "，外部检查未通过"
    return ""


def summarize_result(tool_name: str, result: Any, *, args: dict | None = None) -> str:
    """生成简短的工具结果摘要。"""
    # [summary-args 2026-05-19] Why: approval events already carry full details,
    # but handoff_progress only has this short summary. How: accept optional tool
    # arguments and use per-tool snippets for the parameters operators look for.
    # Purpose: keep the public message format unchanged while making each row
    # informative enough to identify the command, search, memory, or handoff.
    safe_args = args if isinstance(args, dict) else {}

    if tool_name.startswith("dispatch_to_") or tool_name.startswith("dispatch:"):
        target = _dispatch_target(tool_name, safe_args)
        instruction = _clip_one_line(safe_args.get("instruction", ""), 40)
        return _summary_line(f"委派 {target}: {instruction}" if instruction else f"委派 {target}")
    if tool_name == "finish":
        text = _clip_one_line(safe_args.get("text", ""), 40)
        return _summary_line(f"完成: {text}" if text else "完成")
    if tool_name == "reply":
        text = _clip_one_line(safe_args.get("text", ""), 40)
        return _summary_line(f"中间回复: {text}" if text else "中间回复")

    if not isinstance(result, dict):
        extra = _brief_args(safe_args)
        return _summary_line(f"已获得结果: {extra}" if extra else "已获得结果")
    # [AutoC 2026-05-31] Why: migrated tools keep their useful fields under data,
    # while legacy callers still return top-level fields. How: normalize the nested
    # data dict once and let every summary branch consult it before old fields.
    # Purpose: keep progress summaries compatible through the response migration.
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    outcome = tool_outcome(result)
    if not outcome.ok:
        return _summary_line(f"失败: {outcome.error}")
    if tool_name == "read_file":
        data = result.get("data")
        if isinstance(data, dict):
            sc = data.get("successCount", 0)
            fc = data.get("failCount", 0)
            oc = data.get("omittedCount", 0)
            tc = data.get("totalCount", 0)
            if tc == 1 and sc == 1:
                rs = data.get("results", [])
                p = rs[0].get("path", "") if rs else result.get("path", "")
                return _summary_line(f"已读取 {p}")
            if fc > 0 or oc > 0:
                skipped = f", {oc} 个超出单次上限未读" if oc else ""
                return _summary_line(f"读取 {tc} 个文件: {sc} 成功, {fc} 失败{skipped}")
            return _summary_line(f"已读取 {sc} 个文件")
        return _summary_line(f"已读取 {result.get('path', '') or safe_args.get('path', '')}")
    if tool_name == "execute_command":
        # [AutoC 2026-05-31] Why: execute_command now stores returncode under data.
        # How: read data.returncode first and fall back to the legacy top-level
        # field. Purpose: keep shell command progress summaries unchanged.
        rc = result_data.get("returncode", result.get("returncode"))
        cmd_short = _clip_one_line(safe_args.get("command", ""), 60)
        return _summary_line(f"命令完成 (rc={rc}): {cmd_short}" if cmd_short else f"命令完成 (rc={rc})")
    if tool_name == "write_file":
        # [AutoC 2026-05-31] Why: write_file now stores path under data. How: prefer
        # data.path while preserving legacy top-level path and argument fallback.
        # Purpose: keep file-write progress readable through the migration.
        written = result_data.get('path', '') or result.get('path', '') or safe_args.get('path', '')
        return _summary_line(f"已写入 {written}{_edit_check_suffix(result_data)}")
    if tool_name == "search_in_files":
        q = _clip_one_line(safe_args.get("query", ""), 30)
        p = _clip_one_line(safe_args.get("path", "."), 40)
        data = result.get("data", {})
        count = data.get("count", "?") if isinstance(data, dict) else "?"
        return _summary_line(f'搜索 "{q}" in {p} ({count} 结果)')
    if tool_name == "apply_diff":
        p = _clip_one_line(safe_args.get("path", ""), 60)
        diffs = safe_args.get("diffs", [])
        n = len(diffs) if isinstance(diffs, list) else 0
        unchanged = "，内容无变化" if result_data.get("changed") is False else ""
        return _summary_line(f"差异应用 {p} ({n} 处修改{unchanged}){_edit_check_suffix(result_data)}")
    if tool_name == "save_memory":
        memory_id = _clip_one_line(safe_args.get("id", ""), 40)
        book = _clip_one_line(safe_args.get("book", "default"), 40)
        return _summary_line(f"保存记忆 id={memory_id} book={book}")
    if tool_name == "delete_memory":
        memory_id = _clip_one_line(safe_args.get("id", ""), 40)
        return _summary_line(f"删除记忆 id={memory_id}")
    if tool_name == "list_dir":
        data = result.get("data")
        if isinstance(data, dict):
            tf = data.get("totalFiles", 0)
            td = data.get("totalDirs", 0)
            tp = data.get("totalPaths", 0)
            if tp == 1:
                return _summary_line(f"已列出目录 ({td} 目录, {tf} 文件)")
            return _summary_line(f"已列出 {tp} 个目录 ({td} 子目录, {tf} 文件)")
        return _summary_line(f"已列出 {result.get('path', '.')}")
    extra = _brief_args(safe_args)
    return _summary_line(f"已获得结果: {extra}" if extra else "已获得结果")


# 进度行写给人看，QQ 适配器还要从里面认出调的是哪个工具：拼法和拆法只在这里写一份。
_BATCH_NAMES_SHOWN = 5
_BATCH_NAME_SEPARATOR = "、"
_PROGRESS_LINE = re.compile(r"\[[^\]\n]*\] (?P<body>.+)")
_BATCH_BODY = re.compile(r"执行 \d+ 个工具：(?P<names>.+?)(?: 等 \d+ 个工具)?")
_START_BODY = re.compile(r"开始执行 (?P<name>\S+)")
# 工具名自己可以带冒号（dispatch:<目标>），名字截到第一个后面是空格或行尾的冒号。
_RESULT_BODY = re.compile(r"(?P<name>[^\s:：]\S*?):(?: .*)?")


def tool_batch_progress(source: str, tool_names: Sequence[str], call_count: int) -> str:
    """一批真实工具开跑时的进度行：调用数，再列出最多五个工具名。"""
    names = [name for name in tool_names if name]
    line = f"[{source}] 执行 {call_count} 个工具"
    if not names:
        return line
    shown = _BATCH_NAME_SEPARATOR.join(names[:_BATCH_NAMES_SHOWN])
    if len(names) > _BATCH_NAMES_SHOWN:
        shown += f" 等 {len(names)} 个工具"
    return f"{line}：{shown}"


def tool_start_progress(source: str, tool_name: str) -> str:
    """单独的工具任务开跑时的进度行。"""
    return f"[{source}] 开始执行 {tool_name}"


def tool_result_progress(source: str, tool_name: str, summary: str) -> str:
    """一个工具调用有了结果或转到后台时的进度行。"""
    return f"[{source}] {tool_name}: {summary}"


def progress_tool_names(record: str) -> tuple[str, ...]:
    """进度行点到的工具名，按出现的顺序；摘要里的路径、关键词、命令不算。不是工具进度行时为空。"""
    line = _PROGRESS_LINE.fullmatch(str(record or "").strip())
    if line is None:
        return ()
    body = line["body"]
    if batch := _BATCH_BODY.fullmatch(body):
        return tuple(batch["names"].split(_BATCH_NAME_SEPARATOR))
    if start := _START_BODY.fullmatch(body):
        return (start["name"],)
    if result := _RESULT_BODY.fullmatch(body):
        return (result["name"],)
    return ()


def format_tool_trace(entries: list[dict[str, Any]]) -> str:
    """把一批工具调用结果格式化为 CLONOTH_TOOL_TRACE 块。

    v2: 简化字段名，减少冗余前缀。
    """
    lines = ["[CLONOTH_TOOL_TRACE v2]"]
    for e in entries:
        lines.append(f"TOOL: {e['name']} {json.dumps(e.get('args', {}), ensure_ascii=False)}")
        lines.append(f"RESULT_FORMAT: {e.get('format', 'json')}")
        if e.get("truncated"):
            lines.append("RESULT_TRUNCATED: true")
        if e.get("ref"):
            lines.append(f"RESULT_REF: {e['ref']}")
        raw = e.get("raw_inline", "")
        if raw:
            lines.append("RESULT:")
            for ln in raw.splitlines():
                lines.append("  " + ln)
        else:
            lines.append("RESULT: <empty>")
        lines.append(f"SUMMARY: {e.get('summary', '')}")
        atts = e.get("attachments")
        if isinstance(atts, list) and atts:
            att_paths = [str(a.get('path', '')) for a in atts if isinstance(a, dict) and a.get('path')]
            if att_paths:
                lines.append(f"ATTACHMENTS: {', '.join(att_paths)}")
        lines.append("---")
    lines.append("[/CLONOTH_TOOL_TRACE]")
    return "\n".join(lines)

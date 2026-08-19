from __future__ import annotations

import json
from pathlib import Path
import sys

# [AutoC 2026-05-31] Why: the requested pytest command runs this file directly
# from the repository checkout, not from an installed package. How: add the repo
# root to sys.path before importing engine modules. Purpose: keep formatter tests
# runnable in the same lightweight mode as existing tool-step tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.tool_result_formatters import (  # noqa: E402
    ToolResultFormatContext,
    ToolResultFormatter,
    format_tool_result_by_structure,
    json_fallback,
    register_result_formatter,
)
from engine.tool_step import result_to_raw  # noqa: E402


def test_command_like_result_formats_by_structure_for_any_tool_name() -> None:
    # [AutoC 2026-05-31] Why: command output routing must no longer depend on
    # execute_command or remote_exec names. How: pass a returncode/output payload
    # under an unrelated tool name. Purpose: prove structure-based routing handles
    # compatible tools automatically.
    assert result_to_raw("custom_shell", {"ok": True, "returncode": 0, "output": "done"}) == (
        "text",
        "returncode=0\ndone",
    )


def test_search_like_result_formats_by_structure_for_any_tool_name() -> None:
    # [AutoC 2026-05-31] Why: search results are defined by data.count,
    # data.truncated, and file/line/match entries. How: use an unrelated tool name
    # with that shape. Purpose: prevent regression to name-based search routing.
    fmt, raw = result_to_raw(
        "custom_search",
        {
            "success": True,
            "data": {
                "results": [
                    {
                        "file": "engine/tool_step.py",
                        "line": 12,
                        "match": "result_to_raw",
                        "context": "12: def result_to_raw(...)",
                    }
                ],
                "count": 1,
                "truncated": False,
            },
        },
    )

    assert fmt == "text"
    assert raw == "1 results found:\n\nengine/tool_step.py:12 | result_to_raw\n  12: def result_to_raw(...)"


def test_read_file_like_result_formats_by_structure_for_any_tool_name() -> None:
    # [AutoC 2026-05-31] Why: read_file batch rendering moved out of result_to_raw
    # and should match any documented data.results file-entry payload. How: use a
    # text file entry under a custom tool name. Purpose: keep file transcripts
    # readable without checking the tool name.
    assert result_to_raw(
        "custom_reader",
        {
            "success": True,
            "data": {
                "results": [
                    {"path": "notes.txt", "success": True, "type": "text", "content": "1 | hello"}
                ],
                "successCount": 1,
                "failCount": 0,
                "totalCount": 1,
            },
        },
    ) == ("text", "── notes.txt ──\n1 | hello")


def test_mcp_content_parts_concatenate_text_and_mark_non_text_parts() -> None:
    # [AutoC 2026-05-31] Why: MCP tools return content parts, sometimes wrapped in
    # a top-level result field by mcp_runtime.call_tool. How: feed that structure
    # with text, image, and resource parts. Purpose: verify MCP output stays
    # compact and readable after structural routing.
    fmt, raw = result_to_raw(
        "mcp_any_tool",
        {
            "ok": True,
            "result": {
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "image", "path": "data/attachments/image.png"},
                    {"type": "resource", "resource": {"uri": "file:///tmp/item.txt"}},
                ]
            },
        },
    )

    assert fmt == "text"
    assert raw == "alpha\n[image: data/attachments/image.png]\n[resource: file:///tmp/item.txt]"


def test_primary_text_and_description_results_are_readable() -> None:
    # [AutoC 2026-05-31] Why: simple external tools often return one primary text
    # field. How: cover both text and description keys. Purpose: avoid JSON dumps
    # for common one-field result payloads.
    assert result_to_raw("x_search", {"text": "final answer", "usage": {}}) == ("text", "final answer")
    assert result_to_raw("read_image", {"description": "visible text", "model": "vision"}) == (
        "text",
        "visible text",
    )


def test_unknown_structure_falls_back_to_json_dump() -> None:
    # [AutoC 2026-05-31] Why: unrecognized structures must keep the old readable
    # JSON fallback. How: use a dict that does not satisfy any strict formatter
    # predicate. Purpose: preserve backward compatibility for arbitrary tools.
    result = {"items": [1, 2], "nested": {"a": True}}
    assert result_to_raw("unknown_tool", result) == ("json", json.dumps(result, ensure_ascii=False, indent=2))


def test_formatter_exception_is_logged_and_next_formatter_or_fallback_is_used() -> None:
    # [AutoC 2026-05-31] Why: one faulty formatter must not break tool result
    # delivery. How: register a deliberately failing formatter for a unique shape
    # that no later formatter handles. Purpose: verify format_tool_result_by_structure
    # skips exceptions and the caller can still use json_fallback().
    def _boom_predicate(result: object, ctx: ToolResultFormatContext) -> bool:
        return isinstance(result, dict) and result.get("explode_for_test") is True

    def _boom_render(result: object, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
        raise RuntimeError("intentional formatter failure")

    register_result_formatter(
        ToolResultFormatter(
            id="test_formatter_exception_is_skipped",
            priority=5,
            predicate=_boom_predicate,
            render=_boom_render,
        )
    )

    result = {"explode_for_test": True, "payload": [1]}
    assert format_tool_result_by_structure(result, ToolResultFormatContext(tool_name="bad_tool")) is None
    assert result_to_raw("bad_tool", result) == json_fallback(result)


def test_tool_spec_result_format_can_select_a_formatter_id() -> None:
    # [AutoC 2026-05-31] Why: registry specs may now preserve result_format for
    # external tools. How: request the command_output formatter through tool_spec
    # while using a custom tool name. Purpose: prove the metadata route is wired
    # without depending on the executed tool name.
    assert result_to_raw(
        "external_runner",
        {"returncode": 7, "output": "failed"},
        tool_spec={"result_format": "command_output"},
    ) == ("text", "returncode=7\nfailed")

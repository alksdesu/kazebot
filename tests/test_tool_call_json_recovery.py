"""工具调用 JSON 里出现裸换行时的恢复。

模型往 command / content 塞多行脚本时，字符串里给的是真实换行而不是 \\n —— 按 JSON
规范这是非法控制字符。解析失败的代价不只是丢一次调用：hybrid 会把剩下的正文当隐式
finish 发出去，于是整段协议标记连同脚本被甩给用户。
"""
from __future__ import annotations

import json

import pytest

from engine.inference.tool_format import JsonToolFormatter
from providers.base import ProviderResponse

_SCRIPT = "python3 -c \"\nimport os, json\nprint('hi')\n\""


def _broken_json(name: str, arguments: dict) -> str:
    """生成模型真会写出来的那种 JSON：字符串内是裸换行。"""
    return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False).replace("\\n", "\n")


def _wrap(body: str) -> ProviderResponse:
    return ProviderResponse(ok=True, text=f"<<<TOOL_CALL>>>\n{body}\n<<<END_TOOL_CALL>>>")


class TestLenientParsing:
    def test_a_bare_newline_is_rejected_by_the_strict_pass(self) -> None:
        # 前提确认：这确实是标准 json.loads 拒收的输入。
        with pytest.raises(json.JSONDecodeError):
            json.loads(_broken_json("execute_command", {"command": _SCRIPT}))

    def test_but_the_formatter_still_parses_it(self) -> None:
        parsed = JsonToolFormatter._try_parse_json(
            _broken_json("execute_command", {"command": _SCRIPT}))

        assert parsed is not None
        assert parsed["name"] == "execute_command"

    def test_the_script_survives_byte_for_byte(self) -> None:
        parsed = JsonToolFormatter._try_parse_json(
            _broken_json("execute_command", {"command": _SCRIPT}))

        assert parsed["arguments"]["command"] == _SCRIPT

    def test_multiline_file_content_too(self) -> None:
        body = "第一行\n第二行\n\n第四行"
        parsed = JsonToolFormatter._try_parse_json(
            _broken_json("write_file", {"path": "a.txt", "content": body}))

        assert parsed["arguments"]["content"] == body

    def test_well_formed_json_is_unaffected(self) -> None:
        parsed = JsonToolFormatter._try_parse_json(
            json.dumps({"name": "finish", "arguments": {"text": "行一\n行二"}}))

        assert parsed["arguments"]["text"] == "行一\n行二"


class TestEndToEnd:
    def test_a_multiline_command_becomes_a_real_tool_call(self) -> None:
        calls = JsonToolFormatter().parse_tool_calls(
            _wrap(_broken_json("execute_command", {"command": _SCRIPT})))

        assert len(calls) == 1
        assert calls[0].name == "execute_command"
        assert calls[0].arguments["command"] == _SCRIPT

    def test_the_markers_never_survive_into_plain_text(self) -> None:
        response = _wrap(_broken_json("execute_command", {"command": _SCRIPT}))

        assert "<<<TOOL_CALL>>>" not in (JsonToolFormatter().get_plain_text(response) or "")

    def test_prose_around_the_call_is_kept(self) -> None:
        body = _broken_json("execute_command", {"command": _SCRIPT})
        response = ProviderResponse(
            ok=True, text=f"这就去扫\n<<<TOOL_CALL>>>\n{body}\n<<<END_TOOL_CALL>>>")

        assert JsonToolFormatter().get_plain_text(response) == "这就去扫"


class TestRepairFallback:
    """宽松解析都救不回来时，至少要把多行参数捞出来。"""

    def test_command_is_recovered_from_a_truncated_call(self) -> None:
        # 少了结尾的 }}，宽松解析也无能为力。
        raw = '<<<TOOL_CALL>>>\n{"name": "execute_command", "arguments": {"command": "ls -la'
        repaired = JsonToolFormatter._repair_tool_call_json(raw)

        assert repaired is not None
        assert repaired["name"] == "execute_command"
        assert repaired["arguments"]["command"] == "ls -la"

    def test_write_file_keeps_both_path_and_content(self) -> None:
        raw = ('<<<TOOL_CALL>>>\n{"name": "write_file", "arguments": '
               '{"path": "a.txt", "content": "正文')
        repaired = JsonToolFormatter._repair_tool_call_json(raw)

        assert repaired["arguments"]["path"] == "a.txt"
        assert repaired["arguments"]["content"] == "正文"

    def test_a_tool_with_nothing_recoverable_is_still_refused(self) -> None:
        # 捞不到任何已知参数的非终止工具，宁可判失败也不要瞎调。
        raw = '<<<TOOL_CALL>>>\n{"name": "some_tool", "arguments": {"weird": "x"'

        assert JsonToolFormatter._repair_tool_call_json(raw) is None

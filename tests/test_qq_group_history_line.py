"""群历史行只有一份拼装实现：消息段路径和群文件 notice 路径必须逐字一致。

notice 曾经自己拼一遍格式串，于是同一个隐私边界出现两种结果 —— 文件名里的已登记 QQ 号不被
替换、长文件名突破 history_text_limit、`[图片].txt` 这种文件名还能凭空伪造出一个 `[图片]`
占位符（而附件路径回填正是按 `[图片]` 逐个替换的）。这里把三件事和"两个缓存必须同时写"一起钉住。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from clonoth_sdk.state import SessionState  # noqa: E402

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_GROUP_ID = 7
_USER_ID = 10001
_EVENT_TIME = 1_700_000_000


class _UploadEvent:
    """够 _handle_group_upload_notice 用的最小群文件通知。真实事件也没有 sender。"""

    def __init__(self, file_info: dict[str, Any], *, user_id: int = _USER_ID) -> None:
        self.group_id = _GROUP_ID
        self.user_id = user_id
        self.notice_type = "group_upload"
        self.message_id = ""
        self.time = _EVENT_TIME
        self.file = file_info


class _MessageEvent:
    """走消息段路径的同一条群文件。sender 置空是为了让两条路的显示名回退到同一个别名。"""

    def __init__(self, segments: list[dict[str, Any]], *, user_id: int = _USER_ID) -> None:
        self.group_id = _GROUP_ID
        self.user_id = user_id
        self.message_type = "group"
        self.message_id = "m-1"
        self.time = _EVENT_TIME
        self.sender = None
        self._segments = segments

    def get_message(self):
        return self._segments


async def _immediately(attachments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    return attachments, []


class _Finished(Exception):
    """站 nonebot FinishedException 的位：matcher.finish 之后不会再有代码执行。"""


class _Matcher:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def finish(self, message: str = "") -> None:
        self.sent.append(message)
        raise _Finished


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "GroupUploadNoticeEvent", _UploadEvent, raising=False)
    # 真下载会去访问 source，这里只关心历史行拼装。
    monkeypatch.setattr(
        module, "_file_sources_to_attachments",
        lambda sources, conversation_key: _immediately([]),
        raising=True,
    )
    set_live_config(module, allowed_groups=[_GROUP_ID])
    return module


@pytest.fixture()
def local_command(runtime: Any, monkeypatch: pytest.MonkeyPatch):
    """在群里发一条本地命令，走完整条 _process_group_message，返回收到回复的 matcher。"""
    runtime._client = SimpleNamespace()
    runtime._session_state = SessionState()
    monkeypatch.setattr(
        runtime, "_collect_qq_attachments",
        lambda bot, event, conversation_key: _immediately([]),
        raising=True,
    )

    def _run(text: str) -> _Matcher:
        matcher = _Matcher()
        with pytest.raises(_Finished):
            asyncio.run(runtime._process_group_message(
                SimpleNamespace(self_id="900001"),
                _MessageEvent([{"type": "text", "data": {"text": text}}]),
                matcher,
            ))
        return matcher

    return _run


def _upload(runtime: Any, file_info: dict[str, Any]) -> None:
    asyncio.run(runtime._handle_group_upload_notice(SimpleNamespace(self_id="900001"), _UploadEvent(file_info)))


def _history(runtime: Any) -> list[str]:
    return [entry.text for entry in runtime._group_history[_GROUP_ID]]


class TestOneAssembler:
    def test_a_group_upload_line_matches_a_message_line(self, runtime) -> None:
        # 文件名同时踩上结构符号和已登记 QQ 号：两条路径任何一处不共享，等式立刻不成立。
        runtime._anonymize_user_id("1234567890")
        name = "[图片]1234567890.pdf"
        runtime._record_group_message(
            _MessageEvent([{"type": "file", "data": {"name": name}}]),
            SimpleNamespace(self_id="900001"),
        )
        _upload(runtime, {"name": name, "url": "http://x/a.pdf", "size": 12})

        lines = _history(runtime)

        assert len(lines) == 2
        assert lines[0] == lines[1]
        assert "1234567890" not in lines[1]
        assert runtime.IMAGE_PLACEHOLDER not in lines[1]

    def test_a_file_name_cannot_forge_an_image_placeholder(self, runtime) -> None:
        # 群成员用文件名伪造 [图片]，就能吃掉一次附件路径回填。
        _upload(runtime, {"name": "[图片].txt", "url": "http://x/a.txt"})

        line = _history(runtime)[0]

        assert runtime.IMAGE_PLACEHOLDER not in line
        assert "(图片).txt" in line

    def test_a_known_qq_number_in_a_file_name_is_anonymized(self, runtime) -> None:
        alias = runtime._anonymize_user_id("1234567890")
        _upload(runtime, {"name": "1234567890.txt", "url": "http://x/a.txt"})

        line = _history(runtime)[0]

        assert "1234567890" not in line
        assert alias in line

    def test_a_long_file_name_is_compacted(self, runtime) -> None:
        set_live_config(runtime, history_text_limit=50)
        _upload(runtime, {"name": "长" * 100 + ".txt", "url": "http://x/a.txt"})

        line = _history(runtime)[0]
        body = line.split(": ", 1)[1]

        # _safe_attachment_name 放行 120 字符，不压缩的话 notice 行会比任何消息行都长。
        assert len(body) == runtime.live.history_text_limit + 1
        assert body.endswith("…")


class TestBothCachesGetTheLine:
    def test_a_group_upload_writes_both_caches(self, runtime) -> None:
        _upload(runtime, {"name": "报告.pdf", "url": "http://x/报告.pdf"})

        records = list(runtime._group_content_records[_GROUP_ID])

        assert len(_history(runtime)) == 1
        assert len(records) == 1
        assert records[0].formatted_line == _history(runtime)[0]
        assert records[0].timestamp == float(_EVENT_TIME)

    def test_a_bot_reply_writes_both_caches(self, runtime) -> None:
        runtime._record_bot_reply(_GROUP_ID, "好的")

        records = list(runtime._group_content_records[_GROUP_ID])

        assert len(records) == 1
        assert records[0].sender_name == "Bot"
        assert records[0].formatted_line == _history(runtime)[0]
        assert _history(runtime)[0].endswith("] Bot: 好的")

    def test_a_local_command_reply_writes_both_caches(self, runtime, local_command) -> None:
        local_command("/绘图帮助")

        records = list(runtime._group_content_records[_GROUP_ID])

        assert [record.sender_name for record in records][-1] == "Bot"
        assert [record.formatted_line for record in records] == _history(runtime)

    def test_attachments_are_copied_not_shared(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        shared = [{"path": "out/a.txt", "name": "a.txt"}]
        monkeypatch.setattr(
            runtime, "_file_sources_to_attachments",
            lambda sources, conversation_key: _immediately(shared),
            raising=True,
        )
        _upload(runtime, {"name": "a.txt", "url": "http://x/a.txt"})

        shared[0]["path"] = "out/tampered.txt"
        records = list(runtime._group_content_records[_GROUP_ID])

        assert records[0].attachments == [{"path": "out/a.txt", "name": "a.txt"}]


class TestLocalCommandsStayInHistory:
    """本地命令不经 engine，而触发它的那条消息已经把 p99 历史 matcher block 掉了。"""

    def test_a_command_and_its_reply_both_enter_history(self, runtime, local_command) -> None:
        matcher = local_command("/绘图帮助")

        lines = _history(runtime)

        assert "生图命令" in matcher.sent[0]
        assert len(lines) == 2
        assert lines[0].endswith(": /绘图帮助")
        assert "] Bot: " in lines[1]

    def test_a_refused_command_is_recorded_too(self, runtime, local_command) -> None:
        # 拒绝也是一次公开交互：群里看得见，历史里也该看得见。
        matcher = local_command("/切换模型 gpt-4o")

        lines = _history(runtime)

        assert "管理员" in matcher.sent[0]
        assert lines[0].endswith(": /切换模型 gpt-4o")
        assert lines[1].endswith(f"] Bot: {matcher.sent[0]}")

    def test_no_command_branch_finishes_without_recording(self) -> None:
        """新分支直接 matcher.finish(reply) 就又把这段交互从群历史里抹掉了。"""
        import ast

        tree = ast.parse((_ROOT / "adapters/onebot/__init__.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_group_message"
        )
        bare = [
            ast.unparse(node) for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "matcher.finish"
            and node.args
            and ast.unparse(node.args[0]).endswith("_reply")
        ]

        assert bare == []

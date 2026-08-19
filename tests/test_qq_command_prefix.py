"""命令一律要 / 开头，裸词回归成普通聊天。

裸词时代「画图软件推荐哪个」会被当绘图请求送进绘图节点烧钱，「可用表情」会被当管理命令，
私聊一句「ok」能批掉一条待审批。这份用例钉住新契约的两侧：带斜杠必中、不带必不中。
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

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

ADMIN_QQ = 10001
MEMBER_QQ = 10002
HOME_GROUP = 700001


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(
        module,
        admin_users=frozenset({ADMIN_QQ}),
        allowed_groups=frozenset({HOME_GROUP}),
    )
    return module


def in_group(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, group_id=HOME_GROUP, message_id=1, sender=SimpleNamespace(role="member"),
    )


# 每族挑一条代表，覆盖四种尾参形态：无参、可选数字、单参、吞尾。
_COMMANDS = [
    ("_CUSTOM_FACE_LIST_RE", "表情列表"),
    ("_CUSTOM_FACE_LIST_RE", "可用表情"),
    ("_CUSTOM_FACE_SYNC_RE", "同步表情列表"),
    ("_CUSTOM_FACE_ADD_RE", "收藏表情 开心"),
    ("_CUSTOM_FACE_RENAME_RE", "命名表情 3 开心"),
    ("_CUSTOM_FACE_DELETE_RE", "删除表情 开心"),
    ("_DRAW_DIRECT_RE", "生图 一只猫"),
    ("_DRAW_DIRECT_RE", "画图 一只猫"),
    ("_DRAW_HELP_RE", "生图帮助"),
    ("_DRAW_PRESET_LIST_RE", "画师串列表"),
    ("_DRAW_PRESET_SWITCH_RE", "切换画师串 可爱风"),
    ("_MODEL_SHOW_RE", "当前模型"),
    ("_MODEL_SHOW_RE", "model"),
    ("_MODEL_SWITCH_RE", "切换模型 gpt-4o"),
    ("_CLEAR_GROUP_MEMORY_RE", "清除群记忆"),
    ("_DEAD_LETTER_RE", "发送死信"),
    ("_HELP_RE", "帮助"),
]


class TestSlashIsRequired:
    @pytest.mark.parametrize("attr,body", _COMMANDS)
    def test_the_slash_form_matches(self, runtime: Any, attr: str, body: str) -> None:
        assert getattr(runtime, attr).match(f"/{body}") is not None

    @pytest.mark.parametrize("attr,body", _COMMANDS)
    def test_the_fullwidth_slash_matches_too(self, runtime: Any, attr: str, body: str) -> None:
        # 中文输入法打出来的是另一个码位，用户看不出区别。
        assert getattr(runtime, attr).match(f"／{body}") is not None

    @pytest.mark.parametrize("attr,body", _COMMANDS)
    def test_the_bare_word_is_ordinary_chat(self, runtime: Any, attr: str, body: str) -> None:
        assert getattr(runtime, attr).match(body) is None

    @pytest.mark.parametrize("attr,body", _COMMANDS)
    def test_the_bang_prefix_is_not_a_command(self, runtime: Any, attr: str, body: str) -> None:
        # ! 仍然可以是触发前缀，但剥掉之后剩的是裸词，不再是命令。
        assert getattr(runtime, attr).match(f"!{body}") is None


class TestEverydayPhrasesSurvive:
    """这些句子在裸词时代会被命令截胡。"""

    @pytest.mark.parametrize("text", [
        "画图软件推荐哪个",
        "绘图板买哪个好",
        "可用表情有哪些呀",
        "model",
        "帮助一下我",
        "表情列表在哪看",
    ])
    def test_no_command_claims_it(self, runtime: Any, text: str) -> None:
        for attr, _body in _COMMANDS:
            assert getattr(runtime, attr).match(text) is None, attr


class TestPathsAreNotCommands:
    """开了前缀触发信号之后，群里贴路径同样会走到命令链上。"""

    @pytest.mark.parametrize("text", [
        "/var/log/nginx/error.log",
        "/opt/kazebot/data",
        "/www/wwwroot/site/index.html",
    ])
    def test_a_path_is_left_to_the_model(self, runtime: Any, text: str) -> None:
        assert runtime._strip_command_prefix(text) == ""
        assert runtime._parse_proactive_command(text) is None

    def test_a_single_segment_still_parses(self, runtime: Any) -> None:
        assert runtime._strip_command_prefix("/群发 项目组 你好") == "群发 项目组 你好"

    def test_an_argument_may_contain_slashes(self, runtime: Any) -> None:
        # 只看首段：/发文件 的路径参数里当然有斜杠。
        parsed = runtime._parse_proactive_command("/发文件 群 项目组 data/attachments/x.zip")
        assert parsed is not None
        assert parsed["action"] == "file"
        assert parsed["body"] == "data/attachments/x.zip"


class TestForwardAliasesShareOneBranch:
    """六个中文写法加英文 forward 以前是四份逐字相同的分支。"""

    @pytest.mark.parametrize("head", [
        "合并转发", "转发", "转发到", "转发给", "合并转发到", "合并转发给", "forward",
    ])
    def test_every_alias_lands_on_forward(self, runtime: Any, head: str) -> None:
        parsed = runtime._parse_proactive_command(f"/{head} 群 项目组 内容")
        assert parsed is not None
        assert parsed["action"] == "forward"
        assert parsed["target_type"] == "group"
        assert parsed["target_ref"] == "项目组"

    def test_send_english_alias_matches_the_chinese_one(self, runtime: Any) -> None:
        english = runtime._parse_proactive_command("/send 群 项目组 内容")
        chinese = runtime._parse_proactive_command("/发送 群 项目组 内容")
        assert english == chinese


class TestHelpListsOnlyWhatYouCanRun:
    def test_an_admin_sees_the_gated_entries(self, runtime: Any) -> None:
        text = asyncio.run(runtime._maybe_handle_help_command(
            event=in_group(ADMIN_QQ), user_text="/帮助",
        ))
        assert text is not None
        assert "/切换模型" in text
        assert "/清除群记忆" in text

    def test_a_member_sees_only_the_open_ones(self, runtime: Any) -> None:
        text = asyncio.run(runtime._maybe_handle_help_command(
            event=in_group(MEMBER_QQ), user_text="/帮助",
        ))
        assert text is not None
        assert "/生图" in text
        assert "/切换模型" not in text
        assert "/清除群记忆" not in text

    def test_a_bare_word_is_not_the_help_command(self, runtime: Any) -> None:
        assert asyncio.run(runtime._maybe_handle_help_command(
            event=in_group(ADMIN_QQ), user_text="帮助",
        )) is None

    def test_every_catalog_entry_is_written_with_a_slash(self, runtime: Any) -> None:
        for _cap, usage, _desc in runtime._COMMAND_CATALOG:
            assert usage.startswith("/"), usage

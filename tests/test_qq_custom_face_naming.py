"""收藏表情详情列表的展示序号必须等于命名/删除定位用的真实位置。

同名收藏表情允许共存；一旦展示端去重或重新编号，管理员按屏幕序号命名就会改错另一条。
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
HOME_GROUP = 700001

# 两张同名「开心」（md5 各异）+ 一张未命名，钉住去重导致的序号错位。
FACES: list[dict[str, Any]] = [
    {"desc": "开心", "md5": "aaaaaaaa1111", "resId": "r1", "url": "http://x/1"},
    {"desc": "开心", "md5": "bbbbbbbb2222", "resId": "r2", "url": "http://x/2"},
    {"md5": "cccccccc3333", "resId": "r3", "url": "http://x/3"},
]


class FaceBot:
    def __init__(self, faces: list[Any]) -> None:
        self.self_id = "10000"
        self._faces = faces
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        if api == "fetch_custom_face_detail":
            return self._faces
        raise AssertionError(f"unexpected api: {api}")


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


def custom_face(runtime: Any, bot: Any, event: Any, text: str) -> str | None:
    return asyncio.run(runtime._maybe_handle_custom_face_command(
        bot=bot, event=event, user_text=text, conversation_key="k", current_attachments=[],
    ))


def test_details_keep_every_face_at_its_real_index(runtime: Any) -> None:
    bot = FaceBot(FACES)

    items = asyncio.run(runtime.list_custom_face_details(bot, []))

    assert len(items) == 3
    assert [it["index"] for it in items] == [1, 2, 3]


def test_index_round_trips_to_the_same_face(runtime: Any) -> None:
    bot = FaceBot(FACES)

    items = asyncio.run(runtime.list_custom_face_details(bot, []))
    for item in items:
        resolved = asyncio.run(runtime.resolve_custom_face(bot, str(item["index"]), []))
        assert resolved is FACES[item["index"] - 1]
        # 屏幕序号必须解析回它标注的那张脸；md5 是稳定身份，去重错位会让二者不一致。
        assert isinstance(resolved, dict) and resolved.get("md5") == item["md5"]


def test_duplicate_names_carry_md5_and_unnamed_does_not(runtime: Any) -> None:
    bot = FaceBot(FACES)

    items = asyncio.run(runtime.list_custom_face_details(bot, []))
    dups = runtime.duplicated_detail_names(items)
    lines = [runtime.format_custom_face_detail_line(it, dups) for it in items]

    assert "md5:" in lines[0] and "md5:" in lines[1]
    assert "md5:" not in lines[2]


def test_detail_command_prints_true_index(runtime: Any) -> None:
    bot = FaceBot(FACES)

    reply = custom_face(runtime, bot, in_group(ADMIN_QQ), "/表情详情列表")

    assert reply is not None
    assert "3. 未命名#3" in reply
    assert "2. 未命名#3" not in reply


def test_name_list_command_has_no_locator_numbers(runtime: Any) -> None:
    bot = FaceBot(FACES)
    runtime.write_custom_face_names(runtime.CUSTOM_FACE_NAMES_PATH, ["开心", "点赞", "微笑"])

    reply = custom_face(runtime, bot, in_group(ADMIN_QQ), "/表情列表")

    assert reply is not None
    for name in ("开心", "点赞", "微笑"):
        assert f"・{name}" in reply
    assert "1. " not in reply

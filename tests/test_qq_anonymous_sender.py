"""#10 群匿名成员不再折叠成一个别名。

匿名占位号 80000000 全群共用，登记会把所有匿名成员当成一个真实用户、落盘、@ 出去、
共享记忆档案、跨匿名成员取图。改为进程内 Anon* 命名空间 + 占位号中央闸门，匿名一律
fail-closed：不建档、不给管理员能力、不点赞、不 @。
"""
from __future__ import annotations

import re
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

HOME_GROUP = 700001
MEMBER_QQ = 30003
PLACEHOLDER_QQ = 80000000


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return load_runtime(monkeypatch, tmp_path)


def anon_event(anon_id: Any, *, name: str = "匿名甲", flag: str = "f1", group_id: int = HOME_GROUP) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=PLACEHOLDER_QQ, group_id=group_id, message_id=1, time=1_700_000_000,
        sender=SimpleNamespace(card="", nickname="", role="member", user_id=PLACEHOLDER_QQ),
        anonymous=SimpleNamespace(id=anon_id, name=name, flag=flag),
        get_message=lambda: [],
    )


def real_event(*, user_id: int = MEMBER_QQ, group_id: int = HOME_GROUP, card: str = "张三") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, group_id=group_id, message_id=2, time=1_700_000_000,
        sender=SimpleNamespace(card=card, nickname="", role="member", user_id=user_id),
        get_message=lambda: [],
    )


def test_distinct_anonymous_members_get_distinct_aliases(runtime: Any) -> None:
    a1 = runtime._event_user_alias(anon_event(11))
    a2 = runtime._event_user_alias(anon_event(22, name="匿名乙", flag="f2"))

    assert a1 != a2
    assert a1.startswith("Anon") and a2.startswith("Anon")


def test_same_anonymous_id_is_stable(runtime: Any) -> None:
    assert runtime._event_user_alias(anon_event(11)) == runtime._event_user_alias(anon_event(11))


def test_placeholder_never_registers_as_real_user(runtime: Any) -> None:
    runtime._event_user_alias(anon_event(11))

    assert "80000000" not in runtime._anon_users
    assert runtime._anonymize_user_id("80000000") == "AnonUnknown"


def test_history_line_keeps_anonymous_members_distinct(runtime: Any) -> None:
    bot = SimpleNamespace(self_id="90000")
    line1 = runtime._format_history_line(anon_event(11), bot, override_text="a")
    line2 = runtime._format_history_line(anon_event(22, flag="f2"), bot, override_text="b")

    assert "匿名成员" in line1 and "匿名成员" in line2
    ident1 = line1[line1.index("(") + 1:line1.index(")")]
    ident2 = line2[line2.index("(") + 1:line2.index(")")]
    assert ident1.startswith("Anon")
    assert ident1 != ident2


def test_anonymous_sender_is_never_enrolled(runtime: Any, tmp_path: Path) -> None:
    from engine import memory_subjects

    runtime._record_memory_subject_interaction(anon_event(11), True)

    assert memory_subjects.enrolled_subjects(tmp_path) == set()


def test_target_omits_reply_sender_for_anonymous(runtime: Any) -> None:
    anon_target = runtime._target_from_platform_data({
        "type": "group", "group_id": HOME_GROUP, "event": anon_event(11), "conversation_key": "k",
    })
    real_target = runtime._target_from_platform_data({
        "type": "group", "group_id": HOME_GROUP, "event": real_event(), "conversation_key": "k",
    })

    assert "reply_sender_id" not in anon_target
    assert "reply_sender_id" in real_target


def test_anonymous_never_gets_admin_even_when_placeholder_is_listed(runtime: Any) -> None:
    set_live_config(runtime, admin_users=frozenset({PLACEHOLDER_QQ}))

    assert runtime._requester(anon_event(11)).listed_admin is False


def test_real_sender_regression(runtime: Any) -> None:
    event = real_event(card="张三")

    assert re.match(r"^User[A-Z]+$", runtime._event_user_alias(event))
    assert runtime._event_display_name(event) == "张三"


def test_sender_key_distinguishes_anonymous_members(runtime: Any) -> None:
    assert runtime._event_sender_key(anon_event(11)) != runtime._event_sender_key(anon_event(22, flag="f2"))

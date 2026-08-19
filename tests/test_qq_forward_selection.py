"""qq_forward 挑选改用稳定 ref 后的行为。

原始漏洞：list 与 pick 各自在 deque 上算位置下标，deque 满后每来一条新消息位置整体
左移，AI 挑中的就变成错位的另一条消息（然后被转发给第三方）；ref 全落空还会回退到最近
10 条 / 最新一张，把没人要的内容发出去。最近发送附件通路同源。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_SESSION = "sess-1"
_GROUP_ID = 998877
_ADMIN_QQ = 10001


class _Bot:
    self_id = "42"

    def __init__(self) -> None:
        self.group_msgs = 0
        self.private_msgs = 0
        self.api_calls: list[str] = []

    async def send_group_msg(self, **kwargs: Any) -> dict[str, Any]:
        self.group_msgs += 1
        return {"message_id": self.group_msgs}

    async def send_private_msg(self, **kwargs: Any) -> dict[str, Any]:
        self.private_msgs += 1
        return {"message_id": 1000 + self.private_msgs}

    async def call_api(self, api: str, **kwargs: Any) -> dict[str, Any]:
        self.api_calls.append(api)
        return {"message_id": 2000 + len(self.api_calls)}

    @property
    def sends(self) -> int:
        return self.group_msgs + self.private_msgs + len(self.api_calls)


def _register(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "group", "group_id": _GROUP_ID, "user_id": _ADMIN_QQ},
    )


def _push(runtime, text: str, group_id: int = _GROUP_ID) -> int:
    """走单一写入点记一条群消息，返回它的 seq。"""
    return runtime._append_group_record(
        group_id,
        f"[00:00] jia({group_id}): {text}",
        text=text,
        sender_name="jia",
        sender_id="1001",
        timestamp=1_700_000_000.0,
    )


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime = load_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))
    _register(runtime, monkeypatch)

    async def resolve(*args: Any, **kwargs: Any):
        return runtime.ProactiveTarget("group", _GROUP_ID, "当前群"), ""

    async def emojis(text: str, *args: Any, **kwargs: Any):
        return [runtime.MessageSegment.text(text)]

    monkeypatch.setattr(runtime, "_forward_bridge_resolve_target", resolve)
    monkeypatch.setattr(runtime, "process_emojis", emojis)
    return runtime, bot


def _run(runtime, payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("session_id", _SESSION)
    return asyncio.run(runtime._forward_bridge_execute(payload))


def _mkfile(tmp_path: Path, rel: str) -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return str(path)


def test_a_ref_still_points_at_the_same_message_after_the_deque_overflows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    _register(runtime, monkeypatch)
    for i in range(1, 21):
        _push(runtime, f"msg{i}")

    listed = runtime._forward_bridge_list_messages(_SESSION)
    ref = next(item["ref"] for item in listed if "msg20" in item["preview"])

    # 灌满并整体左移：content_records maxlen=20，再来 5 条 → 位置下标全变。
    for i in range(21, 26):
        _push(runtime, f"msg{i}")

    selection = runtime._forward_bridge_pick_records(
        _SESSION, refs=runtime._forward_bridge_parse_record_refs([ref]), query="",
    )

    assert [r.text for r in selection.records] == ["msg20"]


def test_expired_refs_are_refused_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime, bot = _harness(monkeypatch, tmp_path)
    # content_records maxlen=20：灌 21 条把 seq=1 挤出去，但缓存里仍有 20 条可发。
    for i in range(1, 22):
        _push(runtime, f"msg{i}")

    result = _run(runtime, {
        "action": "send", "target_type": "current", "message_refs": ["m1"],
    })

    assert result["ok"] is False
    assert "缓存" in result["error"]
    assert bot.sends == 0


def test_legacy_message_indices_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime, bot = _harness(monkeypatch, tmp_path)
    for i in range(1, 4):
        _push(runtime, f"msg{i}")

    by_msg = _run(runtime, {
        "action": "send", "target_type": "current", "message_indices": [1],
    })
    assert by_msg["ok"] is False
    assert "message_indices" in by_msg["error"]

    by_recent = _run(runtime, {
        "action": "send", "target_type": "current", "recent_indices": [1],
    })
    assert by_recent["ok"] is False
    assert "recent_indices" in by_recent["error"]

    assert bot.sends == 0


def test_partial_miss_is_reported_but_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime, bot = _harness(monkeypatch, tmp_path)
    for i in range(1, 4):
        _push(runtime, f"msg{i}")

    result = _run(runtime, {
        "action": "send", "target_type": "current", "message_refs": ["m2", "m999"],
    })

    assert result["ok"] is True
    assert "1 条消息" in result["result"]
    assert "过期" in result["result"]
    assert bot.sends >= 1


def test_recent_attachment_refs_survive_new_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    _register(runtime, monkeypatch)
    target = {"type": "group", "group_id": _GROUP_ID}
    a1 = _mkfile(tmp_path, "data/attachments/a1.png")
    a2 = _mkfile(tmp_path, "data/attachments/a2.png")
    filler = _mkfile(tmp_path, "data/attachments/filler.png")
    runtime._record_sent_attachments(target, [{"path": a1, "name": "a1.png"}])
    runtime._record_sent_attachments(target, [{"path": a2, "name": "a2.png"}])

    listed = runtime._forward_bridge_list_recent(_SESSION)
    assert [item["ref"] for item in listed] == ["a1", "a2"]

    # 同 bucket 内新老 ref 各命中正确文件。
    picked_a1 = runtime._forward_bridge_pick_recent_attachments(
        _SESSION, refs=runtime._forward_bridge_parse_attachment_refs(["a1"]),
    )
    assert [Path(p["path"]).name for p in picked_a1] == ["a1.png"]

    # 再记 20 张把 bucket（maxlen=20）挤满，a1/a2 被淘汰。
    for _ in range(20):
        runtime._record_sent_attachments(target, [{"path": filler, "name": "filler.png"}])

    gone = runtime._forward_bridge_pick_recent_attachments(
        _SESSION, refs=runtime._forward_bridge_parse_attachment_refs(["a1"]),
    )
    assert gone == []  # 过期 ref 不得回退成最新那张

    survivors = [item["ref"] for item in runtime._forward_bridge_list_recent(_SESSION)]
    latest = runtime._forward_bridge_pick_recent_attachments(
        _SESSION, refs=runtime._forward_bridge_parse_attachment_refs([survivors[-1]]),
    )
    assert [Path(p["path"]).name for p in latest] == ["filler.png"]

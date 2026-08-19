"""qq_forward Bridge 端到端鉴权测试。

覆盖“拒绝时一条消息都没发出去”，这是 forward_authz 的纯逻辑测试无法证明的部分。
原始漏洞：任意白名单群成员都能让 AI 用 op=file 把 data/config.yaml、
data/.admin_token、data/onebot_anon_map.json 私发给自己，或借 Bot 身份群发。
"""
from __future__ import annotations

import asyncio
import json
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
_ADMIN_QQ = 10001
_MEMBER_QQ = 30003
_HOME_GROUP = 99
_OTHER_GROUP = 77


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


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requester: int,
    origin: dict[str, Any] | None = None,
    resolved: tuple[str, int] = ("group", _HOME_GROUP),
):
    """加载插件并把会话登记成 requester 在 _HOME_GROUP 群里发起的请求。"""
    runtime = load_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))

    if origin is None:
        origin = {"type": "group", "group_id": _HOME_GROUP, "user_id": requester}
    if origin:
        monkeypatch.setitem(runtime._session_targets, _SESSION, origin)

    async def resolve(*args: Any, **kwargs: Any):
        return runtime.ProactiveTarget(resolved[0], resolved[1], "Target"), ""

    async def emojis(text: str, *args: Any, **kwargs: Any):
        return [runtime.MessageSegment.text(text)]

    monkeypatch.setattr(runtime, "_forward_bridge_resolve_target", resolve)
    monkeypatch.setattr(runtime, "process_emojis", emojis)
    return runtime, bot


def _run(runtime, payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("session_id", _SESSION)
    return asyncio.run(runtime._forward_bridge_execute(payload))


# --- 跨会话投递 ---


def test_member_remind_to_own_group_is_delivered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_MEMBER_QQ)

    result = _run(runtime, {"action": "remind", "target_type": "current", "text": "开会了"})

    assert result["ok"] is True
    assert bot.sends >= 1


def test_member_remind_to_other_group_sends_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_MEMBER_QQ, resolved=("group", _OTHER_GROUP),
    )

    result = _run(runtime, {"action": "remind", "target_type": "group", "target_ref": "别的群", "text": "hi"})

    assert result["ok"] is False
    assert "管理员" in result["error"]
    assert bot.sends == 0


def test_member_remind_to_other_user_sends_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_MEMBER_QQ, resolved=("private", 55555),
    )

    result = _run(runtime, {"action": "remind", "target_type": "private", "target_ref": "路人", "text": "hi"})

    assert result["ok"] is False
    assert bot.sends == 0


def test_member_can_remind_self(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_MEMBER_QQ, resolved=("private", _MEMBER_QQ),
    )

    result = _run(runtime, {"action": "remind", "target_type": "self", "text": "记得交作业"})

    assert result["ok"] is True
    assert bot.private_msgs == 1


def test_admin_remind_to_other_group_is_delivered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_ADMIN_QQ, resolved=("group", _OTHER_GROUP),
    )

    result = _run(runtime, {"action": "remind", "target_type": "group", "target_ref": "别的群", "text": "hi"})

    assert result["ok"] is True
    assert bot.group_msgs == 1


def test_unregistered_session_is_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """反查不到发起人时必须 fail-closed，不能默认放行。"""
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_ADMIN_QQ, origin={})

    result = _run(runtime, {"action": "remind", "target_type": "current", "text": "hi"})

    assert result["ok"] is False
    assert bot.sends == 0


def test_group_origin_without_user_id_is_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """本次修复前登记的群会话没有 user_id，旧持久化数据不能被当成管理员。"""
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_ADMIN_QQ,
        origin={"type": "group", "group_id": _HOME_GROUP},
        resolved=("group", _OTHER_GROUP),
    )

    result = _run(runtime, {"action": "remind", "target_type": "group", "target_ref": "别的群", "text": "hi"})

    assert result["ok"] is False
    assert bot.sends == 0


def test_payload_cannot_claim_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """payload 全程可被提示词影响，自称 is_admin 不能生效。"""
    runtime, bot = _harness(
        monkeypatch, tmp_path, requester=_MEMBER_QQ, resolved=("group", _OTHER_GROUP),
    )

    result = _run(runtime, {
        "action": "remind", "target_type": "group", "target_ref": "别的群", "text": "hi",
        "is_admin": True,
        "platform_auth": {"is_admin": True, "user_id": str(_ADMIN_QQ)},
    })

    assert result["ok"] is False
    assert bot.sends == 0


# --- op=file 显式路径 ---


def _workspace_file(tmp_path: Path, rel: str, body: str = "x") -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return rel


def test_member_cannot_send_workspace_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rel = _workspace_file(tmp_path, "README.md", "# hi")
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_MEMBER_QQ)

    result = _run(runtime, {"action": "file", "target_type": "current", "file_paths": [rel]})

    assert result["ok"] is False
    assert bot.sends == 0


def test_admin_can_send_ordinary_workspace_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rel = _workspace_file(tmp_path, "README.md", "# hi")
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_ADMIN_QQ)

    result = _run(runtime, {"action": "file", "target_type": "current", "file_paths": [rel]})

    assert result["ok"] is True
    assert bot.sends >= 1


@pytest.mark.parametrize(
    "rel",
    [
        "data/config.yaml", "data/.admin_token", ".env",
        "data/onebot_anon_map.json", "data/onebot_plugin_state.json",
        "data/cache/onebot_reply_attachments.json",
        "config/qq.yaml", "data/qq_live_state.json",
    ],
)
def test_secret_files_are_denied_even_for_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str,
) -> None:
    _workspace_file(tmp_path, rel, "api_key: sk-live-do-not-leak")
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_ADMIN_QQ)

    result = _run(runtime, {"action": "file", "target_type": "current", "file_paths": [rel]})

    assert result["ok"] is False
    assert bot.sends == 0
    assert "sk-live-do-not-leak" not in str(result)


def test_secret_denial_does_not_block_sibling_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """一次请求混着敏感与普通文件时，普通文件照常发出、敏感文件单独回报。"""
    secret = _workspace_file(tmp_path, "data/config.yaml", "api_key: sk-live")
    plain = _workspace_file(tmp_path, "data/attachments/report.md", "# report")
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_ADMIN_QQ)

    result = _run(runtime, {"action": "file", "target_type": "current", "file_paths": [secret, plain]})

    assert result["ok"] is True
    assert "config.yaml" in result["result"]
    assert "report.md" in result["result"]


def test_member_can_send_recent_generated_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """use_recent 只发 Bot 刚生成的附件，不是读取工作区，普通成员应可用。"""
    rel = _workspace_file(tmp_path, "data/attachments/naidiff_1.png", "fake-png")
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_MEMBER_QQ)
    monkeypatch.setattr(
        runtime, "_forward_bridge_pick_recent_attachments",
        lambda *a, **k: [{"type": "image", "path": rel, "name": "naidiff_1.png"}],
    )

    result = _run(runtime, {"action": "file", "target_type": "current", "use_recent": True})

    assert result["ok"] is True
    assert bot.sends >= 1


# --- Bridge 令牌鉴权（#46）与读接口鉴权（#47）---


class _Request:
    def __init__(self, payload: dict[str, Any], headers: dict[str, str] | None = None, remote: str = "127.0.0.1"):
        self._payload = payload
        self.headers = headers or {}
        self.remote = remote

    async def json(self) -> dict[str, Any]:
        return dict(self._payload)


def _bridge_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # env 令牌必须在 import 前清掉，否则 config 会把它固化成常量，令牌来源测不准。
    monkeypatch.delenv("ONEBOT_FORWARD_BRIDGE_TOKEN", raising=False)
    runtime = load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "get_bot", lambda: _Bot())
    return runtime


def _handle(runtime, payload: dict[str, Any], *, token: Any = "__signed__", headers: dict[str, str] | None = None):
    body = dict(payload)
    body.setdefault("session_id", _SESSION)
    hdrs = dict(headers or {})
    if token == "__signed__":
        token = runtime._forward_bridge_token()
    if token is not None:
        hdrs["X-Forward-Token"] = token
    return asyncio.run(runtime._forward_bridge_http_handler(_Request(body, hdrs)))


def _register_group(runtime, monkeypatch: pytest.MonkeyPatch, *, group_id: int = _HOME_GROUP, user_id: int = _MEMBER_QQ) -> None:
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "group", "group_id": group_id, "user_id": user_id},
    )


def _push_record(runtime, group_id: int, text: str) -> int:
    return runtime._append_group_record(
        group_id,
        f"[00:00] jia({group_id}): {text}",
        text=text, sender_name="jia", sender_id="1001", timestamp=1_700_000_000.0,
    )


def test_a_request_without_a_token_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 登记 + 白名单齐全，读闸门本会放行 —— 403 只能来自缺失的令牌。
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    _register_group(runtime, monkeypatch)
    set_live_config(runtime, allowed_groups=frozenset({_HOME_GROUP}))

    resp = _handle(runtime, {"op": "list"}, token=None)

    assert resp.status == 403


def test_a_wrong_token_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    _register_group(runtime, monkeypatch)
    set_live_config(runtime, allowed_groups=frozenset({_HOME_GROUP}))

    resp = _handle(runtime, {"op": "list"}, token="not-the-token")

    assert resp.status == 403


def test_the_self_signed_token_lets_the_tool_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    _register_group(runtime, monkeypatch)
    set_live_config(runtime, allowed_groups=frozenset({_HOME_GROUP}))

    resp = _handle(runtime, {"op": "list"})

    assert resp.status == 200


def test_the_token_file_is_created_once_and_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)

    first = runtime._forward_bridge_token()
    assert first
    assert runtime._forward_bridge_token() == first
    token_file = Path(runtime.FORWARD_BRIDGE_TOKEN_FILE)
    assert token_file.read_text(encoding="utf-8").strip() == first

    runtime._forward_bridge_token_cache = ""
    assert runtime._forward_bridge_token() == first


def test_an_unavailable_token_file_keeps_the_bridge_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(runtime, "FORWARD_BRIDGE_TOKEN_FILE", str(blocker / "nested" / "token"))
    runtime._forward_bridge_token_cache = ""

    assert runtime._forward_bridge_token() == ""
    assert runtime._forward_bridge_check_token(_Request({}, {})) is False

    runtime.refresh_live_config()
    asyncio.run(runtime._start_forward_bridge())
    assert runtime._forward_bridge_started is False


def test_the_token_never_appears_in_runtime_facts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)

    token = runtime._forward_bridge_token()
    facts = runtime._live_runtime_facts()

    assert facts["forward_bridge_token_set"] is True
    assert token not in repr(facts)


def test_listing_an_unregistered_session_is_refused_not_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)

    resp = _handle(runtime, {"op": "list"})

    assert resp.status == 403
    assert json.loads(resp.text)["ok"] is False


def test_listing_a_group_removed_from_the_allowlist_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    _register_group(runtime, monkeypatch, group_id=_HOME_GROUP)
    _push_record(runtime, _HOME_GROUP, "secret-preview-text")
    set_live_config(runtime, allowed_groups=frozenset({_OTHER_GROUP}))

    resp = _handle(runtime, {"op": "list"})

    assert resp.status == 403
    assert "secret-preview-text" not in resp.text


def test_listing_an_allowed_group_still_returns_refs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    _register_group(runtime, monkeypatch, group_id=_HOME_GROUP)
    _push_record(runtime, _HOME_GROUP, "hello world")
    set_live_config(runtime, allowed_groups=frozenset({_HOME_GROUP}))

    resp = _handle(runtime, {"op": "list"})

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert body["messages"]


def test_recent_uses_the_same_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)

    resp = _handle(runtime, {"op": "recent"})
    assert resp.status == 403

    _register_group(runtime, monkeypatch, group_id=_HOME_GROUP)
    set_live_config(runtime, allowed_groups=frozenset({_OTHER_GROUP}))
    resp = _handle(runtime, {"op": "recent"})
    assert resp.status == 403


def test_a_private_origin_outside_the_allowlist_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _MEMBER_QQ},
    )
    set_live_config(
        runtime,
        admin_users=frozenset({_ADMIN_QQ}),
        allowed_private_users=frozenset(),
        allow_private_friends=False,
    )

    resp = _handle(runtime, {"op": "recent"})

    assert resp.status == 403


def test_self_send_is_refused_when_origin_left_the_private_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """写侧同款：被移出私聊白名单后不应还能让 bot 私发给自己（不 mock resolve）。"""
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _MEMBER_QQ},
    )
    set_live_config(
        runtime,
        admin_users=frozenset({_ADMIN_QQ}),
        allowed_private_users=frozenset(),
        allow_private_friends=False,
    )

    result = asyncio.run(runtime._forward_bridge_execute({
        "session_id": _SESSION, "action": "remind", "target_type": "self", "text": "hi",
    }))

    assert result["ok"] is False
    assert "允许列表" in result["error"]
    assert bot.sends == 0


def test_the_friends_switch_does_not_open_the_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """好友总开关只放宽入站。出站没有 sub_type 可查，拿它顶替等于对任何人放行。"""
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _MEMBER_QQ},
    )
    set_live_config(
        runtime,
        admin_users=frozenset({_ADMIN_QQ}),
        allowed_private_users=frozenset(),
        allow_private_friends=True,
    )

    assert _handle(runtime, {"op": "recent"}).status == 403


def test_self_send_needs_the_allowlist_not_just_the_friends_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _MEMBER_QQ},
    )
    set_live_config(
        runtime,
        admin_users=frozenset({_ADMIN_QQ}),
        allowed_private_users=frozenset(),
        allow_private_friends=True,
    )

    result = asyncio.run(runtime._forward_bridge_execute({
        "session_id": _SESSION, "action": "remind", "target_type": "self", "text": "hi",
    }))

    assert result["ok"] is False
    assert bot.sends == 0


def test_the_allowlist_still_lets_its_own_members_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """收紧的是好友那一档，名单本身不能跟着失效。"""
    runtime = _bridge_runtime(monkeypatch, tmp_path)
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _MEMBER_QQ},
    )
    set_live_config(
        runtime,
        admin_users=frozenset(),
        allowed_private_users=frozenset({_MEMBER_QQ}),
        allow_private_friends=False,
    )

    assert _handle(runtime, {"op": "recent"}).status != 403


# --- 目标解析不是探测器（#44）---


def _two_contacts_named(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个联系人都能用「老板」叫到，只有 label（张总/李总）会泄露真实身份。"""
    monkeypatch.setitem(runtime._QQ_USER_PROFILES, "70001", {"address_as": "老板"})
    monkeypatch.setitem(runtime._QQ_USER_PROFILES, "70002", {"address_as": "老板"})

    async def candidates(_bot: Any) -> list[Any]:
        return [
            runtime.ProactiveTarget("private", 70001, "张总"),
            runtime.ProactiveTarget("private", 70002, "李总"),
        ]

    monkeypatch.setattr(runtime, "_private_target_candidates", candidates)


def test_a_member_is_not_shown_which_contacts_matched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "group", "group_id": _HOME_GROUP, "user_id": _MEMBER_QQ},
    )
    _two_contacts_named(runtime, monkeypatch)

    target, err = asyncio.run(runtime._forward_bridge_resolve_target(bot, _SESSION, "private", "老板"))

    assert target is None
    assert "张总" not in err and "李总" not in err


def test_the_roster_still_sees_the_collision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    bot = _Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {"type": "private", "user_id": _ADMIN_QQ},
    )
    _two_contacts_named(runtime, monkeypatch)

    target, err = asyncio.run(runtime._forward_bridge_resolve_target(bot, _SESSION, "private", "老板"))

    assert target is None
    assert "不唯一" in err

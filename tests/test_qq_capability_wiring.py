"""能力档位在真实命令路径上的接线。

判定逻辑本身在 test_qq_capability 里钉住；这里验的是八个门有没有接对同一份判定，
以及群主拿到能力之后够不着别的群。
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

LISTED_QQ = 10001
OWNER_QQ = 20002
GROUP_ADMIN_QQ = 20003
MEMBER_QQ = 30003
HOME_GROUP = 700001
OTHER_GROUP = 700002


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(
        module,
        admin_users=frozenset({LISTED_QQ}),
        allowed_groups=frozenset({HOME_GROUP, OTHER_GROUP}),
    )
    return module


def in_group(user_id: int, *, group_id: int = HOME_GROUP, role: str = "member") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, group_id=group_id, message_id=1, sender=SimpleNamespace(role=role),
    )


def in_private(user_id: int, *, role: str = "") -> SimpleNamespace:
    # 私聊事件没有 group_id。带上 sender.role 是为了证明它够不着判定。
    return SimpleNamespace(user_id=user_id, message_id=1, sender=SimpleNamespace(role=role))


def clear_memory(runtime: Any, event: Any, text: str) -> str | None:
    return asyncio.run(
        runtime._maybe_handle_clear_group_memory_command(bot=None, event=event, user_text=text)
    )


def switch_model(runtime: Any, event: Any) -> str | None:
    return asyncio.run(runtime._maybe_handle_model_command(event=event, user_text="/切换模型 gpt-4o"))


def dead_letter(runtime: Any, event: Any, text: str = "/发送死信") -> str | None:
    return asyncio.run(runtime._maybe_handle_dead_letter_command(event=event, user_text=text))


def dispatch_family(runtime: Any, family: str, event: Any, text: str) -> str | None:
    """把一条命令送进它所属的 handler。off 档下每个 handler 都该对非名单静默。"""
    if family == "draw_preset":
        return asyncio.run(runtime._maybe_handle_drawtools_command(event=event, user_text=text))
    if family == "model":
        return asyncio.run(runtime._maybe_handle_model_command(event=event, user_text=text))
    if family == "custom_face":
        return asyncio.run(runtime._maybe_handle_custom_face_command(
            bot=None, event=event, user_text=text, conversation_key="k", current_attachments=[],
        ))
    if family == "proactive":
        return asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=event, user_text=text, conversation_key="k", current_attachments=[],
        ))
    if family == "clear_memory":
        return asyncio.run(runtime._maybe_handle_clear_group_memory_command(
            bot=None, event=event, user_text=text,
        ))
    raise AssertionError(f"unknown family: {family}")


class TestScopedCapabilityReachesGroupRoles:
    def test_the_owner_can_clear_his_own_group_once_granted(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="owner")

        reply = clear_memory(runtime, in_group(OWNER_QQ, role="owner"), "/清除群记忆")

        assert reply is not None
        assert "本群" in reply or "当前群" in reply

    def test_the_owner_is_still_shut_out_by_default(self, runtime: Any) -> None:
        """默认档位就是升级前的行为：名单之外一个人都进不来。"""
        reply = clear_memory(runtime, in_group(OWNER_QQ, role="owner"), "/清除群记忆")

        assert reply == "该请求不可用。"

    def test_a_group_admin_is_below_the_owner_grant(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="owner")

        reply = clear_memory(runtime, in_group(GROUP_ADMIN_QQ, role="admin"), "/清除群记忆")

        assert reply == "该请求不可用。"

    def test_a_group_admin_gets_in_at_the_wider_grant(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="group_admin")

        reply = clear_memory(runtime, in_group(GROUP_ADMIN_QQ, role="admin"), "/清除群记忆")

        assert reply is not None
        assert reply != "该请求不可用。"

    def test_a_plain_member_never_gets_in(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="group_admin")

        reply = clear_memory(runtime, in_group(MEMBER_QQ, role="member"), "/清除群记忆")

        assert reply == "该请求不可用。"


class TestTheOwnerCannotReachPastHisGroup:
    def test_naming_another_group_is_denied(self, runtime: Any) -> None:
        """群主只要在自己群里带上别人的群号，就等于拿到了所有群 —— 这一层是关键。"""
        set_live_config(runtime, grant_clear_memory="owner")

        reply = clear_memory(runtime, in_group(OWNER_QQ, role="owner"), f"/清除群记忆 {OTHER_GROUP}")

        assert reply is not None
        assert "只在本群有效" in reply

    def test_naming_his_own_group_is_allowed(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="owner")

        reply = clear_memory(runtime, in_group(OWNER_QQ, role="owner"), f"/清除群记忆 {HOME_GROUP}")

        assert reply is not None
        assert "只在本群有效" not in reply

    def test_the_same_owner_gets_nothing_in_a_private_chat(self, runtime: Any) -> None:
        """私聊里没有「本群」。这里也是 sender.role 唯一可能被误读的地方。"""
        set_live_config(runtime, grant_clear_memory="owner")

        reply = clear_memory(runtime, in_private(OWNER_QQ, role="owner"), "/清除群记忆")

        assert reply == "该请求不可用。"

    def test_the_owner_is_never_handed_the_group_listing(self, runtime: Any) -> None:
        """那份清单里是 bot 待过的每一个群，本身就超出他该知道的范围。"""
        set_live_config(runtime, grant_clear_memory="group_admin")

        reply = clear_memory(runtime, in_group(OWNER_QQ, role="owner"), "/清除群记忆 不存在的群")

        assert reply is not None
        assert "可清理" not in reply

    def test_the_roster_still_reaches_every_group(self, runtime: Any) -> None:
        reply = clear_memory(runtime, in_group(LISTED_QQ), f"/清除群记忆 {OTHER_GROUP}")

        assert reply is not None
        assert "只在本群有效" not in reply

    def test_the_private_listing_goes_through_the_scope_rule(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """这道门必须是作用域判定的调用方，不能自己再抄一遍名单判断。"""
        monkeypatch.setattr(
            runtime.capability, "scope_deny_reason",
            lambda cap, requester, target: "SCOPE-STOP",
        )

        assert clear_memory(runtime, in_private(LISTED_QQ), "/清除群记忆") == "SCOPE-STOP"

    @pytest.mark.parametrize("grant", ["off", "admin", "owner", "group_admin"])
    @pytest.mark.parametrize("role", ["", "member", "admin", "owner"])
    def test_no_outsider_ever_sees_the_group_listing_in_private(
        self, runtime: Any, grant: str, role: str,
    ) -> None:
        """私聊里没有「本群」，任何档位都不该把 bot 待过的群名单给名单外的人。"""
        set_live_config(runtime, grant_clear_memory=grant)

        reply = clear_memory(runtime, in_private(OWNER_QQ, role=role), "/清除群记忆")

        assert "可清理群记忆的群" not in (reply or "")


class TestGlobalCapabilitiesStayWithTheRoster:
    def test_the_yaml_cannot_hand_a_global_capability_to_the_owner(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """手改 yaml 写 owner 会在读配置时被夹回 admin，判定端看到的已经是 admin。"""
        # 未初始化的提示排在鉴权之前，不接上就测不到门。
        monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)
        set_live_config(runtime, grant_model="owner")

        assert runtime.live.grant_model == "admin"
        assert switch_model(runtime, in_group(OWNER_QQ, role="owner")) == "模型命令仅限 Clonoth 管理员使用。"

    def test_the_roster_can_still_switch_models(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)

        reply = switch_model(runtime, in_private(LISTED_QQ))

        assert reply != "模型命令仅限 Clonoth 管理员使用。"

    @pytest.mark.parametrize(
        "key", ["approval", "model", "draw_preset", "custom_face", "cross_session", "send_dead_letter"],
    )
    def test_every_global_capability_clamps_the_same_way(self, runtime: Any, key: str) -> None:
        set_live_config(runtime, **{f"grant_{key}": "group_admin"})

        assert getattr(runtime.live, f"grant_{key}") == "admin"


class TestTheYamlIsReadTheWayItWasWritten:
    """控制台写出的是不带引号的 `off`（js-yaml 按 1.2 core schema 判定它不是布尔）。

    这条往返只在真实 yaml 文本上成立，Python 侧自己 dump 会加引号，测不到 —— 而这里
    错一次，界面显示「不开放」、bot 实际按默认档放行，正是这个控制台最该防的那种假象。
    """

    def _write(self, runtime: Any, body: str) -> None:
        live_config = sys.modules[f"{runtime.__name__}.live_config"]
        path = live_config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        live_config.invalidate()

    def test_a_bare_off_still_means_off(self, runtime: Any) -> None:
        self._write(runtime, "permissions:\n  capabilities:\n    draw_preset: off\n")

        assert runtime.live.grant_draw_preset == "off"

    @pytest.mark.parametrize("written", ["no", "NO", "false", "Off"])
    def test_the_other_spellings_of_off_mean_off_too(self, runtime: Any, written: str) -> None:
        self._write(runtime, f"permissions:\n  capabilities:\n    draw_preset: {written}\n")

        assert runtime.live.grant_draw_preset == "off"

    def test_a_quoted_off_reads_the_same(self, runtime: Any) -> None:
        self._write(runtime, 'permissions:\n  capabilities:\n    draw_preset: "off"\n')

        assert runtime.live.grant_draw_preset == "off"

    @pytest.mark.parametrize("written", ["on", "yes", "true"])
    def test_a_truthy_word_is_not_guessed_into_a_grant(self, runtime: Any, written: str) -> None:
        """它们对应哪一档没有答案，只能回落默认，不能猜成「最宽」。"""
        self._write(runtime, f"permissions:\n  capabilities:\n    clear_memory: {written}\n")

        assert runtime.live.grant_clear_memory == "admin"


class TestOffShutsTheDoorOnEveryone:
    def test_the_roster_is_turned_away_too(self, runtime: Any) -> None:
        set_live_config(runtime, grant_clear_memory="off")

        reply = clear_memory(runtime, in_group(LISTED_QQ), "/清除群记忆")

        assert reply == "「清空群记忆」已在配置里关闭。"

    def test_the_reason_says_it_is_off_rather_than_blaming_the_roster(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """名单里的人看到「仅限管理员使用」只会以为是 bug。"""
        monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)
        set_live_config(runtime, grant_model="off")

        assert switch_model(runtime, in_private(LISTED_QQ)) == "「切换模型」已在配置里关闭。"

    def test_an_approval_request_is_denied_instead_of_left_hanging(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没有人能作出决定时挂着只会等到超时，和名单为空是同一种局面。"""
        set_live_config(runtime, grant_approval="off")
        decisions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            runtime, "_client",
            SimpleNamespace(
                approve=lambda aid, decision, comment: _record(decisions, aid, decision, comment),
            ),
            raising=False,
        )

        asyncio.run(runtime.TangQiuCallbacks().show_approval_ui("ap-1", "read_file", {}))

        assert decisions and decisions[0][1] == "deny"
        assert not runtime._pending_approvals

    def test_a_member_is_not_told_the_capability_exists(self, runtime: Any) -> None:
        """对名单之外的人说「这项能力关了」等于把这项能力的存在念给他听。"""
        set_live_config(runtime, grant_clear_memory="off")

        assert clear_memory(runtime, in_group(MEMBER_QQ), "/清除群记忆") is None

    def test_a_granted_owner_is_not_told_either(self, runtime: Any) -> None:
        """受众是名单，不是能力：拿过 owner 档、后来被改成 off 的群主也一样静默。"""
        set_live_config(runtime, grant_clear_memory="off")

        assert clear_memory(runtime, in_group(OWNER_QQ, role="owner"), "/清除群记忆") is None

    @pytest.mark.parametrize(
        ("family", "text"),
        [
            ("draw_preset", "/切换画师串 x"),
            ("model", "/切换模型 gpt-4o"),
            ("custom_face", "表情列表"),
            ("proactive", "群发 700001 你好"),
            ("clear_memory", "/清除群记忆"),
        ],
    )
    def test_every_command_family_stays_silent_when_off(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch, family: str, text: str,
    ) -> None:
        """漏改任何一个调用点，那一档就会把能力名念给非名单用户。"""
        # 未初始化提示排在鉴权之前，model 分支不接 _client 会先撞上它。
        monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)
        set_live_config(runtime, **{f"grant_{family}": "off"})

        assert dispatch_family(runtime, family, in_group(MEMBER_QQ), text) is None


async def _record(sink: list[Any], approval_id: str, decision: str, comment: str) -> None:
    sink.append((approval_id, decision, comment))


class TestProactiveTargetsAreClamped:
    @pytest.fixture()
    def sent(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        box: list[dict[str, Any]] = []

        async def capture(bot: Any, target: Any, body: Any, attachments: Any, **kwargs: Any) -> None:
            box.append({"target": target, "body": body})

        monkeypatch.setattr(runtime, "_send_text_and_attachments", capture)
        return box

    def test_the_owner_can_speak_in_his_own_group(self, runtime: Any, sent: list[Any]) -> None:
        set_live_config(runtime, grant_proactive="owner")

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(OWNER_QQ, role="owner"),
            user_text=f"群发 {HOME_GROUP} 晚上八点开会",
            conversation_key="k", current_attachments=[],
        ))

        assert reply is not None and "已发送" in reply
        assert len(sent) == 1

    def test_the_owner_cannot_speak_in_another_group(self, runtime: Any, sent: list[Any]) -> None:
        set_live_config(runtime, grant_proactive="owner")

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(OWNER_QQ, role="owner"),
            user_text=f"群发 {OTHER_GROUP} 晚上八点开会",
            conversation_key="k", current_attachments=[],
        ))

        assert reply is not None and "只在本群有效" in reply
        assert sent == [], "denied delivery must not reach the send path"

    def test_the_owner_cannot_borrow_the_bot_for_a_private_message(
        self, runtime: Any, sent: list[Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """私聊目标不属于任何群，群主在那边没有可主张的作用域。"""
        set_live_config(runtime, grant_proactive="group_admin")
        monkeypatch.setattr(
            runtime, "_private_target_candidates",
            _async_returning([runtime.ProactiveTarget("private", MEMBER_QQ, "someone")]),
        )

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(OWNER_QQ, role="owner"),
            user_text=f"私信 {MEMBER_QQ} 你好",
            conversation_key="k", current_attachments=[],
        ))

        assert reply is not None and "只在本群有效" in reply
        assert sent == []

    def test_a_private_target_numbered_like_his_group_is_still_denied(
        self, runtime: Any, sent: list[Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """QQ 号和群号可以是同一串数字。作用域只比 id 不看类型的话，
        群主就能借 bot 私信到那个恰好同号的人头上。"""
        set_live_config(runtime, grant_proactive="group_admin")
        monkeypatch.setattr(
            runtime, "_private_target_candidates",
            _async_returning([runtime.ProactiveTarget("private", HOME_GROUP, "someone")]),
        )

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(OWNER_QQ, role="owner"),
            user_text=f"私信 {HOME_GROUP} 你好",
            conversation_key="k", current_attachments=[],
        ))

        assert reply is not None and "只在本群有效" in reply
        assert sent == []

    def test_the_owner_is_not_shown_the_target_listing(self, runtime: Any) -> None:
        set_live_config(runtime, grant_proactive="owner")

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(OWNER_QQ, role="owner"), user_text="主动目标",
            conversation_key="k", current_attachments=[],
        ))

        assert reply == "你只能向本群发送，没有可选目标清单。"

    def test_a_member_learns_nothing_about_the_command(self, runtime: Any) -> None:
        set_live_config(runtime, grant_proactive="owner")

        reply = asyncio.run(runtime._maybe_handle_proactive_command(
            bot=None, event=in_group(MEMBER_QQ), user_text=f"群发 {HOME_GROUP} 你好",
            conversation_key="k", current_attachments=[],
        ))

        assert reply == "该请求不可用。"


def _async_returning(value: Any):
    async def call(*args: Any, **kwargs: Any) -> Any:
        return value
    return call


class TestTheRosterKeepsItsNonCapabilityRoles:
    def test_a_granted_owner_is_not_promoted_in_platform_auth(self, runtime: Any) -> None:
        """platform_auth.is_admin 决定 engine 侧的工作区文件读取豁免。
        群主拿到清群记忆的权限，不该顺带拿到 bot 所在机器的文件。"""
        set_live_config(runtime, grant_clear_memory="group_admin", grant_proactive="group_admin")

        assert not runtime._is_admin_user(OWNER_QQ)

    def test_a_granted_owner_does_not_become_a_private_chat_allowee(self, runtime: Any) -> None:
        set_live_config(runtime, grant_proactive="group_admin", allow_private_friends=False)

        assert not runtime._is_private_allowed(in_private(OWNER_QQ, role="owner"))

    def test_turning_a_capability_off_does_not_empty_the_roster(self, runtime: Any) -> None:
        """名单还兼着审批收件人和私聊白名单，档位关的是能力，不是身份。"""
        set_live_config(runtime, grant_approval="off")

        assert runtime._is_admin_user(LISTED_QQ)


class TestDeadLetterCommandIsRosterOnly:
    """死信清单带真实群号/QQ 号，只对名单成员开放，且挂在私聊入口。"""

    def test_a_group_owner_is_denied_in_a_private_chat(self, runtime: Any) -> None:
        # 私聊里没有「本群」，群主就是普通用户 —— 清单绝不能广播真实群号给他。
        assert dead_letter(runtime, in_private(OWNER_QQ, role="owner")) == "该请求不可用。"

    def test_a_plain_member_is_denied(self, runtime: Any) -> None:
        assert dead_letter(runtime, in_private(MEMBER_QQ)) == "该请求不可用。"

    def test_the_roster_can_list(self, runtime: Any) -> None:
        reply = dead_letter(runtime, in_private(LISTED_QQ))

        assert reply is not None
        assert reply != "该请求不可用。"

    def test_turning_it_off_says_so(self, runtime: Any) -> None:
        set_live_config(runtime, grant_send_dead_letter="no")

        assert dead_letter(runtime, in_private(LISTED_QQ)) == "「发送死信」已在配置里关闭。"


class TestNameLookupIsNotAProbe:
    """按名字解析目标时，回答只能取决于请求者输入了什么，不能取决于那个名字
    是不是别人的群/联系人 —— 否则这个命令就是一台群名/昵称探测器。"""

    @pytest.fixture()
    def two_groups(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(runtime, "_group_target_candidates", _async_returning([
            runtime.ProactiveTarget("group", HOME_GROUP, "家里"),
            runtime.ProactiveTarget("group", OTHER_GROUP, "公司群"),
        ]))
        return runtime

    def test_another_groups_name_answers_like_a_name_that_exists_nowhere(
        self, two_groups: Any,
    ) -> None:
        set_live_config(two_groups, grant_clear_memory="owner")

        hit = clear_memory(two_groups, in_group(OWNER_QQ, role="owner"), "/清除群记忆 公司群")
        miss = clear_memory(two_groups, in_group(OWNER_QQ, role="owner"), "/清除群记忆 隔壁群")

        assert hit is not None and miss is not None
        assert hit.replace("公司群", "X") == miss.replace("隔壁群", "X")

    def test_his_own_group_name_still_resolves(self, two_groups: Any) -> None:
        set_live_config(two_groups, grant_clear_memory="owner")

        reply = clear_memory(two_groups, in_group(OWNER_QQ, role="owner"), "/清除群记忆 家里")

        assert reply is not None
        assert "家里" in reply and "没有找到" not in reply

    def test_the_roster_still_sees_which_names_collided(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "_group_target_candidates", _async_returning([
            runtime.ProactiveTarget("group", HOME_GROUP, "同名群"),
            runtime.ProactiveTarget("group", OTHER_GROUP, "同名群"),
        ]))

        reply = clear_memory(runtime, in_group(LISTED_QQ), "/清除群记忆 同名群")

        assert reply is not None and "不唯一" in reply

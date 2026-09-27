"""平台 ID 一律按字符串处理，纯数字的结果和改之前逐字节一致。

新平台的 ID 未必是数字（QQ 官方的 openid、Telegram 的负数群号）：判权、冷却、表情包收集、
SDK 水位、发送身份都要认得它们，而 QQ 的数字 ID 走哪条路都得到同一个键。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import capability, trigger_policy  # noqa: E402
from adapters._shared.active_account import ActiveAccountStore, normalize_account  # noqa: E402
from adapters.onebot import forward_authz  # noqa: E402
from adapters._shared.send_guard import SendContractError  # noqa: E402
from adapters.onebot.send_contract import (  # noqa: E402
    make_idempotency_key,
    target_from_idempotency_key,
    target_identity,
)
from clonoth_sdk.state import SessionState, TriggerInfo, channel_key  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import ID_NUMERIC, ID_STRING, STATE_ACTIVE_ACCOUNT, PlatformDescriptor  # noqa: E402
from platforms.ids import id_text_pattern, normalize_id, same_id  # noqa: E402

_OPENID = "E2B7C1A09F3D4E5B8A6C7D8E9F0A1B2C"


class TestNormalize:
    @pytest.mark.parametrize("value,expected", [
        (12345, "12345"),
        ("12345", "12345"),
        (" 12345 ", "12345"),
        (0, "0"),
        (-1001234567890, "-1001234567890"),
        (_OPENID, _OPENID),
        ("user.name@host", "user.name@host"),
    ])
    def test_accepted_ids(self, value, expected) -> None:
        assert normalize_id(value) == expected

    @pytest.mark.parametrize("value", [
        None, "", "   ", True, False, 1.5, b"123", ["1"],
        "group:1", "a/b", "../etc", "has space", "群里", "-", "x" * 200,
    ])
    def test_refused_ids(self, value) -> None:
        assert normalize_id(value) == ""

    def test_the_numeric_shape_only_takes_digits(self) -> None:
        assert normalize_id("12345", ID_NUMERIC) == "12345"
        assert normalize_id(12345, ID_NUMERIC) == "12345"
        assert normalize_id(_OPENID, ID_NUMERIC) == ""
        assert normalize_id(-5, ID_NUMERIC) == ""
        assert normalize_id("１２３", ID_NUMERIC) == ""

    def test_an_unknown_shape_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            normalize_id("1", "hex")

    def test_same_id_compares_across_types(self) -> None:
        assert same_id(123, "123")
        assert same_id(_OPENID, f" {_OPENID} ")
        assert not same_id("", "")
        assert not same_id(None, None)
        assert not same_id("0123", 123)


_STRING_PLATFORM = PlatformDescriptor(
    id="probe", display_name="Probe", short_name="Probe", id_shape=ID_STRING,
    state_files={STATE_ACTIVE_ACCOUNT: "probe_active_account.json"},
)


class TestIdTextPattern:
    def test_numeric_ids_keep_the_digit_boundaries(self) -> None:
        pattern = id_text_pattern(["12345", "123456"], ID_NUMERIC)

        assert pattern.pattern == r"(?<!\d)(?:123456|12345)(?!\d)"
        assert pattern.findall("12345 1234567 123456") == ["12345", "123456"]

    def test_string_ids_stop_at_word_characters_but_not_at_at_signs(self) -> None:
        pattern = id_text_pattern([_OPENID])

        assert pattern.findall(f"@{_OPENID} x{_OPENID} {_OPENID}_") == [_OPENID]

    def test_an_unknown_shape_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            id_text_pattern(["1"], "hex")

    def test_an_empty_id_list_is_refused(self) -> None:
        # 空的备选在每个位置都匹配空串，拿去替换等于在每两个字符之间插一个别名。
        with pytest.raises(ValueError):
            id_text_pattern([], ID_NUMERIC)


class TestActiveAccountStore:
    def test_qq_accounts_stay_numeric(self) -> None:
        # NapCat 的账号恒为纯数字：脏值宁可不加作用域，也不能分叉进算不回来的命名空间。
        assert normalize_account("12345", QQ_NAPCAT) == "12345"
        assert normalize_account("not-a-qq", QQ_NAPCAT) == ""

    def test_string_accounts_are_kept_when_the_platform_says_so(self) -> None:
        assert normalize_account(_OPENID, _STRING_PLATFORM) == _OPENID

    def test_a_string_account_round_trips_through_the_scope_file(self, tmp_path: Path) -> None:
        store = ActiveAccountStore(_STRING_PLATFORM, tmp_path)
        assert store.save(_OPENID) is True
        assert store.load() == _OPENID
        # 同一份文件按数字形状读，读不出合法账号就退化成不加作用域。
        assert ActiveAccountStore(replace(_STRING_PLATFORM, id_shape=ID_NUMERIC), tmp_path).load() == ""

    def test_the_file_name_comes_from_the_platform(self, tmp_path: Path) -> None:
        # 旧文件名不动：换了名字，已登记的账号读不回来，会话键就落回空作用域。
        assert ActiveAccountStore(QQ_NAPCAT, tmp_path).path == tmp_path / "data" / "onebot_active_bot.json"

    def test_a_platform_without_an_account_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError):
            ActiveAccountStore(replace(_STRING_PLATFORM, state_files={}), tmp_path)


class TestRequester:
    def test_numeric_ids_become_strings(self) -> None:
        requester = capability.Requester(user_id=10001, listed_admin=False, group_id=20002, group_role="owner")
        assert (requester.user_id, requester.group_id) == ("10001", "20002")
        assert requester.in_group

    def test_no_group_means_a_private_chat(self) -> None:
        requester = capability.Requester(user_id="10001", listed_admin=False, group_id=None, group_role="owner")
        assert requester.group_id == "" and not requester.in_group
        assert requester.rank == capability.RANK_MEMBER

    def test_a_string_group_scopes_like_a_numeric_one(self) -> None:
        owner = capability.Requester(user_id="u1", listed_admin=False, group_id="-100777", group_role="owner")
        assert capability.scope_deny_reason("clear_memory", owner, "-100777") == ""
        assert capability.scope_deny_reason("clear_memory", owner, "-100778") != ""

    def test_int_and_str_targets_are_the_same_group(self) -> None:
        owner = capability.Requester(user_id=1, listed_admin=False, group_id=20002, group_role="owner")
        assert capability.scope_deny_reason("clear_memory", owner, 20002) == ""
        assert capability.scope_deny_reason("clear_memory", owner, "20002") == ""


class TestCooldown:
    def _config(self) -> trigger_policy.TriggerConfig:
        return trigger_policy.TriggerConfig(cooldown_group_sec=30.0, cooldown_user_sec=0.0)

    def test_int_and_str_ids_share_one_cooldown(self) -> None:
        cooldown = trigger_policy.CooldownState()
        cooldown.record(trigger_policy.TriggerInput(group_id=9, user_id=1, now=0.0))

        left, scope = cooldown.remaining(trigger_policy.TriggerInput(group_id="9", user_id="1", now=10.0), self._config())

        assert scope == "per_group" and left == pytest.approx(20.0)

    def test_string_groups_are_tracked_and_forgotten(self) -> None:
        cooldown = trigger_policy.CooldownState()
        cooldown.record(trigger_policy.TriggerInput(group_id="-100777", user_id=_OPENID, now=0.0))
        cooldown.forget_group("-100777")

        left, _ = cooldown.remaining(trigger_policy.TriggerInput(group_id="-100777", now=1.0), self._config())

        assert left == 0.0

    def test_trigger_input_holds_strings(self) -> None:
        inp = trigger_policy.TriggerInput(group_id=9, user_id=None)
        assert (inp.group_id, inp.user_id) == ("9", "")


class TestSdkWatermarks:
    def test_int_and_str_channels_share_one_watermark(self) -> None:
        state = SessionState()
        state.advance_watermark(998877, 3)
        assert state.get_high_watermark("998877") == 3
        state.reset_channel_watermark("998877")
        assert state.get_high_watermark(998877) == -1

    def test_a_pending_watermark_is_keyed_by_string(self) -> None:
        state = SessionState()
        state.register_watermark(7, 998877, 5)
        assert state.pending_watermarks[7] == ("998877", 5)
        assert state.accept_watermark(7) == ("998877", 5)
        assert state.get_high_watermark(998877) == 5

    def test_generations_are_shared_across_types(self) -> None:
        state = SessionState()
        generation = state.watermark_generation(1)
        state.reset_channel_watermark("1")
        state.register_watermark(8, 1, 4, generation=generation)
        assert state.pending_watermarks == {}

    def test_string_dm_channels(self) -> None:
        state = SessionState()
        state.register_trigger(TriggerInfo(
            inbound_seq=1, conversation_key="tg_private:x", session_id="s1", is_dm=True,
            platform_data={"channel_id": _OPENID},
        ))
        assert state.get_dm_channel("s1") == _OPENID
        state.cleanup_dm_channels_for(_OPENID)
        assert state.get_dm_channel("s1") is None

    def test_numeric_dm_channels_are_stored_as_strings(self) -> None:
        state = SessionState()
        state.register_dm_channel("s1", 10001)
        assert state.get_dm_channel("s1") == "10001"
        state.cleanup_dm_channels_for(10001)
        assert state.get_dm_channel("s1") is None

    def test_an_empty_channel_is_not_registered(self) -> None:
        state = SessionState()
        state.register_dm_channel("s1", "")
        state.register_trigger(TriggerInfo(
            inbound_seq=1, conversation_key="k", session_id="s2", is_dm=True, platform_data={"channel_id": None},
        ))
        assert state.session_dm_channels == {}
        assert channel_key(True) == ""


class TestSendIdentity:
    def test_numeric_targets_are_unchanged(self) -> None:
        assert target_identity({"type": "group", "group_id": 123}) == "group:123"
        assert target_identity({"type": "private", "user_id": "42"}) == "private:42"

    def test_string_targets_round_trip_through_the_claim_key(self) -> None:
        target = {"type": "private", "user_id": _OPENID}
        key = make_idempotency_key(target, "hello")
        assert target_from_idempotency_key(key) == f"private:{_OPENID}"
        assert target_from_idempotency_key(f"content:group:-100777:{'0' * 64}") == "group:-100777"

    def test_a_malformed_target_id_is_a_contract_error(self) -> None:
        with pytest.raises(SendContractError):
            target_identity({"type": "group", "group_id": "a:b"})


class TestForwardOrigin:
    @pytest.mark.parametrize("target_id,origin", [(123, "123"), ("123", 123), (_OPENID, _OPENID)])
    def test_the_origin_matches_across_types(self, target_id, origin) -> None:
        assert forward_authz.target_is_origin(
            target_type="private", target_id=target_id, origin_user_id=origin, origin_group_id=None,
        )

    def test_a_missing_origin_never_matches(self) -> None:
        assert not forward_authz.target_is_origin(
            target_type="group", target_id="", origin_user_id=None, origin_group_id="",
        )

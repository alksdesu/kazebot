"""QQ 热载配置的解析、优先级与 fail-closed 保护。

这一层的失手代价不对称：解析错一个键就是「群白名单塌成空 = 全群静默」或者
「管理员名单塌成空 = 审批全拒」，而且两种都不报错，只是 bot 突然不说话了。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_live_config, write_live_config  # noqa: E402

_ADAPTER_ROOT = _ROOT / "adapters" / "onebot"
_ALL_QQ_ENV = (
    "CLONOTH_ALLOWED_GROUPS", "ONEBOT_ALLOWED_GROUPS",
    "CLONOTH_ALLOWED_PRIVATE_USERS", "ONEBOT_ALLOWED_PRIVATE_USERS",
    "CLONOTH_ADMIN_QQ_USERS", "ONEBOT_ADMIN_USERS",
    "ONEBOT_ALLOW_PRIVATE_FRIENDS", "ONEBOT_GROUP_TRIGGER", "ONEBOT_TRIGGER_PREFIXES",
    "ONEBOT_STRIP_MARKDOWN_STYLES", "ONEBOT_ENABLE_REACTIONS", "ONEBOT_ENABLE_QQ_QUEUE",
    "ONEBOT_QQ_QUEUE_WORKERS", "ONEBOT_GROUP_HISTORY_MAX", "ONEBOT_ENABLE_FORWARD_BRIDGE",
    "ONEBOT_MAX_IMAGES_PER_TURN", "ONEBOT_AUTO_LIKE_TIMES",
)


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """一份干净的 live_config：清掉开发机上可能存在的所有 QQ 相关环境变量。"""
    for key in _ALL_QQ_ENV:
        monkeypatch.delenv(key, raising=False)
    return load_live_config(monkeypatch, tmp_path)


def _write_yaml_text(module: Any, text: str) -> None:
    """把 yaml 原文落盘并让它立刻生效。

    布尔字面量那类陷阱只存在于文本层：写 Python 值等于替解析器把标量定好了类型，
    正是当初漏掉 `words: [on]` 的原因。
    """
    path = module.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    module.invalidate()


class TestPrecedence:
    def test_no_file_no_env_gives_the_declared_defaults(self, live: Any) -> None:
        assert live.live.allowed_groups == ()
        assert live.live.admin_users == ()
        assert live.live.group_trigger == "mention_only"
        assert live.live.enable_reactions is True
        assert live.live.group_history_max == 20

    def test_env_fills_in_when_the_file_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path, CLONOTH_ALLOWED_GROUPS="111,222,[占位符]",
        )

        assert module.live.allowed_groups == (111, 222)

    def test_the_first_declared_env_name_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path,
            CLONOTH_ALLOWED_GROUPS="111", ONEBOT_ALLOWED_GROUPS="222",
        )

        assert module.live.allowed_groups == (111,)

    def test_the_second_env_name_covers_an_empty_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path,
            CLONOTH_ALLOWED_GROUPS="   ", ONEBOT_ALLOWED_GROUPS="222",
        )

        assert module.live.allowed_groups == (222,)

    def test_an_empty_bool_env_means_off_not_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """_env_bool 的老语义：设成空串就是关。列表键相反，空串等于没配。"""
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, ONEBOT_ENABLE_REACTIONS="")

        assert module.live.enable_reactions is False

    def test_an_empty_env_leaves_a_tristate_unset(
        self, live: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """空串对布尔是「关」，对三态只能是「没配」—— 否则升级时会静默关掉 @ 触发。"""
        monkeypatch.setenv("ONEBOT_PROBE_SIGNAL", "")
        probe = live.LiveKey(
            "probe_signal", "trigger.signals.probe", live.TRISTATE_BOOL, None,
            env=("ONEBOT_PROBE_SIGNAL",),
        )

        assert probe.resolve({}) is None

    def test_yaml_beats_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, CLONOTH_ALLOWED_GROUPS="111")

        write_live_config(module, allowed_groups=[999])

        assert module.live.allowed_groups == (999,)

    def test_a_key_absent_from_yaml_still_reads_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, CLONOTH_ADMIN_QQ_USERS="10001")

        write_live_config(module, allowed_groups=[999])

        assert module.live.admin_users == (10001,)

    def test_an_explicit_empty_list_in_yaml_is_honoured(self, live: Any) -> None:
        """yaml 里显式写空名单是「我就是要全拒」，不能当成没配而回落 env。"""
        write_live_config(live, allowed_groups=[111])
        write_live_config(live, allowed_groups=[])

        assert live.live.allowed_groups == ()


class TestCorruptFileNeverOpensOrClosesTheGate:
    def test_a_broken_file_keeps_the_previous_values(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111], admin_users=[10001])

        live.config_path().write_text("channels: [oops\n", encoding="utf-8")
        live.invalidate()

        assert live.live.allowed_groups == (111,)
        assert live.live.admin_users == (10001,)

    def test_a_non_mapping_top_level_keeps_the_previous_values(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111])

        live.config_path().write_text("- a\n- b\n", encoding="utf-8")
        live.invalidate()

        assert live.live.allowed_groups == (111,)

    def test_invalidate_alone_never_drops_the_last_valid_values(self, live: Any) -> None:
        """invalidate() 只让缓存过期，不能把「上一份有效配置」一起丢掉。

        写入侧的正常流程就是「写文件 -> invalidate -> 重读」，如果 invalidate 顺手
        清掉了有效标记，那么写坏文件的那一刻白名单就塌成空 —— 恰好是 fail-closed
        保护要防的情形，而且是被保护机制自己触发的。
        """
        write_live_config(live, allowed_groups=[111])
        live.config_path().write_text("channels: [oops\n", encoding="utf-8")

        live.invalidate()
        live.invalidate()

        assert live.live.allowed_groups == (111,)

    def test_a_file_broken_from_the_start_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        path = tmp_path / "config" / "qq.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("channels: [oops\n", encoding="utf-8")

        module = load_live_config(monkeypatch, tmp_path, CLONOTH_ALLOWED_GROUPS="111")

        assert module.live.allowed_groups == (111,)

    def test_recovering_the_file_takes_effect(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111])
        live.config_path().write_text("channels: [oops\n", encoding="utf-8")
        live.invalidate()
        assert live.live.allowed_groups == (111,)

        write_live_config(live, allowed_groups=[222])

        assert live.live.allowed_groups == (222,)
        assert live.state_payload()["parse_error"] == ""

    def test_the_parse_error_is_reported_not_swallowed(self, live: Any) -> None:
        live.config_path().parent.mkdir(parents=True, exist_ok=True)
        live.config_path().write_text("channels: [oops\n", encoding="utf-8")
        live.invalidate()

        assert "ParserError" in live.state_payload()["parse_error"]

    def test_an_empty_file_is_a_valid_empty_document(self, live: Any) -> None:
        live.config_path().parent.mkdir(parents=True, exist_ok=True)
        live.config_path().write_text("", encoding="utf-8")
        live.invalidate()

        assert live.live.allowed_groups == ()
        assert live.state_payload()["parse_error"] == ""


class TestCoercion:
    @pytest.mark.parametrize(
        "raw,expected",
        [(True, True), (False, False), ("1", True), ("0", False), ("on", True), ("off", False), (1, True), (0, False)],
    )
    def test_bool_forms(self, live: Any, raw: Any, expected: bool) -> None:
        write_live_config(live, enable_reactions=raw)

        assert live.live.enable_reactions is expected

    def test_a_garbage_bool_falls_back_to_the_default(self, live: Any) -> None:
        """"maybe" 不该悄悄变成 True —— 那是「非空字符串即真」的经典误伤。"""
        write_live_config(live, enable_reactions="maybe")
        assert live.live.enable_reactions is True

        write_live_config(live, enable_auto_like="maybe")
        assert live.live.enable_auto_like is False

    def test_ints_are_clamped_to_the_declared_range(self, live: Any) -> None:
        write_live_config(live, max_images_per_turn=999, auto_like_times=0)

        assert live.live.max_images_per_turn == 16
        assert live.live.auto_like_times == 1

    def test_a_garbage_int_falls_back_to_the_default(self, live: Any) -> None:
        write_live_config(live, group_history_max="lots")

        assert live.live.group_history_max == 20

    def test_an_id_list_takes_ints_and_strings_and_drops_junk(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111, "222", "[占位符]", None, 111])

        assert live.live.allowed_groups == (111, 222)

    @pytest.mark.parametrize(
        "item",
        [-500, "-500", "+500", "1_0", "１２３", "0", "9" * 20, " ", "12a45"],
    )
    def test_an_id_list_drops_everything_that_is_not_a_plain_positive_number(
        self, live: Any, item: Any,
    ) -> None:
        """这些项 Python 的 int() 都收，但没有一个是能匹配上的真实号码。

        `+500` / `1_0` / 全角数字进名单后永远匹配不上任何人，20 位数字会在控制台的
        JSON 往返里被改写成另一个号，负数和 0 根本不是号码。
        """
        write_live_config(live, allowed_groups=[111, item])

        assert live.live.allowed_groups == (111,)

    def test_an_id_list_keeps_small_ids(self, live: Any) -> None:
        """短号不是非法号码：长度下限会把测试环境和本地调试配置一起打死。"""
        write_live_config(live, allowed_groups=[7], admin_users=["42"])

        assert live.live.allowed_groups == (7,)
        assert live.live.admin_users == (42,)

    def test_an_env_id_list_follows_the_same_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """env 逗号串是第二条入口。它松一点就等于收紧了一半。"""
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path, CLONOTH_ALLOWED_GROUPS="111,-500,+500,0,１２３,222,111",
        )

        assert module.live.allowed_groups == (111, 222)

    @pytest.mark.parametrize("item", [-500, "+500", "1_0", "１２３", "0", "9" * 20])
    def test_a_number_that_got_dropped_says_so_in_the_log(
        self, live: Any, caplog: pytest.LogCaptureFixture, item: Any,
    ) -> None:
        """这几项以前收得下。静默收紧的话，运营者会一直以为号码还配着。"""
        with caplog.at_level("WARNING"):
            write_live_config(live, allowed_groups=[111, item])
            groups = live.live.allowed_groups

        assert groups == (111,)
        assert any("not a usable QQ id" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("item", ["[占位符]", "12a45", "me"])
    def test_descriptive_text_is_dropped_without_a_warning(
        self, live: Any, caplog: pytest.LogCaptureFixture, item: str,
    ) -> None:
        """占位符是文档里的默认值，它一直被丢弃；为它告警会把真正收紧掉的那几项淹掉。"""
        with caplog.at_level("WARNING"):
            write_live_config(live, allowed_groups=[111, item])
            groups = live.live.allowed_groups

        assert groups == (111,)
        assert [r.getMessage() for r in caplog.records if "not a usable QQ id" in r.getMessage()] == []

    def test_id_list_order_follows_the_file(self, live: Any) -> None:
        """审批通知按名单顺序逐个私发，转成 set 会让日志每次都不一样。"""
        write_live_config(live, admin_users=[333, 111, 222])

        assert live.live.admin_users == (333, 111, 222)

    def test_prefixes_trim_dedupe_and_keep_order(self, live: Any) -> None:
        write_live_config(live, trigger_prefixes=[" ! ", "!", "#", ""])

        assert live.live.trigger_prefixes == ("!", "#")

    def test_a_comma_string_still_works_for_prefixes(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  prefixes: '!,#'\n")

        assert live.live.trigger_prefixes == ("!", "#")


class TestYamlBooleanLiteralsStayText:
    """PyYAML 默认按 YAML 1.1 收布尔，on/off/yes/no 在进到 coercer 之前就成了 True/False。

    后果是配了 `words: [on, off, 早安]` 的人得到 ('True', 'False', '早安')：配的词不命中，
    还凭空多两个，全程没有任何提示。
    """

    def test_a_keyword_list_keeps_on_and_off(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  keyword:\n    words: [on, off, 早安]\n")

        assert live.live.keyword_words == ("on", "off", "早安")

    def test_a_bare_keyword_scalar_keeps_no(self, live: Any) -> None:
        """标量走的是另一条分支：读成 False 时 str(False or "") 是空串，整项静默消失。"""
        _write_yaml_text(live, "trigger:\n  keyword:\n    words: no\n")

        assert live.live.keyword_words == ("no",)

    def test_a_name_list_keeps_yes(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  name:\n    words: [yes, 咪啪]\n")

        assert live.live.name_words == ("yes", "咪啪")

    def test_a_prefix_list_keeps_on(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  prefixes: [on, '!']\n")

        assert live.live.trigger_prefixes == ("on", "!")

    def test_a_plain_string_keeps_off(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  llm_intent:\n    node_id: off\n")

        assert live.live.llm_intent_node_id == "off"

    def test_a_trigger_mode_keeps_off(self, live: Any) -> None:
        """读成布尔时会回落到 mention_only，运营者看不出自己写了个不存在的模式。"""
        _write_yaml_text(live, "trigger:\n  group_mode: off\n")

        assert live.live.group_trigger == "off"
        assert live.live.group_trigger_is_known is False

    def test_a_capability_grant_keeps_off(self, live: Any) -> None:
        _write_yaml_text(live, "permissions:\n  capabilities:\n    approval: off\n")

        assert live.live.grant_approval == "off"

    @pytest.mark.parametrize(
        "token,expected", [("yes", True), ("on", True), ("off", False), ("no", False)],
    )
    def test_a_bool_switch_still_takes_the_one_one_spellings(
        self, live: Any, token: str, expected: bool,
    ) -> None:
        """这四个词由 coercer 认，换 loader 不能把它们一起关掉。"""
        _write_yaml_text(live, f"extensions:\n  reactions: {token}\n")

        assert live.live.enable_reactions is expected

    @pytest.mark.parametrize("token,expected", [("true", True), ("false", False)])
    def test_yaml_1_2_booleans_are_still_booleans(
        self, live: Any, token: str, expected: bool,
    ) -> None:
        _write_yaml_text(live, f"extensions:\n  reactions: {token}\n")

        assert live.live.enable_reactions is expected

    def test_saving_another_key_does_not_rewrite_the_word_list(self, live: Any) -> None:
        """控制台改一个不相干的键会把整份文档 dump 回去，词表跟着变成布尔就再也回不来了。"""
        _write_yaml_text(live, "trigger:\n  keyword:\n    words: [on, off]\n")

        write_live_config(live, allowed_groups=[111])

        assert live.live.keyword_words == ("on", "off")
        assert "true" not in live.config_path().read_text(encoding="utf-8")

    def test_an_unset_tristate_signal_is_still_none(self, live: Any) -> None:
        """三态开关靠 null 表达「这一项我没管」，被 loader 改动波及就会静默改判定。"""
        _write_yaml_text(live, "trigger:\n  signals:\n    at:\n    reply: null\n")

        assert live.live.signal_at is None
        assert live.live.signal_reply is None


class TestARealBooleanIsDroppedNotStringified:
    """`true` 本身当关键词是真歧义：YAML 1.2 里它就是布尔，只能要求加引号。"""

    def test_a_boolean_in_a_keyword_list_is_dropped_with_a_warning(
        self, live: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            _write_yaml_text(live, "trigger:\n  keyword:\n    words: [true, 早安]\n")
            words = live.live.keyword_words

        assert words == ("早安",)
        assert any("quote it" in r.getMessage() for r in caplog.records)

    def test_quoting_gets_the_literal_word_back(self, live: Any) -> None:
        _write_yaml_text(live, "trigger:\n  keyword:\n    words: ['true', 'false']\n")

        assert live.live.keyword_words == ("true", "false")

    def test_a_boolean_plain_string_falls_back_to_the_default(
        self, live: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            _write_yaml_text(live, "trigger:\n  llm_intent:\n    node_id: true\n")
            node_id = live.live.llm_intent_node_id

        assert node_id == "qq.intent"
        assert any("quote it" in r.getMessage() for r in caplog.records)

    def test_a_boolean_trigger_mode_falls_back_to_the_default(
        self, live: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            _write_yaml_text(live, "trigger:\n  group_mode: false\n")
            mode = live.live.group_trigger

        assert mode == "mention_only"
        assert any("quote it" in r.getMessage() for r in caplog.records)

    def test_a_boolean_prefix_list_falls_back_to_the_defaults(
        self, live: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            _write_yaml_text(live, "trigger:\n  prefixes: true\n")
            prefixes = live.live.trigger_prefixes

        assert prefixes == ("!", "！", "/")
        assert any("quote it" in r.getMessage() for r in caplog.records)


class TestAllowPrivateFriendsDerivation:
    """好友放行以前是从白名单文本里找「好友」二字推导的，env 兜底路径要保留原语义。"""

    def test_an_empty_private_list_means_friends_are_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path)

        assert module.live.allow_private_friends is True

    def test_a_numeric_only_private_list_means_friends_are_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path, CLONOTH_ALLOWED_PRIVATE_USERS="10001,10002",
        )

        assert module.live.allow_private_friends is False

    @pytest.mark.parametrize("raw", ["10001,好友", "10001,friend", "[私聊只允许已经通过好友请求的人]"])
    def test_the_friend_marker_in_the_list_still_allows_friends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path, CLONOTH_ALLOWED_PRIVATE_USERS=raw,
        )

        assert module.live.allow_private_friends is True

    def test_the_placeholder_text_does_not_warn_about_bad_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """占位符文本是这条推导的正常输入，给它告警等于每次启动都喊一次狼来了。"""
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        with caplog.at_level("WARNING"):
            module = load_live_config(
                monkeypatch, tmp_path,
                CLONOTH_ALLOWED_PRIVATE_USERS="[私聊只允许已经通过好友请求的人]",
            )
            assert module.live.allow_private_friends is True

        assert [r.getMessage() for r in caplog.records if "not a usable QQ id" in r.getMessage()] == []

    def test_an_explicit_env_flag_beats_the_derivation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path,
            CLONOTH_ALLOWED_PRIVATE_USERS="10001,好友",
            ONEBOT_ALLOW_PRIVATE_FRIENDS="0",
        )

        assert module.live.allow_private_friends is False

    def test_yaml_beats_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(
            monkeypatch, tmp_path, CLONOTH_ALLOWED_PRIVATE_USERS="10001",
        )

        write_live_config(module, allow_private_friends=True)

        assert module.live.allow_private_friends is True


class TestCacheAndPinning:
    def test_an_unchanged_file_is_not_reparsed(self, live: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        write_live_config(live, allowed_groups=[111])
        assert live.live.allowed_groups == (111,)

        def explode(*args: Any, **kwargs: Any):
            raise AssertionError("re-parsed an unchanged file")

        monkeypatch.setattr(live, "load_yaml", explode)

        assert live.live.allowed_groups == (111,)

    def test_a_pin_survives_another_writer(self, live: Any) -> None:
        """处理中的消息用进入时的快照，不中途换标准。

        文件改动直接落盘而不走 save()：真实情形就是 supervisor 在另一个进程里写，
        钉住的这一侧只会看到文件变了，不会被通知去解开自己的快照。
        """
        write_live_config(live, allowed_groups=[111])

        with live.pinned():
            live.config_path().write_text(
                yaml.safe_dump(live.document_from({"allowed_groups": [222]}), allow_unicode=True),
                encoding="utf-8",
            )
            live.invalidate()

            assert live.live.allowed_groups == (111,)

        assert live.live.allowed_groups == (222,)

    def test_the_pin_is_released_on_exit(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111])
        with live.pinned(allowed_groups=(999,)):
            assert live.live.allowed_groups == (999,)

        assert live.live.allowed_groups == (111,)

    def test_pinning_a_prefix_list_recomputes_the_derived_value(self, live: Any) -> None:
        with live.pinned(trigger_prefixes=("$", "/")):
            assert live.live.trigger_prefixes_strippable == ("$",)

    def test_pinning_an_unknown_key_is_an_error(self, live: Any) -> None:
        with pytest.raises(KeyError, match="unknown live config keys"):
            with live.pinned(allowed_grops=()):
                pass

    def test_saving_releases_the_savers_own_pin(self, live: Any) -> None:
        """写入侧改完要能立刻自检；读到旧值会被当成「没生效」。"""
        write_live_config(live, allowed_groups=[111])
        live.refresh()

        write_live_config(live, allowed_groups=[222])

        assert live.live.allowed_groups == (222,)

    def test_saving_rejects_a_non_mapping(self, live: Any) -> None:
        with pytest.raises(Exception):
            live.save([1, 2, 3])

    def test_a_value_yaml_cannot_represent_fails_before_the_write(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111])
        before = live.config_path().read_text(encoding="utf-8")

        with pytest.raises(yaml.YAMLError):
            live.save({"channels": {"allowed_groups": object()}})

        assert live.config_path().read_text(encoding="utf-8") == before
        assert live.live.allowed_groups == (111,)

    def test_saving_leaves_no_temp_file_behind(self, live: Any) -> None:
        write_live_config(live, allowed_groups=[111])

        assert list(live.config_path().parent.glob("qq.yaml.*")) == []

    def test_saving_goes_through_an_atomic_replace(
        self, live: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """bot 进程随时在读，直接覆写会让它读到半截 yaml —— 那等于整份配置塌掉。"""
        replaced: list[Any] = []
        real_replace = live.os.replace

        def spy(src: Any, dst: Any) -> Any:
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(live.os, "replace", spy)

        write_live_config(live, allowed_groups=[111])

        assert [dst for _, dst in replaced] == [str(live.config_path())]


class TestReadAccess:
    def test_a_typo_raises_instead_of_returning_none(self, live: Any) -> None:
        with pytest.raises(AttributeError, match="no live config key named 'allowed_grops'"):
            live.live.allowed_grops

    def test_the_snapshot_cannot_be_written_through(self, live: Any) -> None:
        with pytest.raises(AttributeError, match="read-only"):
            live.live.allowed_groups = (1,)

    def test_dir_lists_every_key(self, live: Any) -> None:
        names = set(dir(live.live))

        assert names == set(live.LIVE_KEYS_BY_NAME) | set(live.DERIVED_NAMES)


class TestDeclarationTable:
    def test_names_and_paths_are_unique(self, live: Any) -> None:
        names = [k.name for k in live.LIVE_KEYS]
        paths = [k.path for k in live.LIVE_KEYS]

        assert len(names) == len(set(names))
        assert len(paths) == len(set(paths))

    def test_no_declared_key_collides_with_a_derived_name(self, live: Any) -> None:
        assert not set(live.LIVE_KEYS_BY_NAME) & set(live.DERIVED_NAMES)

    def test_every_env_name_is_namespaced(self, live: Any) -> None:
        offenders = [
            (k.name, env) for k in live.LIVE_KEYS for env in k.env
            if not env.startswith(("ONEBOT_", "CLONOTH_"))
        ]

        assert offenders == []

    def test_the_reconciled_set_is_exactly_the_startup_bound_keys(self, live: Any) -> None:
        assert set(live.RECONCILED_KEYS) == {
            "group_history_max", "group_history_undelivered_ratio",
            "enable_queue", "queue_workers", "enable_forward_bridge",
        }

    def test_every_reconciled_key_explains_why(self, live: Any) -> None:
        """标成 reconciled 就意味着有人得去重建运行期对象，说明必须写下来。"""
        missing = [k.name for k in live.LIVE_KEYS if k.reload == live.RELOAD_RECONCILED and not k.note]

        assert missing == []

    def test_document_from_places_values_at_the_declared_paths(self, live: Any) -> None:
        document = live.document_from({"allowed_groups": (1, 2), "queue_workers": 8})

        assert document == {"channels": {"allowed_groups": [1, 2]}, "queue": {"workers": 8}}

    def test_document_from_merges_into_a_base(self, live: Any) -> None:
        base = live.document_from({"allowed_groups": (1,)})

        document = live.document_from({"admin_users": (9,)}, base=base)

        assert document["channels"]["allowed_groups"] == [1]
        assert document["permissions"]["admin_users"] == [9]

    def test_document_from_rejects_an_unknown_key(self, live: Any) -> None:
        with pytest.raises(KeyError, match="unknown live config keys"):
            live.document_from({"allowed_grops": ()})

    def test_document_from_orders_sets(self, live: Any) -> None:
        """集合没顺序，落盘前必须定序，否则每次保存都产生一份不同的 yaml。"""
        document = live.document_from({"admin_users": frozenset({3, 1, 2})})

        assert document["permissions"]["admin_users"] == [1, 2, 3]

    def test_every_value_is_json_serializable(self, live: Any) -> None:
        import json

        json.dumps(live.state_payload())


class TestExampleFileStaysInSync:
    """qq.example.yaml 是运营者唯一的键位说明，和声明表脱钩就等于文档骗人。"""

    def _example(self, live: Any) -> dict[str, Any]:
        """校验和生效解析必须是同一个 loader，否则文档按一套语义、bot 按另一套。"""
        text = (_ROOT / "config" / "qq.example.yaml").read_text(encoding="utf-8")
        loaded = live.load_yaml(text)
        assert isinstance(loaded, dict)
        return loaded

    def _leaf_paths(self, document: Any, prefix: str = "") -> list[str]:
        """文档里存在的点分路径。

        按「键在不在」算而不是「值是不是 None」：三态开关的示例值就是 null，
        拿 _dig 的返回值判断会把它们全当成没写。
        """
        if isinstance(document, dict) and document:
            leaves: list[str] = []
            for key, value in document.items():
                leaves.extend(self._leaf_paths(value, f"{prefix}.{key}" if prefix else str(key)))
            return leaves
        return [prefix] if prefix else []

    def test_the_example_parses_as_a_mapping(self, live: Any) -> None:
        assert self._example(live)

    def test_every_declared_key_appears_in_the_example(self, live: Any) -> None:
        present = set(self._leaf_paths(self._example(live)))
        missing = [k.name for k in live.LIVE_KEYS if k.path not in present]

        assert missing == [], f"这些键在 qq.example.yaml 里没有说明: {missing}"

    def test_the_example_declares_no_key_that_the_table_does_not_know(self, live: Any) -> None:
        known = {k.path for k in live.LIVE_KEYS} | {"version"}

        unknown = [leaf for leaf in self._leaf_paths(self._example(live)) if leaf not in known]

        assert unknown == []

    def test_the_example_values_match_the_declared_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """示例文件必须就是当前默认行为，否则复制一份就等于悄悄改了配置。"""
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path)
        defaults = {k.name: getattr(module.live, k.name) for k in module.LIVE_KEYS}

        path = module.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((_ROOT / "config" / "qq.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        module.invalidate()

        assert {k.name: getattr(module.live, k.name) for k in module.LIVE_KEYS} == defaults


class TestStatePayload:
    def test_it_covers_every_key_including_derived(self, live: Any) -> None:
        values = live.state_payload()["values"]

        assert set(values) == set(live.LIVE_KEYS_BY_NAME) | set(live.DERIVED_NAMES)

    def test_it_reports_the_file_fingerprint(self, live: Any) -> None:
        assert live.state_payload()["file"]["exists"] is False

        write_live_config(live, allowed_groups=[111])

        state = live.state_payload()
        assert state["file"]["exists"] is True
        assert state["file"]["size"] > 0

    def test_it_marks_which_keys_need_reconciling(self, live: Any) -> None:
        reload_modes = live.state_payload()["reload"]

        assert reload_modes["queue_workers"] == live.RELOAD_RECONCILED
        assert reload_modes["enable_reactions"] == live.RELOAD_LIVE

    def test_it_reflects_the_effective_value_not_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """前端拿 /qq/state 对比表单，所以它必须是「生效值」，含 env 兜底那部分。"""
        for key in _ALL_QQ_ENV:
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, CLONOTH_ADMIN_QQ_USERS="10001")

        assert module.state_payload()["values"]["admin_users"] == [10001]


class TestPublishedInputRules:
    """控制台按这份规则校验新增项。前端自己写一份就会漂移，而漂移的表现是
    「界面收下了、保存后消失」—— 运营者以为号码加上了，实际名单里没有。
    """

    def test_every_list_key_publishes_a_rule(self, live: Any) -> None:
        rules = live.state_payload()["input_rules"]

        assert rules["allowed_groups"]["kind"] == "id"
        assert rules["allowed_private_users"]["kind"] == "id"
        assert rules["admin_users"]["kind"] == "id"
        assert rules["trigger_prefixes"]["kind"] == "word"
        assert rules["name_words"]["kind"] == "word"
        assert rules["keyword_words"]["kind"] == "word"

    def test_no_list_coercer_is_left_without_a_rule(self, live: Any) -> None:
        """将来新增一个名单键忘了挂规则，控制台会直接禁用那个输入框。"""
        listish = {live.ID_LIST, live.PREFIX_LIST, live.WORD_LIST}
        rules = live.state_payload()["input_rules"]

        missing = [key.name for key in live.LIVE_KEYS if key.coerce in listish and key.name not in rules]

        assert missing == []

    def test_the_published_id_rule_is_what_the_coercer_enforces(self, live: Any) -> None:
        """公布的值域和实际执行的值域不可能分家：边界样本直接由公布的规则生成。"""
        rule = live.state_payload()["input_rules"]["allowed_groups"]
        low, high = int(rule["min"]), int(rule["max"])

        write_live_config(live, allowed_groups=[low, high, low - 1, high + 1])

        assert live.live.allowed_groups == (low, high)

    def test_the_published_id_pattern_is_what_the_coercer_enforces(self, live: Any) -> None:
        rule = live.state_payload()["input_rules"]["allowed_groups"]
        pattern = re.compile(rule["pattern"])
        samples = ["222", "-500", "+500", "1_0", "１２３", "12a45"]

        write_live_config(live, allowed_groups=samples)

        accepted = {str(value) for value in live.live.allowed_groups}
        assert accepted == {token for token in samples if pattern.match(token)}

    def test_the_rules_survive_json(self, live: Any) -> None:
        """MappingProxyType 直接进 json.dumps 会 TypeError。"""
        import json

        assert json.loads(json.dumps(live.state_payload()))["input_rules"]["name_words"]["trim"] is True

    def test_the_frontend_declares_every_field_that_gets_published(self, live: Any) -> None:
        """后端加一个规则字段而前端类型没跟上 —— 那个字段在校验里被静默忽略。"""
        source = (
            _ROOT / "adapters" / "web" / "frontend" / "src" / "api" / "supervisorClient.ts"
        ).read_text(encoding="utf-8")
        body = re.search(r"export interface QqInputRule \{(.*?)\n\}", source, re.S)
        assert body is not None, "supervisorClient.ts 里找不到 QqInputRule 接口"
        declared = set(re.findall(r"^\s*(\w+)\??:", body.group(1), re.M))

        published = {field for rule in live.state_payload()["input_rules"].values() for field in rule}

        assert published <= declared, f"前端 QqInputRule 没声明这些字段: {sorted(published - declared)}"


class TestNoStaleConstantsLeftBehind:
    """A 组常量必须彻底消失，而不是留一份冻结副本等人误用。"""

    _MIGRATED = (
        "ADMIN_QQ_USERS", "ALLOWED_GROUPS", "ALLOWED_PRIVATE_USERS", "ALLOW_PRIVATE_FRIENDS",
        "GROUP_TRIGGER", "GROUP_TRIGGER_IS_KNOWN", "TRIGGER_PREFIXES", "TRIGGER_PREFIXES_STRIPPABLE",
        "STRIP_MARKDOWN_ASTERISK_STYLES", "STRIP_MARKDOWN_UNDERSCORE_STYLES", "STRIP_MARKDOWN_STYLES",
        "REPLY_TO_TRIGGER", "QQ_MESSAGE_LIMIT", "HISTORY_TEXT_LIMIT", "CUSTOM_FACE_PROMPT_LIMIT",
        "ENABLE_REACTIONS", "ENABLE_AUTO_LIKE", "AUTO_LIKE_TIMES", "ENABLE_PREEMPT",
        "ENABLE_IMAGE_INPUT", "MAX_IMAGES_PER_TURN", "ENABLE_FILE_INPUT", "MAX_FILES_PER_TURN",
        "ENABLE_FORWARD_MSG_INPUT", "FORWARD_MSG_MAX_DEPTH", "FORWARD_MSG_MAX_MESSAGES",
        "FORWARD_MSG_TEXT_LIMIT", "ENABLE_QQ_QUEUE", "QQ_QUEUE_INTERVAL", "QQ_QUEUE_REPLY_TIMEOUT",
        "QQ_QUEUE_WORKERS", "QQ_QUEUE_WAIT_FOR_REPLY", "GROUP_HISTORY_MAX",
        "ENABLE_FORWARD_BRIDGE", "FORWARD_BRIDGE_MAX_MESSAGES",
    )

    def test_config_py_no_longer_defines_them(self) -> None:
        tree = ast.parse((_ADAPTER_ROOT / "config.py").read_text(encoding="utf-8"))
        defined = {
            target.id
            for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
            if isinstance(target, ast.Name)
        }

        assert sorted(defined & set(self._MIGRATED)) == []

    def test_the_plugin_no_longer_imports_them(self) -> None:
        tree = ast.parse((_ADAPTER_ROOT / "__init__.py").read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }

        assert sorted(imported & set(self._MIGRATED)) == []

    def test_every_migrated_key_has_a_live_counterpart(self, live: Any) -> None:
        """迁走一个键就要在声明表里补一个，不能只是删掉。"""
        assert len(live.LIVE_KEYS) >= len(self._MIGRATED) - 4  # 三个派生值 + 一个已废弃的旧总开关


class TestTheAdapterImportsOnlyWhatItUses:
    """未使用的 import 会让读代码的人以为某条通路还接着。"""

    # 看着没用但有消费方，删了就红：
    _RE_EXPORTED = {
        # tests/test_onebot_send_contract.py 通过 runtime 模块属性拿它。
        "OneBotAmbiguousAckError",
    }

    def test_no_module_level_import_is_unused(self) -> None:
        tree = ast.parse((_ADAPTER_ROOT / "__init__.py").read_text(encoding="utf-8"))
        bound = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound.add(alias.asname or alias.name)
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

        unused = bound - referenced - self._RE_EXPORTED - {"annotations"}

        assert unused == set(), f"这些 import 没有消费方: {sorted(unused)}"


class TestFileExtraAllowedExtensions:
    def test_default_is_empty(self, live: Any) -> None:
        assert live.live.file_extra_allowed_extensions == ()

    def test_yaml_list_is_parsed(self, live: Any) -> None:
        _write_yaml_text(live, "input:\n  file_extra_allowed_extensions: [ipynb]\n")
        assert live.live.file_extra_allowed_extensions == ("ipynb",)

"""认密钥、打码：日志、诊断导出、审批预览和调试工具的输出共用的那一套。

审批 details 进事件流、控制台和调试工具，模型和群友都看得到；藏少了是外泄，藏多了审批人看不出要批的是什么。
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.tool_step import summarize_result  # noqa: E402
from plugin_runtime.manager import _diagnostic as plugin_diagnostic  # noqa: E402
from plugin_runtime.manager import _secret_values as plugin_secret_values  # noqa: E402
from toolbox.redaction import (  # noqa: E402
    HIDDEN,
    SECRET_ENV_NAME,
    Hide,
    ValueHider,
    counts_as_secret,
    counts_token_usage,
    hide_level,
    hide_values,
    mask,
    mask_sensitive,
    redact,
    secret_values,
)


class TestMaskSensitive:
    def test_a_value_under_a_secret_key_is_hidden_whatever_it_looks_like(self) -> None:
        # 中转站的 key 可以是任意串，按前缀认不出来，只能看键名。
        assert mask_sensitive({"api_key": "relay-anything", "model": "gpt"}) == {"api_key": HIDDEN, "model": "gpt"}

    @pytest.mark.parametrize("key", [
        "API_KEY", "apiKey", "x-api-key", "access_token", "Authorization", "client_secret", "PASSWORD", "bearer",
    ])
    def test_secret_key_names_are_recognised_however_they_are_spelled(self, key: str) -> None:
        assert mask_sensitive({key: "value-123456"}) == {key: HIDDEN}

    def test_everything_below_a_secret_key_is_hidden(self) -> None:
        masked = mask_sensitive({
            "headers": {"Authorization": {"scheme": "Bearer", "value": "abc"}, "Accept": "application/json"},
            "tokens": ["t1", "t2"],
        })

        assert masked == {
            "headers": {"Authorization": {"scheme": HIDDEN, "value": HIDDEN}, "Accept": "application/json"},
            "tokens": [HIDDEN, HIDDEN],
        }

    def test_empty_values_flags_and_limits_stay_visible(self) -> None:
        # 空着的照实给，分得清是没配还是藏起来了；max_tokens 这种上限不会被当成密钥。
        value = {"api_key": "", "token": "   ", "max_tokens": 4096, "secret_enabled": True, "password": None}

        assert mask_sensitive(value) == value

    @pytest.mark.parametrize("key", [
        "password", "DB_PASSWORD", "passwd", "client_secret", "api_key", "x-api-key", "Authorization", "bearer",
    ])
    @pytest.mark.parametrize("number", [123456, 0, -7, 1234.5])
    def test_a_number_under_a_password_secret_or_key_is_hidden(self, key: str, number: float) -> None:
        # 口令常是纯数字，YAML 里读出来就是 int，只认字符串的话原样漏出去。
        assert mask_sensitive({key: number}) == {key: HIDDEN}

    @pytest.mark.parametrize("value", [True, False, None, "", "   "])
    def test_flags_none_and_blank_strings_under_those_keys_stay_visible(self, value: object) -> None:
        shown = {"password": value, "client_secret": value, "api_key": value}

        assert mask_sensitive(shown) == shown

    @pytest.mark.parametrize("key", ["max_tokens", "access_token", "token", "prompt_tokens", "TOKEN_LIMIT"])
    @pytest.mark.parametrize("number", [4096, 0.5])
    def test_a_number_under_a_token_key_stays_visible(self, key: str, number: float) -> None:
        # token 底下短于 6 位的数字多是上限和用量，藏了看的人就不知道配了多少。
        assert mask_sensitive({key: number}) == {key: number}

    def test_numbers_in_lists_below_a_secret_key_are_hidden(self) -> None:
        masked = mask_sensitive({
            "secret": {"pins": [1234, 5678.0]},
            "passwords": (111111, True, None),
            "env": {"DB_PASSWORD": 123456, "PORT": 5432},
            "tokens": [1, 2],
        })

        assert masked == {
            "secret": {"pins": [HIDDEN, HIDDEN]},
            "passwords": [HIDDEN, True, None],
            "env": {"DB_PASSWORD": HIDDEN, "PORT": 5432},
            "tokens": [1, 2],
        }

    def test_the_stricter_key_wins_however_they_are_nested(self) -> None:
        # 往下只升不降：api_key 底下的 max_tokens 也藏，token 底下的 password 照样藏。
        assert mask_sensitive({"api_key": {"max_tokens": 5}}) == {"api_key": {"max_tokens": HIDDEN}}
        assert mask_sensitive({"token": {"password": 1234, "ttl": 60}}) == {"token": {"password": HIDDEN, "ttl": 60}}

    def test_other_strings_still_go_through_redact(self) -> None:
        masked = mask_sensitive({"prompt": "用 sk-ABCDEFGHIJKLMNOP 调", "url": "https://u:p@relay.example/v1"})

        assert masked == {"prompt": "用 ******MNOP 调", "url": f"https://{HIDDEN}@relay.example/v1"}

    def test_a_secret_in_an_argument_list_is_caught_by_its_shape(self) -> None:
        # MCP 的 args、url 里也会夹令牌：键名帮不上忙时只能靠 redact 认形状。
        masked = mask_sensitive({"args": ["--token=abcdef123456", "--header", "Authorization: Bearer abcdef123456"]})

        assert "abcdef123456" not in str(masked)
        assert masked["args"][1] == "--header"

    def test_the_original_is_not_touched(self) -> None:
        original = {"api_key": "k-123456", "nested": {"token": "t-123456"}, "items": ["sk-ABCDEFGHIJKL"]}
        snapshot = copy.deepcopy(original)

        mask_sensitive(original)

        assert original == snapshot

    def test_tuples_and_sets_come_back_as_lists(self) -> None:
        assert mask_sensitive({"args": ("--port", "8080")}) == {"args": ["--port", "8080"]}
        assert mask_sensitive({"tokens": frozenset({"only-one"})}) == {"tokens": [HIDDEN]}

    def test_bytes_are_hidden_only_under_a_secret_key(self) -> None:
        assert mask_sensitive({"secret": b"raw"}) == {"secret": HIDDEN}
        assert mask_sensitive({"blob": b"raw"}) == {"blob": b"raw"}

    def test_keys_that_are_not_strings_are_kept(self) -> None:
        assert mask_sensitive({1: "sk-ABCDEFGHIJKLMNOP"}) == {1: "******MNOP"}

    def test_a_bare_string_or_scalar_is_handled_too(self) -> None:
        assert mask_sensitive("key sk-ABCDEFGHIJKLMNOP") == "key ******MNOP"
        assert mask_sensitive(None) is None
        assert mask_sensitive(3) == 3

    def test_the_hidden_marker_is_not_masked_again(self) -> None:
        assert mask_sensitive({"api_key": HIDDEN}) == {"api_key": HIDDEN}
        assert mask_sensitive(mask_sensitive({"url": "https://u:p@r.example"})) == {"url": f"https://{HIDDEN}@r.example"}


class TestUserinfoInAddresses:
    @pytest.mark.parametrize("url", [
        "https://alice:s3cret@relay.example/v1",
        "redis://:s3cret@cache:6379/0",
        "postgresql+psycopg://alice:s3cret@db/app",
        "ftp://alice@files.example/",
    ])
    def test_the_account_part_is_hidden_in_any_scheme(self, url: str) -> None:
        masked = redact(url)

        assert "s3cret" not in masked
        assert "alice" not in masked
        assert f"://{HIDDEN}@" in masked

    def test_the_host_and_path_stay_readable(self) -> None:
        assert redact("proxy http://bob:pw@10.0.0.2:7890/x failed") == f"proxy http://{HIDDEN}@10.0.0.2:7890/x failed"

    def test_an_address_without_an_account_is_left_alone(self) -> None:
        text = "see https://relay.example/v1/models and mail a@b.example"

        assert redact(text) == text

    def test_masking_twice_changes_nothing(self) -> None:
        once = redact("https://alice:s3cret@relay.example/v1 Bearer abcdef123456 token=relay-abcdefghij")

        assert redact(once) == once

    def test_the_hidden_marker_is_not_taken_for_a_value(self) -> None:
        # 就地打码不再看长度，打过码的样子得原样跳过，不然再过一遍就把标记和留下的尾巴也换掉
        assert redact(f"api_key={HIDDEN}") == f"api_key={HIDDEN}"
        assert redact("token=******abcd password=******") == "token=******abcd password=******"


class TestMask:
    @pytest.mark.parametrize("value", ["a", "abcd", "abcdef", "sk-ABCDEFGHIJKL"])
    def test_a_short_value_shows_neither_its_tail_nor_its_length(self, value: str) -> None:
        # 6 位的口令留 4 位尾巴，等于告诉人大半个口令
        assert mask(value) == "******"

    @pytest.mark.parametrize(("value", "shown"), [("0123456789abcdef", "******cdef"), ("sk-ABCDEFGHIJKLMNOP", "******MNOP")])
    def test_a_long_value_keeps_its_last_four_characters_to_tell_keys_apart(self, value: str, shown: str) -> None:
        assert mask(value) == shown


class TestRuleKeys:
    """讲长度、次数、上下限、时限、策略的配置项，底下是密钥的规则，不是密钥本身。"""

    @pytest.mark.parametrize("key", [
        "password_min_length", "passwordMinLength", "PASSWORD_MIN_LENGTH", "max_password_attempts",
        "secret_rotation_days", "authorization_timeout", "password_expire_days", "token_ttl", "api_key_max_age",
        "credential_expiry", "cookie_max_age", "min_password", "APIKeyTTL",
    ])
    def test_a_rule_key_stays_visible(self, key: str) -> None:
        assert hide_level(key) is Hide.NOTHING
        assert mask_sensitive({key: 8}) == {key: 8}
        assert mask_sensitive({key: "strict"}) == {key: "strict"}

    def test_a_policy_block_shows_its_rules_but_not_a_secret_kept_inside_it(self) -> None:
        masked = mask_sensitive({"password_policy": {"min_length": 8, "mode": "strict", "current_password": "hunter2xyz"}})

        assert masked == {"password_policy": {"min_length": 8, "mode": "strict", "current_password": HIDDEN}}

    @pytest.mark.parametrize("key", [
        "admin_password", "account_password", "password_history", "policy_secret", "password_hint", "client_secret_expires_at",
    ])
    def test_only_the_first_and_the_last_word_count(self, key: str) -> None:
        # admin 里的 min、account 里的 count 不算；history、hint 底下放的就是口令
        assert mask_sensitive({key: "value-123456"}) == {key: HIDDEN}

    def test_a_rule_key_below_a_secret_is_still_hidden(self) -> None:
        assert mask_sensitive({"api_key": {"max_tokens": 5, "min_length": 8}}) == {
            "api_key": {"max_tokens": HIDDEN, "min_length": HIDDEN},
        }

    @pytest.mark.parametrize("key", ["api_key_env", "token_url", "tokenizer", "password_file", "secret_type", "auth_header_token"])
    def test_keys_that_say_where_a_secret_lives_stay_hidden(self, key: str) -> None:
        assert mask_sensitive({key: "somewhere"}) == {key: HIDDEN}


class TestCookiesAndCredentials:
    def test_they_are_hidden_like_passwords(self) -> None:
        masked = mask_sensitive({
            "cookie": "sid=abc123", "credentials": {"user": "alice", "pass": "hunter2"}, "Set-Cookie": 1234567,
        })

        assert masked == {"cookie": HIDDEN, "credentials": {"user": HIDDEN, "pass": HIDDEN}, "Set-Cookie": HIDDEN}

    def test_a_cookie_header_in_a_log_line_is_masked(self) -> None:
        out = redact("Set-Cookie: session=abc123def; Path=/")

        assert "abc123def" not in out
        assert out.endswith("; Path=/")


class TestHideValues:
    def test_a_long_value_is_hidden_wherever_it_appears(self) -> None:
        assert hide_values("token=xxsecret-123456yy", ["secret-123456"]) == f"token=xx{HIDDEN}yy"

    def test_a_short_value_is_hidden_only_where_it_stands_alone(self) -> None:
        # 本地模型的占位 key 常是 c、1：按子串换会把 checks、id 这些键名也抹坏
        text = 'checks c x-c c-x 记c个 "c"'

        assert hide_values(text, ["c"]) == f'checks {HIDDEN} x-c c-x 记c个 "{HIDDEN}"'

    def test_the_longer_value_wins_where_two_overlap(self) -> None:
        assert hide_values("key abcdefgh end", ["abcdef", "abcdefgh"]) == f"key {HIDDEN} end"

    def test_a_marker_put_in_is_never_matched_again(self) -> None:
        assert hide_values("pw-long-secret 已隐藏", ["pw-long-secret", "已隐藏"]) == f"{HIDDEN} {HIDDEN}"

    def test_nothing_to_hide_leaves_the_text_alone(self) -> None:
        assert hide_values("abc", []) == "abc"
        assert hide_values("abc", [""]) == "abc"

    def test_a_hider_built_once_serves_many_texts(self) -> None:
        hider = ValueHider(["secret-123456", "k9"])

        assert hider("a secret-123456") == f"a {HIDDEN}"
        assert hider("key k9, relay-k9x") == f"key {HIDDEN}, relay-k9x"


class TestSecretValues:
    def test_strings_below_a_secret_key_are_collected_at_any_depth(self) -> None:
        found = secret_values({
            "providers": {"relay": {"api_key": "relay-key"}}, "auth": {"credentials": {"user": "alice"}}, "model": "gpt",
        })

        assert found == {"relay-key", "alice"}

    def test_numbers_are_judged_like_the_same_digits_written_as_text(self) -> None:
        # 口令档多短都收；token 档短于 6 位的不收；上限和开关不收
        found = secret_values({
            "password": 31415926, "pin_password": 1234, "token": 123456789, "bot_token": 42, "max_tokens": 200000,
            "secret_enabled": True,
        })

        assert found == {"31415926", "1234", "123456789"}

    @pytest.mark.parametrize("value", ["1", 1, "12345", 12345, "-7", 3.5])
    def test_a_short_number_under_a_token_key_is_not_collected(self, value: object) -> None:
        # 收进来按整词替换，导出里每个独立的 1 都会被抹掉
        assert secret_values({"token": value}) == set()

    @pytest.mark.parametrize(("value", "expected"), [("12", "12"), (12, "12"), ("0", "0")])
    def test_a_short_number_under_a_password_key_is_still_collected(self, value: object, expected: str) -> None:
        assert secret_values({"password": value}) == {expected}

    def test_rule_keys_are_left_out(self) -> None:
        # password_policy: strict 收进来，报告里每个 strict 都会被抹掉
        assert secret_values({"password_policy": "strict", "password_min_length": 123456}) == set()

    def test_the_token_after_an_auth_scheme_is_collected_on_its_own_too(self) -> None:
        assert secret_values({"Authorization": "Bearer tok-123456"}) == {"Bearer tok-123456", "tok-123456"}

    def test_a_reference_is_collected_as_what_it_resolves_to(self) -> None:
        resolved = {"${RELAY_KEY}": "relay-key"}

        found = secret_values({"api_key": "${RELAY_KEY}", "token": "${UNSET}"}, resolve=lambda raw: resolved.get(raw, ""))

        assert found == {"relay-key"}

    def test_a_caller_can_count_one_more_word(self) -> None:
        data = {"key": "plugin-key", "model": "m"}

        assert secret_values(data) == set()
        assert secret_values(data, also=re.compile("key", re.I)) == {"plugin-key"}


class TestTheConsumersAgree:
    """日志、诊断导出、审批预览、插件诊断和工具进度摘要按同一张表判：同一个键，几处结论一样。"""

    _VALUE = "relay-abcdefghij"

    @pytest.mark.parametrize(("key", "secret"), [
        ("api_key", True), ("x-api-key", True), ("db_password", True), ("client_secret", True), ("Authorization", True),
        ("bot_token", True), ("session_cookie", True), ("credentials", True),
        ("max_token", False), ("min_password", False), ("model", False),
    ])
    def test_every_consumer_reaches_the_same_verdict(self, key: str, secret: bool) -> None:
        assert (mask_sensitive({key: self._VALUE})[key] == HIDDEN) is secret
        assert (self._VALUE not in redact(f"{key}={self._VALUE}")) is secret
        assert (self._VALUE in secret_values({key: self._VALUE})) is secret
        assert (self._VALUE in plugin_secret_values({key: self._VALUE})) is secret
        assert ("<redacted>" in summarize_result("some_tool", "done", args={key: self._VALUE})) is secret


class TestCountsAsSecret:
    """各处收值、打码共用的一条判定：字符串和数字同一个口径。"""

    @pytest.mark.parametrize(("value", "level", "secret"), [
        ("12", Hide.TEXT, False), (12, Hide.TEXT, False), ("-7", Hide.TEXT, False), (3.5, Hide.TEXT, False),
        ("123456", Hide.TEXT, True), (123456, Hide.TEXT, True), ("abc", Hide.TEXT, True), ("k", Hide.TEXT, True),
        ("12", Hide.TEXT_AND_NUMBERS, True), (12, Hide.TEXT_AND_NUMBERS, True), (0, Hide.TEXT_AND_NUMBERS, True),
        ("on", Hide.TEXT_AND_NUMBERS, False), (" False ", Hide.TEXT, False), (True, Hide.TEXT_AND_NUMBERS, False),
        (None, Hide.TEXT, False), ("", Hide.TEXT, False), ("   ", Hide.TEXT_AND_NUMBERS, False),
        ("relay-key", Hide.NOTHING, False), (123456, Hide.NOTHING, False),
    ])
    def test_the_verdict(self, value: object, level: Hide, secret: bool) -> None:
        assert counts_as_secret(value, level) is secret

    def test_logs_mask_a_short_number_only_under_a_password_like_key(self) -> None:
        assert redact("token=12") == "token=12"
        assert redact("password=12") == "password=******"

    def test_mask_sensitive_treats_digits_as_text_and_numbers_alike(self) -> None:
        assert mask_sensitive({"token": "12", "bot_token": 12}) == {"token": "12", "bot_token": 12}
        assert mask_sensitive({"token": "123456789", "bot_token": 123456789}) == {"token": HIDDEN, "bot_token": HIDDEN}
        assert mask_sensitive({"password": "12", "pin_password": 12}) == {"password": HIDDEN, "pin_password": HIDDEN}

    @pytest.mark.parametrize(("key", "value"), [
        ("api_key", None), ("api_key", True), ("api_key", False), ("api_key", "on"), ("api_key", "OFF"), ("api_key", ""),
        ("token", "12"), ("token", 12), ("prompt_tokens", 152340), ("token_usage", 4096),
    ])
    def test_the_progress_summary_shows_what_mask_sensitive_shows(self, key: str, value: object) -> None:
        summary = summarize_result("some_tool", "done", args={key: value})

        assert "<redacted>" not in summary
        assert mask_sensitive({key: value}) == {key: value}

    @pytest.mark.parametrize(("key", "value"), [
        ("api_key", "relay-abcdefghij"), ("api_key", 12), ("api_key", "12"), ("password", "x"),
        ("access_token", 123456789), ("api_key", {"value": "relay-abcdefghij"}), ("tokens", ["abcdefgh"]),
    ])
    def test_the_progress_summary_hides_the_rest_under_a_secret_key(self, key: str, value: object) -> None:
        assert "<redacted>" in summarize_result("some_tool", "done", args={key: value})

    def test_the_progress_summary_names_the_value_it_shows(self) -> None:
        assert "prompt_tokens=152340" in summarize_result("some_tool", "done", args={"prompt_tokens": 152340})
        assert "token=12" in summarize_result("some_tool", "done", args={"token": "12"})


class TestTokenUsage:
    """键名的词里有复数 tokens 的是用量：底下 token 档的数字多长都照给，口令档照藏，字符串照旧按 token 档藏。"""

    @pytest.mark.parametrize(("key", "usage"), [
        ("prompt_tokens", True), ("total_tokens", True), ("budget_tokens", True), ("output_tokens_details", True),
        ("maxOutputTokens", True), ("PROMPT_TOKENS", True), ("tokens", True),
        ("token", False), ("access_token", False), ("tokenizer", False), ("token_usage", False), ("api_key", False),
    ])
    def test_which_keys_count_tokens(self, key: str, usage: bool) -> None:
        assert counts_token_usage(key) is usage

    @pytest.mark.parametrize("value", [152340, "152340", 4096])
    def test_a_usage_number_stays_visible(self, value: object) -> None:
        assert mask_sensitive({"prompt_tokens": value}) == {"prompt_tokens": value}

    def test_usage_below_a_token_key_stays_visible(self) -> None:
        value = {"token_usage": {"prompt_tokens": 152340, "output_tokens_details": {"reasoning_tokens": 123456}}}

        assert mask_sensitive(value) == value

    def test_a_plain_key_below_a_usage_key_counts_usage_too(self) -> None:
        assert mask_sensitive({"usage_tokens": {"prompt": 152340}}) == {"usage_tokens": {"prompt": 152340}}

    def test_a_long_number_under_a_credential_token_key_is_hidden(self) -> None:
        assert mask_sensitive({"access_token": 123456789}) == {"access_token": HIDDEN}
        assert mask_sensitive({"output_tokens_details": {"access_token": 123456789}}) == {
            "output_tokens_details": {"access_token": HIDDEN},
        }

    def test_a_password_tier_above_still_hides_the_numbers(self) -> None:
        assert mask_sensitive({"credentials": {"prompt_tokens": 152340}}) == {"credentials": {"prompt_tokens": HIDDEN}}
        assert secret_values({"credentials": {"prompt_tokens": 152340}}) == {"152340"}

    def test_strings_under_a_tokens_key_are_still_hidden(self) -> None:
        assert mask_sensitive({"tokens": ["abcdefgh"]}) == {"tokens": [HIDDEN]}
        assert secret_values({"tokens": ["abcdefgh"]}) == {"abcdefgh"}

    def test_usage_numbers_are_not_collected(self) -> None:
        assert secret_values({"budget_tokens": 128000, "token_usage": {"total_tokens": "152340"}}) == set()


class TestSwitchValues:
    """挂在密钥键下、写成文字的开关不是密钥：.env 的值全是字符串，qq.yaml 也把 on/off/yes/no 留成文字。"""

    @pytest.mark.parametrize("line", [
        "allow_credentials=true",
        "password: off",
        'api_key="On"',
        "token = yes",
        "client_secret=FALSE",
        "credentials: no",
        '{"cookie": "true"}',
    ])
    def test_a_switch_written_as_text_is_left_alone_in_logs(self, line: str) -> None:
        assert redact(line) == line

    def test_only_a_whole_switch_word_counts(self) -> None:
        assert redact("password=trueblue") == "password=******"
        assert redact("api_key=on-call-key") == "api_key=******"

    @pytest.mark.parametrize("value", ["on", " true ", "OFF", "No"])
    def test_a_switch_is_not_collected_for_value_hiding(self, value: str) -> None:
        assert secret_values({"api_key": value}) == set()
        assert secret_values({"cookies": [value], "password": value}) == set()

    def test_a_reference_that_resolves_to_a_switch_is_not_collected(self) -> None:
        assert secret_values({"api_key": "${FEATURE_FLAG}"}, resolve=lambda raw: "on") == set()

    def test_mask_sensitive_keeps_a_switch_like_it_keeps_a_bool(self) -> None:
        assert mask_sensitive({"password": "false", "api_key": " On ", "token": True}) == {
            "password": "false", "api_key": " On ", "token": True,
        }
        assert mask_sensitive({"password": "falsehood"}) == {"password": HIDDEN}

    def test_hiding_by_value_leaves_every_standalone_switch_word(self) -> None:
        text = "cors allow_credentials=true, websocket on, retry yes"

        assert hide_values(text, secret_values({"api_key": "on", "secret": "yes", "password": "true"})) == text


class TestPluginDiagnostics:
    """插件出错的原文进控制台的插件页：配置里的密钥按值藏，报错里认得出的密钥形状再过一遍 redact。"""

    @staticmethod
    def _message(config: dict, error: str) -> str:
        return plugin_diagnostic("setup", ValueError(error), plugin_secret_values(config))["message"]

    def test_a_short_value_under_a_bare_key_is_hidden_only_where_it_stands_alone(self) -> None:
        # 插件设置常把凭据就叫 key；三个字符的值按子串换会把别的词啃掉一截
        message = self._message({"key": "k9z"}, "rejected k9z, see k9zebra")

        assert message == f"rejected {HIDDEN}, see k9zebra"

    def test_cookie_and_credential_values_are_hidden(self) -> None:
        message = self._message(
            {"session_cookie": "sid-abcdef", "credentials": "user:pass-123456"},
            "login failed with sid-abcdef as user:pass-123456",
        )

        assert "sid-abcdef" not in message
        assert "pass-123456" not in message

    def test_a_numeric_password_is_hidden_but_a_limit_is_not(self) -> None:
        message = self._message({"password": 31415926, "max_tokens": 409600}, "pin 31415926 rejected, max 409600")

        assert "31415926" not in message
        assert "409600" in message

    def test_a_switch_under_a_bare_key_is_not_taken_for_a_secret(self) -> None:
        config = {"key": "on", "api_key": "true", "token": "Off"}

        assert plugin_secret_values(config) == set()
        assert self._message(config, "feature on, verbose true, cache off") == "feature on, verbose true, cache off"

    def test_the_token_after_an_auth_scheme_is_hidden_on_its_own(self) -> None:
        message = self._message({"headers": {"Authorization": "Bearer tok-abcdef"}}, "401 for tok-abcdef")

        assert "tok-abcdef" not in message

    def test_a_secret_shaped_assignment_in_the_error_is_masked_without_any_config(self) -> None:
        message = plugin_diagnostic("request", RuntimeError("upstream said token=abcdef123456 is revoked"))["message"]

        assert "abcdef123456" not in message
        assert "is revoked" in message


class TestSecretEnvNames:
    @pytest.mark.parametrize(("name", "matched"), [
        ("OPENAI_API_KEY", True), ("SOME_APIKEY", True), ("BOT_TOKEN", True), ("APP_SECRET", True),
        ("DB_PASSWORD", True), ("bot_token", True),
        ("TOKEN_LIMIT", False), ("DB_PASSWD", False), ("HTTP_AUTHORIZATION", False), ("SESSION_COOKIE", False),
        ("EXA_BASE_URL", False),
    ])
    def test_the_diagnostics_export_keeps_judging_env_names_by_their_ending(self, name: str, matched: bool) -> None:
        # 按后缀收进程环境的口径维持原样，PASSWD、AUTHORIZATION、COOKIE 结尾的照旧不收
        assert bool(SECRET_ENV_NAME.search(name)) is matched

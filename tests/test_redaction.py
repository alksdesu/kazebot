"""认密钥、打码：日志、诊断导出、审批预览和调试工具的输出共用的那一套。

审批 details 进事件流、控制台和调试工具，模型和群友都看得到；藏少了是外泄，藏多了审批人看不出要批的是什么。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.redaction import HIDDEN, mask_sensitive, redact  # noqa: E402


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
        # token 底下的数字多是上限和用量，藏了看的人就不知道配了多少。
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
        masked = mask_sensitive({"prompt": "用 sk-ABCDEFGHIJKL 调", "url": "https://u:p@relay.example/v1"})

        assert masked == {"prompt": "用 ******IJKL 调", "url": f"https://{HIDDEN}@relay.example/v1"}

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
        assert mask_sensitive({1: "sk-ABCDEFGHIJKL"}) == {1: "******IJKL"}

    def test_a_bare_string_or_scalar_is_handled_too(self) -> None:
        assert mask_sensitive("key sk-ABCDEFGHIJKL") == "key ******IJKL"
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

    def test_the_hidden_marker_is_too_short_to_look_like_a_value(self) -> None:
        assert redact(f"api_key={HIDDEN}") == f"api_key={HIDDEN}"

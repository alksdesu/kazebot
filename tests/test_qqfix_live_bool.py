"""热载配置的布尔开关和 clonoth_runtime.parse_bool 认同一张真假词表；空串仍算「关」。"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from adapters._shared.live_config import coerce_bool
from clonoth_runtime import parse_bool

_WORDS = ["1", "true", "yes", "y", "on", "0", "false", "no", "n", "off", "TRUE", " On ", "N"]


@pytest.mark.parametrize("word", _WORDS)
@pytest.mark.parametrize("default", [True, False])
def test_the_word_table_is_the_runtime_one(word: str, default: bool) -> None:
    assert parse_bool(word) is not None
    assert coerce_bool(word, default) is parse_bool(word)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_value_switches_it_off(raw: Any) -> None:
    # env 设成空串就是关掉这个开关，通用解析会把它当成没写。
    assert parse_bool(raw) is None
    assert coerce_bool(raw, True) is False


@pytest.mark.parametrize("default", [True, False])
def test_an_unknown_word_keeps_the_default_and_says_so(default: bool, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="adapters._shared.live_config"):
        assert coerce_bool("maybe", default) is default

    assert "unrecognized boolean" in caplog.text


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), (1, True), (0, False)])
def test_yaml_scalars_pass_through(raw: Any, expected: bool) -> None:
    assert coerce_bool(raw, not expected) is expected

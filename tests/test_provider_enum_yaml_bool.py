"""YAML 把裸 off/on 读成布尔，ENUM 档位要按字面还原。

`safety_threshold: off` 是最容易踩的一处：PyYAML 给出 False，旧逻辑 str() 成
"false" 匹配不上档位，于是静默回落 default —— 配置写着关掉过滤，实际全开。
"""
from __future__ import annotations

import pytest
import yaml

from providers.options import ENUM, OptionSpec

SAFETY = OptionSpec(
    key="safety_threshold", label="安全过滤", kind=ENUM, default="default",
    choices=(("default", "跟随默认"), ("block_none", "不拦"), ("off", "整个关掉")),
)


def test_yaml_really_turns_bare_off_into_a_bool():
    """前提复核：这条不成立的话下面的还原就没有意义。"""
    assert yaml.safe_load("safety_threshold: off")["safety_threshold"] is False


def test_bare_off_in_yaml_selects_the_off_choice():
    loaded = yaml.safe_load("safety_threshold: off")["safety_threshold"]

    assert SAFETY.coerce(loaded) == "off"


def test_quoted_off_still_works():
    assert SAFETY.coerce("off") == "off"


def test_a_bool_without_a_matching_choice_falls_back():
    """选项里没有 on/off 档时不硬凑，照旧回落默认值。"""
    spec = OptionSpec(key="mode", label="模式", kind=ENUM, default="auto",
                      choices=(("auto", "自动"), ("level", "分档")))

    assert spec.coerce(False) == "auto"
    assert spec.coerce(True) == "auto"


@pytest.mark.parametrize("raw,expected", [("OFF", "off"), (" off ", "off"), ("nope", "default")])
def test_text_values_are_normalised_as_before(raw, expected):
    assert SAFETY.coerce(raw) == expected

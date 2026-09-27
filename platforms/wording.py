"""平台名接中文时隔不隔空格：名字以拉丁字母或数字结尾隔一个，以汉字结尾直接连上。控制台的 nameWith 是同一个规矩。"""
from __future__ import annotations

import re

# 范围和控制台那边保持一致，两边拼出来的句子才一样：CJK 标点、汉字（含扩展区）、全角符号。
_CJK_CHAR = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯\U00020000-\U0003134f]")


def name_with(name: str, text: str) -> str:
    """名字后面接一句中文：「NapCat 没有热载配置」「保险库没有热载配置」。"""
    if not text:
        return name
    last = name[-1:]
    return f"{name}{text}" if not last or _CJK_CHAR.match(last) else f"{name} {text}"

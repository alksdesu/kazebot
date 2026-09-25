from __future__ import annotations

import math
import re


_DIGITS = {character: value for value, character in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "两": 2})
_POWERS = {"十": 10, "百": 100, "千": 1000}
_UNITS = {"天": 86400, "小时": 3600, "时": 3600, "分钟": 60, "分": 60, "秒钟": 1, "秒": 1}
_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+|半)"
_DURATION_PART = re.compile(rf"({_NUMBER})\s*(个)?\s*(半)?\s*(小时|分钟|秒钟|天|时|分|秒)")


def _number(text: str) -> float | None:
    if text == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total, pending, previous_power, zero_after_power = 0, None, 10000, False
    for character in text:
        if character in _DIGITS:
            value = _DIGITS[character]
            if pending is not None and pending != 0:
                return None
            pending = value
            zero_after_power = zero_after_power or value == 0
        elif character in _POWERS:
            power = _POWERS[character]
            if power >= previous_power or pending == 0 or (pending is None and (power != 10 or total)):
                return None
            total += (pending if pending is not None else 1) * power
            pending, previous_power, zero_after_power = None, power, False
        else:
            return None
    # “一百五”可能表示 105 或 150，不能替用户选择。
    if total and pending and previous_power > 10 and not zero_after_power:
        return None
    return float(total + (pending or 0))


def parse_duration_seconds(text: str) -> float | None:
    value = str(text or "").strip()
    if not value or len(value) > 128:
        return None
    if value in {"一刻钟", "一刻"}:
        return 900.0
    position, total, previous_unit = 0, 0.0, float("inf")
    parts, last_fractional = 0, False
    for match in _DURATION_PART.finditer(value):
        if value[position:match.start()].strip():
            return None
        number = _number(match[1])
        unit = _UNITS[match[4]]
        if number is None or unit >= previous_unit or (match[3] and (not match[2] or match[1] == "半" or "." in match[1])):
            return None
        if match[2] and match[4] not in {"小时", "时"}:
            return None
        total += (number + (0.5 if match[3] else 0)) * unit
        last_fractional = number % 1 != 0 or bool(match[3])
        position, previous_unit, parts = match.end(), unit, parts + 1
    tail = value[position:].strip()
    if tail == "半" and parts and not last_fractional:
        total += previous_unit / 2
    elif tail:
        return None
    return total if parts and math.isfinite(total) else None


def canonicalize_quiet_command(text: str) -> str:
    original = str(text or "")
    value = original.strip().replace("／", "/", 1).rstrip("。！!").strip()
    if value in {"恢复", "/恢复"}:
        return "/恢复"
    if value in {"安静状态", "/安静状态"}:
        return "/安静状态"
    match = re.fullmatch(r"/?(安静|旁听)\s*(.*)", value)
    if not match:
        return original
    duration = match[2].strip()
    if not duration:
        return f"/{match[1]}" if value.startswith("/") else original
    seconds = parse_duration_seconds(duration)
    if seconds is None:
        return original
    return f"/{match[1]} {seconds:g}秒"

"""表情包的本地粗排。

两条要紧的：闲聊时必须压分（宁可不发也不要乱发），刚发过的必须排除（同一张反复刷最像机器人）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from stickers import rank as sr


def _item(name: str, tags: list[str], *, digest: str = "", sent: int = 0):
    return SimpleNamespace(
        sha256=digest or f"{name}-hash", name=name, tags=tags, sent_count=sent,
    )


# ── 标签命中 ──

def test_exact_tag_in_text_scores_high() -> None:
    score, hints = sr.score_one("今天好开心", name="猫", tags=["开心"])
    assert score >= sr._STRONG_TAG
    assert "开心" in hints


def test_long_tag_matches_by_substring() -> None:
    # 标签"开心猫猫"，正文只说了"开心"，也该算命中。
    score, _ = sr.score_one("今天好开心", name="x", tags=["开心猫猫"])
    assert score >= sr._PARTIAL_TAG


def test_two_char_tag_needs_a_full_match() -> None:
    # 两字标签再拆就是单字，单字子串误命中率太高。
    score, _ = sr.score_one("我在看地图", name="x", tags=["开心"])
    assert score == 0.0


def test_unrelated_text_scores_zero() -> None:
    score, hints = sr.score_one("明天几点开会", name="猫", tags=["哭泣", "伤心"])
    assert score == 0.0 and hints == []


def test_type_tags_barely_count() -> None:
    # 几乎每张图都带"表情包"，命中它不说明语境对得上。
    typed, _ = sr.score_one("发个表情包", name="x", tags=["表情包"])
    real, _ = sr.score_one("我好开心", name="x", tags=["开心"])
    assert typed < real


def test_more_hits_score_higher() -> None:
    one, _ = sr.score_one("开心", name="x", tags=["开心"])
    two, _ = sr.score_one("开心又想哭", name="x", tags=["开心", "哭"])
    assert two > one


def test_name_in_text_counts() -> None:
    score, hints = sr.score_one("来个猫猫", name="猫猫", tags=[])
    assert score >= sr._NAME_HIT
    assert any("名字" in hint for hint in hints)


def test_empty_text_scores_zero() -> None:
    assert sr.score_one("", name="猫", tags=["开心"]) == (0.0, [])


def test_blank_tags_are_skipped() -> None:
    score, _ = sr.score_one("开心", name="x", tags=["", "   ", "开心"])
    assert score >= sr._STRONG_TAG


# ── 意图组 ──

@pytest.mark.parametrize("text,intent", [
    ("哈哈哈笑死我了", "positive"),
    ("好难过想哭", "sad"),
    ("气死我了", "angry"),
    ("这也太离谱了", "shock"),
    ("谢谢你", "thanks"),
    ("对不起我错了", "apology"),
    ("困死了想睡觉", "tired"),
    ("好饿想干饭", "hungry"),
    ("早上好", "greet"),
])
def test_intents_are_detected(text: str, intent: str) -> None:
    assert intent in sr.detect_intents(text)


def test_plain_text_has_no_intent() -> None:
    assert sr.detect_intents("明天下午三点开会") == []


def test_intent_lifts_matching_tags() -> None:
    # 正文说"笑死"，图片标签是"大笑"——字面对不上，但意图对得上。
    score, hints = sr.score_one("哈哈哈笑死", name="x", tags=["大笑"])
    assert score >= sr._INTENT_BASE
    assert any("positive" in hint for hint in hints)


def test_intent_does_not_lift_unrelated_tags() -> None:
    score, _ = sr.score_one("哈哈哈笑死", name="x", tags=["伤心"])
    assert score == 0.0


# ── 疲劳与排除 ──

def test_frequently_sent_images_rank_lower() -> None:
    fresh, _ = sr.score_one("开心", name="x", tags=["开心"], sent_count=0)
    worn, _ = sr.score_one("开心", name="x", tags=["开心"], sent_count=10)
    assert worn < fresh


def test_fatigue_never_beats_relevance() -> None:
    # 疲劳只做微调：一张高度相关但常发的图，仍该赢过毫不相关的冷门图。
    relevant, _ = sr.score_one("开心", name="x", tags=["开心"], sent_count=99)
    assert relevant > 0


def test_recently_sent_are_excluded_entirely() -> None:
    items = [_item("甲", ["开心"], digest="d1"), _item("乙", ["开心"], digest="d2")]
    result = sr.rank("好开心", items, recently_sent={"d1"})
    assert [row.sha256 for row in result.items] == ["d2"]


def test_excluding_everything_yields_nothing() -> None:
    # 全被排除时就是不发图，这是对的：宁缺毋滥。
    items = [_item("甲", ["开心"], digest="d1")]
    assert sr.rank("好开心", items, recently_sent={"d1"}).items == []


def test_items_without_a_digest_are_skipped() -> None:
    assert sr.rank("开心", [SimpleNamespace(sha256="", name="x", tags=["开心"])]).items == []


# ── 闲聊闸门 ──

def test_generic_chatter_is_capped() -> None:
    # 没有任何意图也没有标签命中，就是闲聊，分数压到阈值以下。
    items = [_item("甲", ["开心"])]
    result = sr.rank("明天下午三点在哪开会", items)
    assert result.generic
    assert all(row.score <= sr._GENERIC_CEILING for row in result.items)


def test_clear_intent_is_not_capped() -> None:
    items = [_item("甲", ["开心"])]
    result = sr.rank("哈哈哈笑死我了", items)
    assert not result.generic
    assert result.best is not None and result.best.score > sr._GENERIC_CEILING


def test_direct_tag_hit_is_not_generic() -> None:
    items = [_item("甲", ["猫猫"])]
    result = sr.rank("给我看猫猫", items)
    assert not result.generic


# ── 排序与截断 ──

def test_best_is_the_highest_scorer() -> None:
    items = [_item("弱", ["照片"]), _item("强", ["开心", "大笑"])]
    result = sr.rank("哈哈开心", items)
    assert result.best is not None and result.best.name == "强"


def test_ranking_is_deterministic() -> None:
    # 同分时顺序必须稳定，否则同一句话每次选出不同的图，没法复现问题。
    items = [_item("乙", ["开心"]), _item("甲", ["开心"])]
    first = [row.name for row in sr.rank("开心", items).items]
    second = [row.name for row in sr.rank("开心", list(reversed(items))).items]
    assert first == second


def test_limit_truncates() -> None:
    items = [_item(f"图{i}", ["开心"]) for i in range(20)]
    assert len(sr.rank("开心", items, limit=5).items) == 5


def test_zero_limit_yields_nothing() -> None:
    assert sr.rank("开心", [_item("甲", ["开心"])], limit=0).items == []


def test_empty_library_is_handled() -> None:
    result = sr.rank("开心", [])
    assert result.items == [] and result.best is None


# ── 给模型看的候选行 ──

def test_prompt_lines_carry_tags() -> None:
    result = sr.rank("开心", [_item("甲", ["开心", "猫"])])
    line = sr.prompt_lines(result)[0]
    assert "甲" in line and "开心" in line


def test_prompt_lines_handle_untagged_items() -> None:
    result = sr.rank("甲", [_item("甲", [])])
    assert sr.prompt_lines(result) == ["甲（无标签）"]


def test_prompt_lines_cap_tag_count() -> None:
    # 候选行会乘以 N 张图进提示词，标签不截断就是 token 炸弹。
    result = sr.rank("开心", [_item("甲", [f"标签{i}" for i in range(20)] + ["开心"])])
    assert sr.prompt_lines(result)[0].count("、") <= 5

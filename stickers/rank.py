"""表情包检索的本地粗排。

拿标签去正文里找，不切查询词 —— 中文没有空格，剔停用词会把含停用词子串的实词切碎。
零平台依赖，可脱 NoneBot 单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# 线索词出现在正文里，就把这一组的标签当成想要的方向。
_INTENT_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "positive": {
        "cues": ("哈哈", "笑死", "好笑", "开心", "快乐", "爽", "赞", "牛", "好耶", "嘻嘻"),
        "tags": ("笑", "大笑", "开心", "快乐", "搞笑", "欢乐", "可爱", "赞", "牛"),
    },
    "sad": {
        "cues": ("难过", "伤心", "哭", "委屈", "破防", "emo", "悲", "心酸"),
        "tags": ("哭", "流泪", "伤心", "难过", "委屈", "破防", "悲伤"),
    },
    "angry": {
        "cues": ("生气", "气死", "愤怒", "烦", "火大", "离谱", "有病"),
        "tags": ("生气", "愤怒", "怒", "不满", "烦躁", "吐槽"),
    },
    "shock": {
        "cues": ("震惊", "惊讶", "离谱", "不懂", "懵", "啊？", "啊?", "什么鬼", "?"),
        "tags": ("震惊", "惊讶", "疑惑", "懵", "离谱", "问号"),
    },
    "awkward": {
        "cues": ("尴尬", "无语", "沉默", "汗", "绷不住", "呃"),
        "tags": ("尴尬", "无语", "沉默", "流汗", "汗", "吐槽"),
    },
    "thanks": {
        "cues": ("谢谢", "感谢", "辛苦", "爱你", "谢了"),
        "tags": ("谢谢", "感谢", "鞠躬", "比心", "爱心", "可爱"),
    },
    "apology": {
        "cues": ("抱歉", "对不起", "不好意思", "我错了", "错了"),
        "tags": ("抱歉", "对不起", "道歉", "鞠躬", "委屈"),
    },
    "agree": {
        "cues": ("可以", "没问题", "好呀", "同意", "支持", "确实", "对对对"),
        "tags": ("可以", "没问题", "点头", "赞", "支持", "ok"),
    },
    "reject": {
        "cues": ("不要", "不行", "拒绝", "算了", "别", "不至于"),
        "tags": ("拒绝", "不行", "不要", "摇头", "无语"),
    },
    "tired": {
        "cues": ("累", "困", "疲惫", "熬夜", "摆烂", "躺平"),
        "tags": ("累", "困", "疲惫", "睡觉", "躺平", "摆烂"),
    },
    "hungry": {
        "cues": ("饿", "吃饭", "好吃", "馋", "干饭"),
        "tags": ("吃", "干饭", "馋", "美食", "流口水"),
    },
    "greet": {
        "cues": ("早上好", "晚安", "早", "在吗", "你好", "hi", "hello"),
        "tags": ("打招呼", "挥手", "早安", "晚安", "睡觉"),
    },
}

# 这些标签描述的是图的种类而不是情绪，命中了不说明语境对得上。
_TYPE_TAGS = frozenset({"表情包", "照片", "图片", "梗图", "截图", "头像", "壁纸", "未分类"})

_STRONG_TAG = 9.0
_PARTIAL_TAG = 4.0
_NAME_HIT = 6.0
_INTENT_BASE = 4.5
_TYPE_TAG_HIT = 0.5
_HIT_BONUS = 2.0
_MAX_HIT_BONUS = 4
_INTENT_BONUS = 1.2
_MAX_INTENT_BONUS = 3
# 泛泛而谈时把分数压到阈值以下：宁可不发，也不要在闲聊里乱发图。
_GENERIC_CEILING = 1.0
# 常发的图轮转一下，同一张反复出现最像机器人。
_FATIGUE_STEP = 0.05
_MAX_FATIGUE = 10

_CJK = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class Ranked:
    sha256: str
    name: str
    tags: list[str]
    score: float
    hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RankResult:
    items: list[Ranked]
    generic: bool
    intents: list[str]

    @property
    def best(self) -> Ranked | None:
        return self.items[0] if self.items else None


def _normalize(text: str) -> str:
    return str(text or "").strip().lower()


def detect_intents(text: str) -> list[str]:
    """正文命中了哪些意图组。"""
    lowered = _normalize(text)
    if not lowered:
        return []
    return [
        key for key, spec in _INTENT_GROUPS.items()
        if any(cue in lowered for cue in spec["cues"])
    ]


def _intent_tags(intents: Iterable[str]) -> dict[str, set[str]]:
    return {key: set(_INTENT_GROUPS[key]["tags"]) for key in intents if key in _INTENT_GROUPS}


def _tag_hits(lowered: str, tags: Sequence[str]) -> tuple[float, int, list[str]]:
    score = 0.0
    hits = 0
    hints: list[str] = []
    for raw in tags:
        tag = _normalize(raw)
        if not tag:
            continue
        if tag in lowered:
            # 种类标签几乎每张图都有，命中它不代表语境对上了。
            score += _TYPE_TAG_HIT if raw in _TYPE_TAGS else _STRONG_TAG
            if raw not in _TYPE_TAGS:
                hits += 1
                hints.append(raw)
            continue
        # 只对多字中文标签做部分匹配："开心猫" 命中 "开心"。单字太容易误命中。
        if len(tag) >= 2 and _CJK.search(tag):
            for size in range(len(tag) - 1, 1, -1):
                if any(tag[at : at + size] in lowered for at in range(len(tag) - size + 1)):
                    score += _PARTIAL_TAG
                    hits += 1
                    hints.append(raw)
                    break
    return score, hits, hints


def score_one(
    text: str,
    *,
    name: str,
    tags: Sequence[str],
    sent_count: int = 0,
    intents: Sequence[str] | None = None,
) -> tuple[float, list[str]]:
    """一张图和这段正文有多搭。返回分数和命中理由。"""
    lowered = _normalize(text)
    if not lowered:
        return 0.0, []

    score, hits, hints = _tag_hits(lowered, tags)

    clean_name = _normalize(name)
    if clean_name and clean_name in lowered:
        score += _NAME_HIT
        hints.append(f"名字:{name}")

    tag_set = {_normalize(tag) for tag in tags}
    groups = _intent_tags(intents if intents is not None else detect_intents(text))
    for key, wanted in groups.items():
        overlap = tag_set & {_normalize(tag) for tag in wanted}
        if overlap:
            score += _INTENT_BASE + min(len(overlap), _MAX_INTENT_BONUS) * _INTENT_BONUS
            hints.append(f"{key}:{len(overlap)}")

    score += min(hits, _MAX_HIT_BONUS) * _HIT_BONUS
    # 疲劳只做微调，不该让高度相关的图输给一张不相关的冷门图。
    score -= min(max(int(sent_count), 0), _MAX_FATIGUE) * _FATIGUE_STEP
    return max(score, 0.0), hints


def rank(
    text: str,
    items: Iterable[Any],
    *,
    recently_sent: frozenset[str] | set[str] = frozenset(),
    limit: int = 10,
) -> RankResult:
    """粗排出候选。items 里每个对象要有 sha256/name/tags/sent_count。

    刚发过的直接排除而不是降权 —— 留在候选里模型照样会挑，等于没防。
    """
    intents = detect_intents(text)
    scored: list[Ranked] = []
    for item in items:
        digest = str(getattr(item, "sha256", "") or "")
        if not digest or digest in recently_sent:
            continue
        tags = list(getattr(item, "tags", ()) or ())
        name = str(getattr(item, "name", "") or "")
        value, hints = score_one(
            text, name=name, tags=tags,
            sent_count=int(getattr(item, "sent_count", 0) or 0),
            intents=intents,
        )
        if value <= 0:
            continue
        scored.append(Ranked(digest, name, tags, value, hints))

    # 分数并列时按发送次数升序，让冷门的先出场；再按名字保证顺序稳定可测。
    scored.sort(key=lambda row: (-row.score, row.name))
    generic = not intents and not any(row.hints for row in scored)
    if generic:
        scored = [
            Ranked(row.sha256, row.name, row.tags, min(row.score, _GENERIC_CEILING), row.hints)
            for row in scored
        ]
    return RankResult(scored[: max(int(limit), 0)], generic, intents)


def prompt_lines(result: RankResult) -> list[str]:
    """给模型看的候选行。带上标签，模型才判断得出语境合不合。"""
    return [
        f"{row.name}（{('、'.join(row.tags[:6])) or '无标签'}）"
        for row in result.items
    ]

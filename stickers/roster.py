"""挑表情包：每轮摆给模型的清单，和按名字、按标签找图。

图发不发得出去（文件在不在、大小合不合平台）、发出去怎么记账，都归平台。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from .store import Sticker, StickerStore

_ENTRY_TAGS = 8


@dataclass(frozen=True)
class RosterRules:
    # 清单最多列几张；0 表示从不给。
    limit: int = 60
    # 每轮多大概率把清单摊给模型。
    probability: float = 0.2
    # 同一会话这段时间内发过的图不进清单，也不会被按名字挑中。
    repeat_window_sec: int = 1800


def offered(rules: RosterRules, roll: float) -> bool:
    """这一轮给不给清单。roll 是调用方掷的 [0, 1) 骰子，判定是 roll < probability。"""
    return rules.limit > 0 and roll < rules.probability


def prompt_roster(
    store: StickerStore, conversation_key: str, rules: RosterRules, *, sendable: Callable[[Sticker], bool],
) -> list[Sticker]:
    """这一轮摆给模型看的表情包。

    不按群友那句话预筛。表情包配的是 bot 自己要回的语气，而候选是在模型开口之前
    就得定下来的 —— 拿对方说的话去筛，筛出来的情绪往往正好是反的。清单直接摊开，
    让唯一同时知道「对方说了什么」和「我要回什么」的角色去挑。
    """
    recent = store.recently_sent(conversation_key, within_sec=rules.repeat_window_sec)
    usable = [row for row in store.all_usable() if row.sha256 not in recent and sendable(row)]
    # 装不下就让发得最少的先上，轮着来；否则冷门图永远排在字典序后面没人见过。
    usable.sort(key=lambda row: (row.sent_count, row.name))
    return usable[:rules.limit]


def roster_entry(row: Sticker) -> str:
    return f"{row.name}（{'、'.join(row.tags[:_ENTRY_TAGS]) or '无标签'}）"


def candidates(store: StickerStore, name: str, *, skip: set[str]) -> Iterator[Sticker]:
    """名字对上的先来，再按标签一张张退让。给出去的记进 skip，换下一张时不会再轮到它。

    名单外的词照样能用：模型想表达的情绪未必对得上某张图的名字，但多半对得上它的某个标签。
    """
    row = store.by_name(name)
    while True:
        if row is not None and row.sha256 not in skip:
            skip.add(row.sha256)
            yield row
        row = store.by_tag(name, exclude=skip)
        if row is None:
            return

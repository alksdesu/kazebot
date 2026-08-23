"""模型自己挑表情包这条路。

原先是拿群友那句话去匹配标签，筛完才给模型看。可表情包配的是 bot 自己要回的语气，
候选却得在模型开口之前定下来 —— 对方说「考试挂了」筛出一堆哭脸，bot 想回「哈哈活该」，
要的是嘲笑。现在清单直接摊开，挑谁由模型定；它写的词不在名单里就去标签里找。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers.store import CAPTION_DONE, CAPTION_PENDING, STATE_LIBRARY, StickerStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> StickerStore:
    return StickerStore(tmp_path / "stickers.sqlite3")


def _add(store: StickerStore, name: str, tags: list[str], *, sent: int = 0) -> str:
    """走真实入库路径落一张打好标的在库图。手写 INSERT 会在 schema 变动时静默漂移。"""
    digest = f"sha-{name}"
    store.add(
        sha256=digest, rel_path=f"data/stickers/library/{name}.gif",
        source="test", state=STATE_LIBRARY, name=name,
    )
    store.set_auto_tags(digest, tags, version=1)
    store.mark_caption(digest, CAPTION_DONE)
    for _ in range(sent):
        store.record_sent("qq_group:t", digest)
    return digest


class TestByTag:
    def test_精确等于的标签优先于只是被包含(self, store: StickerStore) -> None:
        _add(store, "含混", ["大笑不止"])
        _add(store, "正解", ["大笑"])

        assert store.by_tag("大笑").name == "正解"

    def test_没有精确命中时退回包含(self, store: StickerStore) -> None:
        _add(store, "甲", ["塔菲唐笑", "坏笑"])

        # 「唐笑」是「塔菲唐笑」的一截 —— 模型想不到那个全名，但想得到这半个。
        assert store.by_tag("唐笑").name == "甲"

    def test_同档里发得最少的先出场(self, store: StickerStore) -> None:
        _add(store, "老面孔", ["无语"], sent=9)
        _add(store, "生面孔", ["无语"], sent=0)

        assert store.by_tag("无语").name == "生面孔"

    def test_刚发过的排除在外(self, store: StickerStore) -> None:
        hot = _add(store, "刚发过", ["无语"], sent=0)
        _add(store, "备胎", ["无语"], sent=5)

        assert store.by_tag("无语", exclude={hot}).name == "备胎"

    def test_一个都不沾边就不发(self, store: StickerStore) -> None:
        _add(store, "甲", ["大笑"])

        # 宁可不发也不硬塞：塞一张不相干的图比不发更糟。
        assert store.by_tag("悲伤") is None

    def test_空词不返回任何图(self, store: StickerStore) -> None:
        _add(store, "甲", ["大笑"])

        assert store.by_tag("") is None
        assert store.by_tag("   ") is None

    def test_大小写不影响匹配(self, store: StickerStore) -> None:
        _add(store, "甲", ["OK", "点头"])

        assert store.by_tag("ok").name == "甲"

    def test_没打标的图不参与(self, store: StickerStore) -> None:
        digest = _add(store, "生图", ["无语"])
        store.mark_caption(digest, CAPTION_PENDING)

        # 标签是检索的全部依据，没打完标的图对模型不可描述。
        assert store.by_tag("无语") is None

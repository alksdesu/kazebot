"""表情包打标。

模型不听话是常态：说了不要 Markdown 照样包 ```json，说了只输出数组照样带一句解释。
解析这一层必须扛得住，否则整库都打不上标。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from stickers import store as ss
from stickers import tagger as st


# ── 解析模型回复 ──

def test_plain_array_is_parsed() -> None:
    assert st.parse_tags('["表情包","开心"]') == ["表情包", "开心"]


def test_markdown_fence_is_stripped() -> None:
    assert st.parse_tags('```json\n["表情包","猫"]\n```') == ["表情包", "猫"]


def test_bare_fence_is_stripped() -> None:
    assert st.parse_tags('```\n["猫"]\n```') == ["猫"]


def test_surrounding_prose_is_tolerated() -> None:
    assert st.parse_tags('好的，标签如下：["猫","开心"] 希望有帮助') == ["猫", "开心"]


@pytest.mark.parametrize("raw", ["", "   ", "抱歉我看不了图", "{}", '{"tags":["猫"]}', "[", "null"])
def test_unusable_replies_yield_nothing(raw: str) -> None:
    assert st.parse_tags(raw) == []


def test_non_string_entries_are_coerced() -> None:
    assert st.parse_tags('["猫",123,null,""]') == ["猫", "123"]


# ── 归一化 ──

def test_duplicates_are_dropped() -> None:
    assert st.normalize_tags(["猫", "猫", "开心"]) == ["猫", "开心"]


def test_only_one_type_tag_survives() -> None:
    # 两个类型标签会让检索里的类型权重翻倍。
    out = st.normalize_tags(["表情包", "照片", "猫", "开心", "可爱"])
    assert out.count("表情包") + out.count("照片") == 1


def test_type_tag_comes_first() -> None:
    assert st.normalize_tags(["猫", "表情包", "开心", "可爱"])[0] == "表情包"


def test_overlong_tags_are_dropped() -> None:
    assert "很长很长很长很长很长很长很长" not in st.normalize_tags(
        ["猫", "很长很长很长很长很长很长很长", "开心"]
    )


def test_tag_count_is_capped() -> None:
    # 标签会乘以候选数进提示词，不封顶就是 token 炸弹。
    assert len(st.normalize_tags([f"标签{i}" for i in range(30)])) <= 8


def test_whitespace_and_punctuation_are_cleaned() -> None:
    assert st.normalize_tags([" 开 心 ", "猫，"]) == ["开心", "猫"]


def test_fallback_fills_a_too_short_result() -> None:
    assert "备用名" in st.normalize_tags(["猫"], fallback="备用名")


def test_fallback_is_not_added_when_enough_tags() -> None:
    assert "备用名" not in st.normalize_tags(["猫", "开心", "可爱"], fallback="备用名")


def test_empty_input_stays_empty() -> None:
    assert st.normalize_tags([]) == []


# ── 打标流程 ──

@pytest.fixture()
def bench(tmp_path: Path):
    store = ss.StickerStore(tmp_path / "stickers.sqlite3")
    (tmp_path / "data" / "stickers").mkdir(parents=True)
    holder = {"config": st.TaggerConfig(min_interval=0.0)}
    tagger = st.StickerTagger(
        tmp_path, config=lambda: holder["config"], open_store=lambda: store,
    )
    yield type("Bench", (), {
        "root": tmp_path, "store": store, "tagger": tagger, "holder": holder,
    })
    store.close()


def _seed(bench, digest: str = "a" * 64) -> str:
    path = bench.root / "data" / "stickers" / f"{digest}.png"
    path.write_bytes(b"fake png bytes")
    bench.store.add(
        sha256=digest,
        rel_path=path.relative_to(bench.root).as_posix(),
        source=ss.SOURCE_GROUP,
        state=ss.STATE_LIBRARY,
        name="待标",
    )
    return digest


class _Vision:
    usable = True
    error = ""
    base_url = "https://vision.example.com/v1"
    api_key = "k"
    model = "m"

    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"


@pytest.mark.asyncio
async def test_successful_pass_writes_tags(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = _seed(bench)
    monkeypatch.setattr(st, "resolve_vision_channel", lambda **_: _Vision(), raising=False)
    monkeypatch.setattr(
        bench.tagger, "_describe",
        lambda *_a, **_k: _async(["表情包", "开心", "猫"]),
    )
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))

    assert await bench.tagger._sweep(bench.holder["config"]) == 1
    row = bench.store.get(digest)
    assert row.caption_state == ss.CAPTION_DONE
    assert row.tags == ["表情包", "开心", "猫"]


@pytest.mark.asyncio
async def test_missing_file_fails_immediately(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = _seed(bench)
    (bench.root / bench.store.get(digest).rel_path).unlink()
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))

    await bench.tagger._sweep(bench.holder["config"])
    assert bench.store.get(digest).caption_state == ss.CAPTION_FAILED


@pytest.mark.asyncio
async def test_transient_errors_are_retried_before_giving_up(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 网络抖一下不该让一张图永久进失败堆。
    digest = _seed(bench)
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))

    def boom(*_a, **_k):
        raise RuntimeError("连接超时")

    monkeypatch.setattr(bench.tagger, "_describe", boom)

    for _ in range(st._MAX_ATTEMPTS - 1):
        await bench.tagger._sweep(bench.holder["config"])
        assert bench.store.get(digest).caption_state == ss.CAPTION_PENDING

    await bench.tagger._sweep(bench.holder["config"])
    assert bench.store.get(digest).caption_state == ss.CAPTION_FAILED


@pytest.mark.asyncio
async def test_empty_tag_reply_counts_as_a_failure(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = _seed(bench)
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))
    monkeypatch.setattr(bench.tagger, "_describe", lambda *_a, **_k: _async([]))

    await bench.tagger._sweep(bench.holder["config"])
    assert bench.store.get(digest).caption_state != ss.CAPTION_DONE


@pytest.mark.asyncio
async def test_unusable_channel_stops_before_spending_anything(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = _seed(bench)
    broken = _Vision()
    broken.usable = False
    broken.error = "没配 key"
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(broken))
    called = []
    monkeypatch.setattr(bench.tagger, "_describe", lambda *a, **k: called.append(1) or _async([]))

    assert await bench.tagger._sweep(bench.holder["config"]) == 0
    assert not called
    assert bench.store.get(digest).caption_state == ss.CAPTION_PENDING


@pytest.mark.asyncio
async def test_nothing_to_do_returns_zero(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))
    assert await bench.tagger._sweep(bench.holder["config"]) == 0


@pytest.mark.asyncio
async def test_batch_size_is_respected(bench, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(5):
        _seed(bench, str(index) * 64)
    bench.holder["config"] = st.TaggerConfig(batch=2, min_interval=0.0)
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))
    monkeypatch.setattr(bench.tagger, "_describe", lambda *_a, **_k: _async(["表情包", "猫"]))

    assert await bench.tagger._sweep(bench.holder["config"]) == 2


@pytest.mark.asyncio
async def test_stop_is_safe_without_a_running_task(bench) -> None:
    await bench.tagger.stop()


def _async(value):
    async def _inner():
        return value
    return _inner()


class _FakeChannel:
    """顶掉 tools._channel，让 _sweep 拿到我们指定的渠道。"""

    def __init__(self, vision) -> None:
        self._vision = vision

    def resolve_vision_channel(self, **_kwargs):
        return self._vision

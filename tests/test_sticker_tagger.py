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
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))


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
    provider = "openai"
    wire = "openai"
    family = "openai"

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


# ── 真发出去的那一份 ──

class _Reply:
    def __init__(self, status: int, payload, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Sink(list):
    """顶掉 httpx.AsyncClient，把发出去的那一份留下来看。"""

    def client(self, *replies: _Reply):
        sink = self

        class _Client:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc) -> bool:
                return False

            async def post(self, url, headers=None, json=None):
                sink.append({"url": url, "headers": headers or {}, "body": json or {}})
                # 回复用完就一直返回最后一个，省得每个用例都数清楚发了几次。
                return replies[min(len(sink) - 1, len(replies) - 1)]

        return _Client


def _seed_real_png(bench, digest: str = "c" * 64) -> Path:
    path = bench.root / "data" / "stickers" / f"{digest}.png"
    # 只看文件头认格式，不解码整图，所以这几个字节就够。
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk")
    bench.store.add(
        sha256=digest, rel_path=path.relative_to(bench.root).as_posix(),
        source=ss.SOURCE_GROUP, state=ss.STATE_LIBRARY, name="待标",
    )
    return path


def _vision(wire: str, base_url: str) -> _Vision:
    import _vision_wire as vw

    channel = _Vision()
    channel.wire = wire
    channel.family = vw.family_for_wire(wire)
    channel.base_url = base_url
    return channel


@pytest.mark.asyncio
async def test_a_gemini_channel_gets_gemini_shaped_requests(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    path = _seed_real_png(bench)
    sink = _Sink()
    reply = _Reply(200, {"candidates": [{"content": {"parts": [{"text": '["表情包","猫"]'}]}}]})
    monkeypatch.setattr(httpx, "AsyncClient", sink.client(reply))

    tags = await bench.tagger._describe(
        _vision("gemini", "https://generativelanguage.googleapis.com"), path,
    )

    assert tags == ["表情包", "猫"]
    sent = sink[0]
    assert sent["url"].endswith("/v1beta/models/m:generateContent")
    assert sent["headers"]["x-goog-api-key"] == "k"
    assert sent["body"]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/png"


@pytest.mark.asyncio
async def test_an_openai_channel_still_gets_chat_completions(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    path = _seed_real_png(bench)
    sink = _Sink()
    reply = _Reply(200, {"choices": [{"message": {"content": '["表情包","猫"]'}}]})
    monkeypatch.setattr(httpx, "AsyncClient", sink.client(reply))

    tags = await bench.tagger._describe(_vision("openai", "https://relay.example/v1"), path)

    assert tags == ["表情包", "猫"]
    assert sink[0]["url"] == "https://relay.example/v1/chat/completions"
    assert sink[0]["headers"]["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_the_upstream_reason_reaches_the_failure_record(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 只报一句 HTTP 400 的话，分不清是模型名错还是格式选错。
    import httpx

    path = _seed_real_png(bench)
    reply = _Reply(400, {"error": {"message": "model not found"}})
    monkeypatch.setattr(httpx, "AsyncClient", _Sink().client(reply))

    with pytest.raises(ValueError, match="model not found"):
        await bench.tagger._describe(_vision("openai", "https://relay.example/v1"), path)


@pytest.mark.asyncio
async def test_thinking_is_off_so_the_budget_goes_to_the_tags(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    path = _seed_real_png(bench)
    sink = _Sink()
    reply = _Reply(200, {"candidates": [{"content": {"parts": [{"text": '["表情包"]'}]}}]})
    monkeypatch.setattr(httpx, "AsyncClient", sink.client(reply))

    await bench.tagger._describe(_vision("gemini", "https://relay.example"), path)

    assert sink[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.asyncio
async def test_a_model_that_refuses_to_stop_thinking_is_retried_with_room(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gemini 的 pro 系列不收 thinkingBudget=0，退让一次比整库打不上标强。
    import httpx

    path = _seed_real_png(bench)
    sink = _Sink()
    refuse = _Reply(400, {"error": {"message": "thinkingBudget must be at least 128"}})
    ok = _Reply(200, {"candidates": [{"content": {"parts": [{"text": '["表情包","猫"]'}]}}]})
    monkeypatch.setattr(httpx, "AsyncClient", sink.client(refuse, ok))

    tags = await bench.tagger._describe(_vision("gemini", "https://relay.example"), path)

    assert tags == ["表情包", "猫"]
    assert len(sink) == 2
    assert "thinkingConfig" not in sink[1]["body"]["generationConfig"]


@pytest.mark.asyncio
async def test_an_unrelated_error_is_not_retried(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    path = _seed_real_png(bench)
    sink = _Sink()
    monkeypatch.setattr(
        httpx, "AsyncClient",
        sink.client(_Reply(400, {"error": {"message": "model not found"}})),
    )

    with pytest.raises(ValueError, match="model not found"):
        await bench.tagger._describe(_vision("gemini", "https://relay.example"), path)
    assert len(sink) == 1


@pytest.mark.asyncio
async def test_a_truncated_reply_says_so_instead_of_blaming_the_prompt(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 截断的正文是半截 JSON，报"没给出标签"会让人去查提示词，方向就错了。
    import httpx

    path = _seed_real_png(bench)
    cut = _Reply(200, {"candidates": [
        {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '["表情包","笑'}]}},
    ]})
    monkeypatch.setattr(httpx, "AsyncClient", _Sink().client(cut))

    with pytest.raises(ValueError, match="max_tokens"):
        await bench.tagger._describe(_vision("gemini", "https://relay.example"), path)


@pytest.mark.asyncio
async def test_a_reply_without_text_is_an_error_not_an_empty_tag_list(
    bench, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 空标签会被当成"模型没给出标签"重试三次，但渠道形状不对时重试没有意义。
    import httpx

    path = _seed_real_png(bench)
    monkeypatch.setattr(httpx, "AsyncClient", _Sink().client(_Reply(200, {"choices": []})))

    with pytest.raises(ValueError):
        await bench.tagger._describe(_vision("openai", "https://relay.example/v1"), path)


@pytest.mark.asyncio
async def test_channel_problems_are_logged_once_not_every_sweep(
    bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # 打标每分钟醒一次，渠道没配好时每轮都喊一遍会把日志刷没。
    _seed(bench)
    broken = _Vision()
    broken.usable = False
    broken.error = "没配 key"
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(broken))

    with caplog.at_level("WARNING"):
        for _ in range(3):
            await bench.tagger._sweep(bench.holder["config"])

    assert len([r for r in caplog.records if "打标停用" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_the_warning_comes_back_after_the_channel_recovers(
    bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(bench)
    broken = _Vision()
    broken.usable = False
    broken.error = "没配 key"
    monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(broken))
    monkeypatch.setattr(bench.tagger, "_describe", lambda *_a, **_k: _async(["表情包", "猫"]))

    with caplog.at_level("WARNING"):
        await bench.tagger._sweep(bench.holder["config"])
        monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(_Vision()))
        await bench.tagger._sweep(bench.holder["config"])
        # 上一轮把队列清空了，而队列空时压根不解析渠道，得再给一张才谈得上重新报错。
        _seed(bench, "b" * 64)
        monkeypatch.setitem(sys.modules, "tools._channel", _FakeChannel(broken))
        await bench.tagger._sweep(bench.holder["config"])

    assert len([r for r in caplog.records if "打标停用" in r.getMessage()]) == 2

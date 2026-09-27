"""摆给模型看的表情包清单。

清单不再按群友那句话预筛 —— 筛出来的情绪和 bot 要回的语气经常是反的。
改成按概率整份摊开，发不发、发哪张全由模型定。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _node_prompts import assembled_prompt  # noqa: E402
from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from stickers.store import (  # noqa: E402
    CAPTION_DONE, CAPTION_FAILED, CAPTION_PENDING, CAPTION_RUNNING,
    STATE_DISCARDED, STATE_LIBRARY, STATE_PENDING, StickerStore,
)

_CONV = "qq_group:t"


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


@pytest.fixture()
def store(runtime: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StickerStore:
    handle = StickerStore(tmp_path / "stickers.sqlite3")
    monkeypatch.setattr(runtime.emotes, "_sticker_store_handle", lambda: handle)
    return handle


def _add(
    store: StickerStore, name: str, tags: list[str], *, sent: int = 0,
    content: bytes | None = None, state: str = STATE_LIBRARY, caption: str = CAPTION_DONE,
) -> str:
    payload = content if content is not None else b"GIF89a" + name.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    rel_path = f"data/stickers/library/{digest}.gif"
    path = store.path.parent / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    store.add(
        sha256=digest, rel_path=rel_path, size=len(payload),
        source="test", state=state, name=name,
    )
    store.set_auto_tags(digest, tags, version=1)
    store.mark_caption(digest, caption)
    for _ in range(sent):
        # 记在别的会话上：sent_count 是全局的，落在 _CONV 会顺带算成「刚发过」。
        store.record_sent("qq_group:seed", digest)
    return digest


def _native_block(runtime: Any) -> str:
    """这一轮的收藏表情说明；不给时是空串。"""
    section = runtime.output_guide._native_emoji_section()
    return "" if section is None else section.text


def _sticker_block(runtime: Any) -> str:
    """这一轮的表情包说明；没抽中时是空串。"""
    section = runtime.output_guide._sticker_section(_CONV)
    return "" if section is None else section.text


def _always(runtime: Any, monkeypatch: pytest.MonkeyPatch, hit: bool) -> None:
    """把概率骰子钉死。0.0 必中，0.99 必不中（判定是 random() >= p）。"""
    monkeypatch.setattr(runtime.emotes.random, "random", lambda: 0.0 if hit else 0.99)


class TestRoster:
    @pytest.mark.parametrize("roll,listed", [(0.0, True), (0.399999, True), (0.4, False), (0.999999, False)])
    def test_每轮零点四概率边界不变(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch, roll: float, listed: bool,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=0.4)
        monkeypatch.setattr(runtime.emotes.random, "random", lambda: roll)

        assert bool(runtime.emotes._sticker_prompt_entries(_CONV)) is listed
        assert runtime.state.live.sticker_send_probability == 0.4

    @pytest.mark.parametrize("size,listed", [(0, False), (1, True), (3 * 1024 * 1024, True), (3 * 1024 * 1024 + 1, False)])
    def test_可发送文件大小边界(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch, size: int, listed: bool,
    ) -> None:
        _add(store, "边界图", ["大笑"], content=b"x" * size)
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        assert bool(runtime.emotes._sticker_prompt_entries(_CONV)) is listed

    @pytest.mark.parametrize("replacement", ["missing", "directory"])
    def test_不存在或不是文件的候选不列出(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch, replacement: str,
    ) -> None:
        digest = _add(store, "不可读", ["大笑"])
        path = store.path.parent / store.get(digest).rel_path
        path.unlink()
        if replacement == "directory":
            path.mkdir()
        _add(store, "正常", ["无语"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        assert runtime.emotes._sticker_prompt_entries(_CONV) == ["正常（无语）"]

    @pytest.mark.parametrize("state,caption", [
        (STATE_PENDING, CAPTION_DONE), (STATE_DISCARDED, CAPTION_DONE),
        (STATE_LIBRARY, CAPTION_PENDING), (STATE_LIBRARY, CAPTION_RUNNING),
        (STATE_LIBRARY, CAPTION_FAILED),
    ])
    def test_未入库或未完成打标的状态不列出(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch, state: str, caption: str,
    ) -> None:
        _add(store, "不可用", ["大笑"], state=state, caption=caption)
        _add(store, "正常", ["无语"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        assert runtime.emotes._sticker_prompt_entries(_CONV) == ["正常（无语）"]

    def test_抽中时整份摊开_不按语境筛(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        _add(store, "乙", ["猫猫爆炸"])
        _add(store, "丙", ["无语"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        lines = runtime.emotes._sticker_prompt_entries(_CONV)

        # 三张标签互不相干，换成旧的关键词预筛这里最多出一张。
        assert len(lines) == 3
        assert any("甲（大笑）" == line for line in lines)

    def test_没抽中就一张都不给(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=0.5)
        _always(runtime, monkeypatch, False)

        assert runtime.emotes._sticker_prompt_entries(_CONV) == []

    def test_概率为零就是彻底关掉(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=0.0)
        # 骰子掷出 0.0 —— 概率为 0 时连这个都不该放行。
        _always(runtime, monkeypatch, True)

        assert runtime.emotes._sticker_prompt_entries(_CONV) == []

    def test_装不下时发得最少的先上(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "老", ["a"], sent=9)
        _add(store, "中", ["b"], sent=3)
        _add(store, "新", ["c"], sent=0)
        set_live_config(runtime, sticker_send_probability=1.0, sticker_prompt_limit=2)
        _always(runtime, monkeypatch, True)

        lines = runtime.emotes._sticker_prompt_entries(_CONV)

        # 按名字排的话「中」会挤掉「新」，冷门图永远没机会露面。
        assert [line.split("（")[0] for line in lines] == ["新", "中"]

    def test_刚发过的不进清单(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hot = _add(store, "刚发过", ["a"])
        _add(store, "没发过", ["b"])
        store.record_sent(_CONV, hot)
        set_live_config(
            runtime, sticker_send_probability=1.0, sticker_repeat_window_sec=3600,
        )
        _always(runtime, monkeypatch, True)

        lines = runtime.emotes._sticker_prompt_entries(_CONV)

        assert [line.split("（")[0] for line in lines] == ["没发过"]

    def test_没标签的图也列出来_只是标注一下(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        digest = _add(store, "甲", [])
        store.mark_caption(digest, CAPTION_DONE)
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        assert runtime.emotes._sticker_prompt_entries(_CONV) == ["甲（无标签）"]


class TestPromptBlock:
    def test_只有收藏时不声明本地情绪词回退(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime.emotes, "_current_custom_face_names", lambda: ["收藏甲"])
        set_live_config(runtime, sticker_send_probability=0.0)

        native = _native_block(runtime)

        assert native.endswith("只能用这些名字：收藏甲")
        assert _sticker_block(runtime) == ""
        assert "[表情:无语]" not in native
        assert "会去标签里找" not in native

    def test_收藏和表情包各占一段_各带各的用法(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "本地甲", ["大笑"])
        monkeypatch.setattr(runtime.emotes, "_current_custom_face_names", lambda: ["收藏甲"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        native, stickers = _native_block(runtime), _sticker_block(runtime)

        assert native.endswith("只能用这些名字：收藏甲") and "本地甲" not in native
        assert "本地甲（大笑）" in stickers and "收藏甲" not in stickers
        assert "[表情:无语]" in stickers and "[表情:无语]" not in native
        assert "会去标签里找" in stickers

    @pytest.mark.parametrize("node_id", ["qq.orchestrator", "qq.orchestrator.example"])
    def test_节点规则按输出说明认表情_不认段落标题(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch, node_id: str,
    ) -> None:
        _add(store, "甲", ["大笑"])
        monkeypatch.setattr(runtime.emotes, "_current_custom_face_names", lambda: ["收藏甲"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)
        prompt = assembled_prompt(node_id)

        assert _native_block(runtime).splitlines()[0] == "【可用收藏表情】"
        assert _sticker_block(runtime).splitlines()[0] == "【可用表情包】"
        # 标题随平台变，规则认的是输出说明的包裹标记。
        assert "[OUTPUT_GUIDE:CONSTANT]" in prompt and "[OUTPUT_GUIDE:ACTIVE]" in prompt
        assert "【可用表情】" not in prompt and "【QQ可用收藏表情】" not in prompt and "【QQ表情表态】" not in prompt
        assert "平台自带的表情只能用说明里列出的名字" in prompt
        assert "只有给了表情包清单时" in prompt and "[表情:无语]" in prompt
        # 说明不写进对话历史，没有旧清单可沿用。
        assert "不要沿用历史清单" not in prompt

    def test_抽中时才出现表情包那几行(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        block = _sticker_block(runtime)

        assert "甲（大笑）" in block
        assert "只想甩一张图不说话" in block

    def test_没抽中时整块可以是空的(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=0.5)
        _always(runtime, monkeypatch, False)

        # 没有收藏表情、这轮又没抽中，就不该白占一段提示词。
        assert _native_block(runtime) == "" and _sticker_block(runtime) == ""

    def test_收藏和表情包都在时的逐字写法(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "本地甲", ["大笑"])
        monkeypatch.setattr(runtime.emotes, "_current_custom_face_names", lambda: ["收藏甲", "收藏乙"])
        set_live_config(runtime, sticker_send_probability=1.0, face_prompt_limit=1)
        _always(runtime, monkeypatch, True)

        assert _native_block(runtime) == "\n".join([
            "【可用收藏表情】",
            "在回复里写 [表情:名称] 就能发收藏表情，只能用这些名字：收藏甲（另有 1 个未展示）",
        ])
        assert _sticker_block(runtime) == "\n".join([
            "【可用表情包】",
            "在回复里写 [表情:名称] 就能发表情包，一行一张，括号里是它的标签：",
            "  本地甲（大笑）",
            "配不配表情包你自己定，不合适就别写 —— 每句话都配图很烦人。",
            "只想甩一张图不说话时，整条回复就写一个 [表情:名称]，别硬凑话。",
            "名单里挑不出想要的，也可以直接写想表达的情绪，比如 [表情:无语]，会去标签里找。",
        ])

    def test_收藏名单进缓存前缀_表情包清单进动态区(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        monkeypatch.setattr(runtime.emotes, "_current_custom_face_names", lambda: ["收藏甲"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        # 表情包每轮按概率抽；收藏名单跟着它进动态区的话，每轮都白付一遍这段的钱。
        assert runtime.output_guide._native_emoji_section().volatile is False
        assert runtime.output_guide._sticker_section(_CONV).volatile is True

    def test_不再限制模型只能用名单里的名字(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        block = _sticker_block(runtime)

        # 这句话曾经把描述式那条路堵死。
        assert "不要臆造未列出的" not in block
        assert "[表情:无语]" in block


class TestAutoTag:
    """自动打标是花钱的那一步，得能单独关掉。"""

    def test_默认开着(self, runtime: Any) -> None:
        assert runtime.state.live.sticker_auto_tag is True

    def test_关掉之后打标循环不再干活(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_live_config(runtime, sticker_auto_tag=False)
        seen: list[bool] = []
        monkeypatch.setattr(
            runtime.emotes, "StickerTagger",
            lambda *a, **kw: seen.append(kw["config"]().enabled) or _Stub(),
        )
        runtime.state._adapter.sticker_tagger = None
        runtime.emotes._start_sticker_tagger()

        assert seen == [False]

    def test_开关是每轮现读的_不用重启(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        holder: list[Any] = []
        monkeypatch.setattr(
            runtime.emotes, "StickerTagger",
            lambda *a, **kw: holder.append(kw["config"]) or _Stub(),
        )
        runtime.state._adapter.sticker_tagger = None
        runtime.emotes._start_sticker_tagger()
        read = holder[0]

        assert read().enabled is True
        set_live_config(runtime, sticker_auto_tag=False)
        # 同一个 callable 再问一次就该是新值 —— 存快照的话这里还是 True。
        assert read().enabled is False


class _Stub:
    def start(self) -> None:
        return None


class TestRosterLayout:
    """一行一张。挤成一行时，标签自带顿号或名字带括号就分不清归属了。"""

    def test_每张图各占一行(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add(store, "甲", ["大笑", "猫"])
        _add(store, "乙", ["无语"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        block = _sticker_block(runtime)
        rows = [line.strip() for line in block.splitlines() if line.startswith("  ")]

        # 顺序另有规则（发得少的在前），这里只认「一张一行、标签不串行」。
        assert sorted(rows) == sorted(["甲（大笑、猫）", "乙（无语）"])

    def test_标签自带顿号也不会串到别张图上(
        self, runtime: Any, store: StickerStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 打标模型偶尔会把两个标签合成一个，normalize_tags 只 strip 首尾不拆中间。
        _add(store, "甲", ["开心、快乐"])
        _add(store, "乙", ["无语"])
        set_live_config(runtime, sticker_send_probability=1.0)
        _always(runtime, monkeypatch, True)

        block = _sticker_block(runtime)

        assert "  甲（开心、快乐）" in block
        assert "  乙（无语）" in block

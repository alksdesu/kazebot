"""共享的出站标记解析：切分、剥离、提及反查、表态，以及按能力声明降级。

QQ 的逐字节行为由快照钉住；这里钉的是任何平台都要遵守的规则。
"""
from __future__ import annotations

import pytest

from adapters._shared import outbound
from adapters._shared.outbound import (
    EMOJI_LIBRARY,
    EMOJI_NATIVE,
    EVERYONE,
    FOREIGN_CUSTOM_EMOJI,
    REACT_MARKER,
    EmojiPiece,
    MarkerGrammar,
    MentionPiece,
    Rendered,
    TextPiece,
    emoji_lookup,
    extract_reactions,
    join_rendered,
    limit_parts,
    mention_as_text,
    pick_reaction,
    resolve_mention,
    split_mention_tokens,
    split_reply,
    strip_markup,
    tokenize,
    truncate_text,
)
from platforms.descriptor import ID_NUMERIC, ID_STRING

_PLAIN = MarkerGrammar()
_NATIVE = MarkerGrammar(
    emoji_tags=("表情", "emoji", "sticker"),
    native_mentions=(r"<@(\w+)>",),
    strip=(FOREIGN_CUSTOM_EMOJI, REACT_MARKER, r"\[face:[^\]]*\]"),
)


class TestSplitting:
    def test_split_marker_separates_messages_and_keeps_empty_parts(self) -> None:
        assert split_reply("收到[SPLIT]我看看[SPLIT]") == ["收到", "我看看", ""]

    def test_nothing_to_send_is_no_parts(self) -> None:
        assert split_reply("") == []

    def test_the_marker_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(outbound, "SPLIT_MARKER", "<br/>")
        assert split_reply("a<br/>b[SPLIT]c") == ["a", "b[SPLIT]c"]

    def test_parts_beyond_the_limit_fold_into_the_last_one(self) -> None:
        assert limit_parts(["一", "二", "三", "四"], 3) == ["一", "二", "三\n四"]

    def test_blank_parts_do_not_count_against_the_limit(self) -> None:
        assert limit_parts(["一", " ", "二", "", "三"], 3) == ["一", " ", "二", "", "三"]
        assert limit_parts(["一", " ", "二", "三", "四"], 3) == ["一", "二", "三\n四"]

    @pytest.mark.parametrize("limit", [0, 4, 10])
    def test_no_limit_or_enough_room_keeps_the_parts(self, limit: int) -> None:
        assert limit_parts(["一", "二", "三", "四"], limit) == ["一", "二", "三", "四"]


class TestTruncation:
    def test_an_overlong_text_ends_with_the_notice_and_fits(self) -> None:
        cut = truncate_text("字" * 50, 20, suffix="（截断）")

        assert len(cut) == 20 and cut.endswith("（截断）")

    @pytest.mark.parametrize("limit", [0, 50, 100])
    def test_a_text_that_fits_is_untouched(self, limit: int) -> None:
        assert truncate_text("字" * 50, limit, suffix="（截断）") == "字" * 50

    @pytest.mark.parametrize("limit", [3, 4])
    def test_a_limit_with_no_room_for_the_notice_cuts_without_it(self, limit: int) -> None:
        assert truncate_text("字" * 50, limit, suffix="（截断）") == "字" * limit

    def test_an_atomic_chunk_is_kept_whole_or_dropped(self) -> None:
        chunks = [Rendered("看"), Rendered("<@u-42>", atomic=True), Rendered("这里")]

        assert join_rendered(chunks, 7, suffix="…") == "看…"
        assert join_rendered(chunks, 9, suffix="…") == "看<@u-42>…"

    def test_plain_chunks_are_cut_inside(self) -> None:
        assert join_rendered([Rendered("一二"), Rendered("三四五")], 4, suffix="…") == "一二三…"

    @pytest.mark.parametrize("limit", [0, 8])
    def test_chunks_that_fit_are_joined_as_they_are(self, limit: int) -> None:
        chunks = [Rendered("看"), Rendered("<@u-42>", atomic=True)]

        assert join_rendered(chunks, limit, suffix="…") == "看<@u-42>"


class TestReactions:
    def test_every_marker_is_taken_out_of_the_text(self) -> None:
        assert extract_reactions("[REACT:4]好的[REACT: 76 ]") == ("好的", ["4", "76"])

    def test_an_empty_marker_is_removed_without_a_reaction(self) -> None:
        assert extract_reactions("好[REACT: ]的") == ("好的", [])

    def test_only_the_first_reaction_in_the_catalog_is_used(self) -> None:
        assert pick_reaction(["999", " 76", "4"], frozenset({"4", "76"})) == "76"

    def test_nothing_in_the_catalog_means_no_reaction(self) -> None:
        assert pick_reaction(["999"], {"4": "赞"}) == ""
        assert pick_reaction([], {"4": "赞"}) == ""


class TestStripping:
    def test_foreign_markup_and_leftover_reactions_are_removed(self) -> None:
        assert strip_markup("好<:smile:123>的[REACT:4]", _PLAIN, asterisk_styles=True, underscore_styles=False) == "好的"

    def test_the_platform_adds_its_own_strip_rules_in_order(self) -> None:
        text = "看[face:微笑]这个"

        assert strip_markup(text, _PLAIN, asterisk_styles=False, underscore_styles=False) == text
        assert strip_markup(text, _NATIVE, asterisk_styles=False, underscore_styles=False) == "看这个"

    def test_markdown_is_flattened_where_it_does_not_render(self) -> None:
        text = "# 标题\n**粗体** *斜体* `代码` [链接](https://example.invalid)\n```py\nprint(1)\n```"

        flat = strip_markup(text, _PLAIN, asterisk_styles=True, underscore_styles=False)

        assert flat == "标题\n粗体 斜体 代码 链接（https://example.invalid）\nprint(1)"

    def test_underscores_survive_by_default(self) -> None:
        text = "PUB_CACHE 和 user_id 和 __init__"

        assert strip_markup(text, _PLAIN, asterisk_styles=True, underscore_styles=False) == text

    def test_a_markdown_platform_keeps_the_styles(self) -> None:
        text = "**粗体** `代码` [链接](https://example.invalid)"

        assert strip_markup(
            text + "[REACT:4]", _PLAIN, keep_markdown=True, asterisk_styles=True, underscore_styles=True,
        ) == text


class TestMentionTokens:
    @pytest.mark.parametrize("raw, tokens", [
        ("UserA", ("UserA",)),
        ("UserA,UserB、木；火", ("UserA", "UserB", "木", "火")),
        ("UserA  UserB", ("UserA", "UserB")),
        ("张 三", ("张 三",)),
        ("  ", ()),
        (",,,", (",,,",)),
    ])
    def test_several_names_in_one_marker(self, raw: str, tokens: tuple[str, ...]) -> None:
        assert split_mention_tokens(raw) == tokens

    def test_a_known_name_resolves_to_its_platform_id(self) -> None:
        assert resolve_mention("@UserA", {"UserA": "u-1"}.get, id_shape=ID_STRING) == "u-1"

    def test_digits_are_an_id_only_on_numeric_platforms(self) -> None:
        assert resolve_mention("12345", None, id_shape=ID_NUMERIC) == "12345"
        assert resolve_mention("12345", None, id_shape=ID_STRING) == ""

    def test_everyone_is_the_fallback_not_the_first_guess(self) -> None:
        assert resolve_mention("all", {"all": "u-all"}.get, id_shape=ID_STRING) == "u-all"
        assert resolve_mention("全体成员", None, id_shape=ID_STRING) == EVERYONE
        assert resolve_mention("@全体成员", None, id_shape=ID_STRING) == EVERYONE

    def test_a_failing_resolver_counts_as_unknown(self) -> None:
        def broken(name: str) -> str:
            raise RuntimeError("alias table not loaded")

        assert resolve_mention("UserA", broken, id_shape=ID_STRING) == ""

    @pytest.mark.parametrize("token", ["", "  ", "@", "@ "])
    def test_an_empty_name_resolves_to_nothing(self, token: str) -> None:
        assert resolve_mention(token, {"": "u-1"}.get, id_shape=ID_STRING) == ""

    def test_an_unresolved_name_falls_back_to_readable_text(self) -> None:
        assert mention_as_text(" UserZ ") == "@UserZ"
        assert mention_as_text("") == ""


class TestTokenize:
    def test_markers_come_out_in_order_with_the_text_between(self) -> None:
        assert tokenize("早[at:UserA,UserB]看[表情:开心]好", _PLAIN) == [
            TextPiece("早"), MentionPiece(tokens=("UserA", "UserB")), TextPiece("看"), EmojiPiece("开心"), TextPiece("好"),
        ]

    def test_whitespace_only_text_between_markers_is_dropped(self) -> None:
        assert tokenize(" [表情:开心] \n [at:UserA] ", _PLAIN) == [EmojiPiece("开心"), MentionPiece(tokens=("UserA",))]

    def test_emoji_tags_are_case_insensitive_and_allow_spacing(self) -> None:
        assert tokenize("[EMOJI : 开心][表情： 无语 ]", _PLAIN) == [EmojiPiece("开心"), EmojiPiece("无语")]

    @pytest.mark.parametrize("text", ["前[表情: ]后", "前[at:  ]后"])
    def test_an_empty_marker_is_consumed_without_a_piece(self, text: str) -> None:
        assert tokenize(text, _PLAIN) == [TextPiece("前"), TextPiece("后")]

    def test_a_marker_of_separators_keeps_the_raw_name(self) -> None:
        assert tokenize("[at:,,,]", _PLAIN) == [MentionPiece(tokens=(",,,",))]

    def test_a_platform_mention_already_carries_the_id(self) -> None:
        assert tokenize("<@u42> 你好", _NATIVE) == [MentionPiece(platform_id="u42"), TextPiece(" 你好")]

    def test_a_platform_mention_inside_an_at_marker_is_not_counted_twice(self) -> None:
        assert tokenize("[at:<@u42>]", _NATIVE) == [MentionPiece(tokens=("<@u42>",))]

    def test_unknown_tags_are_plain_text(self) -> None:
        assert tokenize("[sticker:猫]", _PLAIN) == [TextPiece("[sticker:猫]")]
        assert tokenize("[sticker:猫]", _NATIVE) == [EmojiPiece("猫")]

    def test_grammar_detects_emoji_markers(self) -> None:
        assert _PLAIN.has_emoji("前[emoji:开心]后")
        assert not _PLAIN.has_emoji("[表情:未闭合")


class TestGrammar:
    def test_a_native_mention_pattern_needs_exactly_one_group(self) -> None:
        with pytest.raises(ValueError):
            MarkerGrammar(native_mentions=(r"<@\w+>",))
        with pytest.raises(ValueError):
            MarkerGrammar(native_mentions=(r"<(@)(\w+)>",))

    def test_a_grammar_needs_an_emoji_tag(self) -> None:
        with pytest.raises(ValueError):
            MarkerGrammar(emoji_tags=())

    def test_tags_are_matched_literally(self) -> None:
        grammar = MarkerGrammar(emoji_tags=("e.x",))

        assert tokenize("[e.x:猫][eax:狗]", grammar) == [EmojiPiece("猫"), TextPiece("[eax:狗]")]


class TestEmojiLookup:
    def test_the_platform_emoji_comes_first_by_default(self) -> None:
        assert emoji_lookup("native_first") == (EMOJI_NATIVE, EMOJI_LIBRARY)

    def test_the_library_can_be_put_first(self) -> None:
        assert emoji_lookup(" Library_First ") == (EMOJI_LIBRARY, EMOJI_NATIVE)

    @pytest.mark.parametrize("raw", ["", None, "whatever"])
    def test_an_unknown_order_falls_back_to_the_platform_emoji(self, raw: object) -> None:
        assert emoji_lookup(raw) == (EMOJI_NATIVE, EMOJI_LIBRARY)  # type: ignore[arg-type]

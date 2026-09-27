"""模型回复里的标记：共享层解析成语义段，平台驱动按能力声明渲染或降级。

标记有四种：[SPLIT] 分条、[at:名字] 提及、[表情:名称] 表情、[REACT:ID] 表态。
平台自己的旧写法、渲染不了要剥掉的写法，由平台在 MarkerGrammar 里补。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Iterable, Mapping, Optional, Sequence, Union

from platforms.descriptor import ID_NUMERIC

SPLIT_MARKER = "[SPLIT]"
# 驱动拿到这个 ID 就发「全体成员」。
EVERYONE = "all"

REACT_MARKER = r"\[REACT:[^\]]+\]"
# `<:name:id>` 这种自定义表情写法，模型常从语料里带出来，只有个别平台渲染得了。
FOREIGN_CUSTOM_EMOJI = r"<a?:\w+:\d+>"

# [表情:名称] 去哪找：平台自己的表情（收藏表情、服务器表情），还是各平台共用的表情包库。
EMOJI_NATIVE = "native"
EMOJI_LIBRARY = "library"
# 两边都有同名的时候先用谁，是平台配置。
EMOJI_NATIVE_FIRST = "native_first"
EMOJI_LIBRARY_FIRST = "library_first"

# 驱动渲染好、等着发的表情段：表情包只记图库哈希，发的时候才读文件；平台表情记平台和平台自己的引用。
SEGMENT_STICKER = "sticker"
SEGMENT_NATIVE_EMOJI = "native_emoji"

_REACT_RE = re.compile(REACT_MARKER)
_MENTION_MARKER = r"\[at:([^\]]+)\]"
# 一个 [at:] 里写多个名字时的分隔：中英文逗号、顿号、分号，或者连续空白。
_MENTION_TOKEN_SPLIT_RE = re.compile(r"[,，、;；]+|\s{2,}")
# 这些词也可能恰好是某人的名字，所以反查不到人时才当全体成员。
_EVERYONE_WORDS = frozenset({"all", "at_all", "atall", "全体成员", "@全体成员"})

_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
_UNDERLINE_BOLD_RE = re.compile(r"__(.*?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", re.DOTALL)
_UNDERLINE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)

MentionResolver = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class MarkerGrammar:
    """一个平台认哪些标记写法。"""

    # [表情:名称] 的几种写法，大小写不敏感。
    emoji_tags: tuple[str, ...] = ("表情", "emoji")
    # 平台自己的提及写法，唯一的分组就是平台 ID，不用再反查别名。
    native_mentions: tuple[str, ...] = ()
    # 发送前整段剥掉的写法，按顺序剥；顺序会影响嵌套写法剩下什么。
    strip: tuple[str, ...] = (FOREIGN_CUSTOM_EMOJI, REACT_MARKER)
    _emoji_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _mention_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _strip_res: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.emoji_tags:
            raise ValueError("a marker grammar needs at least one emoji tag")
        tags = "|".join(re.escape(tag) for tag in self.emoji_tags)
        object.__setattr__(self, "_emoji_re", re.compile(rf"\[(?:{tags})\s*[:：]\s*(.+?)\]", re.IGNORECASE))
        for pattern in self.native_mentions:
            if re.compile(pattern).groups != 1:
                raise ValueError(f"a native mention pattern needs exactly one group: {pattern!r}")
        # 合成一个正则再 finditer：分开扫会让嵌在 [at:] 里的平台写法再算一次。
        object.__setattr__(self, "_mention_re", re.compile("|".join((_MENTION_MARKER, *self.native_mentions))))
        object.__setattr__(self, "_strip_res", tuple(re.compile(pattern) for pattern in self.strip))

    def has_emoji(self, text: str) -> bool:
        return bool(self._emoji_re.search(text or ""))


@dataclass(frozen=True)
class TextPiece:
    text: str


@dataclass(frozen=True)
class MentionPiece:
    # [at:甲,乙] 里写的名字，一个名字一项；空元组表示标记里什么都没写。
    tokens: tuple[str, ...] = ()
    # 平台自己的提及写法已经带着平台 ID，不用再反查。
    platform_id: str = ""


@dataclass(frozen=True)
class EmojiPiece:
    name: str


Piece = Union[TextPiece, MentionPiece, EmojiPiece]


def emoji_lookup(order: str) -> tuple[str, str]:
    """配置里的查找顺序换成先后两处；认不出的按平台表情优先。"""
    if str(order or "").strip().lower() == EMOJI_LIBRARY_FIRST:
        return EMOJI_LIBRARY, EMOJI_NATIVE
    return EMOJI_NATIVE, EMOJI_LIBRARY


@dataclass(frozen=True)
class ResolvedSticker:
    """图库里挑中的一张：库里的哈希，和挑中时读到的文件内容哈希。发送前按后者核对，图变了就不发。"""

    sha256: str
    content_sha256: str


def sticker_segment(sha256: str, content_sha256: str) -> dict[str, str]:
    return {"type": SEGMENT_STICKER, "sha256": sha256, "content_sha256": content_sha256}


def native_emoji_segment(platform: str, ref: str) -> dict[str, str]:
    return {"type": SEGMENT_NATIVE_EMOJI, "platform": platform, "ref": ref}


def planned_stickers(segments: Iterable[Mapping[str, Any]]) -> list[str]:
    """段里挑中的图，按出现先后去重；发送确认后按这份记账。"""
    return list(dict.fromkeys(
        str(segment["sha256"]) for segment in segments if segment.get("type") == SEGMENT_STICKER
    ))


def split_reply(text: str) -> list[str]:
    """按 [SPLIT] 切成几条。空段原样保留，要不要跳过由调用方定。"""
    return text.split(SPLIT_MARKER) if text else []


def limit_parts(parts: Sequence[str], max_parts: int, *, joiner: str = "\n") -> list[str]:
    """平台限制了条数时，超出的并进最后一条；0 表示不限。"""
    kept = [part for part in parts if part.strip()]
    if max_parts <= 0 or len(kept) <= max_parts:
        return list(parts)
    return [*kept[:max_parts - 1], joiner.join(part.strip() for part in kept[max_parts - 1:])]


@dataclass(frozen=True)
class Rendered:
    """驱动渲染出来的一小段平台文本。"""

    text: str
    # 提及这类平台语法截一半就坏了，截断时整段留或整段丢。
    atomic: bool = False


def join_rendered(chunks: Sequence[Rendered], limit: int, *, suffix: str) -> str:
    """把渲染好的几段拼成一条消息，超长就截断并加上说明；limit 为 0 表示不限。

    说明本身放不下时只截断不加说明，拼出来的消息总不超过上限。
    """
    whole = "".join(chunk.text for chunk in chunks)
    if limit <= 0 or len(whole) <= limit:
        return whole
    if len(suffix) >= limit:
        suffix = ""
    budget = limit - len(suffix)
    kept: list[str] = []
    for chunk in chunks:
        if len(chunk.text) <= budget:
            kept.append(chunk.text)
            budget -= len(chunk.text)
            continue
        if not chunk.atomic:
            kept.append(chunk.text[:budget])
        break
    return "".join(kept) + suffix


def truncate_text(text: str, limit: int, *, suffix: str) -> str:
    """超长就截断并加上说明；limit 为 0 表示不限。"""
    return join_rendered((Rendered(text),), limit, suffix=suffix)


def extract_reactions(text: str) -> tuple[str, list[str]]:
    """拿出所有 [REACT:ID]，返回去掉标记后的文本和 ID 列表。"""
    reactions: list[str] = []

    def take(match: re.Match[str]) -> str:
        inner = match.group(0)[len("[REACT:"):-1].strip()
        if inner:
            reactions.append(inner)
        return ""

    return _REACT_RE.sub(take, text).strip(), reactions


def pick_reaction(reactions: Sequence[str], allowed: Collection[str]) -> str:
    """一条回复最多贴一个表态：取第一个在可用目录里的，一个都不在就返回空串。"""
    available = set(allowed)
    return next((ref for ref in (str(item or "").strip() for item in reactions) if ref in available), "")


def strip_markup(
    text: str,
    grammar: MarkerGrammar,
    *,
    keep_markdown: bool = False,
    asterisk_styles: bool = True,
    underscore_styles: bool = False,
) -> str:
    """剥掉平台渲染不了的写法；平台不渲染 Markdown 时再把样式摊平，保留正文。

    单下划线常出现在标识符里，解析它会跨过好几处误删内容，所以星号和下划线分开控制。
    """
    if not text:
        return ""
    for pattern in grammar._strip_res:
        text = pattern.sub("", text)
    if keep_markdown:
        return text.strip()
    text = _CODE_BLOCK_RE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
    if asterisk_styles:
        text = _BOLD_RE.sub(lambda m: m.group(1), text)
        text = _ITALIC_RE.sub(lambda m: m.group(1), text)
    if underscore_styles:
        text = _UNDERLINE_BOLD_RE.sub(lambda m: m.group(1), text)
        text = _UNDERLINE_ITALIC_RE.sub(lambda m: m.group(1), text)
    text = _HEADING_RE.sub("", text)
    return text.strip()


def split_mention_tokens(raw: str) -> tuple[str, ...]:
    value = str(raw or "").strip()
    if not value:
        return ()
    parts = tuple(part.strip() for part in _MENTION_TOKEN_SPLIT_RE.split(value) if part.strip())
    return parts or (value,)


def resolve_mention(token: str, resolver: MentionResolver | None, *, id_shape: str) -> str:
    """把 [at:] 里的一个名字解析成平台 ID；全体成员返回 EVERYONE，认不出返回空串。

    先按人反查，名字恰好叫 all 的人也能被 @ 到；数字 ID 的平台上纯数字直接当 ID。
    """
    raw = str(token or "").strip()
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    if id_shape == ID_NUMERIC and raw.isdigit():
        return raw
    if resolver is not None:
        try:
            resolved = resolver(raw)
        except Exception:
            resolved = None
        if resolved and str(resolved).strip():
            return str(resolved).strip()
    if raw.lower() in _EVERYONE_WORDS:
        return EVERYONE
    return ""


def mention_as_text(token: str) -> str:
    """提及渲染不了或者认不出人时，退回成一段可读的文字。"""
    value = str(token or "").strip()
    return f"@{value}" if value else ""


def tokenize(text: str, grammar: MarkerGrammar) -> list[Piece]:
    """按出现位置把表情和提及标记切出来，标记之间的文字原样保留，只含空白的文字丢掉。"""
    matches = sorted(
        [
            *((match.start(), match.end(), False, match) for match in grammar._emoji_re.finditer(text)),
            *((match.start(), match.end(), True, match) for match in grammar._mention_re.finditer(text)),
        ],
        key=lambda item: item[0],
    )
    pieces: list[Piece] = []
    last_end = 0
    for start, end, is_mention, match in matches:
        before = text[last_end:start]
        if before.strip():
            pieces.append(TextPiece(before))
        if is_mention:
            mention = _mention_piece(match)
            if mention.tokens or mention.platform_id:
                pieces.append(mention)
        else:
            name = match.group(1).strip()
            if name:
                pieces.append(EmojiPiece(name))
        last_end = end
    after = text[last_end:]
    if after.strip():
        pieces.append(TextPiece(after))
    return pieces


def _mention_piece(match: re.Match[str]) -> MentionPiece:
    if match.group(1) is not None:
        return MentionPiece(tokens=split_mention_tokens(match.group(1)))
    platform_id = next((group for group in match.groups()[1:] if group is not None), "")
    return MentionPiece(platform_id=str(platform_id).strip())

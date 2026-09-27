"""QQ 的出站标记写法、收藏表情与输出文本清理。

标记解析在 adapters/_shared/outbound.py；这里补 QQ 自己的写法，把解析出的段渲染成待发的段，
收藏表情走 NapCat 的私有接口。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from adapters._shared.outbound import (
    EMOJI_LIBRARY,
    EMOJI_NATIVE,
    FOREIGN_CUSTOM_EMOJI,
    REACT_MARKER,
    MarkerGrammar,
    MentionPiece,
    ResolvedSticker,
    TextPiece,
    mention_as_text,
    native_emoji_segment,
    resolve_mention,
    split_mention_tokens,
    sticker_segment,
    strip_markup,
    tokenize,
)
from platforms.builtin import QQ_NAPCAT

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

QQ_MARKERS = MarkerGrammar(
    # QQ_EMOJI 和收藏表情是旧提示词里的写法，历史消息里还留着，模型会照抄。
    emoji_tags=("QQ_EMOJI", "表情", "emoji", "收藏表情"),
    # OneBot 的 CQ 码，里面已经是真实 QQ 号。
    native_mentions=(r"\[CQ:at,qq=(\d+)\]",),
    # 入站把内置表情渲染成 [QQ表情:微笑] 给模型看，模型会照抄回来；出站没有这个语义，漏出去就是一行裸标记。
    strip=(FOREIGN_CUSTOM_EMOJI, REACT_MARKER, r"\[QQ表情(?:\s*[:：]\s*[^\]]*)?\]"),
)


def has_emoji_markers(text: str) -> bool:
    return QQ_MARKERS.has_emoji(text)


# 输入别名/显示名，返回真实 QQ 号字符串；解析不到返回 None。
AtAliasResolver = Callable[[str], Optional[str]]
# 表情包名 -> 图库里挑中的一张，挑不出返回 None。和收藏表情谁先问由查找顺序定。
StickerResolver = Callable[[str], Awaitable[Optional[ResolvedSticker]]]


# fetch_custom_face_detail 在 NapCat 中会返回 QQ 收藏表情详情。该列表一般不会在一轮
# 回复内频繁变化，因此做一个短 TTL 缓存，避免模型连续输出多个 [表情:...] 时重复拉取。
_CUSTOM_FACE_CACHE_TTL = float(os.environ.get("ONEBOT_CUSTOM_FACE_CACHE_TTL", "60") or "60")


def _custom_face_cache_key(bot: Any) -> str:
    # 断线重连会换 Bot 实例，id(bot) 既会泄漏条目又会被复用命中别人的缓存。
    return str(getattr(bot, "self_id", "") or id(bot))


def _load_name_lines(path: str) -> List[str]:
    """读取一行一个名称的文本文件；空行和 # 注释会被忽略。"""
    if not path:
        return []
    try:
        content = Path(path).read_text("utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    names: List[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        norm = _normalize_name(item)
        if norm and norm not in seen:
            seen.add(norm)
            names.append(item)
    return names


def load_bqbs(path: str) -> List[str]:
    """读取旧 bqbs.txt 形式的 QQ 收藏表情顺序别名文件。"""
    return _load_name_lines(path)


def load_custom_face_names(path: str) -> List[str]:
    """读取 AI 可见的 QQ 收藏表情名称文件。"""
    return _load_name_lines(path)


def write_custom_face_names(path: str, names: List[str]) -> None:
    """写入 AI 可见的 QQ 收藏表情名称文件。"""
    if not path:
        return
    out: List[str] = []
    seen: set[str] = set()
    for name in names:
        item = str(name or "").strip()
        norm = _normalize_name(item)
        if item and norm and norm not in seen:
            seen.add(norm)
            out.append(item)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# QQ 收藏表情名称列表。",
        "# 一行一个名称；AI 只会看到并使用这里列出的名称。",
        "# 请用 QQ 命令“同步表情列表”从 NapCat 刷新；无名称表情不会写入。",
        "# 如需给未命名表情命名，请先用“表情详情列表”查序号，再发“命名表情 <序号> <名称>”。",
        "",
    ]
    target.write_text("\n".join(header + out) + ("\n" if out else ""), encoding="utf-8")


def load_custom_face_metadata(path: str) -> List[Dict[str, Any]]:
    """读取程序内部使用的 QQ 收藏表情元数据文件。"""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get("items") or data.get("faces") or []
    else:
        items = data
    if not isinstance(items, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            result.append(dict(item))
    return result


def write_custom_face_metadata(path: str, items: List[Dict[str, Any]]) -> None:
    """写入程序内部使用的 QQ 收藏表情元数据文件。"""
    if not path:
        return
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        norm = _normalize_name(name)
        if not name or not norm or norm in seen:
            continue
        seen.add(norm)
        clean = {k: v for k, v in item.items() if v not in (None, "", [])}
        clean["name"] = name
        out.append(clean)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "description": "QQ 收藏表情内部元数据。AI 不读取本文件；AI 只读取 qq_custom_faces.txt。",
        "items": out,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def strip_output_markers(
    text: str,
    *,
    strip_asterisk_styles: bool = True,
    strip_underscore_styles: bool = False,
    strip_markdown_styles: bool | None = None,
) -> str:
    """清理 QQ 渲染不了的标记，并按配置摊平 Markdown 样式。

    默认只去掉星号样式、完整保留下划线；``strip_markdown_styles`` 是旧调用方的总开关，给了就两者一起定。
    """
    if strip_markdown_styles is not None:
        strip_asterisk_styles = strip_markdown_styles
        strip_underscore_styles = strip_markdown_styles
    return strip_markup(
        text,
        QQ_MARKERS,
        keep_markdown=QQ_NAPCAT.capabilities.markdown,
        asterisk_styles=strip_asterisk_styles,
        underscore_styles=strip_underscore_styles,
    )


def _normalize_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _extract_list_payload(data: Any) -> List[Any]:
    """兼容 OneBot/NapCat 可能包一层 data/result/list 的返回结构。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("emojiInfoList", "emoji_info_list", "list", "items", "result", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_list_payload(value)
                if nested:
                    return nested
    return []


def _pick_first_string(data: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _custom_face_url(face: Any) -> str:
    """从 fetch_custom_face_detail/fetch_custom_face 返回项中取出可发送图片地址。

    NapCat 的详情字段会随版本变化；这里尽量兼容 url/file/image/originUrl 等常见字段。
    只要取到 HTTP(S)、base64、file:// 或本地路径，OneBot 的 image segment 就可以尝试发送。
    """
    if isinstance(face, str):
        return face
    if not isinstance(face, dict):
        return ""

    direct = _pick_first_string(face, (
        "url", "URL", "image", "file", "path", "emojiPath", "emoji_path",
        "originUrl", "origin_url", "originalUrl", "original_url", "thumbUrl", "thumb_url",
        "cdnUrl", "cdn_url", "downloadUrl", "download_url", "previewUrl", "preview_url",
    ))
    if direct:
        return direct

    # 有些实现会把可发送地址放在嵌套对象里。
    for value in face.values():
        if isinstance(value, dict):
            nested = _custom_face_url(value)
            if nested:
                return nested
    return ""


def _custom_face_field(face: Any, *keys: str) -> str:
    if not isinstance(face, dict):
        return ""
    for key in keys:
        value = face.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _custom_face_explicit_name(face: Any, index: int, bqbs: List[str]) -> str:
    """取适合展示给 AI 的显式名称；没有名称时返回空字符串。"""
    if index < len(bqbs):
        return str(bqbs[index] or "").strip()
    if isinstance(face, dict):
        for key in ("desc", "description", "name", "title", "alias"):
            value = str(face.get(key) or "").strip()
            if value:
                return value
    return ""


def custom_face_metadata_item(face: Any, index: int, bqbs: List[str]) -> Dict[str, Any] | None:
    """把已命名收藏表情详情压缩成可持久化的内部元数据；未命名返回 None。

    base_name 是 AI 可见的基础名；name 是内部唯一显示名（同名会加 (1)/(2) 后缀）。
    """
    name = _custom_face_explicit_name(face, index, bqbs)
    if not name:
        return None
    item: Dict[str, Any] = {"name": name, "base_name": name, "index": index + 1}
    if isinstance(face, dict):
        item.update({
            "emoji_id": _custom_face_field(face, "emojiId", "emoji_id", "emoId", "emoid"),
            "res_id": _custom_face_field(face, "resId", "res_id", "id"),
            "md5": _custom_face_field(face, "md5", "MD5"),
            "file_name": _custom_face_field(face, "fileName", "file_name"),
            "url": _custom_face_url(face),
        })
    elif isinstance(face, str):
        item["url"] = face
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def extract_named_custom_face_metadata(faces: List[Any], bqbs: List[str]) -> List[Dict[str, Any]]:
    """从收藏表情详情中提取已命名表情的内部元数据。

    允许同名共存：同名表情全部保留，并给内部显示名加 (1)/(2) 后缀。
    base_name 保持一致，供 AI 用统一名称调用。
    """
    items: List[Dict[str, Any]] = []
    name_counts: Dict[str, int] = {}
    for pos, face in enumerate(faces):
        item = custom_face_metadata_item(face, pos, bqbs)
        if not item:
            continue
        base_name = str(item.get("base_name") or item.get("name") or "").strip()
        norm = _normalize_name(base_name)
        if not norm:
            continue
        count = name_counts.get(norm, 0)
        name_counts[norm] = count + 1
        item["base_name"] = base_name
        item["name"] = base_name if count == 0 else f"{base_name}({count})"
        items.append(item)
    return items


def count_duplicate_face_names(metadata: List[Dict[str, Any]]) -> Dict[str, int]:
    """统计同名（base_name）表情数量，仅返回出现多次的名称。"""
    counts: Dict[str, int] = {}
    for item in metadata or []:
        base = str(item.get("base_name") or item.get("name") or "").strip()
        if base:
            counts[base] = counts.get(base, 0) + 1
    return {name: c for name, c in counts.items() if c > 1}


def _metadata_matches_face(meta: Dict[str, Any], face: Any) -> bool:
    if not isinstance(face, dict):
        return False
    for meta_key, face_keys in (
        ("md5", ("md5", "MD5")),
        ("res_id", ("resId", "res_id", "id")),
        ("emoji_id", ("emojiId", "emoji_id", "emoId", "emoid")),
        ("file_name", ("fileName", "file_name")),
    ):
        expected = str(meta.get(meta_key) or "").strip()
        if not expected:
            continue
        actual = _custom_face_field(face, *face_keys)
        if actual and actual == expected:
            return True
    return False


def _metadata_aliases(meta: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []
    for key in ("name", "base_name", "file_name", "emoji_id", "res_id", "md5"):
        value = str(meta.get(key) or "").strip()
        if not value:
            continue
        aliases.append(value)
        if key == "file_name":
            aliases.append(Path(value).stem)
        if key == "md5" and len(value) >= 8:
            aliases.append(value[:8])
    return aliases


def _metadata_pick_url(entry: Any) -> str:
    """从索引项取可发送 URL；命中同名候选组时随机选一个。"""
    if isinstance(entry, dict) and "__group__" in entry:
        group = [g for g in entry["__group__"] if str(g.get("url") or "").strip()]
        if not group:
            return ""
        return str(random.choice(group).get("url") or "").strip()
    if isinstance(entry, dict):
        return str(entry.get("url") or "").strip()
    return ""


def build_metadata_face_index(metadata: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构建“别名 -> 元数据项”的索引，可直接用元数据里的 URL 发送。

    基础名（base_name）会映射到一个候选列表，供发送时在同名表情中随机选择；
    其余唯一别名（name/md5/resId/file_name）仍直接映射到具体项。
    """
    index: Dict[str, Any] = {}
    base_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in metadata or []:
        if not isinstance(item, dict):
            continue
        base = str(item.get("base_name") or item.get("name") or "").strip()
        if base:
            base_groups.setdefault(_normalize_name(base), []).append(item)
        for alias in _metadata_aliases(item):
            index.setdefault(_normalize_name(alias), item)
    # 基础名映射为候选组，覆盖单项映射，实现同名随机。
    for norm, group in base_groups.items():
        index[norm] = {"__group__": group}
    return index


def _custom_face_aliases(face: Any, index: int, bqbs: List[str], preferred_names: List[str] | None = None) -> List[str]:
    aliases: List[str] = []
    if preferred_names and index < len(preferred_names):
        aliases.append(preferred_names[index])
    explicit = _custom_face_explicit_name(face, index, bqbs)
    if explicit:
        aliases.append(explicit)
    if isinstance(face, dict):
        for key in (
            "desc", "description", "name", "title", "alias", "fileName", "file_name",
            "emojiId", "emoji_id", "emoId", "emoid", "resId", "res_id", "md5",
        ):
            value = face.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            aliases.append(text)
            if key in {"fileName", "file_name"}:
                aliases.append(Path(text).stem)
            if key == "md5" and len(text) >= 8:
                aliases.append(text[:8])
    elif isinstance(face, str):
        aliases.append(Path(face.split("?", 1)[0]).stem)
    aliases.append(str(index + 1))

    # 去重并过滤空值。
    seen: set[str] = set()
    result: List[str] = []
    for alias in aliases:
        clean = str(alias or "").strip()
        norm = _normalize_name(clean)
        if clean and norm not in seen:
            seen.add(norm)
            result.append(clean)
    return result


def build_custom_face_index(
    faces: List[Any],
    bqbs: List[str],
    preferred_names: List[str] | None = None,
    metadata: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """构建“别名 -> 表情详情”的索引；元数据 md5/resId 命中的项优先。"""
    index: Dict[str, Any] = {}
    for item in metadata or []:
        for face in faces:
            if _metadata_matches_face(item, face):
                for alias in _metadata_aliases(item):
                    index.setdefault(_normalize_name(alias), face)
                break
    for pos, face in enumerate(faces):
        for alias in _custom_face_aliases(face, pos, bqbs, preferred_names):
            index.setdefault(_normalize_name(alias), face)
    return index


def extract_named_custom_face_names(faces: List[Any], bqbs: List[str]) -> List[str]:
    """从收藏表情详情中提取 AI 可见的基础名；同名去重，只保留一个基础名。"""
    names: List[str] = []
    seen: set[str] = set()
    for item in extract_named_custom_face_metadata(faces, bqbs):
        base = str(item.get("base_name") or item.get("name") or "").strip()
        norm = _normalize_name(base)
        if base and norm and norm not in seen:
            seen.add(norm)
            names.append(base)
    return names


def duplicated_detail_names(items: List[Dict[str, Any]]) -> set[str]:
    """出现次数 >1 的规范化展示名集合；未命名项名字自带位置后缀，不会误入。"""
    counts: Dict[str, int] = {}
    for item in items:
        norm = _normalize_name(item.get("name"))
        if norm:
            counts[norm] = counts.get(norm, 0) + 1
    return {norm for norm, times in counts.items() if times > 1}


def format_custom_face_detail_line(item: Dict[str, Any], duplicated: set[str]) -> str:
    """展示行。同名项追加 md5 前缀：序号会随收藏列表变动而漂移，md5 是稳定定位符。"""
    name = item.get("name") or ""
    md5 = str(item.get("md5") or "")
    line = f"{item.get('index')}. {name}"
    if md5 and _normalize_name(name) in duplicated:
        line += f"（md5:{md5[:8]}）"
    return line


class EmojiRenderer:
    """出站表情渲染：一个平台连接一份，持有 resolver 注入点和收藏表情缓存。"""

    def __init__(
        self,
        *,
        at_alias_resolver: AtAliasResolver | None = None,
        sticker_resolver: StickerResolver | None = None,
        custom_face_cache_ttl: float = _CUSTOM_FACE_CACHE_TTL,
    ) -> None:
        self.at_alias_resolver = at_alias_resolver
        self.sticker_resolver = sticker_resolver
        self.custom_face_cache_ttl = custom_face_cache_ttl
        # 账号 -> (拉取时间, 收藏表情详情)。
        self.custom_face_cache: Dict[str, tuple[float, List[Any]]] = {}

    def bind(self, *, at_alias_resolver: AtAliasResolver, sticker_resolver: StickerResolver) -> None:
        # 启动时才注入：两个 resolver 都要读启动阶段才加载好的别名表和表情包库。
        self.at_alias_resolver = at_alias_resolver
        self.sticker_resolver = sticker_resolver

    def resolve_at_token(self, token: str) -> str:
        """把 [at:] 里的一个名字解析成真实 QQ 号；全体成员返回 "all"，认不出返回空串。"""
        return resolve_mention(token, self.at_alias_resolver, id_shape=QQ_NAPCAT.id_shape)

    def resolve_at_tokens(self, raw_token: str) -> List[Dict[str, str]]:
        """一个 [at:] 里可以写几个名字，逐个解析；元素是 {"raw": 原名, "qq": QQ 号或空串}。"""
        return [{"raw": part, "qq": self.resolve_at_token(part)} for part in split_mention_tokens(raw_token)]

    def mention_segments(self, piece: MentionPiece) -> List[Dict[str, str]]:
        """一个提及标记渲染成的 at 段；认不出的名字留成「@名字」，几个 @ 之间补空格免得粘连。"""
        if not piece.tokens:
            return [{"type": "at", "qq": piece.platform_id}] if piece.platform_id else []
        segments: List[Dict[str, str]] = []
        for index, token in enumerate(piece.tokens):
            qq_id = self.resolve_at_token(token)
            if qq_id:
                segments.append({"type": "at", "qq": qq_id})
            else:
                segments.append({"type": "text", "content": mention_as_text(token)})
            if index < len(piece.tokens) - 1:
                segments.append({"type": "text", "content": " "})
        return segments

    def invalidate_custom_face_cache(self, bot: Any | None = None) -> None:
        """清空收藏表情详情缓存；添加/删除/改名收藏表情后调用。"""
        if bot is None:
            self.custom_face_cache.clear()
            return
        self.custom_face_cache.pop(_custom_face_cache_key(bot), None)

    async def fetch_custom_face_details(self, bot: Any, count: int = 200, *, force: bool = False) -> List[Any]:
        """获取 NapCat QQ 收藏表情详情列表。

        仅使用 NapCat 的 fetch_custom_face_detail 详情接口，返回带
        emojiId/resId/md5 等字段的详情对象；不再回退旧 fetch_custom_face。
        旧接口只返回图片 URL，缺少命名/删除所需字段，会导致
        命名表情/删除表情 无法执行，因此这里直接放弃回退。

        如果当前 OneBot/NapCat 端不支持该接口，会抛出异常，由上层
        给出“请确认 NapCat 支持 fetch_custom_face_detail”的明确提示。
        """
        key = _custom_face_cache_key(bot)
        now = time.time()
        cached = self.custom_face_cache.get(key)
        if not force and cached and now - cached[0] <= self.custom_face_cache_ttl:
            return list(cached[1])
        for stale in [k for k, (stamp, _faces) in self.custom_face_cache.items() if now - stamp > self.custom_face_cache_ttl]:
            self.custom_face_cache.pop(stale, None)

        detail = await bot.call_api("fetch_custom_face_detail", count=int(count))
        faces = _extract_list_payload(detail)
        self.custom_face_cache[key] = (now, faces)
        return list(faces)

    async def list_custom_face_details(self, bot: Any, bqbs: List[str], count: int = 200) -> List[Dict[str, Any]]:
        """返回收藏表情展示条目；index 是 fetch 顺序里的真实 1-based 位置，命名/删除表情按它定位。"""
        faces = await self.fetch_custom_face_details(bot, count=count)
        items: List[Dict[str, Any]] = []
        for pos, face in enumerate(faces):
            explicit = _custom_face_explicit_name(face, pos, bqbs)
            if explicit:
                name = explicit
            elif isinstance(face, str):
                name = f"未命名#{pos + 1}（仅URL，不可命名）"
            else:
                name = f"未命名#{pos + 1}"
            items.append({
                "index": pos + 1,
                "name": name,
                "md5": _custom_face_field(face, "md5", "MD5") if isinstance(face, dict) else "",
                "named": bool(explicit),
            })
        return items

    async def resolve_custom_face(self, bot: Any, name: str, bqbs: List[str], count: int = 200) -> Any | None:
        """按名称、描述、文件名、emojiId/resId/md5 或序号解析收藏表情。"""
        faces = await self.fetch_custom_face_details(bot, count=count)
        key = _normalize_name(name)
        if not key:
            return None
        index = build_custom_face_index(faces, bqbs)
        return index.get(key)

    async def find_custom_faces_by_base_name(
        self,
        bot: Any,
        name: str,
        bqbs: List[str],
        count: int = 200,
    ) -> List[Dict[str, Any]]:
        """按基础名找出全部同名收藏表情，返回带 1-based 序号的候选列表。

        序号与 list_custom_face_details / 表情详情列表 的展示序号一致，便于管理员指定。
        每项包含：index(1-based)、face(原始详情)、md5/res_id 等定位字段。
        """
        faces = await self.fetch_custom_face_details(bot, count=count)
        key = _normalize_name(name)
        if not key:
            return []
        matches: List[Dict[str, Any]] = []
        for pos, face in enumerate(faces):
            base = _custom_face_explicit_name(face, pos, bqbs)
            if base and _normalize_name(base) == key:
                matches.append({
                    "index": pos + 1,
                    "base_name": base,
                    "face": face,
                    "md5": _custom_face_field(face, "md5", "MD5") if isinstance(face, dict) else "",
                    "res_id": _custom_face_field(face, "resId", "res_id", "id") if isinstance(face, dict) else "",
                    "emoji_id": _custom_face_field(face, "emojiId", "emoji_id", "emoId", "emoid") if isinstance(face, dict) else "",
                    "file_name": _custom_face_field(face, "fileName", "file_name") if isinstance(face, dict) else "",
                })
        return matches

    async def _reply_face_index(
        self,
        bot: Any,
        bqbs: List[str],
        preferred_names: List[str] | None,
        metadata: List[Dict[str, Any]] | None,
    ) -> Dict[str, Any]:
        try:
            faces = await self.fetch_custom_face_details(bot)
        except Exception:
            logger.warning("收藏表情列表读取失败，尝试本地图库", exc_info=True)
            return {}
        return build_custom_face_index(faces, bqbs, preferred_names, metadata)

    async def process_emojis(
        self,
        text: str,
        bot: Any,
        bqbs: List[str],
        preferred_names: List[str] | None = None,
        metadata: List[Dict[str, Any]] | None = None,
        *,
        strip_asterisk_styles: bool = True,
        strip_underscore_styles: bool = False,
        strip_markdown_styles: bool | None = None,
        lookup: Sequence[str] = (EMOJI_NATIVE, EMOJI_LIBRARY),
    ) -> List[Dict[str, Any]]:
        """把一条回复渲染成待发的段：先剥掉渲染不了的写法，再按共享解析切出的段逐个渲染。

        表情名按 lookup 的先后去收藏表情和表情包库里找，出平台表情段或表情包段，发送时才翻成 OneBot 图片。
        返回 dict 而不是 MessageSegment，这个模块才不用依赖 NoneBot。
        """
        text = strip_output_markers(
            text,
            strip_asterisk_styles=strip_asterisk_styles,
            strip_underscore_styles=strip_underscore_styles,
            strip_markdown_styles=strip_markdown_styles,
        )
        segments: List[Dict[str, Any]] = []
        faces = _FaceLookup(self, bot, bqbs, preferred_names, metadata)

        for piece in tokenize(text, QQ_MARKERS):
            if isinstance(piece, TextPiece):
                segments.append({"type": "text", "content": piece.text})
                continue
            if isinstance(piece, MentionPiece):
                segments.extend(self.mention_segments(piece))
                continue

            segment = await self._resolve_emoji(piece.name, faces, lookup)
            if segment is not None:
                segments.append(segment)
            else:
                # 名字对不上说明是模型臆造的，留占位等于把内部格式发给用户，而提示词
                # 里正要求不要暴露它。丢掉标记只丢一个编出来的名字，留日志便于发现。
                logger.info("custom face not found, marker dropped: %s", piece.name)

        return segments

    async def _resolve_emoji(
        self, name: str, faces: "_FaceLookup", lookup: Sequence[str],
    ) -> Optional[Dict[str, str]]:
        """按查找顺序找一个表情名：收藏表情出平台表情段，图库出表情包段。"""
        for source in lookup:
            if source == EMOJI_NATIVE:
                url = await faces.url(name)
                if url:
                    return native_emoji_segment(QQ_NAPCAT.id, url)
            elif source == EMOJI_LIBRARY:
                sticker = await self._library_sticker(name)
                if sticker is not None:
                    return sticker_segment(sticker.sha256, sticker.content_sha256)
        return None

    async def _library_sticker(self, name: str) -> Optional[ResolvedSticker]:
        if self.sticker_resolver is None:
            return None
        try:
            resolved = await self.sticker_resolver(name)
        except Exception:
            logger.warning("sticker resolve failed: %s", name, exc_info=True)
            return None
        # 缺了内容哈希，发送前就核对不了选中的还是不是这张图。
        if isinstance(resolved, ResolvedSticker) and resolved.sha256 and resolved.content_sha256:
            return resolved
        return None


class _FaceLookup:
    """一条回复里查收藏表情：先用落盘的元数据，查不到再拉实时列表，整条回复最多拉一次。"""

    def __init__(
        self,
        renderer: EmojiRenderer,
        bot: Any,
        bqbs: List[str],
        preferred_names: List[str] | None,
        metadata: List[Dict[str, Any]] | None,
    ) -> None:
        self._renderer = renderer
        self._bot = bot
        self._bqbs = bqbs
        self._preferred_names = preferred_names
        self._metadata = metadata
        # 优先用持久化元数据里的 URL 直接发送，避免每条消息都拉 NapCat 详情。
        self._index: Dict[str, Any] | None = build_metadata_face_index(metadata) if metadata else None
        self._live_loaded = False

    async def url(self, name: str) -> str:
        key = _normalize_name(name)
        if self._index is None:
            self._index = await self._live_index()
        url, face = _face_hit(self._index.get(key))
        if not url and face is None and not self._live_loaded:
            # 元数据未命中时回退实时详情，兼容名称文件与收藏列表短暂不一致。
            self._index.update(await self._live_index())
            url, face = _face_hit(self._index.get(key))
        if not url and face is not None:
            url = _custom_face_url(face)
        return url

    async def _live_index(self) -> Dict[str, Any]:
        self._live_loaded = True
        return await self._renderer._reply_face_index(self._bot, self._bqbs, self._preferred_names, self._metadata)


def _face_hit(entry: Any) -> tuple[str, Any]:
    """元数据候选组同名随机选一个 URL；实时详情原样交回，由调用方取地址。"""
    if isinstance(entry, dict) and "__group__" in entry:
        return _metadata_pick_url(entry), None
    return "", entry

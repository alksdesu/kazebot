"""QQ 表情和输出文本清理工具。

Clonoth 后端可能输出 QQ 表情标记、Discord 表情标记、Reaction 标记或 Markdown。
本模块在发往 QQ 前统一处理这些内容，目的是让 QQ 群只看到可渲染的纯文本
和可发送的自定义/收藏表情图片。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

# 模型易写的收藏表情标记。兼容旧格式 [QQ_EMOJI:名称]，并新增：
# [表情: 开心] / [emoji: 开心] / [收藏表情: 开心]
_QQ_EMOJI_RE = re.compile(r"\[(?:QQ_EMOJI|表情|emoji|收藏表情)\s*[:：]\s*(.+?)\]", re.IGNORECASE)

# 入站把内置表情渲染成 [QQ表情:微笑] 给模型看，模型会照抄回来；出站没有这个语义，漏出去就是一行裸标记。
_INBOUND_FACE_RE = re.compile(r"\[QQ表情(?:\s*[:：]\s*[^\]]*)?\]")

# Clonoth 模型输出 [at:QQ号] 格式的 at 标记。QQ 端需要将其
# 转换为 OneBot MessageSegment.at()，否则只会被当作纯文本发送。
# 同时兼容旧的 [CQ:at,qq=xxx] 格式。
#
# 2026-07-14 扩展：模型看到的是匿名别名（UserA/UserAF 等），很容易输出
# [at:UserAF] 而不是真实 QQ 号。旧正则只匹配纯数字，导致这类 @ 标记
# 被当成纯文本原样发出（群里看到 "@UserAF"）。这里允许方括号内为
# 数字或非数字别名/显示名，非数字部分在 process_emojis 里通过
# _at_alias_resolver 反查为真实 QQ 号（后续由 NapCat 渲染为群昵称）。
_AT_RE = re.compile(r"\[at:([^\]]+)\]|\[CQ:at,qq=(\d+)\]")

# at 别名反查回调：由 __init__.py 在导入后注入，输入别名/显示名，返回真实 QQ 号
# 字符串；无法解析时返回 None。默认 None 表示未注入（只识别纯数字 QQ 号）。
_at_alias_resolver: Any = None


def set_at_alias_resolver(resolver: Any) -> None:
    """注入 at 别名 -> 真实 QQ 号 的解析回调。"""
    global _at_alias_resolver
    _at_alias_resolver = resolver


# 表情包名 -> 可发送地址的解析回调，同样由 __init__.py 注入。收藏表情查不到时才问它，
# 所以同名的收藏表情优先级更高。
_sticker_resolver: Any = None


def set_sticker_resolver(resolver: Any) -> None:
    """注入表情包名 -> 可发送地址（base64:// 或 http）的解析回调。"""
    global _sticker_resolver
    _sticker_resolver = resolver


# 一个 [at:...] 标记内多个目标的分隔符：英文逗号、中文逗号、顿号、分号、或连续空白。
# 例如 [at:UserA,UserB、木] 会拆成 ["UserA", "UserB", "张三"]。
_AT_TOKEN_SPLIT_RE = re.compile(r"[,\uFF0C\u3001;\uFF1B]+|\s{2,}")


# 可能表示“@全体成员”的关键字（小写比较）。注意：这些词也可能恰好是某个
# 群友的昵称/别名（如有人就叫 "all"），因此不能直接当全体成员，
# 而是先走用户反查，反查不到时才视为 @全体成员。
_AT_ALL_KEYWORDS = {"all", "at_all", "atall", "全体成员", "@全体成员"}


def _resolve_at_token(token: str) -> str:
    """把 [at:xxx] 里的单个 token 解析为真实 QQ 号字符串。

    解析优先级（2026-07-14 调整）：
    - 纯数字：直接返回（视为真实 QQ 号）。
    - 其余（含 all/全体成员 等关键字）：**先**调用 resolver 按别名/显示名/群昵称
      反查真实用户；命中就 @ 那个人（避免昵称叫 all 的群友被误伤）。
    - 反查不到且 token 是 @全体关键字：返回 "all"，交由上层生成 @全体成员。
    - 其余反查失败：返回空串。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    # 兼容模型可能写成 [at:@UserA] 的情况。
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    # 先按用户反查（包括昵称恰好叫 all 的情况）。
    if _at_alias_resolver is not None:
        try:
            resolved = _at_alias_resolver(raw)
        except Exception:
            resolved = None
        if resolved:
            resolved = str(resolved).strip()
            if resolved:
                return resolved
    # 用户反查不到，且确实是 @全体关键字时，才视为 @全体成员。
    if raw.lower() in _AT_ALL_KEYWORDS:
        return "all"
    return ""


def _resolve_at_tokens(raw_token: str) -> List[Dict[str, str]]:
    """把一个 [at:...] 标记内容拆成多个目标并逐个解析。

    2026-07-14 新增：支持“一条消息 @ 多个用户”。一个标记里可写多个
    名字（逗号/顿号/分号分隔），每个名字独立反查真实 QQ 号。

    返回每个子 token 的结果列表，元素为 {"raw": 原始子串, "qq": 解析后 QQ 号或空串}。
    若整个标记不含分隔符，则列表只有一项（完全兼容单个 @）。
    """
    raw = str(raw_token or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in _AT_TOKEN_SPLIT_RE.split(raw) if p.strip()]
    if not parts:
        parts = [raw]
    return [{"raw": part, "qq": _resolve_at_token(part)} for part in parts]

# Discord 自定义表情在 QQ 无法渲染。这里直接剥离，避免群内出现平台私有格式。
_DC_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

# [REACT:...] 反应标记。QQ 端现在支持 reaction，由 _extract_reactions 提取后执行。
_REACT_RE = re.compile(r"\[REACT:[^\]]+\]")

# fetch_custom_face_detail 在 NapCat 中会返回 QQ 收藏表情详情。该列表一般不会在一轮
# 回复内频繁变化，因此做一个短 TTL 缓存，避免模型连续输出多个 [表情:...] 时重复拉取。
_CUSTOM_FACE_CACHE_TTL = float(os.environ.get("ONEBOT_CUSTOM_FACE_CACHE_TTL", "60") or "60")
_custom_face_cache: Dict[str, tuple[float, List[Any]]] = {}


def _custom_face_cache_key(bot: Any) -> str:
    # 断线重连会换 Bot 实例，id(bot) 既会泄漏条目又会被复用命中别人的缓存。
    return str(getattr(bot, "self_id", "") or id(bot))


def _extract_reactions(text: str) -> tuple[str, List[str]]:
    """从文本中提取 [REACT:emoji_id] 标记，返回 (清理后文本, emoji_id列表)。"""
    reactions: List[str] = []

    def _repl(m: re.Match) -> str:
        inner = m.group(0)[7:-1].strip()  # 去掉 [REACT: 和 ]
        if inner:
            reactions.append(inner)
        return ""

    cleaned = _REACT_RE.sub(_repl, text).strip()
    return cleaned, reactions


# Markdown 在 QQ 中不会按预期渲染。以下规则只做轻量剥离，保留正文内容。
_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
_UNDERLINE_BOLD_RE = re.compile(r"__(.*?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", re.DOTALL)
_UNDERLINE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)


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
    """清理平台专用标记，并按配置选择是否剥离 Markdown 样式。

    默认清理 ``*斜体*`` / ``**粗体**`` 的外层星号，但完整保留下划线。
    单下划线常用于 PUB_CACHE、user_id 等标识符；解析它可能跨越多处标识符
    并误删内容。``strip_markdown_styles`` 仅作为旧调用方的兼容总开关。
    """
    if not text:
        return ""

    if strip_markdown_styles is not None:
        strip_asterisk_styles = strip_markdown_styles
        strip_underscore_styles = strip_markdown_styles

    text = _DC_EMOJI_RE.sub("", text)
    text = _REACT_RE.sub("", text)
    text = _INBOUND_FACE_RE.sub("", text)
    text = _CODE_BLOCK_RE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
    if strip_asterisk_styles:
        text = _BOLD_RE.sub(lambda m: m.group(1), text)
        text = _ITALIC_RE.sub(lambda m: m.group(1), text)
    if strip_underscore_styles:
        text = _UNDERLINE_BOLD_RE.sub(lambda m: m.group(1), text)
        text = _UNDERLINE_ITALIC_RE.sub(lambda m: m.group(1), text)
    text = _HEADING_RE.sub("", text)
    return text.strip()


def invalidate_custom_face_cache(bot: Any | None = None) -> None:
    """清空收藏表情详情缓存；添加/删除/改名收藏表情后调用。"""
    if bot is None:
        _custom_face_cache.clear()
        return
    _custom_face_cache.pop(_custom_face_cache_key(bot), None)


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


async def fetch_custom_face_details(bot: Any, count: int = 200, *, force: bool = False) -> List[Any]:
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
    cached = _custom_face_cache.get(key)
    if not force and cached and now - cached[0] <= _CUSTOM_FACE_CACHE_TTL:
        return list(cached[1])
    for stale in [k for k, (stamp, _faces) in _custom_face_cache.items() if now - stamp > _CUSTOM_FACE_CACHE_TTL]:
        _custom_face_cache.pop(stale, None)

    detail = await bot.call_api("fetch_custom_face_detail", count=int(count))
    faces = _extract_list_payload(detail)
    _custom_face_cache[key] = (now, faces)
    return list(faces)


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


async def list_custom_face_details(bot: Any, bqbs: List[str], count: int = 200) -> List[Dict[str, Any]]:
    """返回收藏表情展示条目；index 是 fetch 顺序里的真实 1-based 位置，命名/删除表情按它定位。"""
    faces = await fetch_custom_face_details(bot, count=count)
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


async def resolve_custom_face(bot: Any, name: str, bqbs: List[str], count: int = 200) -> Any | None:
    """按名称、描述、文件名、emojiId/resId/md5 或序号解析收藏表情。"""
    faces = await fetch_custom_face_details(bot, count=count)
    key = _normalize_name(name)
    if not key:
        return None
    index = build_custom_face_index(faces, bqbs)
    return index.get(key)


async def find_custom_faces_by_base_name(
    bot: Any,
    name: str,
    bqbs: List[str],
    count: int = 200,
) -> List[Dict[str, Any]]:
    """按基础名找出全部同名收藏表情，返回带 1-based 序号的候选列表。

    序号与 list_custom_face_details / 表情详情列表 的展示序号一致，便于管理员指定。
    每项包含：index(1-based)、face(原始详情)、md5/res_id 等定位字段。
    """
    faces = await fetch_custom_face_details(bot, count=count)
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


async def process_emojis(
    text: str,
    bot: Any,
    bqbs: List[str],
    preferred_names: List[str] | None = None,
    metadata: List[Dict[str, Any]] | None = None,
    *,
    strip_asterisk_styles: bool = True,
    strip_underscore_styles: bool = False,
    strip_markdown_styles: bool | None = None,
) -> List[Dict[str, Any]]:
    """处理文本中的 QQ 收藏表情标记，返回可发送的消息段描述。

    处理方式如下：
    1. 先移除 Discord 表情、Reaction 标记和 Markdown 标记。
    2. 遇到 [表情:name] / [emoji:name] / [QQ_EMOJI:name] 时拆分文本。
    3. 通过 NapCat fetch_custom_face_detail 获取收藏表情详情，并按别名索引取图。

    返回 dict 而不是直接返回 MessageSegment，是为了让该模块保持轻量，避免
    在工具层直接依赖 NoneBot2 的消息段实现。
    """
    text = strip_output_markers(
        text,
        strip_asterisk_styles=strip_asterisk_styles,
        strip_underscore_styles=strip_underscore_styles,
        strip_markdown_styles=strip_markdown_styles,
    )
    segments: List[Dict[str, Any]] = []
    last_end = 0
    # 优先用持久化元数据里的 URL 直接发送，避免每条消息都拉 NapCat 详情。
    face_index: Dict[str, Any] | None = build_metadata_face_index(metadata) if metadata else None
    live_loaded = False

    # 合并 QQ 收藏表情和 at 标记，按位置排序统一处理。
    emoji_matches = [(m.start(), m.end(), "emoji", m) for m in _QQ_EMOJI_RE.finditer(text)]
    at_matches = [(m.start(), m.end(), "at", m) for m in _AT_RE.finditer(text)]
    all_matches = sorted(emoji_matches + at_matches, key=lambda x: x[0])

    for start, end, match_type, match in all_matches:
        before = text[last_end:start]
        if before.strip():
            segments.append({"type": "text", "content": before})

        if match_type == "at":
            # group(1) 匹配 [at:xxx]（xxx 可能是 QQ 号/别名/显示名，也可以是多个
            # 名字用逗号/顿号/分号分隔）；group(2) 匹配旧格式 [CQ:at,qq=xxx]（仅数字）。
            raw_token = match.group(1)
            if raw_token is not None:
                # 支持一个标记 @ 多人：逐个拆分、解析、生成多个 at 段。
                resolved_list = _resolve_at_tokens(raw_token)
                for idx, item in enumerate(resolved_list):
                    qq_id = item.get("qq") or ""
                    if qq_id:
                        segments.append({"type": "at", "qq": qq_id})
                    else:
                        # 单个名字无法解析时保留可读文本，避免静默丢失。
                        fallback = str(item.get("raw") or "").strip()
                        if fallback:
                            segments.append({"type": "text", "content": f"@{fallback}"})
                    # 多个 @ 之间补一个空格，避免在部分客户端粘连成一块。
                    if idx < len(resolved_list) - 1:
                        segments.append({"type": "text", "content": " "})
            else:
                qq_id = str(match.group(2) or "").strip()
                if qq_id:
                    segments.append({"type": "at", "qq": qq_id})
            last_end = end
            continue

        name = match.group(1).strip()
        if name:
            key = _normalize_name(name)
            if face_index is None:
                faces = await fetch_custom_face_details(bot)
                face_index = build_custom_face_index(faces, bqbs, preferred_names, metadata)
                live_loaded = True
            entry = face_index.get(key)
            # 元数据候选组：同名随机选一个 URL 直接发送。
            url = _metadata_pick_url(entry) if isinstance(entry, dict) and "__group__" in entry else ""
            face = None if (isinstance(entry, dict) and "__group__" in entry) else entry
            if not url and face is None and not live_loaded:
                # 元数据未命中时回退实时详情，兼容名称文件与收藏列表短暂不一致。
                faces = await fetch_custom_face_details(bot)
                live_index = build_custom_face_index(faces, bqbs, preferred_names, metadata)
                face_index.update(live_index)
                live_loaded = True
                entry = face_index.get(key)
                url = _metadata_pick_url(entry) if isinstance(entry, dict) and "__group__" in entry else ""
                face = None if (isinstance(entry, dict) and "__group__" in entry) else entry
            if not url:
                url = _custom_face_url(face) if face is not None else ""
            if not url and _sticker_resolver is not None:
                try:
                    url = await _sticker_resolver(name) or ""
                except Exception:
                    logger.warning("sticker resolve failed: %s", name, exc_info=True)
            if url:
                # 标记 emoji=True：这是 QQ 收藏表情，发送时走 image 表情子类型
                # （sub_type=1），让客户端按小图/贴纸渲染，而不是普通大图。
                segments.append({"type": "image", "url": url, "emoji": True})
            else:
                # 名字对不上说明是模型臆造的，留占位等于把内部格式发给用户，而提示词
                # 里正要求不要暴露它。丢掉标记只丢一个编出来的名字，留日志便于发现。
                logger.info("custom face not found, marker dropped: %s", name)

        last_end = end

    after = text[last_end:]
    if after.strip():
        segments.append({"type": "text", "content": after})

    return segments

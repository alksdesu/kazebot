"""入站图片与文件的解析、下载与近期缓存。"""
from __future__ import annotations

import logging
import asyncio
import base64
import binascii
import os
import re
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping
from urllib.parse import unquote, urlsplit
import httpx
from httpx import HTTPStatusError, TimeoutException
from nonebot.adapters.onebot.v11 import Bot, Event
from engine.attachments import sniff_image_mime
from ..config import (
    CLONOTH_WORKSPACE,
    IMAGE_CACHE_TTL_SECONDS,
    IMAGE_DOWNLOAD_TIMEOUT,
    LOCAL_SOURCE_ROOTS,
    RECENT_FILE_MAX_AGE_SECONDS,
    RECENT_IMAGE_MAX_AGE_SECONDS,
)
from ..live_config import live
from ..attachment_policy import (
    inbound_file_reject_reason,
    looks_like_file_query,
    looks_like_image_query,
    select_recent_attachment_entries,
    should_fallback_to_recent_attachments,
)

from . import common, identity, message_text, models, outbound_attachments, proactive, routing, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


_IMAGE_EXT_BY_MIME = MappingProxyType({
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
})


def _attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"图片太大，已超过当前限制 {live.image_max_bytes // 1024 // 1024}MB。"
    if error == "download_failed":
        return "我收到了图片，但下载失败了，可能是 QQ 临时链接已过期。"
    if error == "unsupported_mime":
        return "我收到了图片，但它不是我能识别的图片格式（只支持 PNG/JPEG/GIF/WEBP/BMP）。"
    if error == "local_denied":
        return "我收到了图片，但它指向 Clonoth 工作区之外的本机路径，已拒绝读取（如为同机部署，请把该目录加入 ONEBOT_LOCAL_SOURCE_ROOTS）。"
    return "我收到了图片，但处理图片时失败了。"


def _touch_attachment_files(attachments: Iterable[Any]) -> None:
    """把被引用的附件 mtime 推到现在：适配层和 engine data_cleanup 都只按 mtime 判龄，续期是唯一能同时对两边生效的手段。"""
    now = time.time()
    for attachment in attachments:
        path = outbound_attachments._resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            os.utime(path, (now, now))
        except OSError:
            continue


def _cleanup_old_qq_attachments(now: float | None = None) -> None:
    """清理 QQ 会话附件目录中的过期文件；默认保留 IMAGE_CACHE_TTL_SECONDS。"""
    now = time.time() if now is None else now
    if now - state._adapter.last_attachment_cleanup_at < models._ATTACHMENT_CLEANUP_INTERVAL_SEC:
        return
    state._adapter.last_attachment_cleanup_at = now
    root = Path(CLONOTH_WORKSPACE) / "data" / "attachments"
    if not root.exists():
        return
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    try:
        for conv_dir in root.iterdir():
            # data/attachments 同时住着 engine 产物和其他适配器的附件，只清 QQ 自己的会话目录。
            if not conv_dir.is_dir() or not conv_dir.name.startswith(models._QQ_ATTACHMENT_DIR_PREFIXES):
                continue
            for p in conv_dir.rglob("*"):
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    continue
            for sub in sorted(
                (x for x in conv_dir.rglob("*") if x.is_dir()),
                key=lambda x: len(x.parts), reverse=True,
            ):
                try:
                    sub.rmdir()
                except Exception:
                    pass
            try:
                conv_dir.rmdir()
            except Exception:
                pass
    except Exception as exc:
        common.logger.debug("QQ attachment cleanup skipped: %s", exc)


def _remember_recent_images(conversation_key: str, event: Event, attachments: List[Dict[str, Any]]) -> None:
    if not attachments:
        return
    # sender_key 用于"同一发送者"判定（匿名成员之间不互相命中）；sender_qq 会被当真实 QQ 号
    # 塞进转发 node，占位号发不出去，匿名时留空。
    sender_key = identity._event_sender_key(event)
    sender_qq = "" if identity._anonymous_identity(event) else str(getattr(event, "user_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    now = time.time()
    q = state._adapter.recent_images[conversation_key]
    fq = state._adapter.recent_files[conversation_key]
    image_atts: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for att in attachments:
        if not att.get("path"):
            continue
        att_copy = dict(att)
        kind = str(att.get("type") or "")
        if kind == "image":
            image_atts.append(att_copy)
            q.append(models.RecentAttachmentEntry(att_copy, now, sender_key, message_id))
        elif kind == "file":
            fq.append(models.RecentAttachmentEntry(att_copy, now, sender_key, message_id))
        else:
            continue
        kept.append(att_copy)
    state._adapter.recent_images.touch(conversation_key)
    state._adapter.recent_files.touch(conversation_key)
    # 引用索引收全部类型：引用一条文件消息再问，和引用图片一样是明确的指向。
    if kept and message_id:
        _remember_reply_attachments(message_id, conversation_key, sender_qq, kept, created_at=now)


def _remember_reply_attachments(
    message_id: Any,
    conversation_key: str,
    sender_id: str,
    attachments: List[Dict[str, Any]],
    *,
    created_at: float | None = None,
) -> None:
    """持久化引用消息附件索引，供后续主动转发兜底使用。"""
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    now = time.time() if created_at is None else float(created_at)
    kept = [dict(att) for att in attachments if isinstance(att, dict) and att.get("path")]
    if not kept:
        return
    # 旧图绑到新消息时把 mtime 推到现在，否则按 mtime 判龄的清理会在保留期内先删掉源文件。
    _touch_attachment_files(kept)
    images = [att for att in kept if str(att.get("type") or "") != "file"]
    files = [att for att in kept if str(att.get("type") or "") == "file"]
    state._adapter.reply_attachment_cache[mid] = {
        "conversation_key": str(conversation_key or ""),
        "sender_id": str(sender_id or ""),
        "created_at": now,
        # 两类各按各的上限截断：混在一起用图片那个数，三个文件就能把图全挤掉。
        "attachments": images[:live.max_images_per_turn] + files[:live.max_files_per_turn],
    }
    _trim_reply_attachment_cache()
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(routing._save_reply_attachment_cache())
    except Exception:
        pass


def _trim_reply_attachment_cache() -> None:
    if not state._adapter.reply_attachment_cache:
        return
    now = time.time()
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    for mid, item in list(state._adapter.reply_attachment_cache.items()):
        try:
            created_at = float(item.get("created_at") or 0.0)
        except Exception:
            created_at = 0.0
        if created_at and created_at < cutoff:
            state._adapter.reply_attachment_cache.pop(mid, None)
    while len(state._adapter.reply_attachment_cache) > 1000:
        oldest = min(
            state._adapter.reply_attachment_cache,
            key=lambda key: float(state._adapter.reply_attachment_cache.get(key, {}).get("created_at") or 0.0),
        )
        state._adapter.reply_attachment_cache.pop(oldest, None)


def _forward_nodes_from_cached_reply(bot: Bot, reply_message_id: Any) -> list[dict[str, Any]]:
    mid = str(reply_message_id or "").strip()
    if not mid:
        return []
    item = state._adapter.reply_attachment_cache.get(mid)
    if not isinstance(item, dict):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    if not attachments:
        return []
    node = proactive._make_forward_node(
        bot,
        "",
        [dict(att) for att in attachments if isinstance(att, dict)],
        nickname="引用图片",
        user_id=item.get("sender_id") or getattr(bot, "self_id", "") or "10000",
    )
    return [node] if node else []


def _cached_reply_image_attachments(message_id: Any, conversation_key: str = "") -> List[Dict[str, Any]]:
    """Return persisted source images bound to an inbound or Bot reply message."""
    mid = str(message_id or "").strip()
    if not mid:
        return []
    item = state._adapter.reply_attachment_cache.get(mid)
    if not isinstance(item, dict):
        return []
    cached_conversation = str(item.get("conversation_key") or "")
    if conversation_key and cached_conversation and cached_conversation != str(conversation_key):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    result = [
        dict(att)
        for att in attachments[:live.max_images_per_turn]
        if isinstance(att, dict)
        and str(att.get("type") or "") == "image"
        and att.get("path")
        and (p := outbound_attachments._resolve_attachment_path(att)) is not None
        and p.exists()
    ]
    _touch_attachment_files(result)
    return result


def _recent_images_for_text(conversation_key: str, event: Event) -> List[Dict[str, Any]]:
    """Return one unambiguous recent image batch from the current sender only."""
    return select_recent_attachment_entries(
        state._adapter.recent_images.get(conversation_key, ()),
        sender_id=identity._event_sender_key(event),
        now=time.time(),
        max_age_seconds=RECENT_IMAGE_MAX_AGE_SECONDS,
        max_items=live.max_images_per_turn,
    )


def _recent_files_for_text(conversation_key: str, event: Event) -> List[Dict[str, Any]]:
    """Return one unambiguous recent file batch from the current sender only."""
    return select_recent_attachment_entries(
        state._adapter.recent_files.get(conversation_key, ()),
        sender_id=identity._event_sender_key(event),
        now=time.time(),
        max_age_seconds=RECENT_FILE_MAX_AGE_SECONDS,
        max_items=live.max_files_per_turn,
    )


def _text_looks_like_image_query(text: str) -> bool:
    return looks_like_image_query(text)


def _text_looks_like_file_query(text: str) -> bool:
    return looks_like_file_query(text)


async def _merge_recent_attachments_after_text(
    *,
    event: Event,
    conversation_key: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
) -> None:
    reply_message_id = message_text._extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    # A reply must stay on its explicit message chain. _build_reply_context will
    # recover an image directly from the quoted message or from the Bot reply's
    # persisted source-image binding. Falling back to an unrelated recent image
    # here caused "再仔细看看图" to replace the intended PNG with an older GIF.
    if should_fallback_to_recent_attachments(
        has_attachments=bool(attachments),
        input_enabled=live.enable_image_input,
        looks_like_query=_text_looks_like_image_query(user_text),
        reply_message_id=reply_message_id,
    ):
        if live.image_wait_after_text_sec > 0:
            await asyncio.sleep(live.image_wait_after_text_sec)
        attachments.extend(_recent_images_for_text(conversation_key, event))

    # 文件走同一套约束，但不等：群文件那条消息早就发完了，不像图那样可能还在路上。
    # 上面真取到图时 has_attachments 已经变真，这一段自己就不会再进。
    if should_fallback_to_recent_attachments(
        has_attachments=bool(attachments),
        input_enabled=live.enable_file_input,
        looks_like_query=_text_looks_like_file_query(user_text),
        reply_message_id=reply_message_id,
    ):
        attachments.extend(_recent_files_for_text(conversation_key, event))


def _segment_image_url(data: Mapping[str, Any]) -> str:
    return str(data.get("url") or data.get("path") or data.get("file") or "").strip()


def _attachment_label(attachment: Mapping[str, Any]) -> str:
    """回填进正文时的名字。表情包和照片对模型是两回事：一个是情绪，一个是内容。"""
    return "表情包" if attachment.get("sticker") else "图片"


def _iter_qq_image_sources(message: Any) -> List[models.ImageSource]:
    """从 OneBot Message、segment list 或 CQ 字符串中提取图片下载地址。"""
    sources: List[models.ImageSource] = []
    for seg_type, data in message_text._iter_segments(message):
        if seg_type not in message_text.IMAGE_SEGMENT_TYPES:
            continue
        url = _segment_image_url(data)
        if url:
            sources.append(models.ImageSource(url, seg_type in message_text.STICKER_SEGMENT_TYPES))
    return sources


def _safe_attachment_name(name: str, default: str = "attachment") -> str:
    """清理 QQ 文件名，避免路径穿越和控制字符进入附件目录。"""
    raw = Path(str(name or "").replace("\\", "/")).name.strip().strip(". ")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw:
        raw = default
    return raw[:120]


def _segment_file_source(data: Mapping[str, Any]) -> Dict[str, Any] | None:
    """保留下载来源和文件 ID，文件名不作为网络地址。"""
    file_id = str(data.get("file_id") or data.get("id") or "").strip()
    candidates = []
    for key in ("url", "path", "file"):
        value = str(data.get(key) or "").strip()
        if value and not (key == "file" and value == file_id) and _file_source_kind(value) and value not in candidates:
            candidates.append(value)
    src = candidates[0] if candidates else ""
    name = str(data.get("name") or data.get("file_name") or data.get("filename") or "").strip()
    file_value = str(data.get("file") or "").strip()
    if not name and file_value and file_value != file_id and not _file_source_kind(file_value):
        name = file_value
    name_inferred = not name and bool(src)
    if not name and src:
        name = Path(unquote(urlsplit(src).path) if src.lower().startswith(("http://", "https://", "file://")) else src).name
    if not src and not name and not file_id:
        return None
    return {"source": src, "sources": candidates, "file_id": file_id, "name_inferred": name_inferred,
            "name": _safe_attachment_name(name, "file") if name else "", "size": _file_size(data)}


def _file_size(data: Mapping[str, Any]) -> int:
    for key in ("size", "file_size", "filesize"):
        try:
            value = data.get(key)
            if value is not None and not isinstance(value, bool):
                return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def _file_source_kind(source: str) -> str:
    if source.startswith("base64://"):
        return "base64"
    if source.startswith("file://") or source.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", source):
        return "local"
    try:
        url = urlsplit(source)
        if url.scheme.lower() in {"http", "https"} and url.hostname:
            return "remote"
    except ValueError:
        pass
    return ""


def _iter_qq_file_sources(message: Any) -> List[Dict[str, Any]]:
    """从 OneBot file 段中提取可下载/可复制的普通文件来源。"""
    sources: List[Dict[str, Any]] = []
    for seg_type, data in message_text._iter_segments(message):
        if seg_type != "file":
            continue
        source = _segment_file_source(data)
        if source is not None:
            sources.append(source)
    return sources


def _file_attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"文件太大，已超过当前限制 {live.file_max_bytes // 1024 // 1024}MB。"
    if error == "no_source":
        return "我收到了文件消息，但 OneBot 未提供可用的下载地址或文件 ID。"
    if error == "resolve_failed":
        return "无法通过文件 ID 从 NapCat 获取文件来源，请重新发送文件后重试。"
    if error == "resolve_timeout":
        return "向 NapCat 查询文件来源超时，请稍后重试。"
    if error == "download_timeout":
        return "文件下载超时，请稍后重试。"
    if error.startswith("http_") and error[5:].isdigit():
        return f"文件下载失败，文件服务返回 HTTP {error[5:]}。"
    if error == "invalid_source":
        return "文件来源不是有效的 HTTP(S) 地址、本地路径或 base64 数据。"
    if error == "invalid_content":
        return "NapCat 返回的文件数据格式无效，无法解码。"
    if error == "size_mismatch":
        return "取得的文件大小与上报大小不一致，可能下载不完整，请重新发送后重试。"
    if error == "local_unavailable":
        return "NapCat 返回的本地文件路径在机器人进程中不可访问，请检查容器文件挂载或下载接口。"
    if error == "storage_failed":
        return "文件读取或保存失败，请检查机器人的目录权限和磁盘空间。"
    if error == "download_failed":
        return "文件下载失败，请稍后重试；具体原因已记录日志。"
    if error == "unsupported_type":
        return "这个文件类型我不接收，只收文档、文本、图片、音视频和压缩包。"
    if error == "executable_content":
        return "这个文件的内容是可执行程序，我不会接收。"
    if error == "local_denied":
        return "我收到了文件，但它指向 Clonoth 工作区之外的本机路径，已拒绝读取（如为同机部署，请把该目录加入 ONEBOT_LOCAL_SOURCE_ROOTS）。"
    return "我收到了文件，但处理文件时失败了。"


def _local_source_path(source: str) -> Path | None:
    """把 file:// / 本地绝对路径解析成允许读取的真实文件；越界或不存在返回 None。

    事件里的 path 字段由 OneBot 实现决定，只允许工作区与显式配置的 ONEBOT_LOCAL_SOURCE_ROOTS，
    否则等于按对端内容读本机任意文件。
    """
    raw = source[7:] if source.startswith("file://") else source
    try:
        resolved = Path(raw).resolve()
    except Exception:
        return None
    if not resolved.is_file():
        return None
    for root in (CLONOTH_WORKSPACE, *LOCAL_SOURCE_ROOTS):
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return resolved
        except Exception:
            continue
    common.logger.warning(
        "collect QQ attachment refused local path outside workspace: %s "
        "(set ONEBOT_LOCAL_SOURCE_ROOTS to allow this directory)",
        resolved,
    )
    return None


async def _read_remote_bytes(client: httpx.AsyncClient, url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """流式读取远端附件，超过 max_bytes 立刻断流并抛 ValueError("too_large")。"""
    content = bytearray()
    content_type = "application/octet-stream"
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", content_type)
        declared = str(response.headers.get("content-length") or "").strip()
        # Content-Length 可信时一个 body 字节都不必读。
        if declared.isdigit() and int(declared) > max_bytes:
            raise ValueError("too_large")
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("too_large")
    return bytes(content), content_type


async def _read_file_source_bytes(client: httpx.AsyncClient, source: str) -> tuple[bytes, str]:
    """读取 QQ 文件来源；支持 URL、file:// 和本地路径，并限制最大字节数。"""
    kind = _file_source_kind(source)
    if kind == "base64":
        encoded = source[len("base64://"):]
        if len(encoded) > 4 * ((live.file_max_bytes + 2) // 3):
            raise ValueError("too_large")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid_content") from exc
        if len(content) > live.file_max_bytes:
            raise ValueError("too_large")
        return content, "application/octet-stream"
    if kind == "local":
        if source.startswith("file://"):
            raw = source[len("file://"):]
            if re.match(r"^[a-zA-Z]:[\\/]", raw):
                source = unquote(raw)
            else:
                parsed = urlsplit(source)
                if parsed.netloc not in {"", "localhost"}:
                    raise ValueError("local_denied")
                source = unquote(parsed.path)
                if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", source):
                    source = source[1:]
        local = _local_source_path(source)
        if local is None:
            if not Path(source).is_file():
                raise ValueError("local_unavailable")
            raise ValueError("local_denied")
        if local.stat().st_size > live.file_max_bytes:
            raise ValueError("too_large")
        with local.open("rb") as stream:
            content = stream.read(live.file_max_bytes + 1)
        if len(content) > live.file_max_bytes:
            raise ValueError("too_large")
        return content, "application/octet-stream"
    if kind != "remote":
        raise ValueError("invalid_source")
    return await _read_remote_bytes(client, source, max_bytes=live.file_max_bytes)


def _file_read_error(exc: Exception) -> str:
    if isinstance(exc, HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, (TimeoutException, asyncio.TimeoutError)):
        return "download_timeout"
    if isinstance(exc, ValueError):
        return str(exc) or "invalid_source"
    if isinstance(exc, OSError):
        return "storage_failed"
    return "download_failed"


async def _file_content(
    client: httpx.AsyncClient, item: Mapping[str, Any], *, bot: Bot | None, group_id: Any,
) -> tuple[bytes, str, str]:
    name = str(item.get("name") or "").strip()
    file_id = str(item.get("file_id") or "").strip()
    if item.get("name_inferred") and file_id and inbound_file_reject_reason(name, extra_allowed=live.file_extra_allowed_extensions):
        name = ""
    expected_size = _file_size(item)
    read_errors: list[str] = []
    seen: set[str] = set()
    pending_sources: list[str] = []

    async def read(data: Mapping[str, Any]) -> tuple[bytes, str, str] | None:
        nonlocal name, expected_size
        supplied_name = str(data.get("name") or data.get("file_name") or data.get("filename") or "").strip()
        if not name and supplied_name and not data.get("name_inferred"):
            name = supplied_name
        returned_size = _file_size(data)
        if max(returned_size, expected_size) > live.file_max_bytes:
            raise ValueError("too_large")
        expected_size = expected_size or returned_size
        candidates = []
        if isinstance(data.get("base64"), str):
            encoded = data["base64"]
            candidates.append(encoded if encoded.startswith("base64://") else "base64://" + encoded)
        candidates.extend(pending_sources)
        pending_sources.clear()
        sources = data.get("sources")
        if isinstance(sources, list):
            candidates.extend(value for value in sources if isinstance(value, str))
        for key in ("source", "url", "path", "file"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        if not name:
            pending_sources.extend(candidates)
            return None
        name = _safe_attachment_name(name, "file")
        reject = inbound_file_reject_reason(name, extra_allowed=live.file_extra_allowed_extensions)
        if reject:
            raise ValueError(reject)
        for source in candidates:
            if source in seen:
                continue
            seen.add(source)
            if not _file_source_kind(source):
                read_errors.append("invalid_source")
                continue
            try:
                content, mime_type = await _read_file_source_bytes(client, source)
                if len(content) > live.file_max_bytes:
                    raise ValueError("too_large")
                if expected_size and len(content) != expected_size:
                    raise ValueError("size_mismatch")
            except Exception as exc:
                code = _file_read_error(exc)
                logger.warning("QQ file read failed: name=%s source_type=%s reason=%s error_type=%s",
                               name, _file_source_kind(source), code, type(exc).__name__)
                if code == "too_large":
                    raise ValueError(code) from exc
                read_errors.append(code)
                continue
            reject = inbound_file_reject_reason(name, content, extra_allowed=live.file_extra_allowed_extensions)
            if reject:
                raise ValueError(reject)
            return content, mime_type, name
        return None

    result = await read(item)
    if result is not None:
        return result
    resolve_error = "resolve_failed"
    if file_id and bot is not None:
        api_calls = [("get_private_file_url", {"file_id": file_id})]
        if group_id is not None and str(group_id).isdigit() and int(group_id) > 0:
            api_calls = [("get_group_file_url", {"group_id": str(group_id), "file_id": file_id})]
        api_calls.append(("get_file", {"file_id": file_id}))
        for api, parameters in api_calls:
            try:
                data = await asyncio.wait_for(bot.call_api(api, **parameters), timeout=IMAGE_DOWNLOAD_TIMEOUT)
                if not isinstance(data, Mapping):
                    raise ValueError("invalid API response")
                if "status" in data or "retcode" in data:
                    if data.get("status") != "ok" or data.get("retcode", 0) != 0:
                        raise ValueError("unsuccessful API response")
                    data = data.get("data")
                if not isinstance(data, Mapping):
                    raise ValueError("missing API data")
            except Exception as exc:
                resolve_error = "resolve_timeout" if isinstance(exc, (TimeoutException, asyncio.TimeoutError)) else "resolve_failed"
                logger.warning("QQ file lookup failed: name=%s api=%s error_type=%s", name, api, type(exc).__name__)
                continue
            result = await read(data)
            if result is not None:
                return result
    if read_errors:
        # 下载错误比后续接口不可用更能说明已经取得的地址为何失败。
        meaningful = [code for code in read_errors if code != "invalid_source"]
        raise ValueError(meaningful[-1] if meaningful else (resolve_error if file_id else "invalid_source"))
    raise ValueError(resolve_error if file_id else "no_source")


async def _file_sources_to_attachments(
    file_sources: List[Dict[str, Any]], conversation_key: str, *, bot: Bot | None = None, group_id: Any = None,
) -> tuple[list[dict], list[str]]:
    """下载/复制 OneBot 普通文件，返回 Clonoth 附件描述。"""
    result: list[dict] = []
    errors: list[str] = []
    if not live.enable_file_input or not file_sources:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        common.logger.warning("collect QQ file attachment cannot create directory %s: %s", att_dir, exc)
        return result, [_file_attachment_error_text("storage_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for item in file_sources[:live.max_files_per_turn]:
            file_path: Path | None = None
            try:
                content, content_type, name = await _file_content(client, item, bot=bot, group_id=group_id)
                target_name = f"{os.urandom(8).hex()}_{name}"
                file_path = att_dir / target_name
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "file",
                    "path": rel_path,
                    "mime_type": content_type or "application/octet-stream",
                    "name": name,
                    "source": "onebot",
                })
            except ValueError as exc:
                errors.append(_file_attachment_error_text(str(exc) or "download_failed"))
            except Exception as exc:
                logger.warning("QQ file attachment failed: error_type=%s", type(exc).__name__)
                if file_path is not None:
                    try:
                        file_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                errors.append(_file_attachment_error_text(_file_read_error(exc)))
    return result, errors


async def _image_sources_to_attachments(
    image_sources: List[models.ImageSource], conversation_key: str,
) -> tuple[list[dict], list[str]]:
    """下载 OneBot 图片 URL/path 列表，并返回 Clonoth 附件描述与用户可读错误。"""
    result: list[dict] = []
    errors: list[str] = []
    if not live.enable_image_input or not image_sources:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        common.logger.warning("collect QQ attachments cannot create directory %s: %s", att_dir, exc)
        return result, [_attachment_error_text("download_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for image in image_sources[:live.max_images_per_turn]:
            try:
                source = image.url.strip()
                if not source:
                    continue
                if source.startswith("file://") or re.match(r"^[a-zA-Z]:[\\/]", source) or source.startswith("/"):
                    local = _local_source_path(source)
                    if local is None:
                        errors.append(_attachment_error_text("local_denied"))
                        continue
                    if local.stat().st_size > live.image_max_bytes:
                        raise ValueError("too_large")
                    content = local.read_bytes()
                else:
                    content, _content_type = await _read_remote_bytes(client, source, max_bytes=live.image_max_bytes)
                if not content:
                    common.logger.warning("collect QQ image attachment skipped empty response: %s", source)
                    errors.append(_attachment_error_text("download_failed"))
                    continue
                mime_type = sniff_image_mime(content)
                if not mime_type:
                    common.logger.warning("collect QQ image attachment unsupported header: url=%s", source)
                    errors.append(_attachment_error_text("unsupported_mime"))
                    continue
                ext = _IMAGE_EXT_BY_MIME[mime_type]
                filename = f"{os.urandom(16).hex()}{ext}"
                file_path = att_dir / filename
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "image",
                    "path": rel_path,
                    "mime_type": mime_type,
                    "name": f"image{ext}",
                    "source": "onebot",
                    "sticker": image.sticker,
                })
            except ValueError as exc:
                errors.append(_attachment_error_text(str(exc) or "download_failed"))
            except Exception as exc:
                common.logger.warning("collect QQ image attachment failed: url=%s error=%s", image.url, exc)
                errors.append(_attachment_error_text("download_failed"))
    return result, errors


async def _iter_message_media(
    bot: Bot,
    message: Any,
    *,
    depth: int,
    visited: set[str],
    remaining: List[int],
) -> tuple[List[models.ImageSource], List[Dict[str, Any]]]:
    """按正文顺序收集图片地址与文件来源，转发卡片就地展开。

    顺序必须和展开后的正文一致：引用块是按 [图片] 占位符出现的先后回填路径的。
    """
    urls: List[models.ImageSource] = []
    files: List[Dict[str, Any]] = []
    for seg_type, data in message_text._iter_segments(message):
        if seg_type in message_text.IMAGE_SEGMENT_TYPES:
            url = _segment_image_url(data)
            if url:
                urls.append(models.ImageSource(url, seg_type in message_text.STICKER_SEGMENT_TYPES))
            continue
        if seg_type == "file":
            source = _segment_file_source(data)
            if source is not None:
                files.append(source)
            continue
        if seg_type != "forward" or depth <= 0 or remaining[0] <= 0:
            continue
        card = message_text._forward_source_from_data(data)
        if card is None:
            continue
        if card.forward_id:
            if card.forward_id in visited:
                continue
            visited.add(card.forward_id)
        for item in (await message_text._forward_source_messages(bot, card)) or []:
            if remaining[0] <= 0:
                break
            remaining[0] -= 1
            content = item.get("content") if item.get("content") is not None else item.get("message")
            sub_urls, sub_files = await _iter_message_media(
                bot, content, depth=depth - 1, visited=visited, remaining=remaining,
            )
            urls.extend(sub_urls)
            files.extend(sub_files)
        visited.discard(card.forward_id)
    return urls, files


def _scan_top_level_media(message: Any) -> tuple[List[models.ImageSource], List[Dict[str, Any]]]:
    """只扫当前这一层。图片挂了不该连累文件，所以两边各自兜底。"""
    try:
        urls = _iter_qq_image_sources(message)
    except Exception as exc:
        common.logger.warning("collect QQ image attachments skipped current message: %s", exc)
        urls = []
    try:
        files = _iter_qq_file_sources(message)
    except Exception as exc:
        common.logger.warning("collect QQ file attachments skipped current message: %s", exc)
        files = []
    return urls, files


async def _collect_message_media(
    bot: Bot, message: Any, *, expand_forward: bool = True,
) -> tuple[List[models.ImageSource], List[Dict[str, Any]]]:
    """当前消息里的图片和文件，转发卡片按开关决定要不要下钻。

    正文展开已经把卡片内容拉进缓存，下钻这一趟通常不再走协议往返。
    """
    if not expand_forward:
        return _scan_top_level_media(message)
    if not live.enable_forward_msg_input or not live.enable_forward_msg_media:
        return _scan_top_level_media(message)
    if not message_text._extract_forward_sources(message):
        return _scan_top_level_media(message)
    timeout = float(live.forward_msg_timeout_sec)
    try:
        return await asyncio.wait_for(
            _iter_message_media(
                bot,
                message,
                depth=int(live.forward_msg_max_depth),
                visited=set(),
                remaining=[int(live.forward_msg_max_messages)],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        common.logger.warning("forward media scan timed out after %.1fs", timeout)
    except Exception as exc:
        common.logger.warning("forward media scan failed: %s", exc)
    return _scan_top_level_media(message)


async def _collect_qq_attachments(
    bot: Bot, event, conversation_key: str, *, expand_forward: bool = True,
) -> tuple[list[dict], list[str]]:
    """下载 QQ 当前消息中的图片/普通文件，并返回 Clonoth 附件列表。引用消息由增强 reply 逻辑单独处理。"""
    message = event.get_message() if hasattr(event, "get_message") else None
    image_sources, file_sources = await _collect_message_media(bot, message, expand_forward=expand_forward)
    image_attachments, image_errors = await _image_sources_to_attachments(image_sources, conversation_key)
    file_attachments, file_errors = await _file_sources_to_attachments(
        file_sources, conversation_key, bot=bot, group_id=getattr(event, "group_id", None),
    )
    return image_attachments + file_attachments, image_errors + file_errors


def _attachment_abs_path(attachment: Dict[str, Any]) -> Path | None:
    """把 Clonoth 附件描述解析成本地绝对路径，用于 NapCat add_custom_face。"""
    raw = str(attachment.get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / raw
    return path


async def _collect_reply_image_attachments(bot: Bot, event: Event, conversation_key: str) -> tuple[list[dict], list[str]]:
    """为“收藏表情”命令补充引用消息中的图片。"""
    reply_message_id = message_text._extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    if reply_message_id is None:
        return [], []
    reply_obj = await message_text._get_reply_message(bot, reply_message_id)
    if not reply_obj:
        return [], []
    image_sources: list[models.ImageSource] = []
    for key in ("message", "raw_message"):
        value = reply_obj.get(key)
        if value:
            image_sources.extend(_iter_qq_image_sources(value))
    # 去重，避免同一引用图被重复下载。
    image_sources = list(dict.fromkeys(image_sources))
    return await _image_sources_to_attachments(image_sources, conversation_key)


async def _custom_face_command_attachments(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """为收藏表情命令选择图片：当前消息 > 引用消息 > 最近图片。"""
    if current_attachments:
        return current_attachments, []

    reply_attachments, reply_errors = await _collect_reply_image_attachments(bot, event, conversation_key)
    if reply_attachments:
        return reply_attachments, reply_errors

    recent = _recent_images_for_text(conversation_key, event)
    if recent:
        return recent, []
    return [], reply_errors

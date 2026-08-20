"""Pure attachment-selection policy for the OneBot adapter.

Kept free of NoneBot imports so the ambiguity rules can be regression-tested even
when optional platform dependencies are not installed in the core test environment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


# Inbound files are relayed back out by qq_forward and inlined into prompts, so the gate is a
# whitelist: an unknown extension is refused instead of stored.
INBOUND_FILE_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    "txt", "md", "markdown", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml",
    "xml", "html", "htm", "css", "ini", "toml", "conf", "cfg", "sql", "srt", "vtt",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf", "odt", "ods", "odp", "epub",
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "amr",
    "mp4", "mkv", "mov", "webm", "avi",
    "zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz", "zst",
    "py", "c", "h", "cpp", "hpp", "cs", "java", "go", "rs", "rb", "php", "lua", "kt",
    "swift", "patch", "diff",
})

# Never unlocked by operator config: forwarding one of these turns the bot into a malware relay.
INBOUND_FILE_BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    "exe", "com", "scr", "bat", "cmd", "pif", "ps1", "psm1", "psd1", "vbs", "vbe",
    "js", "jse", "wsf", "wsh", "hta", "msi", "msp", "mst", "cpl", "reg", "lnk", "url",
    "dll", "sys", "ocx", "drv", "apk", "jar", "iso", "img", "vhd", "vhdx", "efi",
    "elf", "so", "dylib",
})

# Magic numbers of native executables. OLE2 (d0cf11e0) is deliberately absent: legacy
# .doc/.xls share it.
_EXECUTABLE_MAGIC: tuple[bytes, ...] = (
    b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
    b"L\x00\x00\x00\x01\x14\x02\x00",
)


def normalize_extension(name: str) -> str:
    """Trailing extension of a file name, lowercase and without the dot."""
    return Path(str(name or "")).suffix.lower().lstrip(".")


def looks_like_executable(content: bytes) -> bool:
    """True when the header is a native executable or a Windows shortcut."""
    head = bytes(content[:8])
    return any(head.startswith(magic) for magic in _EXECUTABLE_MAGIC)


def blocked_file_reject_reason(name: str, content: bytes = b"") -> str:
    """The always-refused half of the inbound gate: blocked extension or executable header.

    Safe to apply at any layer, unlike the allow-whitelist which is QQ-inbound-specific;
    the generic Supervisor upload endpoint enforces only this half.
    Codes: "unsupported_type", "executable_content".
    """
    if normalize_extension(name) in INBOUND_FILE_BLOCKED_EXTENSIONS:
        return "unsupported_type"
    if looks_like_executable(content):
        return "executable_content"
    return ""


def inbound_file_reject_reason(name: str, content: bytes = b"", *, extra_allowed: Iterable[str] = ()) -> str:
    """Return an error code for a refused inbound file, or "" when it is accepted.

    Codes: "unsupported_type", "executable_content". extra_allowed widens the whitelist but
    can never unlock a blocked type: a renamed executable is still caught by the header check.
    """
    blocked = blocked_file_reject_reason(name, content)
    if blocked:
        return blocked
    ext = normalize_extension(name)
    if not ext:
        return "unsupported_type"
    # extra_allowed 是裸扩展名（"ipynb" / ".ipynb"），不是文件名，不能走 normalize_extension。
    extra = {str(item).strip().lower().lstrip(".") for item in extra_allowed} - {""}
    if ext in INBOUND_FILE_ALLOWED_EXTENSIONS or ext in extra:
        return ""
    return "unsupported_type"


def looks_like_file_query(text: str) -> bool:
    """Detect a follow-up that points at a file just sent to the group.

    QQ 的群文件是独立一条消息、带不了 @，所以「读一下上面那个」永远是下一条才来。
    只认明确提到文件的说法，别把「看看这个」这种含糊指代也算进来。
    """
    value = str(text or "").strip().lower()
    if not value:
        return False
    keywords = (
        "这个文件", "那个文件", "上面的文件", "刚发的文件", "刚才的文件", "发的文件",
        "这份文件", "那份文件", "文件里", "文件内容", "读一下文件", "看一下文件",
        "这个表格", "这份表格", "这个文档", "这份文档", "附件",
        "json", "csv", "yaml", "yml", "excel", "pdf", "docx", "xlsx", "zip",
        "read the file", "this file", "that file", "attachment",
    )
    return any(keyword in value for keyword in keywords)


def looks_like_image_query(text: str) -> bool:
    """Detect explicit image references without matching generic text requests."""
    value = str(text or "").strip().lower()
    if not value:
        return False
    keywords = (
        "这张图", "那张图", "上图", "原图", "图里", "图中", "看图", "看看图",
        "看一下图", "识图", "读图", "图片", "截图", "照片", "ocr", "表情包",
        "image", "photo", "screenshot",
    )
    return any(keyword in value for keyword in keywords)


def should_fallback_to_recent_attachments(
    *,
    has_attachments: bool,
    input_enabled: bool,
    looks_like_query: bool,
    reply_message_id: Any,
) -> bool:
    """Allow implicit recent-attachment lookup only for non-reply queries.

    引用有自己的消息链，绝不能在这里回退：「再仔细看看图」曾因此拿到无关的旧图。
    """
    return bool(
        not has_attachments
        and input_enabled
        and looks_like_query
        and reply_message_id is None
    )


def source_attachments_from_merged(attachments: Iterable[Any]) -> list[dict[str, Any]]:
    """Copy and de-duplicate attachments used by a merged queued task."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        key = (str(attachment.get("type") or ""), str(attachment.get("path") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(dict(attachment))
    return result


def select_recent_attachment_entries(
    entries: Iterable[Any],
    *,
    sender_id: str,
    now: float,
    max_age_seconds: float,
    max_items: int,
) -> list[dict[str, Any]]:
    """Select the latest single-message attachment batch from the current sender.

    Never falls back across senders and never combines separate QQ messages. This
    prevents a follow-up such as “再仔细看看图” from silently receiving an older,
    unrelated image.
    """
    eligible = [
        item
        for item in entries
        if str(getattr(item, "sender_id", "") or "") == str(sender_id or "")
        and now - float(getattr(item, "created_at", 0.0) or 0.0) <= max_age_seconds
    ]
    if not eligible or max_items <= 0:
        return []

    latest_message_id = str(getattr(eligible[-1], "message_id", "") or "")
    if latest_message_id:
        eligible = [
            item
            for item in eligible
            if str(getattr(item, "message_id", "") or "") == latest_message_id
        ]
    else:
        eligible = eligible[-1:]

    selected = eligible[-max_items:]
    return [
        dict(getattr(item, "attachment", {}) or {})
        for item in selected
        if isinstance(getattr(item, "attachment", None), dict)
    ]

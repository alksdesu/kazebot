from __future__ import annotations

"""
External tool (Clonoth): read_web

只读抓取公网网页/接口内容并提炼正文。用标准库实现（urllib），无需第三方依赖。

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped by the runtime
"""

SPEC = {
    "name": "read_web",
    "description": (
        "只读抓取一个公网网页或接口地址的内容并提炼正文。"
        "当你已经知道目标 URL（用户给出的链接、文档地址、API 端点等），想读取其内容时使用本工具，"
        "而不是用 execute_command + curl。用法：{\"url\": \"https://example.com/page\"}。"
        "对静态页面/JSON 接口有效；返回的是抓取时刻的原始内容提炼（HTML 会自动去标签抽正文），"
        "对需要 JavaScript 渲染的动态页面可能只能拿到有限内容。"
        "本工具只做 GET 读取，不会写入或提交任何数据，也不能访问 localhost/内网地址。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要读取的完整 URL，必须是 http:// 或 https:// 开头。必填。",
            },
            "max_chars": {
                "type": "integer",
                "default": 8000,
                "description": "返回正文的最大字符数，默认 8000，最大 120000；超出会截断。",
            },
            "raw": {
                "type": "boolean",
                "default": False,
                "description": "是否返回原始内容（不做 HTML 去标签提炼）。默认 false，一般不要开启。",
            },
        },
        "required": ["url"],
    },
}

TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import gzip
    import io
    import ipaddress
    import json
    import re
    import socket
    import sys
    import zlib
    from html import unescape
    from typing import Any
    from urllib.parse import urlparse, urlsplit
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    MAX_BYTES = 16 * 1024 * 1024  # 最多读取 16MB 响应体，避免超大页面拖垮进程
    MAX_CHARS_CAP = 120000
    DEFAULT_MAX_CHARS = 8000
    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ClonothReadWeb/1.0"
    )

    def output(result: dict[str, Any]) -> None:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error: str, *, hint: str = "") -> None:
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps(
            {"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}},
            ensure_ascii=False,
        ))
        sys.exit(1)

    def extract_url(value: Any) -> str:
        """从多种可能的入参形态中提取 URL，容忍模型传参不规范。"""
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, dict):
            return ""
        for key in ("url", "link", "href", "address", "uri", "u"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key in ("args", "arguments", "params", "parameters", "data"):
            if key in value:
                nested = extract_url(value.get(key))
                if nested:
                    return nested
        return ""

    def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def is_public_url(url: str) -> tuple[bool, str]:
        """校验 URL 为 http(s) 且主机不是 localhost/环回/内网/保留地址（防 SSRF）。"""
        try:
            parts = urlsplit(url)
        except Exception as exc:
            return False, f"URL 解析失败：{exc}"
        if parts.scheme not in ("http", "https"):
            return False, "只支持 http:// 或 https:// 地址。"
        host = parts.hostname or ""
        if not host:
            return False, "URL 缺少主机名。"
        low = host.lower()
        if low in ("localhost", "localhost.localdomain") or low.endswith(".localhost"):
            return False, "禁止访问 localhost。"
        # 解析所有 IP，任一命中内网/保留段即拒绝。
        addrs: list[str] = []
        try:
            for info in socket.getaddrinfo(host, None):
                addrs.append(info[4][0])
        except Exception:
            # 无法解析时，若 host 本身是 IP 字面量则直接校验，否则放行给后续请求报错。
            addrs = [host]
        for addr in addrs:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False, f"禁止访问内网/保留地址：{addr}"
        return True, ""

    def decode_body(raw: bytes, encoding_header: str, content_type: str) -> str:
        # 处理 gzip/deflate
        body = raw
        enc = (encoding_header or "").lower()
        try:
            if "gzip" in enc:
                body = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            elif "deflate" in enc:
                try:
                    body = zlib.decompress(raw)
                except zlib.error:
                    body = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            body = raw
        # 猜测字符集
        charset = ""
        m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
        if m:
            charset = m.group(1).strip().lower()
        if not charset:
            m2 = re.search(rb"charset=[\"']?([\w\-]+)", body[:2048], re.I)
            if m2:
                charset = m2.group(1).decode("ascii", "ignore").lower()
        for cs in [charset, "utf-8", "gbk", "gb18030", "latin-1"]:
            if not cs:
                continue
            try:
                return body.decode(cs)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", "replace")

    def html_to_text(html: str) -> tuple[str, str]:
        """极简 HTML 去标签抽正文，返回 (title, text)。"""
        title = ""
        mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if mt:
            title = unescape(re.sub(r"\s+", " ", mt.group(1))).strip()
        # 去掉 script/style/noscript/head 等不可见内容
        cleaned = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", html)
        cleaned = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", cleaned)
        # 块级标签转换行，便于阅读
        cleaned = re.sub(r"(?i)<(/?)(p|div|br|li|tr|h[1-6]|section|article)[^>]*>", "\n", cleaned)
        # 去掉剩余标签
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        text = unescape(cleaned)
        # 归一化空白
        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        text = "\n".join(lines)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return title, text.strip()

    def looks_like_json(content_type: str, body: str) -> bool:
        if "json" in (content_type or "").lower():
            return True
        stripped = body.lstrip()
        return stripped[:1] in ("{", "[")

    # ---- 主流程 ----
    try:
        raw_input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))
    except Exception as exc:
        fail(f"无法解析工具输入 JSON：{exc}")
        raise SystemExit(1)

    args = raw_input if isinstance(raw_input, dict) else {}
    url = extract_url(raw_input)
    if not url:
        fail("缺少 url。", hint="请重新调用 read_web，格式：{\"url\": \"https://要读取的地址\"}。")

    if not re.match(r"^https?://", url, re.I):
        # 容忍用户漏写协议，默认补 https
        if "://" not in url:
            url = "https://" + url

    ok, reason = is_public_url(url)
    if not ok:
        fail(f"该地址不允许访问：{reason}", hint="请提供一个公网 http(s) 网址，不能是内网或本机地址。")

    max_chars = clamp_int(args.get("max_chars"), DEFAULT_MAX_CHARS, 200, MAX_CHARS_CAP)
    want_raw = bool(args.get("raw"))

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })

    status = 0
    final_url = url
    content_type = ""
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 0
            final_url = resp.geturl() or url
            content_type = resp.headers.get("Content-Type", "") or ""
            content_encoding = resp.headers.get("Content-Encoding", "") or ""
            raw_bytes = resp.read(MAX_BYTES + 1)
    except HTTPError as exc:
        # 4xx/5xx 仍尝试读取错误体，给模型可读信息
        try:
            body_err = exc.read(MAX_BYTES) if hasattr(exc, "read") else b""
        except Exception:
            body_err = b""
        detail = decode_body(body_err, "", exc.headers.get("Content-Type", "") if exc.headers else "") if body_err else ""
        fail(
            f"HTTP {exc.code} {exc.reason}（{url}）" + (f"\n响应片段：{detail[:500]}" if detail else ""),
            hint="确认链接可公开访问；若需登录/鉴权或被反爬拦截，本工具无法绕过。",
        )
        raise SystemExit(1)
    except URLError as exc:
        fail(f"无法连接目标地址：{getattr(exc, 'reason', exc)}（{url}）",
             hint="确认网址正确、域名可解析、目标站点在线。")
        raise SystemExit(1)
    except (socket.timeout, TimeoutError):
        fail(f"读取超时（>{TIMEOUT_SEC:.0f}s）：{url}", hint="目标站点响应过慢，可稍后重试或换用 web_search。")
        raise SystemExit(1)
    except Exception as exc:
        fail(f"读取失败：{exc}（{url}）")
        raise SystemExit(1)

    truncated_bytes = len(raw_bytes) > MAX_BYTES
    raw_bytes = raw_bytes[:MAX_BYTES]

    body = decode_body(raw_bytes, content_encoding, content_type)

    title = ""
    if want_raw:
        text = body
        kind = "raw"
    elif looks_like_json(content_type, body):
        # JSON 直接美化输出，便于模型阅读
        try:
            parsed = json.loads(body)
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
            kind = "json"
        except Exception:
            text = body
            kind = "text"
    elif "html" in content_type.lower() or re.search(r"<html|<!doctype html", body[:2048], re.I):
        title, text = html_to_text(body)
        kind = "html"
    else:
        text = body.strip()
        kind = "text"

    text_truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        text_truncated = True

    header_lines = [f"读取网页：{final_url}"]
    if title:
        header_lines.append(f"标题：{title}")
    header_lines.append(f"HTTP {status} · 类型 {kind}" + (f" · {content_type.split(';')[0].strip()}" if content_type else ""))
    if text_truncated or truncated_bytes:
        header_lines.append(f"（内容较长已截断，仅返回前 {max_chars} 字符）")
    result_text = "\n".join(header_lines) + "\n\n" + (text or "（未提取到可读正文，可能是动态渲染页面或空内容。）")

    output({
        "ok": True,
        "data": {
            "result": result_text,
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "kind": kind,
            "title": title,
            "truncated": bool(text_truncated or truncated_bytes),
            "citations": [final_url],
        },
    })

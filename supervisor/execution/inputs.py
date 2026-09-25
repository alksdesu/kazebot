from __future__ import annotations

import html
import http.client
import ipaddress
import re
import socket
import ssl
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit


def fetch_url(url: str, max_bytes: int) -> str:
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("仅支持无凭据的 HTTP/HTTPS 链接")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("链接目标必须是公网地址")
        connection = http.client.HTTPConnection(next(iter(addresses)), port, timeout=20)
        try:
            connection.connect()
            if parsed.scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
            target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
            if parsed.query:
                target += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
            connection.request("GET", target, headers={
                "Host": parsed.netloc, "User-Agent": "Clonoth/1.0", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("重定向缺少目标")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"读取链接失败：HTTP {response.status}")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("链接内容超过大小限制")
            content_type = response.getheader("Content-Type", "")
            if content_type and not any(kind in content_type for kind in ("text/", "json", "xml")):
                raise ValueError("该链接不是可读取的文本页面，请先上传文件")
            text = data.decode("utf-8", errors="replace")
            if "html" in content_type:
                text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
                text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            return text
        finally:
            connection.close()
    raise ValueError("链接重定向次数过多")


def read_document(path: Path, max_bytes: int, workspace: Path) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError("文件超过处理大小限制")
    suffix = path.suffix.lower()
    if suffix in {".docx", ".xlsx", ".pptx", ".pdf"}:
        from engine.materials.process import run_worker

        result = run_worker(workspace, "parse", path=str(path))
        warnings = [f"解析提示：{item.get('message', '')}" for item in result.get("warnings", [])]
        return "\n".join(warnings + [f"{item.get('locator', {}).get('label', '')}\n{item.get('text', '')}" for item in result.get("spans", [])])
    if suffix not in {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".yaml", ".yml"}:
        raise ValueError("此文件格式暂不支持文本批处理")
    return path.read_text(encoding="utf-8-sig")

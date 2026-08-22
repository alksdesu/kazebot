"""S3 兼容图床同步：把远端桶里的图拉进本地表情包库。

签名、URL 构造、XML 解析都是纯函数，网络 IO 收在 ImagebedClient 一层，可脱平台单测。
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit

import httpx

from clonoth_runtime import resolve_env_ref

from .filter import read_shape
from .store import SOURCE_IMAGEBED, STATE_PENDING, StickerStore

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()

ADDRESSING_AUTO = "auto"
ADDRESSING_PATH = "path"
ADDRESSING_VIRTUAL = "virtual"
ADDRESSING = (ADDRESSING_AUTO, ADDRESSING_PATH, ADDRESSING_VIRTUAL)

# 非 AWS 的 S3 兼容服务大多不校验 region，"auto" 是它们的通行写法。
DEFAULT_REGION = "auto"
DEFAULT_MAX_OBJECT_SIZE = 8 * 1024 * 1024
DEFAULT_MAX_KEYS = 1000
DEFAULT_TIMEOUT = 30.0

PENDING_DIR = "pending"

_EXTENSIONS = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

_UNRESERVED = "-_.~"
_SPACES = re.compile(r"\s+")
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$")

_OUTCOME_ADDED = "added"
_OUTCOME_KNOWN = "known"
_OUTCOME_OVERSIZE = "oversize"
_OUTCOME_UNSUPPORTED = "unsupported"
_OUTCOME_FAILED = "failed"
_OUTCOMES = (
    _OUTCOME_ADDED, _OUTCOME_KNOWN, _OUTCOME_OVERSIZE, _OUTCOME_UNSUPPORTED, _OUTCOME_FAILED,
)


class ImagebedError(RuntimeError):
    """远端拒绝、响应读不懂，或者配置不足以发出请求。"""


def _short(text: Any, limit: int = 300) -> str:
    return _SPACES.sub(" ", str(text if text is not None else "")).strip()[:limit]


# ── 签名 ───────────────────────────────────

def quote_value(value: Any) -> str:
    return quote(str(value if value is not None else ""), safe=_UNRESERVED)


def quote_key(key: Any) -> str:
    """对象键里的斜杠是路径分隔符，不参与转义。"""
    return quote(str(key if key is not None else ""), safe="/" + _UNRESERVED)


def canonical_query(params: Sequence[tuple[str, str]]) -> str:
    encoded = sorted((quote_value(name), quote_value(value)) for name, value in params)
    return "&".join(f"{name}={value}" for name, value in encoded)


def canonical_headers(headers: Mapping[str, Any]) -> tuple[str, str]:
    """返回规范化头块与 SignedHeaders 两串。"""
    items = sorted(
        (str(name).lower().strip(), _SPACES.sub(" ", str(value)).strip())
        for name, value in headers.items()
    )
    return (
        "".join(f"{name}:{value}\n" for name, value in items),
        ";".join(name for name, _ in items),
    )


def canonical_request(
    method: str,
    uri: str,
    query: str,
    headers: Mapping[str, Any],
    payload_hash: str,
) -> str:
    block, signed = canonical_headers(headers)
    return "\n".join([str(method).upper(), uri or "/", query, block, signed, payload_hash])


def credential_scope(date_stamp: str, region: str, service: str = SERVICE) -> str:
    return f"{date_stamp}/{region}/{service}/aws4_request"


def signing_key(secret: str, date_stamp: str, region: str, service: str = SERVICE) -> bytes:
    key = ("AWS4" + str(secret)).encode("utf-8")
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, str(part).encode("utf-8"), hashlib.sha256).digest()
    return key


def string_to_sign(amz_date: str, scope: str, request: str) -> str:
    return "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()]
    )


def sign_string(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def authorization(
    access_key_id: str, scope: str, signed_headers: str, signature: str
) -> str:
    return (
        f"{ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def utc_stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


@dataclass(frozen=True)
class SignedRequest:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


# ── 配置 ───────────────────────────────────

def _text(raw: Any) -> str:
    return str(raw if raw is not None else "").strip()


def _secret(raw: Any) -> str:
    # yaml 里只允许写 ${VAR}，真值住在 .env，控制台快照与备份因此不会带出密钥。
    return resolve_env_ref(_text(raw))


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ImagebedConfig:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = DEFAULT_REGION
    prefix: str = ""
    max_object_size: int = DEFAULT_MAX_OBJECT_SIZE
    addressing: str = ADDRESSING_AUTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _text(self.endpoint).rstrip("/"))
        object.__setattr__(self, "bucket", _text(self.bucket).strip("/"))
        object.__setattr__(self, "access_key_id", _text(self.access_key_id))
        object.__setattr__(self, "secret_access_key", _text(self.secret_access_key))
        object.__setattr__(self, "region", _text(self.region) or DEFAULT_REGION)
        object.__setattr__(self, "prefix", _text(self.prefix).lstrip("/"))
        object.__setattr__(self, "addressing", _text(self.addressing).lower() or ADDRESSING_AUTO)
        object.__setattr__(
            self, "max_object_size", _positive_int(self.max_object_size, DEFAULT_MAX_OBJECT_SIZE)
        )
        self._validate()

    def _validate(self) -> None:
        parts = urlsplit(self.endpoint)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"图床 endpoint 无效: {self.endpoint or '(空)'}")
        if not _BUCKET_RE.match(self.bucket):
            raise ValueError(f"图床 bucket 无效: {self.bucket or '(空)'}")
        for name in ("access_key_id", "secret_access_key"):
            if not getattr(self, name):
                raise ValueError(f"图床配置缺少 {name}")
        if self.addressing not in ADDRESSING:
            raise ValueError(f"图床 addressing 无效: {self.addressing}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImagebedConfig":
        if not isinstance(data, Mapping):
            raise ValueError("图床配置必须是映射")
        return cls(
            endpoint=_secret(data.get("endpoint")),
            bucket=_text(data.get("bucket")),
            access_key_id=_secret(data.get("access_key_id")),
            secret_access_key=_secret(data.get("secret_access_key")),
            region=_text(data.get("region")) or DEFAULT_REGION,
            prefix=_text(data.get("prefix")),
            max_object_size=_positive_int(data.get("max_object_size"), DEFAULT_MAX_OBJECT_SIZE),
            addressing=_text(data.get("addressing")) or ADDRESSING_AUTO,
        )

    @property
    def scheme(self) -> str:
        return urlsplit(self.endpoint).scheme

    @property
    def _endpoint_host(self) -> str:
        return urlsplit(self.endpoint).netloc

    @property
    def _bucket_in_endpoint(self) -> bool:
        return self._endpoint_host.split(":")[0].startswith(self.bucket + ".")

    @property
    def virtual_hosted(self) -> bool:
        if self.addressing == ADDRESSING_PATH:
            return False
        if self.addressing == ADDRESSING_VIRTUAL:
            return True
        return self._bucket_in_endpoint

    @property
    def host(self) -> str:
        if not self.virtual_hosted or self._bucket_in_endpoint:
            return self._endpoint_host
        return f"{self.bucket}.{self._endpoint_host}"

    def canonical_uri(self, key: str = "") -> str:
        head = "" if self.virtual_hosted else "/" + quote_value(self.bucket)
        if not key:
            return head or "/"
        return f"{head}/{quote_key(key)}"

    def url(self, key: str = "", query: str = "") -> str:
        base = f"{self.scheme}://{self.host}{self.canonical_uri(key)}"
        return f"{base}?{query}" if query else base


def sign_get(
    cfg: ImagebedConfig,
    *,
    key: str = "",
    params: Sequence[tuple[str, str]] = (),
    amz_date: str = "",
    payload_hash: str = EMPTY_PAYLOAD_SHA256,
) -> SignedRequest:
    """给定 amz_date 时是纯函数；留空则取当前 UTC。"""
    stamp = amz_date or utc_stamp()
    headers = {
        "host": cfg.host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": stamp,
    }
    query = canonical_query(params)
    uri = cfg.canonical_uri(key)
    request = canonical_request("GET", uri, query, headers, payload_hash)
    scope = credential_scope(stamp[:8], cfg.region)
    signature = sign_string(
        signing_key(cfg.secret_access_key, stamp[:8], cfg.region),
        string_to_sign(stamp, scope, request),
    )
    _block, signed_headers = canonical_headers(headers)
    headers["authorization"] = authorization(
        cfg.access_key_id, scope, signed_headers, signature
    )
    return SignedRequest(cfg.url(key, query), headers)


# ── 列举 ───────────────────────────────────

@dataclass(frozen=True)
class RemoteObject:
    key: str
    size: int = 0
    etag: str = ""


@dataclass(frozen=True)
class ObjectPage:
    objects: tuple[RemoteObject, ...] = ()
    truncated: bool = False
    next_token: str = ""


def list_query(
    cfg: ImagebedConfig,
    *,
    token: str = "",
    start_after: str = "",
    max_keys: int = DEFAULT_MAX_KEYS,
) -> tuple[tuple[str, str], ...]:
    params: list[tuple[str, str]] = [
        ("encoding-type", "url"),
        ("list-type", "2"),
        ("max-keys", str(max(int(max_keys), 1))),
    ]
    if cfg.prefix:
        params.append(("prefix", cfg.prefix))
    if token:
        params.append(("continuation-token", token))
    elif start_after:
        params.append(("start-after", start_after))
    return tuple(params)


def _child_text(node: ET.Element, name: str) -> str:
    child = node.find(f"{{*}}{name}")
    return "" if child is None or child.text is None else str(child.text)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_list_objects(text: str) -> ObjectPage:
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError as exc:
        raise ImagebedError("列举响应不是合法 XML") from exc
    if _local_name(root.tag) == "Error":
        code = _child_text(root, "Code") or "Unknown"
        raise ImagebedError(f"列举被拒绝: {code} {_short(_child_text(root, 'Message'))}".strip())
    if _local_name(root.tag) != "ListBucketResult":
        raise ImagebedError(f"列举响应根节点不认识: {_local_name(root.tag)}")

    # 只有响应自报 url 编码才解码，否则键名里真实存在的 % 会被解坏。
    decode = _child_text(root, "EncodingType").strip().lower() == "url"
    objects = []
    for node in root.findall("{*}Contents"):
        key = _child_text(node, "Key")
        if not key:
            continue
        objects.append(
            RemoteObject(
                key=unquote(key) if decode else key,
                size=_positive_int(_child_text(node, "Size"), 0),
                etag=_child_text(node, "ETag").strip('"'),
            )
        )
    return ObjectPage(
        objects=tuple(objects),
        truncated=_child_text(root, "IsTruncated").strip().lower() == "true",
        next_token=_child_text(root, "NextContinuationToken").strip(),
    )


# ── 网络 ───────────────────────────────────

class ImagebedClient:
    def __init__(
        self,
        cfg: ImagebedConfig,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "ImagebedClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def list_page(self, *, token: str = "", start_after: str = "") -> ObjectPage:
        params = list_query(self.cfg, token=token, start_after=start_after)
        request = sign_get(self.cfg, params=params)
        response = await self._client.get(request.url, headers=request.headers)
        if response.status_code >= 400:
            raise ImagebedError(
                f"列举失败 HTTP {response.status_code}: {_short(response.text)}"
            )
        return parse_list_objects(response.text)

    async def fetch(self, obj: RemoteObject) -> bytes | None:
        """取对象字节。超过大小上限返回 None，并且当场断流不把 body 读完。"""
        limit = self.cfg.max_object_size
        if obj.size > limit:
            return None
        request = sign_get(self.cfg, key=obj.key)
        buffer = bytearray()
        async with self._client.stream("GET", request.url, headers=request.headers) as response:
            if response.status_code >= 400:
                await response.aread()
                raise ImagebedError(
                    f"下载失败 HTTP {response.status_code}: {_short(response.text)}"
                )
            declared = str(response.headers.get("content-length") or "").strip()
            if declared.isdigit() and int(declared) > limit:
                return None
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > limit:
                    return None
        return bytes(buffer)


# ── 进度 ───────────────────────────────────

@dataclass(frozen=True)
class SyncProgress:
    bucket: str = ""
    prefix: str = ""
    last_key: str = ""
    passes: int = 0
    updated_at: int = 0

    def matches(self, cfg: ImagebedConfig) -> bool:
        return self.bucket == cfg.bucket and self.prefix == cfg.prefix

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "last_key": self.last_key,
            "passes": self.passes,
            "updated_at": self.updated_at,
        }


def load_progress(path: Path | None) -> SyncProgress:
    if path is None:
        return SyncProgress()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SyncProgress()
    if not isinstance(data, Mapping):
        return SyncProgress()
    return SyncProgress(
        bucket=_text(data.get("bucket")),
        prefix=_text(data.get("prefix")),
        last_key=str(data.get("last_key") or ""),
        passes=_positive_int(data.get("passes"), 0),
        updated_at=_positive_int(data.get("updated_at"), 0),
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save_progress(path: Path, progress: SyncProgress) -> None:
    _write_atomic(
        Path(path),
        json.dumps(progress.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
    )


# ── 同步 ───────────────────────────────────

@dataclass(frozen=True)
class SyncStats:
    listed: int = 0
    added: int = 0
    known: int = 0
    oversize: int = 0
    unsupported: int = 0
    failed: int = 0
    completed: bool = False


def relative_path(sha256: str, fmt: str) -> str:
    """库内相对路径。两级散列目录，免得一个目录下堆几万个文件。"""
    return f"{PENDING_DIR}/{sha256[:2]}/{sha256}.{_EXTENSIONS.get(fmt, 'bin')}"


def _supported_suffix(key: str) -> bool:
    return Path(key).suffix.lower() in _IMAGE_SUFFIXES


async def _ingest(
    client: ImagebedClient,
    cfg: ImagebedConfig,
    store: StickerStore,
    root: Path,
    obj: RemoteObject,
) -> str:
    if not _supported_suffix(obj.key):
        return _OUTCOME_UNSUPPORTED
    if obj.size > cfg.max_object_size:
        return _OUTCOME_OVERSIZE

    data = await client.fetch(obj)
    if data is None:
        return _OUTCOME_OVERSIZE
    if not data:
        return _OUTCOME_UNSUPPORTED

    digest = hashlib.sha256(data).hexdigest()
    if store.known(digest):
        return _OUTCOME_KNOWN
    shape = read_shape(data)
    if shape is None:
        return _OUTCOME_UNSUPPORTED

    rel_path = relative_path(digest, shape.fmt)
    target = Path(root) / rel_path
    _write_atomic(target, data)
    record = store.add(
        sha256=digest,
        rel_path=rel_path,
        source=SOURCE_IMAGEBED,
        state=STATE_PENDING,
        name=Path(obj.key).stem,
        width=shape.width,
        height=shape.height,
        animated=shape.animated,
        fmt=shape.fmt,
        size=len(data),
        origin=f"s3://{cfg.bucket}/{obj.key}",
    )
    if record is None:
        with contextlib.suppress(OSError):
            target.unlink()
        return _OUTCOME_KNOWN
    return _OUTCOME_ADDED


def _save(path: Path | None, cfg: ImagebedConfig, last_key: str, passes: int) -> None:
    if path is None:
        return
    save_progress(
        path,
        SyncProgress(
            bucket=cfg.bucket,
            prefix=cfg.prefix,
            last_key=last_key,
            passes=passes,
            updated_at=int(time.time()),
        ),
    )


async def sync_once(
    cfg: ImagebedConfig,
    store: StickerStore,
    *,
    root: Path,
    progress_path: Path | None = None,
    limit: int = 0,
    client: ImagebedClient | None = None,
) -> SyncStats:
    """跑一趟远端 → 本地。limit 为一趟最多处理多少个对象，0 表示不限。"""
    handle = client or ImagebedClient(cfg)
    counter: Counter[str] = Counter()
    progress = load_progress(progress_path)
    resume = progress.last_key if progress.matches(cfg) else ""
    passes = progress.passes if progress.matches(cfg) else 0
    last_key = resume
    token = ""
    completed = False

    try:
        while True:
            page = await handle.list_page(token=token, start_after="" if token else resume)
            for obj in page.objects:
                counter["listed"] += 1
                try:
                    counter[await _ingest(handle, cfg, store, root, obj)] += 1
                except (ImagebedError, httpx.HTTPError, OSError, ValueError) as exc:
                    # 单个对象坏掉不该停下整趟；进度照样越过它，下一趟再碰。
                    logger.warning("图床同步跳过对象 %s: %s", obj.key, exc)
                    counter[_OUTCOME_FAILED] += 1
                last_key = obj.key
                if limit and counter["listed"] >= limit:
                    break
            reached = bool(limit) and counter["listed"] >= limit
            drained = not page.truncated or not page.next_token
            if reached or not drained:
                _save(progress_path, cfg, last_key, passes)
            if reached:
                break
            if drained:
                completed = True
                break
            token = page.next_token
    finally:
        if client is None:
            await handle.aclose()

    if completed:
        # 走完一遍就把游标清零：新对象的键名可能排在已扫范围之前，只靠 start-after 会永远看不见。
        _save(progress_path, cfg, "", passes + 1)
    return SyncStats(
        listed=counter["listed"],
        completed=completed,
        **{name: counter[name] for name in _OUTCOMES},
    )

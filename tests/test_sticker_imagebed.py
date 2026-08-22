"""S3 兼容图床同步。

签名一律拿 AWS 官方 SigV4 测试向量对答案 —— 自己和自己一致证明不了签得对。
下载路径全程走 httpx MockTransport，一个真实请求都不发。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers import imagebed as ib
from stickers import store as ss


# ── AWS 官方 SigV4 测试向量 ───────────────────────────────

VECTOR_ACCESS_KEY = "AKIDEXAMPLE"
VECTOR_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
VECTOR_REGION = "us-east-1"
VECTOR_SERVICE = "service"
VECTOR_DATE = "20150830T123600Z"
VECTOR_HOST = "example.amazonaws.com"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@dataclass(frozen=True)
class Vector:
    name: str
    uri: str
    params: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    creq: str
    signature: str


VECTORS = (
    Vector(
        name="get-vanilla",
        uri="/",
        params=(),
        headers={"Host": VECTOR_HOST, "X-Amz-Date": VECTOR_DATE},
        creq=(
            "GET\n"
            "/\n"
            "\n"
            f"host:{VECTOR_HOST}\n"
            f"x-amz-date:{VECTOR_DATE}\n"
            "\n"
            "host;x-amz-date\n"
            f"{EMPTY_SHA256}"
        ),
        signature="5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
    ),
    Vector(
        name="get-vanilla-query-order-key-case",
        uri="/",
        params=(("Param2", "value2"), ("Param1", "value1")),
        headers={"Host": VECTOR_HOST, "X-Amz-Date": VECTOR_DATE},
        creq=(
            "GET\n"
            "/\n"
            "Param1=value1&Param2=value2\n"
            f"host:{VECTOR_HOST}\n"
            f"x-amz-date:{VECTOR_DATE}\n"
            "\n"
            "host;x-amz-date\n"
            f"{EMPTY_SHA256}"
        ),
        signature="b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
    ),
    Vector(
        name="get-header-value-trim",
        uri="/",
        params=(),
        headers={
            "Host": VECTOR_HOST,
            "My-Header1": "  value1  ",
            "My-Header2": '  "a   b   c"  ',
            "X-Amz-Date": VECTOR_DATE,
        },
        creq=(
            "GET\n"
            "/\n"
            "\n"
            f"host:{VECTOR_HOST}\n"
            "my-header1:value1\n"
            'my-header2:"a b c"\n'
            f"x-amz-date:{VECTOR_DATE}\n"
            "\n"
            "host;my-header1;my-header2;x-amz-date\n"
            f"{EMPTY_SHA256}"
        ),
        signature="acc3ed3afb60bb290fc8d2dd0098b9911fcaa05412b367055dee359757a9c736",
    ),
    Vector(
        name="get-unreserved",
        uri=(
            "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
        ),
        params=(),
        headers={"Host": VECTOR_HOST, "X-Amz-Date": VECTOR_DATE},
        creq=(
            "GET\n"
            "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz\n"
            "\n"
            f"host:{VECTOR_HOST}\n"
            f"x-amz-date:{VECTOR_DATE}\n"
            "\n"
            "host;x-amz-date\n"
            f"{EMPTY_SHA256}"
        ),
        signature="07ef7494c76fa4850883e2b006601f940f8a34d404d0cfa977f52a65bbf5f24f",
    ),
)


class TestOfficialSigV4Vectors:
    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.name)
    def test_the_canonical_request_matches_the_published_one(self, vector: Vector) -> None:
        assert ib.canonical_request(
            "GET",
            vector.uri,
            ib.canonical_query(vector.params),
            vector.headers,
            EMPTY_SHA256,
        ) == vector.creq

    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.name)
    def test_the_signature_matches_the_published_one(self, vector: Vector) -> None:
        scope = ib.credential_scope(VECTOR_DATE[:8], VECTOR_REGION, VECTOR_SERVICE)
        signature = ib.sign_string(
            ib.signing_key(VECTOR_SECRET, VECTOR_DATE[:8], VECTOR_REGION, VECTOR_SERVICE),
            ib.string_to_sign(VECTOR_DATE, scope, vector.creq),
        )
        assert signature == vector.signature

    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.name)
    def test_the_authorization_header_matches_the_published_one(self, vector: Vector) -> None:
        scope = ib.credential_scope(VECTOR_DATE[:8], VECTOR_REGION, VECTOR_SERVICE)
        _block, signed_headers = ib.canonical_headers(vector.headers)
        header = ib.authorization(VECTOR_ACCESS_KEY, scope, signed_headers, vector.signature)
        assert header == (
            f"AWS4-HMAC-SHA256 Credential={VECTOR_ACCESS_KEY}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={vector.signature}"
        )

    def test_an_unreserved_path_survives_quoting_untouched(self) -> None:
        raw = "-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        assert ib.quote_key(raw) == raw

    def test_a_key_keeps_its_slashes_but_escapes_the_rest(self) -> None:
        assert ib.quote_key("a b/c+d/é.png") == "a%20b/c%2Bd/%C3%A9.png"

    def test_a_query_value_escapes_its_slashes(self) -> None:
        assert ib.canonical_query([("prefix", "memes/2026 q1")]) == "prefix=memes%2F2026%20q1"


# ── 配置 ───────────────────────────────────

def _config(**overrides: Any):
    values: dict[str, Any] = {
        "endpoint": "https://s3.example.com",
        "bucket": "memes",
        "access_key_id": "AKID",
        "secret_access_key": "SECRET",
        "region": "auto",
    }
    values.update(overrides)
    return ib.ImagebedConfig(**values)


class TestConfig:
    def test_credentials_come_from_env_refs_not_from_the_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMAGEBED_KEY", "real-key")
        monkeypatch.setenv("IMAGEBED_SECRET", "real-secret")
        cfg = ib.ImagebedConfig.from_dict(
            {
                "endpoint": "https://s3.example.com/",
                "bucket": "memes",
                "access_key_id": "${IMAGEBED_KEY}",
                "secret_access_key": "$ENV{IMAGEBED_SECRET}",
            }
        )
        assert (cfg.access_key_id, cfg.secret_access_key) == ("real-key", "real-secret")

    def test_an_unset_env_ref_is_rejected_instead_of_signing_with_a_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("IMAGEBED_MISSING", raising=False)
        with pytest.raises(ValueError, match="access_key_id"):
            ib.ImagebedConfig.from_dict(
                {
                    "endpoint": "https://s3.example.com",
                    "bucket": "memes",
                    "access_key_id": "${IMAGEBED_MISSING}",
                    "secret_access_key": "s",
                }
            )

    @pytest.mark.parametrize("endpoint", ["", "s3.example.com", "ftp://s3.example.com", "https://"])
    def test_a_bad_endpoint_is_rejected(self, endpoint: str) -> None:
        with pytest.raises(ValueError, match="endpoint"):
            _config(endpoint=endpoint)

    @pytest.mark.parametrize("bucket", ["", "/", "-bad", "a"])
    def test_a_bad_bucket_is_rejected(self, bucket: str) -> None:
        with pytest.raises(ValueError, match="bucket"):
            _config(bucket=bucket)

    def test_an_unknown_addressing_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="addressing"):
            _config(addressing="dns")

    def test_a_non_mapping_payload_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="映射"):
            ib.ImagebedConfig.from_dict([])  # type: ignore[arg-type]

    def test_defaults_fill_in_region_and_size_cap(self) -> None:
        cfg = ib.ImagebedConfig.from_dict(
            {
                "endpoint": "https://s3.example.com",
                "bucket": "memes",
                "access_key_id": "k",
                "secret_access_key": "s",
                "max_object_size": "not a number",
            }
        )
        assert (cfg.region, cfg.max_object_size) == (
            ib.DEFAULT_REGION, ib.DEFAULT_MAX_OBJECT_SIZE,
        )

    def test_path_style_puts_the_bucket_in_the_path(self) -> None:
        cfg = _config(addressing="path")
        assert cfg.host == "s3.example.com"
        assert cfg.canonical_uri("a/b.png") == "/memes/a/b.png"
        assert cfg.url("a/b.png") == "https://s3.example.com/memes/a/b.png"

    def test_virtual_hosted_style_puts_the_bucket_in_the_host(self) -> None:
        cfg = _config(addressing="virtual")
        assert cfg.host == "memes.s3.example.com"
        assert cfg.canonical_uri("a/b.png") == "/a/b.png"
        assert cfg.url() == "https://memes.s3.example.com/"

    def test_auto_detects_virtual_hosting_from_the_endpoint(self) -> None:
        cfg = _config(endpoint="https://memes.s3.example.com")
        assert cfg.virtual_hosted is True
        assert cfg.host == "memes.s3.example.com"
        assert cfg.canonical_uri("a.png") == "/a.png"

    def test_auto_falls_back_to_path_style(self) -> None:
        cfg = _config()
        assert cfg.virtual_hosted is False
        assert cfg.canonical_uri() == "/memes"

    def test_an_endpoint_that_already_names_the_bucket_is_not_prefixed_twice(self) -> None:
        cfg = _config(endpoint="https://memes.s3.example.com", addressing="virtual")
        assert cfg.host == "memes.s3.example.com"


class TestSignedRequest:
    def test_the_same_clock_gives_the_same_signature(self) -> None:
        cfg = _config()
        first = ib.sign_get(cfg, key="a.png", amz_date=VECTOR_DATE)
        second = ib.sign_get(cfg, key="a.png", amz_date=VECTOR_DATE)
        assert first == second

    def test_the_signature_covers_the_object_key(self) -> None:
        cfg = _config()
        one = ib.sign_get(cfg, key="a.png", amz_date=VECTOR_DATE)
        other = ib.sign_get(cfg, key="b.png", amz_date=VECTOR_DATE)
        assert one.headers["authorization"] != other.headers["authorization"]

    def test_the_signature_covers_the_query(self) -> None:
        cfg = _config()
        one = ib.sign_get(cfg, params=(("prefix", "a"),), amz_date=VECTOR_DATE)
        other = ib.sign_get(cfg, params=(("prefix", "b"),), amz_date=VECTOR_DATE)
        assert one.headers["authorization"] != other.headers["authorization"]

    def test_the_credential_scope_uses_the_configured_region(self) -> None:
        signed = ib.sign_get(_config(region="us-west-2"), amz_date=VECTOR_DATE)
        assert "Credential=AKID/20150830/us-west-2/s3/aws4_request" in signed.headers["authorization"]

    def test_the_signed_headers_are_the_three_we_actually_send(self) -> None:
        signed = ib.sign_get(_config(), amz_date=VECTOR_DATE)
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in signed.headers["authorization"]
        assert signed.headers["x-amz-content-sha256"] == ib.EMPTY_PAYLOAD_SHA256
        assert signed.headers["x-amz-date"] == VECTOR_DATE

    def test_the_list_query_carries_prefix_and_continuation_token(self) -> None:
        cfg = _config(prefix="memes/")
        params = dict(ib.list_query(cfg, token="TOKEN"))
        assert params["list-type"] == "2"
        assert params["prefix"] == "memes/"
        assert params["continuation-token"] == "TOKEN"
        assert "start-after" not in params

    def test_a_continuation_token_wins_over_the_resume_cursor(self) -> None:
        params = dict(ib.list_query(_config(), token="TOKEN", start_after="a.png"))
        assert "start-after" not in params

    def test_the_resume_cursor_is_sent_when_there_is_no_token(self) -> None:
        params = dict(ib.list_query(_config(), start_after="a.png"))
        assert params["start-after"] == "a.png"


# ── XML ───────────────────────────────────

_NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def _list_xml(
    entries: Sequence[tuple[str, int]],
    *,
    truncated: bool = False,
    token: str = "",
    encoding: str = "url",
    namespace: bool = True,
) -> str:
    body = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size>"
        f'<ETag>&quot;abc{index}&quot;</ETag></Contents>'
        for index, (key, size) in enumerate(entries)
    )
    head = f"<EncodingType>{encoding}</EncodingType>" if encoding else ""
    tail = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        f"<ListBucketResult {_NS if namespace else ''}>"
        f"{head}<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
        f"{body}{tail}</ListBucketResult>"
    )


class TestListObjectsParsing:
    def test_a_normal_page_yields_key_size_and_etag(self) -> None:
        page = ib.parse_list_objects(_list_xml([("a.png", 10), ("b.gif", 20)]))
        assert page.objects == (
            ib.RemoteObject("a.png", 10, "abc0"),
            ib.RemoteObject("b.gif", 20, "abc1"),
        )
        assert page.truncated is False
        assert page.next_token == ""

    def test_an_empty_bucket_yields_no_objects(self) -> None:
        page = ib.parse_list_objects(_list_xml([]))
        assert page.objects == ()
        assert page.truncated is False

    def test_a_truncated_page_carries_the_next_token(self) -> None:
        page = ib.parse_list_objects(_list_xml([("a.png", 1)], truncated=True, token="NEXT"))
        assert (page.truncated, page.next_token) == (True, "NEXT")

    def test_a_namespaceless_response_parses_too(self) -> None:
        page = ib.parse_list_objects(_list_xml([("a.png", 1)], namespace=False))
        assert page.objects[0].key == "a.png"

    def test_url_encoded_keys_are_decoded_when_the_response_says_so(self) -> None:
        page = ib.parse_list_objects(_list_xml([("memes%2Fa%20b.png", 1)]))
        assert page.objects[0].key == "memes/a b.png"

    def test_keys_are_left_alone_when_the_response_is_not_url_encoded(self) -> None:
        page = ib.parse_list_objects(_list_xml([("100%25.png", 1)], encoding=""))
        assert page.objects[0].key == "100%25.png"

    @pytest.mark.parametrize(
        "text",
        ["", "not xml at all", "<ListBucketResult><Contents>", "<<>>"],
    )
    def test_malformed_xml_raises(self, text: str) -> None:
        with pytest.raises(ib.ImagebedError, match="XML"):
            ib.parse_list_objects(text)

    def test_an_error_document_raises_with_its_code(self) -> None:
        with pytest.raises(ib.ImagebedError, match="AccessDenied"):
            ib.parse_list_objects(
                "<Error><Code>AccessDenied</Code><Message>nope</Message></Error>"
            )

    def test_an_unexpected_root_raises(self) -> None:
        with pytest.raises(ib.ImagebedError, match="Whatever"):
            ib.parse_list_objects("<Whatever/>")

    def test_a_content_without_a_key_is_dropped(self) -> None:
        page = ib.parse_list_objects(
            f"<ListBucketResult {_NS}><Contents><Size>3</Size></Contents></ListBucketResult>"
        )
        assert page.objects == ()


# ── 传输桩 ──────────────────────────────────

def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class Recorder:
    """记下每个请求的路径与查询串，用来断言"没有发出下载请求"。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.paths: list[str] = []
        self.queries: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            self.queries.append(dict(request.url.params))
            return self._handler(request)

        return httpx.MockTransport(respond)

    @property
    def downloads(self) -> list[str]:
        return [path for path, query in zip(self.paths, self.queries) if "list-type" not in query]


def _client(cfg, recorder: Recorder):
    return ib.ImagebedClient(cfg, client=httpx.AsyncClient(transport=recorder.transport()))


def _run(coro):
    return asyncio.run(coro)


class TestFetchSizeCap:
    def test_an_object_listed_over_the_cap_is_never_requested(self) -> None:
        recorder = Recorder(lambda request: httpx.Response(200, content=b"x" * 10))
        cfg = _config(max_object_size=100)

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.fetch(ib.RemoteObject("big.png", 10_000))

        assert _run(scenario()) is None
        assert recorder.paths == []

    def test_a_lying_listing_is_caught_by_content_length(self) -> None:
        recorder = Recorder(lambda request: httpx.Response(200, content=b"x" * 500))
        cfg = _config(max_object_size=100)

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.fetch(ib.RemoteObject("big.png", 1))

        assert _run(scenario()) is None
        assert recorder.downloads == ["/memes/big.png"]

    def test_a_chunked_body_is_cut_off_at_the_cap(self) -> None:
        async def chunks():
            for _ in range(10):
                yield b"x" * 40

        recorder = Recorder(lambda request: httpx.Response(200, content=chunks()))
        cfg = _config(max_object_size=100)

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.fetch(ib.RemoteObject("big.png", 0))

        assert _run(scenario()) is None

    def test_an_object_under_the_cap_comes_back_whole(self) -> None:
        payload = _png((10, 20, 30))
        recorder = Recorder(lambda request: httpx.Response(200, content=payload))
        cfg = _config(max_object_size=len(payload))

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.fetch(ib.RemoteObject("ok.png", len(payload)))

        assert _run(scenario()) == payload

    def test_an_http_error_on_download_raises(self) -> None:
        recorder = Recorder(lambda request: httpx.Response(403, text="<Error/>"))
        cfg = _config()

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.fetch(ib.RemoteObject("a.png", 1))

        with pytest.raises(ib.ImagebedError, match="403"):
            _run(scenario())

    def test_an_http_error_on_listing_raises(self) -> None:
        recorder = Recorder(lambda request: httpx.Response(500, text="boom"))
        cfg = _config()

        async def scenario():
            async with _client(cfg, recorder) as client:
                return await client.list_page()

        with pytest.raises(ib.ImagebedError, match="500"):
            _run(scenario())


# ── 同步 ───────────────────────────────────

@pytest.fixture()
def store(tmp_path: Path):
    handle = ss.StickerStore(tmp_path / "stickers.sqlite3")
    yield handle
    handle.close()


class Bucket:
    """一只假桶：列举按键名排序并支持 start-after / continuation-token。"""

    def __init__(self, objects: dict[str, bytes], *, page_size: int = 100) -> None:
        self.objects = objects
        self.page_size = page_size
        self.sizes = {key: len(value) for key, value in objects.items()}

    def oversize(self, key: str, size: int) -> None:
        self.sizes[key] = size

    def respond(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "list-type" in params:
            return httpx.Response(200, text=self._list(params))
        key = request.url.path.split("/memes/", 1)[-1]
        body = self.objects.get(key)
        if body is None:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
        return httpx.Response(200, content=body)

    def _list(self, params: dict[str, str]) -> str:
        keys = sorted(self.sizes)
        prefix = params.get("prefix", "")
        if prefix:
            keys = [key for key in keys if key.startswith(prefix)]
        cursor = params.get("continuation-token") or params.get("start-after") or ""
        if cursor:
            keys = [key for key in keys if key > cursor]
        page, rest = keys[: self.page_size], keys[self.page_size :]
        return _list_xml(
            [(key, self.sizes[key]) for key in page],
            truncated=bool(rest),
            token=page[-1] if rest else "",
        )


def _sync(cfg, store, root: Path, bucket: Bucket, **kwargs: Any):
    recorder = Recorder(bucket.respond)

    async def scenario():
        async with _client(cfg, recorder) as client:
            return await ib.sync_once(cfg, store, root=root, client=client, **kwargs)

    return _run(scenario()), recorder


class TestSync:
    def test_every_remote_image_lands_in_the_library(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"a.png": _png((1, 2, 3)), "b.png": _png((9, 9, 9))})
        stats, _recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.listed, stats.added, stats.completed) == (2, 2, True)
        rows = store.browse(state=ss.STATE_PENDING)
        assert len(rows) == 2
        assert {row.source for row in rows} == {ss.SOURCE_IMAGEBED}
        assert {row.width for row in rows} == {64}
        for row in rows:
            assert (tmp_path / row.rel_path).is_file()
            assert row.origin.startswith("s3://memes/")

    def test_a_known_digest_is_not_stored_twice(self, store, tmp_path: Path) -> None:
        payload = _png((1, 2, 3))
        bucket = Bucket({"a.png": payload, "copy-of-a.png": payload})
        stats, _recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.added, stats.known) == (1, 1)
        assert len(store.browse()) == 1

    def test_a_digest_already_in_the_library_is_skipped(self, store, tmp_path: Path) -> None:
        payload = _png((4, 5, 6))
        digest = hashlib.sha256(payload).hexdigest()
        store.add(sha256=digest, rel_path="pending/x.png", source=ss.SOURCE_GROUP)
        stats, _recorder = _sync(_config(), store, tmp_path, Bucket({"a.png": payload}))
        assert (stats.added, stats.known) == (0, 1)
        assert len(store.browse()) == 1
        assert not (tmp_path / ib.PENDING_DIR).exists()

    def test_a_discarded_digest_stays_discarded(self, store, tmp_path: Path) -> None:
        payload = _png((7, 7, 7))
        digest = hashlib.sha256(payload).hexdigest()
        store.add(sha256=digest, rel_path="pending/x.png", source=ss.SOURCE_GROUP)
        store.discard(digest)
        stats, _recorder = _sync(_config(), store, tmp_path, Bucket({"a.png": payload}))
        assert (stats.added, stats.known) == (0, 1)
        assert store.state_of(digest) == ss.STATE_DISCARDED

    def test_an_oversize_object_is_neither_downloaded_nor_stored(
        self, store, tmp_path: Path
    ) -> None:
        bucket = Bucket({"big.png": _png((1, 1, 1)), "small.png": _png((2, 2, 2))})
        bucket.oversize("big.png", 99_000_000)
        stats, recorder = _sync(_config(max_object_size=1_000_000), store, tmp_path, bucket)
        assert (stats.added, stats.oversize) == (1, 1)
        assert recorder.downloads == ["/memes/small.png"]

    def test_a_non_image_extension_is_skipped_before_any_download(
        self, store, tmp_path: Path
    ) -> None:
        bucket = Bucket({"notes.txt": b"hello", "a.png": _png((3, 3, 3))})
        stats, recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.added, stats.unsupported) == (1, 1)
        assert recorder.downloads == ["/memes/a.png"]

    def test_bytes_that_are_not_an_image_are_dropped(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"fake.png": b"this is not a png at all"})
        stats, _recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.added, stats.unsupported) == (0, 1)
        assert store.browse() == []

    def test_one_broken_object_does_not_stop_the_run(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"a.png": _png((1, 2, 3)), "z.png": _png((4, 5, 6))})
        del bucket.objects["z.png"]
        stats, _recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.listed, stats.added, stats.failed) == (2, 1, 1)

    def test_pagination_walks_every_page(self, store, tmp_path: Path) -> None:
        payloads = {f"{index:02d}.png": _png((index, index, index)) for index in range(5)}
        bucket = Bucket(payloads, page_size=2)
        stats, _recorder = _sync(_config(), store, tmp_path, bucket)
        assert (stats.listed, stats.added, stats.completed) == (5, 5, True)

    def test_the_prefix_filters_the_listing(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"memes/a.png": _png((1, 1, 1)), "other/b.png": _png((2, 2, 2))})
        stats, _recorder = _sync(_config(prefix="memes/"), store, tmp_path, bucket)
        assert (stats.listed, stats.added) == (1, 1)


class TestProgress:
    def test_a_capped_run_records_where_it_stopped(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"a.png": _png((1, 1, 1)), "b.png": _png((2, 2, 2))})
        progress_path = tmp_path / "progress.json"
        stats, _recorder = _sync(
            _config(), store, tmp_path, bucket, progress_path=progress_path, limit=1
        )
        assert (stats.listed, stats.completed) == (1, False)
        saved = json.loads(progress_path.read_text(encoding="utf-8"))
        assert saved["last_key"] == "a.png"
        assert saved["bucket"] == "memes"

    def test_the_next_run_resumes_after_the_recorded_key(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"a.png": _png((1, 1, 1)), "b.png": _png((2, 2, 2))})
        progress_path = tmp_path / "progress.json"
        cfg = _config()
        _sync(cfg, store, tmp_path, bucket, progress_path=progress_path, limit=1)
        stats, recorder = _sync(cfg, store, tmp_path, bucket, progress_path=progress_path)
        assert recorder.queries[0]["start-after"] == "a.png"
        assert (stats.listed, stats.added, stats.completed) == (1, 1, True)

    def test_finishing_a_pass_rewinds_the_cursor(self, store, tmp_path: Path) -> None:
        bucket = Bucket({"a.png": _png((1, 1, 1))})
        progress_path = tmp_path / "progress.json"
        _sync(_config(), store, tmp_path, bucket, progress_path=progress_path)
        saved = json.loads(progress_path.read_text(encoding="utf-8"))
        assert (saved["last_key"], saved["passes"]) == ("", 1)

    def test_changing_the_prefix_invalidates_the_cursor(self, store, tmp_path: Path) -> None:
        progress_path = tmp_path / "progress.json"
        ib.save_progress(
            progress_path,
            ib.SyncProgress(bucket="memes", prefix="old/", last_key="z.png", passes=3),
        )
        bucket = Bucket({"a.png": _png((1, 1, 1))})
        stats, recorder = _sync(
            _config(prefix="new/"), store, tmp_path, bucket, progress_path=progress_path
        )
        assert "start-after" not in recorder.queries[0]
        assert stats.listed == 0
        assert json.loads(progress_path.read_text(encoding="utf-8"))["passes"] == 1

    def test_a_corrupt_progress_file_is_treated_as_no_progress(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.json"
        path.write_text("{not json", encoding="utf-8")
        assert ib.load_progress(path) == ib.SyncProgress()

    def test_a_missing_progress_file_is_treated_as_no_progress(self, tmp_path: Path) -> None:
        assert ib.load_progress(tmp_path / "nope.json") == ib.SyncProgress()

    def test_saving_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.json"
        ib.save_progress(path, ib.SyncProgress(bucket="memes", last_key="a.png"))
        assert [item.name for item in tmp_path.iterdir()] == ["progress.json"]
        assert ib.load_progress(path).last_key == "a.png"


class TestLayout:
    def test_the_relative_path_shards_by_digest(self) -> None:
        digest = "ab" + "c" * 62
        assert ib.relative_path(digest, "png") == f"pending/ab/{digest}.png"

    def test_jpeg_lands_on_the_jpg_extension(self) -> None:
        digest = "f" * 64
        assert ib.relative_path(digest, "jpeg").endswith(".jpg")

    def test_an_unknown_format_falls_back_to_bin(self) -> None:
        assert ib.relative_path("0" * 64, "tiff").endswith(".bin")

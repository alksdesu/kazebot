"""QQ 文件 ID 解析、下载回退与入站文件边界。"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from _onebot_harness import load_runtime, set_live_config


_FILE_ID = "9ca4be68-5ac7-4e3b-8000-55ee67e902ac"
_URL = "https://qq-files.example.test/download"
_CONVERSATION = "qq_group:901:sender:22"


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("ONEBOT_LOCAL_SOURCE_ROOTS", raising=False)
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, enable_file_input=True, file_max_bytes=8192)
    original_client = httpx.AsyncClient

    def unexpected_request(request):
        raise AssertionError(f"未配置的下载请求：{request.url}")

    module.download_handler = unexpected_request
    module.download_requests = []

    def dispatch(request):
        module.download_requests.append(str(request.url))
        return module.download_handler(request)

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(module.media.httpx, "AsyncClient", client)
    return module


def _bot(*responses):
    return SimpleNamespace(call_api=AsyncMock(side_effect=list(responses)))


def _receive(runtime, data, *, bot=None, group_id=None):
    sources = runtime.media._iter_qq_file_sources([{"type": "file", "data": data}])
    return asyncio.run(runtime.media._file_sources_to_attachments(
        sources, _CONVERSATION, bot=bot, group_id=group_id,
    ))


def _assert_file(tmp_path, received, expected, *, name="report.txt"):
    attachments, errors = received
    assert errors == []
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["type"] == "file"
    assert attachment["source"] == "onebot"
    assert attachment["name"] == name
    path = tmp_path / attachment["path"]
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.read_bytes() == expected


def _assert_rejected(tmp_path, received, fragment):
    attachments, errors = received
    assert attachments == []
    assert len(errors) == 1
    assert fragment in errors[0]
    assert not list((tmp_path / "data" / "attachments").rglob("*.*"))


@pytest.mark.parametrize("group_id,api,parameters", [
    (901, "get_group_file_url", {"group_id": "901", "file_id": _FILE_ID}),
    (None, "get_private_file_url", {"file_id": _FILE_ID}),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_production_filename_and_id_resolve_to_download(runtime, tmp_path, group_id, api, parameters, wrapped):
    content = b"a" * 5735
    runtime.download_handler = lambda request: httpx.Response(200, content=content)
    response = {"url": _URL}
    if wrapped:
        response = {"status": "ok", "retcode": 0, "data": response}
    bot = _bot(response)

    received = _receive(runtime, {"file": "report.txt", "id": _FILE_ID, "size": 5735}, bot=bot, group_id=group_id)

    _assert_file(tmp_path, received, content)
    bot.call_api.assert_awaited_once_with(api, **parameters)
    assert runtime.download_requests == [_URL]


@pytest.mark.parametrize("id_key", ["id", "file_id"])
@pytest.mark.parametrize("metadata_key", ["file", "url", "path", "base64"])
def test_id_only_waits_for_get_file_metadata(runtime, tmp_path, id_key, metadata_key):
    content = b"metadata supplies the missing name"
    local = tmp_path / "from-napcat.txt"
    local.write_bytes(content)
    runtime.download_handler = lambda request: httpx.Response(200, content=content)
    source = {
        "file": str(local), "url": _URL, "path": str(local),
        "base64": base64.b64encode(content).decode("ascii"),
    }[metadata_key]
    bot = _bot({"url": _URL}, {metadata_key: source, "file_name": "report.txt", "file_size": len(content)})

    received = _receive(runtime, {id_key: _FILE_ID}, bot=bot, group_id=901)

    _assert_file(tmp_path, received, content)
    assert bot.call_api.await_args_list[0].args == ("get_group_file_url",)
    assert bot.call_api.await_args_list[1].args == ("get_file",)
    assert bot.call_api.await_args_list[1].kwargs == {"file_id": _FILE_ID}


def test_original_url_success_skips_file_lookup(runtime, tmp_path):
    runtime.download_handler = lambda request: httpx.Response(200, content=b"original")
    bot = _bot()

    received = _receive(runtime, {"name": "report.txt", "file_id": _FILE_ID, "url": _URL}, bot=bot, group_id=901)

    _assert_file(tmp_path, received, b"original")
    bot.call_api.assert_not_awaited()


@pytest.mark.parametrize("source_kind", ["url", "path", "file_uri"])
@pytest.mark.parametrize("source_name", ["download", "download.bin"])
def test_opaque_source_name_waits_for_file_metadata(runtime, tmp_path, source_kind, source_name):
    local = tmp_path / source_name
    local.write_bytes(b"opaque local source")
    key, source = {
        "url": ("url", f"https://qq-files.example.test/{source_name}"),
        "path": ("path", str(local)),
        "file_uri": ("file", local.as_uri()),
    }[source_kind]
    bot = _bot({}, {"file_name": "report.txt", "base64": "cmVzb2x2ZWQ="})

    received = _receive(runtime, {"id": _FILE_ID, key: source}, bot=bot)

    _assert_file(tmp_path, received, b"resolved")
    assert [call.args[0] for call in bot.call_api.await_args_list] == ["get_private_file_url", "get_file"]
    assert runtime.download_requests == []


@pytest.mark.parametrize("source_kind", ["url", "path", "file_uri"])
def test_usable_inferred_name_downloads_without_lookup(runtime, tmp_path, source_kind):
    local = tmp_path / "report.txt"
    local.write_bytes(b"direct source")
    runtime.download_handler = lambda request: httpx.Response(200, content=b"direct source")
    key, source = {
        "url": ("url", "https://qq-files.example.test/report.txt?token=abc"),
        "path": ("path", str(local)),
        "file_uri": ("file", local.as_uri()),
    }[source_kind]
    bot = _bot()

    received = _receive(runtime, {"id": _FILE_ID, key: source}, bot=bot)

    _assert_file(tmp_path, received, b"direct source")
    bot.call_api.assert_not_awaited()


def test_file_value_equal_to_id_does_not_become_a_filename(runtime, tmp_path):
    bot = _bot({}, {"file_name": "report.txt", "base64": "b2s="})

    received = _receive(runtime, {"file": _FILE_ID, "file_id": _FILE_ID}, bot=bot)

    _assert_file(tmp_path, received, b"ok")


def test_failed_url_tries_other_segment_source_before_api(runtime, tmp_path):
    local = tmp_path / "available.txt"
    local.write_bytes(b"local copy")
    runtime.download_handler = lambda request: httpx.Response(403)
    bot = _bot()

    received = _receive(runtime, {
        "name": "report.txt", "file_id": _FILE_ID, "url": _URL, "path": str(local),
    }, bot=bot)

    _assert_file(tmp_path, received, b"local copy")
    assert runtime.download_requests == [_URL]
    bot.call_api.assert_not_awaited()


def test_expired_original_and_resolved_links_fall_back_to_get_file(runtime, tmp_path):
    expired = "https://qq-files.example.test/expired"
    runtime.download_handler = lambda request: httpx.Response(403)
    bot = _bot({"url": _URL}, {"status": "ok", "retcode": 0, "data": {
        "file_name": "report.txt", "base64": base64.b64encode(b"recovered").decode("ascii"),
    }})

    received = _receive(runtime, {"file": "report.txt", "id": _FILE_ID, "url": expired}, bot=bot, group_id=901)

    _assert_file(tmp_path, received, b"recovered")
    assert runtime.download_requests == [expired, _URL]
    assert [call.args[0] for call in bot.call_api.await_args_list] == ["get_group_file_url", "get_file"]


@pytest.mark.parametrize("failed_response", [
    RuntimeError("API unsupported"),
    {"status": "failed", "retcode": 1200, "data": {"url": _URL}},
    {"status": "ok", "retcode": 1, "data": {"url": _URL}},
    None,
])
def test_failed_url_api_uses_get_file(runtime, tmp_path, failed_response):
    bot = _bot(failed_response, {"file_name": "report.txt", "base64": "b2s="})

    received = _receive(runtime, {"file_id": _FILE_ID}, bot=bot)

    _assert_file(tmp_path, received, b"ok")
    assert [call.args[0] for call in bot.call_api.await_args_list] == ["get_private_file_url", "get_file"]


@pytest.mark.parametrize("failure,fragment", [
    ("forbidden", "HTTP 403"),
    ("timeout", "下载超时"),
])
def test_download_error_remains_specific_when_apis_fail(runtime, tmp_path, failure, fragment):
    def fail_download(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(403)

    runtime.download_handler = fail_download
    bot = _bot(RuntimeError("unsupported"), RuntimeError("unsupported"))

    received = _receive(runtime, {"name": "report.txt", "url": _URL, "id": _FILE_ID}, bot=bot)

    _assert_rejected(tmp_path, received, fragment)


@pytest.mark.parametrize("bot,fragment", [
    (None, "无法通过文件 ID"),
    (object(), "无法通过文件 ID"),
])
def test_id_with_no_available_api_reports_resolution_failure(runtime, tmp_path, bot, fragment):
    _assert_rejected(tmp_path, _receive(runtime, {"id": _FILE_ID}, bot=bot), fragment)


@pytest.mark.parametrize("failure,fragment", [
    (RuntimeError("no source"), "无法通过文件 ID"),
    (asyncio.TimeoutError(), "查询文件来源超时"),
])
def test_all_lookup_failures_report_actionable_error(runtime, tmp_path, failure, fragment):
    bot = _bot(failure, failure)

    _assert_rejected(tmp_path, _receive(runtime, {"id": _FILE_ID}, bot=bot), fragment)
    assert bot.call_api.await_count == 2


def test_filename_without_id_or_source_reports_missing_source(runtime, tmp_path):
    _assert_rejected(tmp_path, _receive(runtime, {"file": "report.txt"}), "未提供可用的下载地址或文件 ID")
    assert runtime.download_requests == []


@pytest.mark.parametrize("kind", ["local", "http", "base64"])
def test_empty_files_are_saved(runtime, tmp_path, kind):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    runtime.download_handler = lambda request: httpx.Response(200, content=b"")
    source = {"local": str(empty), "http": _URL, "base64": "base64://"}[kind]

    received = _receive(runtime, {"name": "report.txt", "file": source, "size": 0})

    _assert_file(tmp_path, received, b"")


@pytest.mark.parametrize("content", [b"", b"incomplete"])
def test_reported_nonzero_size_rejects_truncated_download(runtime, tmp_path, content):
    runtime.download_handler = lambda request: httpx.Response(200, content=content)

    received = _receive(runtime, {"name": "report.txt", "url": _URL, "size": 5735})

    _assert_rejected(tmp_path, received, "大小与上报大小不一致")


def test_invalid_base64_reports_decoding_error(runtime, tmp_path):
    _assert_rejected(tmp_path, _receive(runtime, {"name": "report.txt", "file": "base64://not valid!"}), "无法解码")


@pytest.mark.parametrize("kind", ["reported", "metadata", "http", "base64", "local"])
def test_size_limit_applies_to_every_source(runtime, tmp_path, kind):
    content = b"a" * 8193
    local = tmp_path / "large.txt"
    local.write_bytes(content)
    runtime.download_handler = lambda request: httpx.Response(200, content=content)
    data = {"name": "report.txt"}
    bot = None
    if kind == "reported":
        data.update(url=_URL, size=8193)
    elif kind == "metadata":
        data["id"] = _FILE_ID
        bot = _bot({"url": _URL, "file_size": 8193})
    elif kind == "http":
        data["url"] = _URL
    elif kind == "base64":
        data["file"] = "base64://" + base64.b64encode(content).decode("ascii")
    else:
        data["path"] = str(local)

    _assert_rejected(tmp_path, _receive(runtime, data, bot=bot), "文件太大")
    if kind in {"reported", "metadata"}:
        assert runtime.download_requests == []


@pytest.mark.parametrize("name,content,fragment", [
    ("report.exe", b"plain text", "文件类型我不接收"),
    ("report.pdf", b"MZ\x90\x00" + b"\0" * 64, "可执行程序"),
])
def test_resolved_files_preserve_type_and_content_policy(runtime, tmp_path, name, content, fragment):
    bot = _bot({}, {"file_name": name, "base64": base64.b64encode(content).decode("ascii")})

    _assert_rejected(tmp_path, _receive(runtime, {"id": _FILE_ID}, bot=bot), fragment)


def test_api_local_path_cannot_read_outside_workspace(runtime, tmp_path, tmp_path_factory, monkeypatch):
    secret = tmp_path_factory.mktemp("external-file-source") / "secret.txt"
    secret.write_bytes(b"private secret")
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.resolve() != secret.resolve(), "越权读取了外部文件"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    bot = _bot({}, {"file_name": "report.txt", "file": str(secret)})

    _assert_rejected(tmp_path, _receive(runtime, {"id": _FILE_ID}, bot=bot), "工作区之外")


def test_container_only_path_can_fall_back_to_resolved_url(runtime, tmp_path):
    runtime.download_handler = lambda request: httpx.Response(200, content=b"mounted elsewhere")
    bot = _bot({"url": _URL})

    received = _receive(runtime, {"name": "report.txt", "id": _FILE_ID, "path": "/napcat/only/in/container/report.txt"}, bot=bot, group_id=901)

    _assert_file(tmp_path, received, b"mounted elsewhere")
    bot.call_api.assert_awaited_once_with("get_group_file_url", group_id="901", file_id=_FILE_ID)


def test_container_path_returned_by_get_file_uses_its_url(runtime, tmp_path):
    runtime.download_handler = lambda request: httpx.Response(200, content=b"downloaded")
    bot = _bot({}, {
        "file_name": "report.txt", "file": "/napcat/only/in/container/report.txt", "url": _URL,
    })

    _assert_file(tmp_path, _receive(runtime, {"id": _FILE_ID}, bot=bot), b"downloaded")


@pytest.mark.parametrize("name_key", ["file", "name", "file_name", "filename"])
def test_known_blocked_extension_is_rejected_before_lookup(runtime, tmp_path, name_key):
    bot = _bot()

    _assert_rejected(tmp_path, _receive(runtime, {
        name_key: "report.exe", "id": _FILE_ID, "url": "https://qq-files.example.test/report.txt",
    }, bot=bot), "文件类型我不接收")
    bot.call_api.assert_not_awaited()
    assert runtime.download_requests == []


def test_disabled_file_input_never_resolves_or_downloads(runtime, tmp_path):
    set_live_config(runtime, enable_file_input=False)
    bot = _bot()

    assert _receive(runtime, {"name": "report.txt", "id": _FILE_ID, "url": _URL}, bot=bot) == ([], [])
    assert runtime.download_requests == []
    bot.call_api.assert_not_awaited()

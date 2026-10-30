"""协程里的阻塞读写放到线程里：读文本附件、给图片编码、选点名的附件、存错误快照都不占事件循环，结果和同步做的一样。"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

# 先把 engine.inference 整个拉起来：只跑部分测试时，先导入 plaintext 会撞上它和 ai_step 的循环导入。
from _engine_loop_fixture import (  # noqa: E402
    NodeRun,
    ScriptedRegistry,
    call,
    make_loop_state,
    real_calls,
    stored,
    turn,
    use_hooks,
)
from engine import attachments, runner  # noqa: E402
from engine import prompt as prompt_module  # noqa: E402
from engine.attachments import (  # noqa: E402
    _TEXT_FILE_MAX_BYTES,
    build_multimodal_content,
    prepare_messages_for_llm,
    strip_image_blocks,
)
from engine.builtin import error_snapshot, fallback_provider, knowledge_inject  # noqa: E402
from engine.builtin.attachment import AttachmentCollector  # noqa: E402
from engine.builtin.preempt import _inject_preempt_message  # noqa: E402
from engine.conversation_routing import turn_input_meta  # noqa: E402
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.dispatch_results import INBOUND_RESULTS_KEY  # noqa: E402
from engine.inference import llm_call, pseudo_handlers  # noqa: E402
from engine.inference.ai_step import _execute_real_tools  # noqa: E402
from engine.inference.llm_call import (  # noqa: E402
    _build_messages_for_provider,
    _call_llm_with_retry,
    digest_provider_call,
    provider_messages,
)
from engine.inference.loop_state import TOOL_RESULT_ATTACHMENT_TYPE  # noqa: E402
from engine.inference.message_assembly import assemble_messages_with_injections  # noqa: E402
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH, TaskAction  # noqa: E402
from providers.base import ProviderResponse, ToolCall  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

NOTE = "data/uploads/note.txt"
NOTE_TEXT = "附件里的正文 second line"
PICTURE_URL = "file://data/attachments/sess/pic.png"


class _Threads:
    """记下被包住的函数每次在哪个线程上跑；建它的线程就是 asyncio.run 跑事件循环的线程。"""

    def __init__(self) -> None:
        self.loop_thread = threading.get_ident()
        self.seen: list[int] = []

    def wrap(self, function: Callable[..., Any]) -> Callable[..., Any]:
        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.seen.append(threading.get_ident())
            return function(*args, **kwargs)

        return recorded

    def off_loop(self) -> bool:
        return bool(self.seen) and self.loop_thread not in self.seen


def _file(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _note(root: Path, relative: str = NOTE, text: str = NOTE_TEXT) -> dict[str, Any]:
    _file(root, relative, text.encode("utf-8"))
    return {"type": "file", "path": relative, "name": Path(relative).name, "mime_type": "text/plain"}


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _picture_view(root: Path, url: str = PICTURE_URL) -> list[dict[str, Any]]:
    _file(root, "data/attachments/sess/pic.png", _png())
    return [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]


def _runs_beside_the_loop(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, start: Callable[[], Awaitable[Any]],
) -> tuple[list[bool], Any]:
    """把 owner.name 换成等一个事件才往下走的版本，由同一个循环上的另一个协程放行。"""
    released = threading.Event()
    waited: list[bool] = []
    real = getattr(owner, name)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        # 在事件循环上跑的话，放行它的那个协程永远轮不到，只能等到超时
        waited.append(released.wait(timeout=5))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, blocked)

    async def run() -> Any:
        async def release() -> None:
            released.set()

        result, _ = await asyncio.gather(start(), release())
        return result

    return waited, asyncio.run(run())


@pytest.fixture()
def reads(monkeypatch: pytest.MonkeyPatch) -> _Threads:
    threads = _Threads()
    monkeypatch.setattr(attachments, "_read_text_head", threads.wrap(attachments._read_text_head))
    return threads


@pytest.fixture()
def encodes(monkeypatch: pytest.MonkeyPatch) -> _Threads:
    threads = _Threads()
    monkeypatch.setattr(llm_call, "prepare_messages_for_llm", threads.wrap(prepare_messages_for_llm))
    return threads


class TestTextAttachments:
    @pytest.mark.parametrize("case", ["text", "binary", "too_large", "missing", "outside_data", "image"])
    def test_the_async_build_gives_what_the_sync_one_gives(self, tmp_path: Path, reads: _Threads, case: str) -> None:
        if case == "text":
            attachment = _note(tmp_path)
        elif case == "binary":
            _file(tmp_path, "data/uploads/archive.zip", b"PK\x03\x04\x00\x00" * 20)
            attachment = {"type": "file", "path": "data/uploads/archive.zip", "mime_type": "application/zip"}
        elif case == "too_large":
            _file(tmp_path, "data/uploads/big.txt", b"a" * (_TEXT_FILE_MAX_BYTES + 1))
            attachment = {"type": "file", "path": "data/uploads/big.txt"}
        elif case == "missing":
            attachment = {"type": "file", "path": "data/uploads/gone.txt"}
        elif case == "outside_data":
            _file(tmp_path, "config/secret.txt", b"not for the model")
            attachment = {"type": "file", "path": "config/secret.txt"}
        else:
            attachment = {"type": "image", "path": "data/uploads/pic.png"}

        content = asyncio.run(attachments.abuild_multimodal_content("看附件", [attachment], workspace_root=tmp_path))
        seen = list(reads.seen)

        assert content == build_multimodal_content("看附件", [attachment], workspace_root=tmp_path)
        assert reads.loop_thread not in seen
        assert bool(seen) is (case in {"text", "binary"})

    @pytest.mark.parametrize("given", [None, []])
    def test_without_attachments_the_text_comes_back_as_is(self, tmp_path: Path, reads: _Threads, given: Any) -> None:
        assert asyncio.run(attachments.abuild_multimodal_content("只有字", given, workspace_root=tmp_path)) == "只有字"
        assert reads.seen == []

    def test_a_file_of_exactly_the_limit_is_still_injected(self, tmp_path: Path) -> None:
        _file(tmp_path, NOTE, b"a" * _TEXT_FILE_MAX_BYTES)

        [part] = attachments.attachments_to_content_parts([{"type": "file", "path": NOTE}], workspace_root=tmp_path)

        assert part["text"].endswith("\n---\n" + "a" * _TEXT_FILE_MAX_BYTES + "\n---")

    def test_a_file_that_grows_after_the_size_check_is_read_only_up_to_the_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = _file(tmp_path, NOTE, b"a" * (3 * _TEXT_FILE_MAX_BYTES))
        requested: list[int] = []

        class _CountingStream:
            def __init__(self, stream: Any) -> None:
                self._stream = stream

            def read(self, size: int = -1) -> bytes:
                requested.append(size)
                return self._stream.read(size)

            def fileno(self) -> int:
                return self._stream.fileno()

            def __enter__(self) -> "_CountingStream":
                return self

            def __exit__(self, *exc_info: Any) -> None:
                self._stream.close()

        class _GrewAfterStat(type(real)):  # type: ignore[misc]
            def stat(self, *, follow_symlinks: bool = True) -> Any:
                return SimpleNamespace(st_size=16)

            def open(self, *args: Any, **kwargs: Any) -> Any:
                return _CountingStream(super().open(*args, **kwargs))

        monkeypatch.setattr(attachments, "resolve_attachment_file", lambda root, relative: _GrewAfterStat(real))

        [part] = attachments.attachments_to_content_parts([{"type": "file", "path": NOTE}], workspace_root=tmp_path)

        assert requested == [_TEXT_FILE_MAX_BYTES + 1]
        assert "File too large to inject: 300.0KB > 100KB limit" in part["text"]
        assert "a" * 64 not in part["text"]

    def test_the_loop_keeps_running_while_an_attachment_is_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        attachment = _note(tmp_path)

        waited, content = _runs_beside_the_loop(
            monkeypatch, attachments, "_read_text_head",
            lambda: attachments.abuild_multimodal_content("看附件", [attachment], workspace_root=tmp_path),
        )

        assert waited == [True] and NOTE_TEXT in str(content)


class TestWhereAttachmentsAreRead:
    def test_the_knowledge_rebuild_reads_the_turns_attachment_in_a_thread(self, tmp_path: Path, reads: _Threads) -> None:
        attachment = _note(tmp_path)
        system_prompt = [{"role": "system", "content": "persona"}]
        history = [{"role": "user", "content": "earlier"}]
        ctx = SimpleNamespace(
            messages=[{"role": "system", "content": "placeholder"}],
            rctx=SimpleNamespace(workspace_root=tmp_path, task_context={}),
            extra={
                "system_prompt": system_prompt, "history": history,
                "instruction_text": "看附件", "attachments": [attachment],
            },
        )

        asyncio.run(knowledge_inject._rebuild_prompt_messages(ctx))
        off_loop = reads.off_loop()

        expected, block_mode = assemble_messages_with_injections(
            workspace_root=tmp_path, system_prompt=system_prompt, history=history,
            instruction="看附件", attachments=[attachment], turn_meta=turn_input_meta({}),
        )
        assert ctx.messages == expected and ctx.extra["is_block_mode"] is block_mode
        assert off_loop

    def test_the_first_assembly_reads_prompt_fragments_and_the_attachment_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: _Threads,
    ) -> None:
        _file(tmp_path, "config/nodes/rules.md", "片段里的规则".encode("utf-8"))
        fragments = _Threads()
        monkeypatch.setattr(prompt_module, "_include_file", fragments.wrap(prompt_module._include_file))
        node = Node(
            id="node-1", type="ai", prompt="基础设定\n{{include:rules.md}}", tool_mode="native",
            tool_access=ToolAccess(mode="all"),
        )
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(), node=node)

        action = run.run(
            turn(call("finish", {"text": "done"}, call_id="f")), instruction="看附件", attachments=[_note(tmp_path)],
        )

        assert action.action == ACTION_FINISH
        assert fragments.off_loop() and reads.off_loop()
        first = run.requests[0]["messages"]
        assert any("片段里的规则" in str(m.get("content")) for m in first if m.get("role") == "system")
        assert any(NOTE_TEXT in str(m.get("content")) for m in first if m.get("role") == "user")

    @pytest.mark.parametrize("deferred", [False, True])
    def test_a_tool_attachment_message_is_built_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: _Threads, deferred: bool,
    ) -> None:
        attachment = _note(tmp_path)

        async def produce(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"result": "wrote it", "attachments": [attachment]}}

        use_hooks(monkeypatch, {"after_tool_call": [AttachmentCollector()]})
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"produce": produce}))
        held: list[dict[str, Any]] | None = [] if deferred else None

        asyncio.run(_execute_real_tools(ls, real_calls(call("produce", call_id="p")), step=1, attachment_messages=held))
        off_loop = reads.off_loop()

        def is_attachment_message(message: dict[str, Any]) -> bool:
            return (message.get("_meta") or {}).get("message_type") == TOOL_RESULT_ATTACHMENT_TYPE

        if deferred:
            [message] = held or []
            assert not any(is_attachment_message(m) for m in ls.messages)
        else:
            [message] = [m for m in ls.messages if is_attachment_message(m)]
        assert message["content"] == build_multimodal_content(
            "以上工具执行产生了以下图片结果：", [attachment], workspace_root=tmp_path,
        )
        assert off_loop

    def test_a_preempt_message_reads_its_attachment_in_a_thread(self, tmp_path: Path, reads: _Threads) -> None:
        attachment = _note(tmp_path)
        store = ConversationStore(tmp_path / "data" / "conversations")
        prompt = [{"role": "system", "content": "persona"}]
        loop = SimpleNamespace(
            rctx=SimpleNamespace(
                workspace_root=tmp_path, task_context={}, session_id="branch", child_session_id="", task_id="task",
                conversation_store=store, consume_preempt=AsyncMock(), emit_event=AsyncMock(return_value=True),
            ),
            messages=list(prompt), history=[], system_prompt=prompt, is_block_mode=False,
            node=Node(id="entry", type="ai"), runtime_cfg={},
            preempt_inject_info={"message": "再看这个", "attachments": [attachment]},
        )

        asyncio.run(_inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop))
        off_loop = reads.off_loop()

        [message] = store.load("branch")
        assert message.content == build_multimodal_content("再看这个", [attachment], workspace_root=tmp_path)
        assert off_loop


def _run_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_input: dict[str, Any]) -> dict[str, Any]:
    """照 engine 收到一轮输入那样跑 _run_node_task，模型那一步换成桩。"""
    lead = Node(id="lead", type="ai", prompt="Work.", tool_mode="native")
    monkeypatch.setattr(runner, "load_node", lambda root, node_id: lead)
    monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(runner, "install_event_bridge", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "run_ai_node", AsyncMock(
        return_value=TaskAction(action="finish", node_id="lead", result={"text": "done"}),
    ))
    task = {"task_id": "task-1", "node_id": "lead", "input": {"task_context": {}, **task_input}}

    async def run_task() -> dict[str, Any]:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as http:
            return await runner._run_node_task(
                http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
                registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
                task=task, worker_id="worker", session_id="session-1", session_generation=1,
                api_key="test", base_url="http://model.invalid", default_model="test",
            )

    return asyncio.run(run_task())


class TestRunnerInput:
    def test_the_turns_attachment_is_read_in_a_thread_before_it_is_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: _Threads,
    ) -> None:
        attachment = _note(tmp_path)

        result = _run_task(tmp_path, monkeypatch, {"instruction": "看附件", "attachments": [attachment]})
        off_loop = reads.off_loop()

        assert result["action"] == "finish"
        [message] = stored(tmp_path, "session-1")
        assert message.content == build_multimodal_content("看附件", [attachment], workspace_root=tmp_path)
        assert off_loop

    def test_each_async_result_delivered_together_is_built_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: _Threads,
    ) -> None:
        first = _note(tmp_path)
        second = _note(tmp_path, "data/uploads/other.txt", "另一份结果")
        results = [
            {
                "inbound_child_node_id": "scout", "inbound_child_task_id": "child-1", "inbound_child_status": "finish",
                "text": "one", "attachments": [first],
            },
            {
                "inbound_child_node_id": "helper", "inbound_child_task_id": "child-2", "inbound_child_status": "finish",
                "text": "two", "attachments": [second],
            },
        ]

        _run_task(tmp_path, monkeypatch, {
            "instruction": "one", "inbound_message_type": "dispatch_result",
            **{key: value for key, value in results[0].items() if key.startswith("inbound_")},
            INBOUND_RESULTS_KEY: results,
        })
        off_loop = reads.off_loop()

        assert [message.content for message in stored(tmp_path, "session-1")] == [
            build_multimodal_content("one", [first], workspace_root=tmp_path),
            build_multimodal_content("two", [second], workspace_root=tmp_path),
        ]
        assert off_loop


class TestImageEncoding:
    @pytest.mark.parametrize("strip_images", [False, True])
    def test_provider_messages_encodes_in_a_thread_and_matches_the_sync_result(
        self, tmp_path: Path, encodes: _Threads, strip_images: bool,
    ) -> None:
        view = _picture_view(tmp_path)
        formatter, provider = create_tool_formatter("native"), SimpleNamespace(name="test")

        sent = asyncio.run(provider_messages(
            view, formatter=formatter, provider=provider, workspace_root=tmp_path, strip_images=strip_images,
        ))
        off_loop = encodes.off_loop()

        expected = prepare_messages_for_llm(_build_messages_for_provider(view, formatter, provider), tmp_path)
        assert sent == (strip_image_blocks(expected) if strip_images else expected)
        assert ("data:image/jpeg;base64," in json.dumps(sent)) is not strip_images
        assert off_loop

    def test_a_missing_image_still_becomes_the_placeholder(self, tmp_path: Path, encodes: _Threads) -> None:
        view = _picture_view(tmp_path, "file://data/attachments/sess/gone.png")

        sent = asyncio.run(provider_messages(
            view, formatter=create_tool_formatter("native"), provider=SimpleNamespace(name="test"),
            workspace_root=tmp_path, strip_images=False,
        ))

        assert "[Image unavailable]" in json.dumps(sent, ensure_ascii=False) and "image_url" not in json.dumps(sent)
        assert encodes.off_loop()

    def test_the_loop_keeps_running_while_images_are_encoded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        view = _picture_view(tmp_path)

        waited, sent = _runs_beside_the_loop(
            monkeypatch, llm_call, "prepare_messages_for_llm",
            lambda: provider_messages(
                view, formatter=create_tool_formatter("native"), provider=SimpleNamespace(name="test"),
                workspace_root=tmp_path, strip_images=False,
            ),
        )

        assert waited == [True] and "data:image/jpeg;base64," in json.dumps(sent)

    @pytest.mark.asyncio
    async def test_the_live_call_encodes_in_a_thread(self, tmp_path: Path, encodes: _Threads) -> None:
        received: list[Any] = []

        class Provider:
            name = "test"
            model = "test"

            async def chat(self, *, messages: Any, tools: Any) -> ProviderResponse:
                received.append(messages)
                return ProviderResponse(ok=True, text="done")

            async def chat_stream(self, *, messages: Any, tools: Any, **callbacks: Any) -> ProviderResponse:
                return await self.chat(messages=messages, tools=tools)

        loop = SimpleNamespace(
            messages=_picture_view(tmp_path), formatter=create_tool_formatter("native"), provider=Provider(),
            node=Node(id="entry", type="ai"), openai_tools=[], use_stream=False,
            retry_max=1, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
            rctx=SimpleNamespace(
                workspace_root=tmp_path, task_id="task", begin_llm_request=lambda: "request", emit_event=AsyncMock(),
            ),
        )

        response = await _call_llm_with_retry(loop, 0)

        assert response.ok and encodes.off_loop()
        assert "data:image/jpeg;base64," in json.dumps(received[0])

    @pytest.mark.asyncio
    async def test_the_fallback_channel_encodes_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encodes: _Threads,
    ) -> None:
        captured: dict[str, Any] = {}

        class Backup:
            name = "gemini"
            model = "backup"

            async def chat(self, *, messages: Any, tools: Any) -> ProviderResponse:
                captured["messages"] = messages
                return ProviderResponse(ok=True, text="done")

        monkeypatch.setattr(fallback_provider, "load_models_config", lambda _root: {"fallbacks": [{
            "provider": "gemini", "base_url": "https://example.test", "api_key": "test", "model": "backup",
            "supports_vision": True,
        }]})
        monkeypatch.setattr(fallback_provider, "_create_fallback_provider", lambda **_kwargs: Backup())
        ctx = SimpleNamespace(
            response=ProviderResponse(ok=False, error="temporary", status_code=502),
            messages=_picture_view(tmp_path), tools=[],
            provider=SimpleNamespace(name="gemini", model="primary"), node=Node(id="entry", type="ai"),
            rctx=SimpleNamespace(workspace_root=tmp_path),
            extra={"loop_state": SimpleNamespace(formatter=create_tool_formatter("native"))},
        )

        await fallback_provider.FallbackProviderHandler().handle(ctx)

        assert ctx.response.ok and encodes.off_loop()
        assert "data:image/jpeg;base64," in json.dumps(captured["messages"])

    def test_the_progress_digest_encodes_in_a_thread_and_drops_images(self, tmp_path: Path, encodes: _Threads) -> None:
        view = _picture_view(tmp_path)
        received: list[Any] = []

        class Provider:
            name = "test"

            async def chat(self, *, messages: Any, tools: Any) -> ProviderResponse:
                received.append(messages)
                return ProviderResponse(ok=True, text="digest")

        formatter, provider = create_tool_formatter("native"), Provider()
        loop = SimpleNamespace(formatter=formatter, provider=provider, rctx=SimpleNamespace(workspace_root=tmp_path))

        response = asyncio.run(digest_provider_call(loop, 0)(view))
        off_loop = encodes.off_loop()

        assert response.ok
        assert received == [strip_image_blocks(prepare_messages_for_llm(
            _build_messages_for_provider(view, formatter, provider), tmp_path,
        ))]
        assert off_loop


class TestNamedAttachments:
    @pytest.mark.parametrize("tool", ["reply", "finish"])
    def test_named_attachments_are_selected_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str,
    ) -> None:
        attachment = _note(tmp_path)
        selects = _Threads()
        monkeypatch.setattr(pseudo_handlers, "_select_attachments", selects.wrap(pseudo_handlers._select_attachments))
        use_hooks(monkeypatch)
        ls = make_loop_state(tmp_path, ScriptedRegistry())
        ls.collected_attachments.append(attachment)

        action = asyncio.run(pseudo_handlers._handle_pseudo_tool(
            ls, ToolCall(id="c1", name=tool, arguments={"text": "给你", "attachment_paths": [NOTE]}), 0,
        ))

        assert selects.off_loop()
        if tool == "reply":
            [payload] = ls.rctx.events.of("intermediate_reply")
            assert action is None and payload["attachments"] == [attachment]
        else:
            assert action.action == ACTION_FINISH and action.result["attachments"] == [attachment]


def test_an_error_snapshot_is_written_and_pruned_in_a_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "data" / "llm_error_snapshots"
    for index in range(error_snapshot._MAX_SNAPSHOTS):
        old = _file(tmp_path, f"data/llm_error_snapshots/old-{index:02d}.json", b"{}")
        os.utime(old, (1_000 + index, 1_000 + index))
    saves = _Threads()
    monkeypatch.setattr(error_snapshot, "_save_snapshot", saves.wrap(error_snapshot._save_snapshot))
    ctx = SimpleNamespace(
        response=ProviderResponse(ok=False, error="bad request", status_code=400),
        rctx=SimpleNamespace(workspace_root=tmp_path, session_id="session-1", task_id="task-1"),
        step=3, messages=[{"role": "user", "content": "hi"}], provider=SimpleNamespace(model="m"),
        node=SimpleNamespace(id="node-1"), extra={},
    )

    assert asyncio.run(error_snapshot.ErrorSnapshotHandler().handle(ctx)) is None

    snapshots = sorted(folder.glob("*.json"))
    [fresh] = [path for path in snapshots if not path.name.startswith("old-")]
    saved = json.loads(fresh.read_text(encoding="utf-8"))
    assert saved["status_code"] == 400 and saved["messages"] == [{"role": "user", "content": "hi"}]
    assert len(snapshots) == error_snapshot._MAX_SNAPSHOTS and not (folder / "old-00.json").exists()
    assert saves.off_loop()

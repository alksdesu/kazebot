"""按人记忆的接线：适配层登记与收集、save_memory 落到谁的档案、参数是否真的送到 engine。

链路跨三个进程（bot.py → supervisor → engine worker），任一段少传一个字段，
「@ 某人就加载他的记忆」就静默退化成原来的关键词召回，而外部看不出区别。
"""
from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from engine import memory_subjects
from engine.builtin.knowledge_inject import _conversation_memory_namespace, save_memory
from toolbox.context import ToolContext

from tests._onebot_harness import load_runtime

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"


def _ctx(tmp_path: Path, node_id: str = "system.memory_extractor") -> ToolContext:
    return ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id=node_id,
    )


def _save(tmp_path: Path, **args: Any) -> dict[str, Any]:
    payload = {"id": "m1", "book": "profile", "content": "内容", "keywords": ["k"], **args}
    result = asyncio.run(save_memory(payload, _ctx(tmp_path)))
    assert result["ok"], result
    return result


def _read(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["entries"]


class TestSaveMemoryRouting:
    def test_an_enrolled_subject_lands_in_their_own_archive(self, tmp_path: Path) -> None:
        memory_subjects.record_interaction(tmp_path, "UserA")

        _save(tmp_path, subject="UserA")

        archive = tmp_path / "data" / "memory" / "user_UserA" / "profile.yaml"
        assert archive.exists()
        assert _read(archive)[0]["subject"] == "UserA"

    def test_an_unenrolled_subject_falls_back_to_the_conversation(self, tmp_path: Path) -> None:
        # 被别人提到但从未主动找过 bot 的人不建档，那条信息归说话人所在的会话。
        _save(tmp_path, subject="UserZ")

        assert not (tmp_path / "data" / "memory" / "user_UserZ").exists()
        conv = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / "profile.yaml"
        assert conv.exists()
        assert "subject" not in _read(conv)[0]

    def test_no_subject_keeps_the_old_conversation_scoping(self, tmp_path: Path) -> None:
        _save(tmp_path)

        conv = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / "profile.yaml"
        assert conv.exists()

    def test_a_path_traversal_subject_cannot_escape_the_memory_dir(self, tmp_path: Path) -> None:
        _save(tmp_path, subject="../../etc")

        conv = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / "profile.yaml"
        assert conv.exists()
        assert not (tmp_path / "data" / "etc").exists()

    def test_node_ids_are_written_instead_of_silently_dropped(self, tmp_path: Path) -> None:
        _save(tmp_path, node_ids=["qq.orchestrator"])

        conv = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / "profile.yaml"
        assert _read(conv)[0]["node_ids"] == ["qq.orchestrator"]

    def test_an_update_does_not_wipe_fields_the_caller_omitted(self, tmp_path: Path) -> None:
        # 整条替换会抹掉手工加的字段；自动提取碰到同一个 id 就把它们清了。
        memory_subjects.record_interaction(tmp_path, "UserA")
        _save(tmp_path, subject="UserA", node_ids=["qq.orchestrator"])

        _save(tmp_path, subject="UserA", content="改过的")

        entry = _read(tmp_path / "data" / "memory" / "user_UserA" / "profile.yaml")[0]
        assert entry["node_ids"] == ["qq.orchestrator"]
        assert entry["content"] == "改过的"

    def test_the_default_scan_depth_is_written_to_disk(self, tmp_path: Path) -> None:
        _save(tmp_path)

        conv = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / "profile.yaml"
        assert _read(conv)[0]["scan_depth"] > 0


class TestAdapterCollection:
    @pytest.fixture()
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        return load_runtime(monkeypatch, tmp_path)

    def _event(self, user_id: int, message: list[dict[str, Any]] | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            user_id=user_id,
            message_id=1,
            get_message=lambda: message or [],
        )

    def _collect(self, runtime, event, text: str, key: str = "qq_group:t") -> list[str]:
        return runtime._collect_memory_subjects(event, text, key)

    def test_a_direct_interaction_enrolls_the_sender(self, runtime, tmp_path: Path) -> None:
        subjects = self._collect(runtime, self._event(10001), "你好")

        alias = runtime._anonymize_user_id(10001)
        assert subjects == [alias]
        assert memory_subjects.is_enrolled(tmp_path, alias)

    def test_at_segments_join_the_subject_list(self, runtime, tmp_path: Path) -> None:
        message = [{"type": "at", "data": {"qq": "10002"}}]

        subjects = self._collect(runtime, self._event(10001, message), "看看")

        assert subjects == [runtime._anonymize_user_id(10001), runtime._anonymize_user_id(10002)]

    def test_being_at_ed_does_not_enroll_the_target(self, runtime, tmp_path: Path) -> None:
        # 只有主动找 bot 才建档；被别人 @ 到不算。
        message = [{"type": "at", "data": {"qq": "10002"}}]
        self._collect(runtime, self._event(10001, message), "看看")

        assert not memory_subjects.is_enrolled(tmp_path, runtime._anonymize_user_id(10002))

    def test_aliases_written_in_the_text_are_picked_up(self, runtime, tmp_path: Path) -> None:
        other = runtime._anonymize_user_id(10005)

        subjects = self._collect(runtime, self._event(10001), f"他说{other}很靠谱")

        assert other in subjects

    def test_at_all_does_not_become_a_subject(self, runtime, tmp_path: Path) -> None:
        message = [{"type": "at", "data": {"qq": "all"}}]

        subjects = self._collect(runtime, self._event(10001, message), "都来看")

        assert subjects == [runtime._anonymize_user_id(10001)]

    def test_display_names_only_resolve_for_enrolled_people(self, runtime, tmp_path: Path, monkeypatch) -> None:
        alias = runtime._anonymize_user_id(10007)
        monkeypatch.setitem(runtime._QQ_USER_PROFILES, "10007", {"display_name": "张三"})

        # 未建档：名字命中也不算涉及
        assert alias not in self._collect(runtime, self._event(10001), "张三昨天说要请客")

        memory_subjects.record_interaction(tmp_path, alias)
        assert alias in self._collect(runtime, self._event(10001), "张三昨天说要请客", key="qq_group:t2")

    def test_group_history_speakers_do_not_become_subjects(self, runtime, tmp_path: Path) -> None:
        """收集只看发言人这一句，不看拼进 instruction 的群历史。

        群历史每行形如 `名字(UserX): ...`，别名就在行首括号里 —— 拿整块历史去扫，
        近 20 行里说过话的人全部会被当成「本轮涉及」，每人一整份档案挤满预算。
        """
        speaker = runtime._anonymize_user_id(10009)
        memory_subjects.record_interaction(tmp_path, speaker)
        history_block = f"[12:00] 某人({speaker}): 昨天那事怎么样了\n\n---\n\n在吗"

        assert speaker in self._collect(runtime, self._event(10001), history_block)
        assert speaker not in self._collect(runtime, self._event(10001), "在吗", key="qq_group:t2")

    def test_last_round_subjects_stay_loaded_this_round(self, runtime, tmp_path: Path) -> None:
        # 「@张三」的下一句通常是「他电话多少」，那句里没有任何别名。
        other = runtime._anonymize_user_id(10011)
        memory_subjects.record_interaction(tmp_path, other)
        message = [{"type": "at", "data": {"qq": "10011"}}]

        self._collect(runtime, self._event(10001, message), "你认识他吗")
        assert other in self._collect(runtime, self._event(10001), "他电话多少")

    def test_stickiness_does_not_accumulate_forever(self, runtime, tmp_path: Path) -> None:
        # 存回去的是本轮实际出现的人，不含继承来的：否则谁进来一次就永久驻留。
        other = runtime._anonymize_user_id(10011)
        memory_subjects.record_interaction(tmp_path, other)
        message = [{"type": "at", "data": {"qq": "10011"}}]

        self._collect(runtime, self._event(10001, message), "你认识他吗")
        self._collect(runtime, self._event(10001), "他电话多少")

        assert other not in self._collect(runtime, self._event(10001), "算了不问了")

    def test_stickiness_expires(self, runtime, tmp_path: Path, monkeypatch) -> None:
        other = runtime._anonymize_user_id(10011)
        memory_subjects.record_interaction(tmp_path, other)
        message = [{"type": "at", "data": {"qq": "10011"}}]

        self._collect(runtime, self._event(10001, message), "你认识他吗")
        clock = runtime.time.monotonic() + runtime._SUBJECT_STICKY_TTL_SEC + 1
        monkeypatch.setattr(runtime.time, "monotonic", lambda: clock)

        assert other not in self._collect(runtime, self._event(10001), "他电话多少")

    def test_sticky_window_is_per_conversation(self, runtime, tmp_path: Path) -> None:
        other = runtime._anonymize_user_id(10011)
        memory_subjects.record_interaction(tmp_path, other)
        message = [{"type": "at", "data": {"qq": "10011"}}]

        self._collect(runtime, self._event(10001, message), "你认识他吗", key="qq_group:a")

        assert other not in self._collect(runtime, self._event(10001), "他电话多少", key="qq_group:b")

    def test_the_sender_stays_first_after_inheriting(self, runtime, tmp_path: Path) -> None:
        # 预算裁剪时发起人的档案最该留下，继承来的人必须排在他后面。
        other = runtime._anonymize_user_id(10011)
        memory_subjects.record_interaction(tmp_path, other)
        message = [{"type": "at", "data": {"qq": "10011"}}]

        self._collect(runtime, self._event(10001, message), "你认识他吗")
        merged = self._collect(runtime, self._event(10001), "他电话多少")

        assert merged[0] == runtime._anonymize_user_id(10001)


class TestEndToEndInjection:
    """从 task_context 到 [MEMORY:ACTIVE] 的完整一跳，缺任何一环都会静默退化。"""

    def _node(self) -> Any:
        from engine.node import Node

        return Node(id="qq.orchestrator", type="ai")

    def _archive(self, tmp_path: Path, alias: str, entries: list[dict[str, Any]]) -> None:
        memory_subjects.record_interaction(tmp_path, alias)
        book_dir = tmp_path / "data" / "memory" / f"user_{alias}"
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "profile.yaml").write_text(
            yaml.safe_dump({"book": "profile", "entries": entries}, allow_unicode=True), encoding="utf-8",
        )

    def _build(self, tmp_path: Path, instruction: str, subjects: list[str]) -> tuple[list, list]:
        from engine.builtin.knowledge_inject import build_knowledge_context

        _skill_static, _skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
            tmp_path, self._node(), instruction, [], {},
            task_context={"conversation_key": _CONV_KEY, "memory_hints": {"subjects": subjects}},
        )
        return memory_static, memory_dynamic

    def test_a_mentioned_persons_archive_reaches_the_prompt(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", [{"id": "m1", "content": "张三是后端负责人"}])

        _static, dynamic = self._build(tmp_path, "问一下这事", ["UserA"])

        assert any("张三是后端负责人" in str(m.get("content") or "") for m in dynamic)

    def test_it_arrives_without_the_sentence_containing_any_keyword(self, tmp_path: Path) -> None:
        # 这就是「@张三后 bot 对张三一无所知」要解决的点：句子里没有任何匹配词。
        self._archive(tmp_path, "UserA", [{"id": "m1", "content": "他偏好简洁回复", "keywords": ["张三"]}])

        _static, dynamic = self._build(tmp_path, "他电话多少", ["UserA"])

        assert any("他偏好简洁回复" in str(m.get("content") or "") for m in dynamic)

    def test_an_unenrolled_person_contributes_nothing(self, tmp_path: Path) -> None:
        book_dir = tmp_path / "data" / "memory" / "user_UserZ"
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "profile.yaml").write_text(
            yaml.safe_dump({"book": "profile", "entries": [{"id": "m1", "content": "不该出现"}]}),
            encoding="utf-8",
        )

        _static, dynamic = self._build(tmp_path, "随便说说", ["UserZ"])

        assert not any("不该出现" in str(m.get("content") or "") for m in dynamic)

    def test_without_hints_nothing_extra_is_injected(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", [{"id": "m1", "content": "张三是后端负责人"}])

        _static, dynamic = self._build(tmp_path, "问一下这事", [])

        assert not any("张三是后端负责人" in str(m.get("content") or "") for m in dynamic)

    def test_a_constant_archive_entry_lands_in_the_static_block(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", [{"id": "m1", "content": "永远用中文", "constant": True}])

        static, _dynamic = self._build(tmp_path, "hello", ["UserA"])

        assert any("永远用中文" in str(m.get("content") or "") for m in static)

    def test_two_peoples_archives_both_arrive(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", [{"id": "a1", "content": "甲的偏好"}])
        self._archive(tmp_path, "UserB", [{"id": "b1", "content": "乙的偏好"}])

        _static, dynamic = self._build(tmp_path, "你们俩", ["UserA", "UserB"])

        body = " ".join(str(m.get("content") or "") for m in dynamic)
        assert "甲的偏好" in body and "乙的偏好" in body


class TestHintPlumbing:
    """三个进程之间少传一个字段，按人加载就静默退回关键词召回。"""

    @staticmethod
    def _keys_of_dicts_containing(path: str, marker: str) -> list[set[str]]:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if marker in keys:
                found.append(keys)
        return found

    def test_the_adapter_sends_memory_hints_with_the_inbound(self) -> None:
        source = Path("adapters/onebot/__init__.py").read_text(encoding="utf-8")

        assert "memory_hints={" in source

    def test_the_sdk_forwards_memory_hints(self) -> None:
        source = Path("clonoth_sdk/client.py").read_text(encoding="utf-8")

        assert "memory_hints" in source
        assert 'payload["memory_hints"]' in source

    def test_the_inbound_schema_accepts_memory_hints(self) -> None:
        source = Path("supervisor/types.py").read_text(encoding="utf-8")

        assert source.count("memory_hints: dict[str, Any]") >= 2

    def test_the_task_context_carries_memory_hints_to_the_engine(self) -> None:
        # knowledge_inject 从 rctx.task_context 读它，少了这一跳就永远收不到。
        contexts = self._keys_of_dicts_containing("supervisor/task_store.py", "platform_auth")

        assert contexts, "找不到 task_context 字面量"
        assert any("memory_hints" in keys for keys in contexts)

    def test_memory_hints_stay_out_of_platform_auth(self) -> None:
        # platform_auth 参与鉴权判定，混入非鉴权字段会让 is_admin 的判断面变大。
        source = Path("adapters/onebot/__init__.py").read_text(encoding="utf-8")
        auth_block = source.split("platform_auth={", 1)[1].split("}", 1)[0]

        assert "subjects" not in auth_block

    def test_the_extractor_prompt_asks_for_subject_and_scan_depth(self) -> None:
        # 靠提示词让模型填参数不可靠，但不写进提示词它一定不填。
        prompt = Path("engine/system_nodes/system.memory_extractor.yaml").read_text(encoding="utf-8")

        assert '"subject"' in prompt
        assert '"scan_depth"' in prompt

    def test_the_orchestrator_is_told_not_to_reveal_where_a_memory_came_from(self) -> None:
        # 跨会话共享意味着 bot 可能说漏「你在别的群说过」，代码层拦不住。
        prompt = Path("config/nodes/qq.orchestrator.yaml").read_text(encoding="utf-8")

        assert "不得转述来源" in prompt

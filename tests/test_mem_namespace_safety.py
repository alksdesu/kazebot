"""记忆 namespace 的统一校验：谁都不能拿它走出 data/memory。

create_agent 的 memory_book 曾只做 strip() 就拼进目录，`../../config` 能让 save_memory /
delete_memory 改写或删除 config/*.yaml；控制台只认 conv_ / user_ 两种目录名，
create_agent 合法建出来的记忆库在页面上既看不见也清不掉。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import memory_admin  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    build_knowledge_context,
    delete_memory,
    list_memories,
    save_memory,
)
from engine.memory_namespace import (  # noqa: E402
    CONVERSATION_PREFIX,
    NAMESPACE_KEY_PREFIX,
    SUBJECT_PREFIX,
    InvalidMemoryNamespace,
    conversation_namespace,
    is_namespace_conversation_key,
    is_valid_namespace,
    memory_book_error,
    memory_dir,
    namespace_conversation_key,
    namespace_dirs,
    namespace_error,
    node_memory_namespace,
)
from engine.node import Node  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_HEX = "0123456789abcdef01234567"

_ESCAPES = [
    pytest.param("..", id="上一级"),
    pytest.param(".", id="当前目录"),
    pytest.param("../../config", id="向上穿越"),
    pytest.param("a/b", id="斜杠"),
    pytest.param("a\\b", id="反斜杠"),
    pytest.param("/etc", id="POSIX 绝对路径"),
    pytest.param("C:\\Windows", id="Windows 盘符"),
    pytest.param("C:", id="只有盘符"),
    pytest.param("", id="空串"),
    pytest.param("   ", id="全是空白"),
    pytest.param(" smith", id="首部空白"),
    pytest.param("smith ", id="尾部空白"),
    pytest.param("a\x00b", id="NUL"),
    pytest.param("a\nb", id="换行"),
    pytest.param(".write", id="记忆库的锁"),
    pytest.param(".hit_cache.json", id="命中侧车文件"),
    pytest.param("notes.", id="结尾的点"),
    pytest.param("default.yaml", id="记忆本后缀"),
    pytest.param("CON", id="Windows 设备名"),
    pytest.param("nul.txt", id="带扩展名的设备名"),
    pytest.param("x" * 65, id="65 个字符"),
    pytest.param(CONVERSATION_PREFIX + "zzz", id="冒充会话目录"),
    pytest.param(SUBJECT_PREFIX + "1bad", id="冒充档案目录"),
    pytest.param("__root__", id="控制台的根目录占位名"),
]


def _ctx(tmp_path: Path, *, conversation_key: str = _CONV_KEY, node_extra: dict[str, Any] | None = None) -> ToolContext:
    ctx = ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=conversation_key,
        node_id="qq.orchestrator",
    )
    if node_extra is not None:
        object.__setattr__(ctx, "_node_extra", node_extra)
    return ctx


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a directory symlink here: {exc}")


class TestNamespaceRules:
    @pytest.mark.parametrize("bad", _ESCAPES)
    def test_anything_that_is_not_one_plain_directory_name_is_refused(self, bad: str) -> None:
        assert namespace_error(bad)
        assert not is_valid_namespace(bad)

    @pytest.mark.parametrize("good", [
        pytest.param(CONVERSATION_PREFIX + _HEX, id="会话目录"),
        pytest.param(SUBJECT_PREFIX + "UserA", id="人物档案"),
        pytest.param("persona_bob", id="节点记忆库"),
        pytest.param("bootstrap.coder", id="带点的节点 id"),
        pytest.param("research-2", id="连字符"),
        pytest.param("研究助手", id="中文名"),
        pytest.param("x" * 64, id="64 个字符"),
    ])
    def test_both_automatic_shapes_and_plain_names_are_accepted(self, good: str) -> None:
        # 已有节点的 memory_book 可能是任何安全的名字，收紧到 ASCII 会让那些记忆读不回来。
        assert namespace_error(good) == ""

    @pytest.mark.parametrize("bad", _ESCAPES)
    def test_memory_dir_refuses_before_touching_the_disk(self, tmp_path: Path, bad: str) -> None:
        if not bad:
            pytest.skip("空串就是 data/memory 本身")
        with pytest.raises(InvalidMemoryNamespace):
            memory_dir(tmp_path, bad)

    def test_the_root_is_the_empty_namespace(self, tmp_path: Path) -> None:
        assert memory_dir(tmp_path, "") == tmp_path / "data" / "memory"

    def test_a_valid_name_lands_directly_under_the_memory_root(self, tmp_path: Path) -> None:
        assert memory_dir(tmp_path, "persona_bob") == tmp_path / "data" / "memory" / "persona_bob"

    def test_a_namespace_linked_outside_the_memory_root_is_refused(self, tmp_path: Path) -> None:
        # 名字本身合规，落点却在 data/memory 外面：照名字放行就等于把整个外部目录交给记忆工具。
        root = tmp_path / "data" / "memory"
        root.mkdir(parents=True)
        outside = tmp_path / "config"
        outside.mkdir()
        _symlink_or_skip(root / "escape", outside)

        with pytest.raises(InvalidMemoryNamespace):
            memory_dir(tmp_path, "escape")
        assert root / "escape" not in namespace_dirs(tmp_path)

    def test_a_new_memory_book_may_not_borrow_an_automatic_directory(self) -> None:
        # 子节点的 memory_book 填成 user_UserA，就是让它读写那个人的跨会话档案。
        assert memory_book_error(SUBJECT_PREFIX + "UserA")
        assert memory_book_error(CONVERSATION_PREFIX + _HEX)
        assert memory_book_error("persona_bob") == ""

    def test_an_invalid_memory_book_raises_instead_of_falling_back(self) -> None:
        # 退回会话 namespace 会把要隔离的记忆写进别人的目录。
        with pytest.raises(InvalidMemoryNamespace):
            node_memory_namespace("../../config", _CONV_KEY)


class TestNamespaceConversationKey:
    @pytest.mark.parametrize("namespace", [
        CONVERSATION_PREFIX + _HEX, SUBJECT_PREFIX + "UserA", "persona_bob", "研究助手", "",
    ])
    def test_the_key_maps_back_to_the_same_namespace(self, namespace: str) -> None:
        assert conversation_namespace(namespace_conversation_key(namespace)) == namespace

    def test_automatic_namespaces_stay_bare(self) -> None:
        # dream 的整理任务一直拿裸目录名当会话键，换个写法就会多出一批「活跃会话」。
        assert namespace_conversation_key(CONVERSATION_PREFIX + _HEX) == CONVERSATION_PREFIX + _HEX

    def test_a_custom_namespace_gets_the_prefix(self) -> None:
        assert namespace_conversation_key("persona_bob") == NAMESPACE_KEY_PREFIX + "persona_bob"

    def test_an_invalid_namespace_has_no_key(self) -> None:
        with pytest.raises(InvalidMemoryNamespace):
            namespace_conversation_key("../x")

    @pytest.mark.parametrize("key", [
        NAMESPACE_KEY_PREFIX + "../x", NAMESPACE_KEY_PREFIX, NAMESPACE_KEY_PREFIX + " persona",
    ])
    def test_a_prefixed_key_with_a_bad_name_is_just_hashed(self, key: str) -> None:
        assert conversation_namespace(key).startswith(CONVERSATION_PREFIX)
        assert not is_namespace_conversation_key(key)

    def test_internal_keys_are_recognised_and_real_ones_are_not(self) -> None:
        assert is_namespace_conversation_key(NAMESPACE_KEY_PREFIX + "persona_bob")
        assert is_namespace_conversation_key(CONVERSATION_PREFIX + _HEX)
        assert not is_namespace_conversation_key(_CONV_KEY)


class _AgentCtx(ToolContext):
    """离线 ToolContext：策略一律放行，只记录送审的 op。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            conversation_key="qq_group:abcdef",
        )
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _seed_template(root: Path) -> None:
    nodes = root / "config" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "tmpl.yaml").write_text(
        yaml.safe_dump({"id": "tmpl", "type": "ai", "tool_access": {"mode": "all"}}), encoding="utf-8",
    )


def _create(root: Path, **args: Any) -> tuple[dict[str, Any], _AgentCtx]:
    ctx = _AgentCtx(root)
    result = asyncio.run(create_agent({"name": "helper", "template": "tmpl", **args}, ctx))
    return result, ctx


class TestCreateAgent:
    @pytest.mark.parametrize("bad", [
        "../../config", "../conv_x", "/etc", "C:\\x", ".hidden", "a/b", "x" * 65,
        SUBJECT_PREFIX + "UserA", CONVERSATION_PREFIX + _HEX, CONVERSATION_PREFIX + "lookalike",
    ])
    def test_a_bad_memory_book_is_refused_before_anything_is_written(self, tmp_path: Path, bad: str) -> None:
        _seed_template(tmp_path)
        (tmp_path / "config" / "policy.yaml").write_text("rules: []\n", encoding="utf-8")

        result, ctx = _create(tmp_path, memory_book=bad)

        assert result["ok"] is False
        assert "memory_book" in result["error"]
        # 审批都不该发：节点文件里会写进这个坏值。
        assert ctx.requested == []
        assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()
        assert (tmp_path / "config" / "policy.yaml").read_text(encoding="utf-8") == "rules: []\n"

    def test_the_default_memory_book_is_the_node_id(self, tmp_path: Path) -> None:
        _seed_template(tmp_path)

        result, ctx = _create(tmp_path)

        assert result["ok"] is True
        assert result["data"]["memory_book"] == "helper"
        assert (tmp_path / "data" / "memory" / "helper").is_dir()
        # 审批预览只截 YAML 前 200 字，记忆库得单独给审批人看。
        assert ctx.requested[0][1]["memory_book"] == "helper"

    def test_a_memory_book_linked_outside_is_refused(self, tmp_path: Path) -> None:
        _seed_template(tmp_path)
        root = tmp_path / "data" / "memory"
        root.mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _symlink_or_skip(root / "linked", outside)

        result, _ctx_ = _create(tmp_path, memory_book="linked")

        assert result["ok"] is False
        assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()

    def test_what_create_agent_builds_shows_up_in_the_console_and_can_be_cleared(self, tmp_path: Path) -> None:
        _seed_template(tmp_path)
        result, _ctx_ = _create(tmp_path, memory_book="研究助手")
        assert result["ok"] is True

        memory_admin.upsert_entry(tmp_path, "研究助手", {"id": "a", "content": "x", "keywords": ["k"]})
        rows = {row["namespace"]: row for row in memory_admin.list_namespaces(tmp_path)}

        assert rows["研究助手"]["kind"] == "memory_book"
        assert rows["研究助手"]["entry_count"] == 1
        assert memory_admin.clear_namespace(tmp_path, "研究助手") == 1
        assert not (tmp_path / "data" / "memory" / "研究助手").exists()


class TestConsole:
    def test_an_empty_memory_book_directory_is_still_listed(self, tmp_path: Path) -> None:
        # create_agent 先建好空目录；看不见就清不掉。
        (tmp_path / "data" / "memory" / "persona_bob").mkdir(parents=True)

        rows = {row["namespace"]: row for row in memory_admin.list_namespaces(tmp_path)}

        assert rows["persona_bob"]["kind"] == "memory_book"
        assert rows["persona_bob"]["entry_count"] == 0

    def test_internal_directories_and_bad_names_are_not_listed(self, tmp_path: Path) -> None:
        root = tmp_path / "data" / "memory"
        for name in (".write.rotate.lock", "default.yaml", CONVERSATION_PREFIX + "lookalike"):
            (root / name).mkdir(parents=True, exist_ok=True)

        assert memory_admin.list_namespaces(tmp_path) == []

    def test_a_directory_linked_outside_is_not_listed(self, tmp_path: Path) -> None:
        root = tmp_path / "data" / "memory"
        root.mkdir(parents=True)
        outside = tmp_path / "config"
        outside.mkdir()
        _symlink_or_skip(root / "escape", outside)

        assert memory_admin.list_namespaces(tmp_path) == []
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.list_entries(tmp_path, "escape")

    def test_the_automatic_kinds_are_unchanged(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, SUBJECT_PREFIX + "UserA", {"id": "a", "content": "x", "keywords": ["k"]})
        memory_admin.upsert_entry(tmp_path, CONVERSATION_PREFIX + _HEX, {"id": "b", "content": "y", "keywords": ["k"]})

        rows = {row["namespace"]: row["kind"] for row in memory_admin.list_namespaces(tmp_path)}

        assert rows == {SUBJECT_PREFIX + "UserA": "subject", CONVERSATION_PREFIX + _HEX: "conversation"}


class TestToolsWithAnInvalidMemoryBook:
    """手写或控制台改坏的节点 memory_book：工具报错，绝不写到 data/memory 外面。"""

    def _seed_config(self, tmp_path: Path) -> Path:
        target = tmp_path / "config" / "default.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump({"book": "default", "entries": [{"id": "m1", "content": "配置", "source": "auto"}]}),
            encoding="utf-8",
        )
        return target

    def test_save_memory_reports_it_and_writes_nothing(self, tmp_path: Path) -> None:
        target = self._seed_config(tmp_path)
        before = target.read_bytes()

        result = asyncio.run(save_memory(
            {"id": "m1", "book": "default", "content": "覆盖", "keywords": ["k"]},
            _ctx(tmp_path, node_extra={"memory_book": "../../config"}),
        ))

        assert result["ok"] is False
        assert "memory_book" in result["error"]
        assert target.read_bytes() == before

    def test_delete_memory_reports_it_and_deletes_nothing(self, tmp_path: Path) -> None:
        target = self._seed_config(tmp_path)

        result = asyncio.run(delete_memory(
            {"id": "m1", "book": "default"}, _ctx(tmp_path, node_extra={"memory_book": "../../config"}),
        ))

        assert result["ok"] is False
        assert target.exists()

    def test_list_memories_reports_it(self, tmp_path: Path) -> None:
        result = asyncio.run(list_memories({}, _ctx(tmp_path, node_extra={"memory_book": "../../config"})))

        assert result["ok"] is False

    def test_injection_skips_the_namespace_without_breaking_the_prompt(self, tmp_path: Path, caplog) -> None:
        node = Node(id="persona", type="ai", extra={"memory_book": "../../config"})
        self._seed_config(tmp_path)

        with caplog.at_level(logging.WARNING, logger="engine.builtin.knowledge_inject"):
            _ss, _sd, memory_static, memory_dynamic = build_knowledge_context(
                tmp_path, node, "配置", [], {}, task_context={"conversation_key": _CONV_KEY},
            )

        assert memory_static == [] and memory_dynamic == []
        assert any("memory_book" in record.getMessage() for record in caplog.records)


class TestDeleteMemoryBookName:
    def test_a_book_name_cannot_walk_into_another_conversation(self, tmp_path: Path) -> None:
        """book 直接拼成文件名：不校验的话 ../ 能删到别的会话的记忆本里去。"""
        other = tmp_path / "data" / "memory" / conversation_namespace("qq_group:other") / "people.yaml"
        other.parent.mkdir(parents=True)
        other.write_text(
            yaml.safe_dump({"book": "people", "entries": [{"id": "m1", "content": "别的群", "source": "auto"}]}),
            encoding="utf-8",
        )
        before = other.read_bytes()

        result = asyncio.run(delete_memory(
            {"id": "m1", "book": f"../{other.parent.name}/people"}, _ctx(tmp_path),
        ))

        assert result["ok"] is False
        assert other.read_bytes() == before

    def test_a_valid_book_name_still_deletes(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert asyncio.run(save_memory({"id": "m1", "book": "people", "content": "x", "keywords": ["k"]}, ctx))["ok"]

        assert asyncio.run(delete_memory({"id": "m1", "book": "people"}, ctx))["ok"]


class TestDefaultBook:
    """省略 book 时 save 和 delete 落在同一本；memory_book 当不了书名时退回 default。"""

    def test_a_dotted_memory_book_still_saves_without_naming_a_book(self, tmp_path: Path) -> None:
        # create_agent 默认拿节点 id 当 memory_book，节点 id 常带点，而书名不许带点。
        ctx = _ctx(tmp_path, node_extra={"memory_book": "bootstrap.coder2"})

        result = asyncio.run(save_memory({"id": "m1", "content": "x", "keywords": ["k"]}, ctx))

        assert result["ok"] is True
        assert (tmp_path / "data" / "memory" / "bootstrap.coder2" / "default.yaml").exists()

    def test_what_is_saved_without_a_book_is_deleted_without_one(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, node_extra={"memory_book": "coder2"})
        assert asyncio.run(save_memory({"id": "m1", "content": "x", "keywords": ["k"]}, ctx))["ok"]
        book_path = tmp_path / "data" / "memory" / "coder2" / "coder2.yaml"
        assert book_path.exists()

        result = asyncio.run(delete_memory({"id": "m1"}, ctx))

        assert result["ok"] is True
        remaining = yaml.safe_load(book_path.read_text(encoding="utf-8")) if book_path.exists() else {}
        assert all(entry.get("id") != "m1" for entry in (remaining or {}).get("entries") or [])

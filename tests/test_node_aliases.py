"""节点改名后旧 id 照样能用：加载、派发、备选链、记忆筛选、读图判定、控制台都落到同一个节点。

节点的身份仍是文件名，旧 id 只认 yaml 里的 aliases：同名文件永远优先；几份都声明同一个旧 id 时，
系统目录优先，同一目录按文件名排序取第一份。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin import fallback_provider as fallback_module  # noqa: E402
from engine.builtin.finish_guard import _has_unresolved_vision_failure, vision_node_ids  # noqa: E402
from engine.builtin.knowledge_inject import build_knowledge_context  # noqa: E402
from engine.inference.resume_builder import _build_resume_messages  # noqa: E402
from engine.node import canonical_node_id, known_node_ids, load_node, node_file  # noqa: E402
from engine.runner import _collect_node_info, _discover_switchable_nodes  # noqa: E402
from shell.tui.services.node_scanner import find_root_nodes, scan_nodes  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    (tmp_path / "engine" / "system_nodes").mkdir(parents=True)
    return tmp_path


def _node(workspace: Path, filename: str, *, system: bool = False, **fields: Any) -> Path:
    folder = workspace / "engine" / "system_nodes" if system else workspace / "config" / "nodes"
    path = folder / f"{filename}.yaml"
    path.write_text(
        yaml.safe_dump({"type": "ai", **fields}, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    return path


def _renamed_vision(workspace: Path, **fields: Any) -> Path:
    return _node(workspace, "chat.vision", id="chat.vision", aliases=["qq.vision"], **fields)


class TestLookup:
    def test_an_old_id_loads_the_renamed_node(self, workspace: Path) -> None:
        _renamed_vision(workspace, name="视觉理解入口")

        node = load_node(workspace, "qq.vision")

        assert (node.id, node.name, node.aliases) == ("chat.vision", "视觉理解入口", ["qq.vision"])
        assert canonical_node_id(workspace, "qq.vision") == "chat.vision"

    def test_without_an_id_field_the_node_is_named_after_its_file_not_the_old_id(self, workspace: Path) -> None:
        _node(workspace, "chat.vision", aliases=["qq.vision"])

        node = load_node(workspace, "qq.vision")

        assert (node.id, node.name) == ("chat.vision", "chat.vision")

    def test_a_single_old_id_may_be_written_as_a_string(self, workspace: Path) -> None:
        _node(workspace, "chat.vision", id="chat.vision", aliases="qq.vision")

        assert load_node(workspace, "qq.vision").id == "chat.vision"

    def test_a_file_under_the_old_name_still_wins(self, workspace: Path) -> None:
        # 部署里留着自己改过的旧文件：按旧 id 找到的仍是它，改名后的节点也就不再冒领这个旧 id。
        _renamed_vision(workspace)
        _node(workspace, "qq.vision", id="qq.vision")

        assert load_node(workspace, "qq.vision").id == "qq.vision"
        assert load_node(workspace, "chat.vision").aliases == []
        assert known_node_ids(workspace, "chat.vision") == ("chat.vision",)

    def test_a_system_node_claim_beats_a_user_node_claim(self, workspace: Path) -> None:
        _node(workspace, "a.user", aliases=["legacy"])
        _node(workspace, "z.system", system=True, aliases=["legacy"])

        assert canonical_node_id(workspace, "legacy") == "z.system"
        assert load_node(workspace, "a.user").aliases == []

    def test_within_one_folder_the_first_file_by_name_wins_and_the_clash_is_logged_once(
        self, workspace: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _node(workspace, "b.node", aliases=["legacy"])
        _node(workspace, "a.node", aliases=["legacy"])

        with caplog.at_level(logging.WARNING, logger="engine.node"):
            first = canonical_node_id(workspace, "legacy")
            second = canonical_node_id(workspace, "legacy")

        assert first == second == "a.node"
        assert load_node(workspace, "b.node").aliases == []
        assert [record.getMessage() for record in caplog.records if "legacy" in record.getMessage()] == [
            "node alias legacy is declared by a.node.yaml, b.node.yaml; a.node.yaml wins",
        ]

    def test_templates_samples_and_shadowed_user_files_claim_nothing(self, workspace: Path) -> None:
        # 模板和示例不会被派发到；被同名系统节点顶掉的用户文件自己都加载不到。
        _node(workspace, "_agent.template", aliases=["from_template"])
        _node(workspace, "qq.orchestrator.example", aliases=["from_sample"])
        _node(workspace, "system.compactor", system=True)
        _node(workspace, "system.compactor", aliases=["from_shadowed"])

        for old in ("from_template", "from_sample", "from_shadowed"):
            assert node_file(workspace, old) is None
            assert load_node(workspace, old) is None

    def test_edits_take_effect_without_a_restart(self, workspace: Path) -> None:
        path = _node(workspace, "chat.vision", id="chat.vision")
        assert canonical_node_id(workspace, "qq.vision") == "qq.vision"

        _renamed_vision(workspace)
        stamp = os.stat(path).st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
        assert canonical_node_id(workspace, "qq.vision") == "chat.vision"

        path.unlink()
        assert canonical_node_id(workspace, "qq.vision") == "qq.vision"

    @pytest.mark.parametrize("node_id", ["../../data/evil", "..\\..\\data\\evil", "sub/evil", "C:evil", "..", "", "  "])
    def test_an_id_that_is_not_a_single_file_name_loads_nothing(self, workspace: Path, node_id: str) -> None:
        # 节点 id 会直接拼成文件名，switch_node 的目标又是模型写的：不能借它把目录外的 yaml 当节点加载。
        (workspace / "data").mkdir(exist_ok=True)
        (workspace / "data" / "evil.yaml").write_text("type: ai\ntool_access: all\n", encoding="utf-8")

        assert node_file(workspace, node_id) is None
        assert load_node(workspace, node_id) is None

    def test_an_old_id_that_is_not_a_single_file_name_is_ignored(self, workspace: Path) -> None:
        _node(workspace, "chat.vision", id="chat.vision", aliases=["../escape", "qq.vision"])

        assert load_node(workspace, "chat.vision").aliases == ["qq.vision"]

    def test_known_ids_put_the_current_one_first(self, workspace: Path) -> None:
        _node(workspace, "chat.vision", id="chat.vision", aliases=["qq.vision", "vision.v1"])

        assert known_node_ids(workspace, "vision.v1") == ("chat.vision", "qq.vision", "vision.v1")
        assert known_node_ids(workspace, "chat.vision") == ("chat.vision", "qq.vision", "vision.v1")
        assert known_node_ids(workspace, "missing.node") == ("missing.node",)
        assert known_node_ids(workspace, "") == ()


class TestDelegation:
    def test_delegate_targets_are_kept_under_current_ids(self, workspace: Path) -> None:
        # 派发工具名、白名单和子会话键都按委派清单算，同一个节点新旧 id 都写了也只算一个。
        _node(workspace, "chat.web_search", id="chat.web_search", aliases=["qq.web_search"])
        _node(
            workspace, "qq.orchestrator", id="qq.orchestrator",
            delegate_targets=["qq.web_search", "chat.web_search", "draw.not_installed"],
        )

        node = load_node(workspace, "qq.orchestrator")

        assert node.delegate_targets == ["chat.web_search", "draw.not_installed"]

    def test_delegate_descriptions_come_from_the_renamed_file(self, workspace: Path) -> None:
        _node(
            workspace, "chat.web_search", id="chat.web_search", aliases=["qq.web_search"],
            name="联网搜索入口", description="只开放联网工具",
        )

        info = _collect_node_info(workspace, ["qq.web_search"])

        assert info == [{"id": "chat.web_search", "name": "联网搜索入口", "description": "只开放联网工具"}]

    def test_an_old_id_is_neither_a_switch_target_nor_a_root(self, workspace: Path) -> None:
        _node(workspace, "chat.web_search", id="chat.web_search", aliases=["qq.web_search"])
        _renamed_vision(workspace)
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", delegate_targets=["qq.web_search"])

        listed = {row["id"] for row in _discover_switchable_nodes(workspace, "qq.orchestrator")}

        # 被委派的不是根节点；旧 id 只是别名，不单独列一项。
        assert listed == {"chat.vision"}

    def test_a_sample_file_does_not_stand_in_for_the_real_node(self, workspace: Path) -> None:
        # 示例文件和真节点写同一个 id，按文件名排在前面：以前列出来的是示例的名字，按示例的委派清单算根节点。
        _node(workspace, "qq.orchestrator.example", id="qq.orchestrator", name="示例", delegate_targets=[])
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", name="主入口", delegate_targets=["chat.web_search"])
        _node(workspace, "chat.web_search", id="chat.web_search")

        listed = _discover_switchable_nodes(workspace, "chat.vision")

        assert listed == [{"id": "qq.orchestrator", "name": "主入口", "description": "主入口"}]

    def test_the_terminal_ui_lists_the_same_entry_nodes(self, workspace: Path) -> None:
        _node(workspace, "qq.orchestrator.example", id="qq.orchestrator", name="示例", delegate_targets=[])
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", name="主入口", delegate_targets=["qq.web_search"])
        _node(workspace, "chat.web_search", id="chat.web_search", aliases=["qq.web_search"])
        _node(workspace, "chat.intent", id="chat.intent", internal=True)
        _node(workspace, "_agent.template", id="agent.template")

        roots = find_root_nodes(scan_nodes(workspace))

        assert [(node.id, node.name) for node in roots] == [("qq.orchestrator", "主入口")]


def _image_request(workspace: Path, node: Any) -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(ok=False, status_code=500, error="primary failed"),
        rctx=SimpleNamespace(workspace_root=workspace),
        node=node,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]}],
        tools=[],
        provider=SimpleNamespace(model="primary", timeout=10.0),
        extra={"loop_state": SimpleNamespace(formatter=None)},
    )


class TestSettingsWrittenUnderAnOldId:
    def test_a_fallback_chain_under_the_old_id_still_applies(self) -> None:
        cfg = {"fallbacks": [{"provider": "openai"}], "node_fallbacks": {"qq.vision": []}}

        assert fallback_module._select_fallbacks_for_node(cfg, "chat.vision", ["qq.vision"]) == []
        assert fallback_module._select_fallbacks_for_node(cfg, "chat.vision") == [{"provider": "openai"}]

    def test_the_current_id_wins_over_an_old_one(self) -> None:
        cfg = {"node_fallbacks": {"qq.vision": [], "chat.vision": [{"provider": "gemini"}]}}

        assert fallback_module._select_fallbacks_for_node(cfg, "chat.vision", ["qq.vision"]) == [{"provider": "gemini"}]

    def test_the_fallback_hook_honours_a_chain_disabled_under_the_old_id(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 视觉节点在旧 id 下写了空链：改名后要是落回全局那条只能读文字的链，图就被丢掉、模型开始瞎猜。
        _renamed_vision(workspace)
        (workspace / "data").mkdir()
        (workspace / "data" / "config.yaml").write_text(yaml.safe_dump({
            "fallbacks": [{"provider": "openai", "base_url": "https://example.test/v1", "api_key": "k", "model": "text"}],
            "node_fallbacks": {"qq.vision": []},
        }), encoding="utf-8")
        created: list[dict] = []
        monkeypatch.setattr(fallback_module, "_create_fallback_provider", lambda **kwargs: created.append(kwargs))
        ctx = _image_request(workspace, load_node(workspace, "qq.vision"))

        assert asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx)) is None

        assert created == []
        assert ctx.response.error == "primary failed"

    def test_memories_scoped_to_the_old_id_still_reach_the_node(self, workspace: Path) -> None:
        _renamed_vision(workspace, memory_book="vision_notes")
        book = workspace / "data" / "memory" / "vision_notes"
        book.mkdir(parents=True)
        (book / "notes.yaml").write_text(yaml.safe_dump({"book": "notes", "entries": [
            {"id": "m1", "content": "读图先看有没有文字", "constant": True, "node_ids": ["qq.vision"]},
            {"id": "m2", "content": "只给别的节点看", "constant": True, "node_ids": ["qq.orchestrator"]},
        ]}, allow_unicode=True), encoding="utf-8")

        skill_static, skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
            workspace, load_node(workspace, "chat.vision"), "看图", [], {},
        )
        rendered = "\n".join(str(m.get("content") or "") for m in [*memory_static, *memory_dynamic])

        assert "读图先看有没有文字" in rendered
        assert "只给别的节点看" not in rendered

    @pytest.mark.parametrize(("configured", "reported"), [("qq.vision", "chat.vision"), ("chat.vision", "qq.vision")])
    def test_a_vision_failure_counts_under_either_id(self, workspace: Path, configured: str, reported: str) -> None:
        _renamed_vision(workspace)
        runtime = {"routing": {"vision": {"entry_node_id": configured}}}
        messages = [{"role": "user", "content": "看看这张图"}]
        messages.extend(_build_resume_messages({"type": "child_failed", "from_node": reported, "error": "timeout"}))

        vision_nodes = vision_node_ids(runtime, workspace)

        assert vision_nodes == frozenset({"chat.vision", "qq.vision"})
        assert _has_unresolved_vision_failure(messages, vision_nodes=vision_nodes) is True


_CONFIG = """version: 1
provider: openai
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-main
  model: gpt-4o-mini
node_fallbacks:
  qq.vision:
    - provider: openai
      api_key: sk-vision-secret
      model: vision-backup
      supports_vision: true
  system.compactor: []
"""


@pytest.fixture()
def store(workspace: Path) -> ConfigStore:
    _renamed_vision(workspace)
    path = workspace / "data" / "config.yaml"
    path.parent.mkdir()
    path.write_text(_CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


def _saved_chains(store: ConfigStore) -> dict:
    return yaml.safe_load(store.path.read_text(encoding="utf-8")).get("node_fallbacks", {})


class TestConsoleFallbackChains:
    def test_the_console_sees_the_chain_under_the_current_id(self, store: ConfigStore) -> None:
        chains = store.get_providers_public()["node_fallbacks"]

        assert set(chains) == {"chat.vision", "system.compactor"}
        assert chains["chat.vision"][0]["model"] == "vision-backup"
        assert "sk-vision-secret" not in str(chains)

    def test_a_chain_of_the_node_itself_hides_the_stale_one(self, store: ConfigStore) -> None:
        store.update_node_fallbacks("chat.vision", [])
        raw = yaml.safe_load(store.path.read_text(encoding="utf-8"))
        raw["node_fallbacks"]["qq.vision"] = [{"provider": "openai", "model": "stale"}]
        store.path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        assert store.get_providers_public()["node_fallbacks"]["chat.vision"] == []

    def test_saving_moves_the_chain_to_the_current_id_and_keeps_its_secrets(self, store: ConfigStore) -> None:
        public = store.get_providers_public()["node_fallbacks"]["chat.vision"]

        store.update_node_fallbacks("chat.vision", [{**public[0], "_origin": 0, "model": "vision-cheaper"}])

        chains = _saved_chains(store)
        assert "qq.vision" not in chains
        assert chains["chat.vision"][0]["model"] == "vision-cheaper"
        assert chains["chat.vision"][0]["api_key"] == "sk-vision-secret"

    def test_clearing_the_chain_clears_the_old_entry_too(self, store: ConfigStore) -> None:
        # 只删新键的话，旧 id 下那条会接着生效，界面上却显示已经跟随全局。
        store.update_node_fallbacks("chat.vision", None)

        chains = _saved_chains(store)
        assert "qq.vision" not in chains and "chat.vision" not in chains
        assert chains == {"system.compactor": []}


class TestShippedRenames:
    @pytest.mark.parametrize(("old", "new"), [("qq.vision", "chat.vision"), ("qq.web_search", "chat.web_search")])
    def test_the_old_id_reaches_the_renamed_node(self, old: str, new: str) -> None:
        node = load_node(_ROOT, old)

        assert node is not None
        assert (node.id, node.aliases) == (new, [old])
        assert "QQ" not in f"{node.name}{node.description}{node.prompt}"

    def test_vision_routing_points_at_the_new_id(self) -> None:
        runtime = yaml.safe_load((_ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))

        assert runtime["routing"]["vision"]["entry_node_id"] == "chat.vision"

    def test_the_vision_node_reads_the_new_env_names_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QQ_VISION_MODEL", "legacy-model")
        monkeypatch.delenv("CLONOTH_VISION_MODEL", raising=False)
        assert load_node(_ROOT, "chat.vision").model == "legacy-model"

        monkeypatch.setenv("CLONOTH_VISION_MODEL", "current-model")
        assert load_node(_ROOT, "chat.vision").model == "current-model"

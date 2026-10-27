"""编程节点和代码审查节点：工具清单、模式和委派按约定装好；审查节点走 review 系统槽位，没配就跟随主渠道。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, turn  # noqa: E402
from _node_prompts import assembled_prompt  # noqa: E402
from clonoth_runtime import SYSTEM_MODEL_SLOTS, parse_bool, resolve_system_model  # noqa: E402
from engine.inference.pseudo_tools import _dispatch_tool_name, _filter_tool_specs  # noqa: E402
from engine.node import load_node  # noqa: E402
from engine.runner import _resolve_system_model_slot  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402

CODER = "bootstrap.coder"
REVIEWER = "bootstrap.code_reviewer"
CODER_TOOLS = {
    "read_file", "list_dir", "search_in_files", "glob", "write_file", "apply_diff", "undo_edits",
    "execute_command", "start_process", "read_process_output", "stop_process", "list_processes",
    "search_context_archive", "read_context_archive", "get_context_window",
    "web_search", "read_web", "read_image",
}
REVIEWER_TOOLS = {
    "read_file", "list_dir", "search_in_files", "glob", "read_context_archive", "search_context_archive",
    "execute_command",
}
WRITE_TOOLS = {"write_file", "apply_diff", "edit_lines", "undo_edits"}


def _shipped(tmp_path: Path, node_id: str) -> Any:
    # 空工作区里加载：拿到的是仓库默认那份，不会被本机 config/nodes 里的同名节点顶掉
    node = load_node(tmp_path, node_id)
    assert node is not None, node_id
    return node


class _NullHooks:
    def register(self, *args: Any, **kwargs: Any) -> None: ...
    def register_plugin_meta(self, *args: Any, **kwargs: Any) -> None: ...


@pytest.fixture(scope="module")
def registry_specs() -> list[dict[str, Any]]:
    from engine.builtin.loader import auto_discover_and_register
    from toolbox.registry import ToolRegistry

    registry = ToolRegistry(workspace_root=_ROOT, tools_dir=_ROOT / "tools")
    auto_discover_and_register(_NullHooks(), tool_registry=registry)
    return registry.list_specs()


class TestTheCoder:
    def test_it_runs_in_native_mode_with_a_todo_list_and_a_reviewer(self, tmp_path: Path) -> None:
        node = _shipped(tmp_path, CODER)

        assert node.tool_mode == "native"
        assert parse_bool(node.extra.get("todo"), False) is True
        assert node.delegate_targets == [REVIEWER]
        assert "edit_mode" not in node.extra

    def test_it_is_granted_exactly_the_programming_tools(self, tmp_path: Path) -> None:
        node = _shipped(tmp_path, CODER)

        assert node.tool_access.mode == "allowlist"
        assert set(node.tool_access.allow) == CODER_TOOLS
        assert len(node.tool_access.allow) == len(CODER_TOOLS)
        # read_tool_output 由引擎自动加；edit_lines 只给 hashline 模式的节点
        assert "read_tool_output" not in node.tool_access.allow
        assert "edit_lines" not in node.tool_access.allow

    def test_every_granted_name_is_a_real_tool(self, tmp_path: Path, registry_specs: list[dict[str, Any]]) -> None:
        node = _shipped(tmp_path, CODER)

        visible = {spec["name"] for spec in _filter_tool_specs(node, registry_specs)}

        assert visible == CODER_TOOLS
        assert "edit_lines" not in visible

    def test_the_model_gets_todos_the_review_dispatch_and_the_output_reader(self, tmp_path: Path, monkeypatch: Any) -> None:
        node = _shipped(tmp_path, CODER)
        names = sorted(CODER_TOOLS | {"edit_lines", "read_tool_output", "save_memory", "request_restart"})
        registry = ScriptedRegistry(specs={name: {} for name in names} | {"edit_lines": {"requires_edit_mode": "hashline"}})
        run = NodeRun(monkeypatch, tmp_path, registry=registry, node=node)

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        offered = set(run.tool_names(0))
        assert {"update_todos", "dispatch_to_bootstrap_code_reviewer", "read_tool_output", "finish"} <= offered
        assert CODER_TOOLS <= offered
        assert {"edit_lines", "save_memory", "request_restart"}.isdisjoint(offered)
        dispatch = next(tool for tool in run.requests[0]["tools"] if tool["function"]["name"] == "dispatch_to_bootstrap_code_reviewer")
        assert "wait" in dispatch["function"]["parameters"]["properties"]

    def test_the_prompt_teaches_the_current_tools(self, tmp_path: Path) -> None:
        prompt = assembled_prompt(CODER, tmp_path)

        for phrase in (
            "update_todos", "dispatch_to_bootstrap_code_reviewer", "wait: true", "undo_edits",
            "read_tool_output", "glob", "search_in_files", "apply_diff is atomic", "AGENTS.md",
        ):
            assert phrase in prompt, phrase
        assert _dispatch_tool_name(REVIEWER) == "dispatch_to_bootstrap_code_reviewer"

    def test_the_prompt_no_longer_forbids_repeating_a_call_outright(self, tmp_path: Path) -> None:
        prompt = assembled_prompt(CODER, tmp_path)

        assert "Each tool should only be called once" not in prompt
        assert "Never repeat the same tool call" not in prompt
        assert "just respond directly without calling any tools" not in prompt


class TestTheReviewer:
    def test_it_is_read_only(self, tmp_path: Path) -> None:
        node = _shipped(tmp_path, REVIEWER)

        assert node.tool_access.mode == "allowlist"
        assert set(node.tool_access.allow) == REVIEWER_TOOLS
        assert WRITE_TOOLS.isdisjoint(node.tool_access.allow)
        assert node.delegate_targets == []

    def test_it_has_a_bounded_loop_and_the_review_slot(self, tmp_path: Path) -> None:
        node = _shipped(tmp_path, REVIEWER)

        assert node.tool_mode == "native"
        assert isinstance(node.extra.get("max_steps"), int) and 10 <= node.extra["max_steps"] <= 64
        assert node.extra.get("system_model_slot") == "review"
        assert _resolve_system_model_slot(node) == "review"

    def test_every_granted_name_is_a_real_tool(self, tmp_path: Path, registry_specs: list[dict[str, Any]]) -> None:
        node = _shipped(tmp_path, REVIEWER)

        visible = {spec["name"] for spec in _filter_tool_specs(node, registry_specs)}

        assert visible == REVIEWER_TOOLS

    def test_the_prompt_asks_for_a_structured_report(self, tmp_path: Path) -> None:
        prompt = assembled_prompt(REVIEWER, tmp_path)

        for phrase in ("文件:行号", "触发场景", "建议的修法", "待确认", "严重程度", "finish", "不要自己改代码", "replace"):
            assert phrase in prompt, phrase
        for topic in ("正确性", "回归", "边界条件", "并发", "安全", "旧数据兼容"):
            assert topic in prompt, topic

    def test_the_yaml_carries_the_standard_keys(self) -> None:
        data = yaml.safe_load((_ROOT / "defaults" / "nodes" / f"{REVIEWER}.yaml").read_text(encoding="utf-8"))

        assert data["id"] == REVIEWER
        assert data["name"] and data["description"] and data["prompt"]


class TestTheReviewSlot:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for field in ("MODEL", "BASE_URL", "API_KEY", "PROVIDER"):
            monkeypatch.delenv(f"CLONOTH_REVIEW_{field}", raising=False)

    @staticmethod
    def _models(tmp_path: Path, body: str) -> None:
        path = tmp_path / "config" / "models.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_an_unconfigured_slot_follows_the_main_channel(self, tmp_path: Path) -> None:
        slot = resolve_system_model(tmp_path, "review", default_model="main-model")

        assert (slot.model, slot.base_url, slot.api_key, slot.provider) == ("main-model", "", "", "")

    def test_the_slot_reads_models_yaml(self, tmp_path: Path) -> None:
        self._models(tmp_path, "system_models:\n  review:\n    model: careful-model\n    provider: anthropic\n")

        slot = resolve_system_model(tmp_path, "review", default_model="main-model")

        assert (slot.model, slot.provider) == ("careful-model", "anthropic")

    def test_the_slot_reads_its_own_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_REVIEW_MODEL", "env-model")

        assert resolve_system_model(tmp_path, "review", default_model="main-model").model == "env-model"

    def test_models_yaml_beats_the_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_REVIEW_MODEL", "env-model")
        self._models(tmp_path, "system_models:\n  review:\n    model: file-model\n")

        assert resolve_system_model(tmp_path, "review", default_model="main-model").model == "file-model"

    def test_a_slot_name_nobody_registered_still_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_AUDIT_X_MODEL", "ignored")

        assert resolve_system_model(tmp_path, "audit_x", default_model="main-model").model == "main-model"
        self._models(tmp_path, "system_models:\n  audit_x:\n    model: custom\n")
        assert resolve_system_model(tmp_path, "audit_x", default_model="main-model").model == "custom"

    def test_the_console_lists_the_review_slot(self, tmp_path: Path) -> None:
        assert "review" in [spec.key for spec in SYSTEM_MODEL_SLOTS]
        store = ConfigStore(path=tmp_path / "config" / "models.yaml")

        [slot] = [item for item in store.get_system_models_public()["slots"] if item["key"] == "review"]

        assert slot["supports_provider"] is True
        assert slot["env_prefix"] == "CLONOTH_REVIEW"
        assert slot["model"] == ""

    def test_the_console_can_set_it(self, tmp_path: Path) -> None:
        store = ConfigStore(path=tmp_path / "config" / "models.yaml")

        store.update_system_model("review", model="careful-model", provider="deepseek")

        [slot] = [item for item in store.get_system_models_public()["slots"] if item["key"] == "review"]
        assert slot["model"] == "careful-model" and slot["provider"] == "deepseek"
        raw = yaml.safe_load((tmp_path / "config" / "models.yaml").read_text(encoding="utf-8"))
        assert raw["system_models"]["review"] == {"model": "careful-model", "provider": "deepseek"}

"""手写或存下来的开关按 parse_bool 读：带引号的 "false"、no、off 算关，认不出的照默认值，引擎各处和控制台读出同一个结论。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from clonoth_runtime import main_session_enabled  # noqa: E402
from engine import memory_admin  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    delete_memory,
    list_skills,
    load_memory_catalog,
    load_skill_catalog,
)
from engine.data_cleanup import _memory_entry_expired  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from engine.node import Node, load_node  # noqa: E402
from engine.prompt import assemble_prompt  # noqa: E402
from engine.runner import _discover_switchable_nodes  # noqa: E402
from shell.tui.services.node_scanner import scan_nodes  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from toolbox import mcp_runtime  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"

# (yaml 里写的值, 默认关的开关读成什么, 默认开的开关读成什么)
_CASES = [
    pytest.param('"false"', False, False, id="quoted-false"),
    pytest.param('"no"', False, False, id="quoted-no"),
    pytest.param('"off"', False, False, id="quoted-off"),
    pytest.param('"0"', False, False, id="quoted-zero"),
    pytest.param("false", False, False, id="false"),
    pytest.param("0", False, False, id="zero"),
    pytest.param('"true"', True, True, id="quoted-true"),
    pytest.param('"yes"', True, True, id="quoted-yes"),
    pytest.param('"on"', True, True, id="quoted-on"),
    pytest.param("true", True, True, id="true"),
    pytest.param("1", True, True, id="one"),
    pytest.param('"maybe"', False, True, id="unrecognised"),
    pytest.param("null", False, True, id="null"),
]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=root,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id="system.memory_extractor",
    )


class TestNodeFile:
    @pytest.mark.parametrize("raw,default_off,_default_on", _CASES)
    def test_persistent(self, tmp_path: Path, raw: str, default_off: bool, _default_on: bool) -> None:
        _write(config_layout.nodes_dir(tmp_path) / "worker.yaml", f"id: worker\ntype: ai\nprompt: hi\npersistent: {raw}\n")

        node = load_node(tmp_path, "worker")

        assert node is not None and node.persistent is default_off

    @pytest.mark.parametrize("raw,default_off,_default_on", _CASES)
    def test_internal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, default_off: bool, _default_on: bool,
    ) -> None:
        # 挡住仓库自带的 defaults/nodes，节点表里只剩这一个
        monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)
        _write(config_layout.nodes_dir(tmp_path) / "judge.yaml", f"id: judge\ntype: ai\nprompt: hi\ninternal: {raw}\n")

        switchable = {row["id"] for row in _discover_switchable_nodes(tmp_path, "someone.else")}
        entries = {node.id for node in scan_nodes(tmp_path)}

        assert switchable == entries == (set() if default_off else {"judge"})

    @pytest.mark.parametrize("raw,_default_off,default_on", _CASES)
    def test_prompt_block_enabled(self, tmp_path: Path, raw: str, _default_off: bool, default_on: bool) -> None:
        blocks = [
            {"role": "system", "content": "BLOCK-UNDER-TEST", "enabled": yaml.safe_load(raw)},
            {"role": "system", "content": "ALWAYS-HERE"},
        ]

        messages = assemble_prompt(tmp_path, Node(id="worker", type="ai", prompt=blocks))

        text = json.dumps(messages, ensure_ascii=False)
        assert "ALWAYS-HERE" in text
        assert ("BLOCK-UNDER-TEST" in text) is default_on


class TestSkillEnabled:
    @pytest.mark.parametrize("raw,_default_off,default_on", _CASES)
    def test_loading_and_both_listings_agree(self, tmp_path: Path, raw: str, _default_off: bool, default_on: bool) -> None:
        _write(config_layout.skills_dir(tmp_path) / "greet" / "SKILL.md", f"---\nname: greet\nenabled: {raw}\n---\n见面先问好。\n")

        [loaded] = load_skill_catalog(tmp_path, _use_cache=False)
        listed = asyncio.run(list_skills({}, _ctx(tmp_path)))

        assert loaded["enabled"] is default_on
        assert [skill["enabled"] for skill in listed["data"]["skills"]] == [default_on]


def _book(root: Path, entries: list[dict[str, Any]]) -> Path:
    namespace = conversation_namespace(_CONV_KEY)
    path = root / "data" / "memory" / namespace / "people.yaml"
    return _write(path, yaml.safe_dump({"book": "people", "entries": entries}, allow_unicode=True))


def _entry(entry_id: str, **flags: Any) -> dict[str, Any]:
    return {"id": entry_id, "content": f"{entry_id} 的内容", "keywords": ["张三"], "source": "auto", **flags}


class TestMemoryEntries:
    def test_injection_and_the_console_listing_read_the_same_flags(self, tmp_path: Path) -> None:
        _book(tmp_path, [
            _entry("off", enabled="false"),
            _entry("pinned", constant="yes"),
            _entry("plain", constant="false"),
        ])
        namespace = conversation_namespace(_CONV_KEY)

        catalog = {item["id"]: item for item in load_memory_catalog(tmp_path, memory_book=namespace, _use_cache=False)}
        listed = {item["id"]: item for item in memory_admin.list_entries(tmp_path, namespace)}

        assert set(catalog) == {"pinned", "plain"}
        assert (catalog["pinned"]["constant"], catalog["plain"]["constant"]) == (True, False)
        assert listed["off"]["enabled"] is False
        assert (listed["pinned"]["constant"], listed["plain"]["constant"]) == (True, False)

    def test_a_quoted_false_constant_no_longer_blocks_deletion(self, tmp_path: Path) -> None:
        _book(tmp_path, [_entry("pinned", constant="true"), _entry("plain", constant="false")])

        pinned = asyncio.run(delete_memory({"id": "pinned", "book": "people"}, _ctx(tmp_path)))
        plain = asyncio.run(delete_memory({"id": "plain", "book": "people"}, _ctx(tmp_path)))

        assert pinned["ok"] is False and "constant" in pinned["error"]
        assert plain["ok"] is True

    @pytest.mark.parametrize("raw,protected", [("yes", True), ("true", True), ("false", False), ("no", False)])
    def test_cleanup_protects_exactly_the_constant_ones(self, raw: str, protected: bool) -> None:
        entry = _entry("old", constant=raw, created_at="2020-01-01T00:00:00+00:00")

        expired = _memory_entry_expired(entry, "ns", time.time(), {}, lambda _cache, _ns, _eid: "")

        assert expired is (not protected)

    def test_the_console_save_reads_quoted_flags(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        payload = {"book": "people", "id": "m1", "content": "张三住在杭州", "keywords": ["张三"], "constant": "false", "enabled": "no"}

        memory_admin.upsert_entry(tmp_path, namespace, payload)

        [stored] = memory_admin.list_entries(tmp_path, namespace)
        assert (stored["constant"], stored["enabled"]) == (False, False)
        raw = yaml.safe_load((tmp_path / "data" / "memory" / namespace / "people.yaml").read_text(encoding="utf-8"))
        assert (raw["entries"][0]["constant"], raw["entries"][0]["enabled"]) == (False, False)


class TestMcpClients:
    @pytest.mark.parametrize("raw,_default_off,default_on", _CASES)
    def test_enabled(self, tmp_path: Path, raw: str, _default_off: bool, default_on: bool) -> None:
        _write(config_layout.mcp_path(tmp_path), f"clients:\n  search:\n    transport: stdio\n    command: fake-mcp-server\n    enabled: {raw}\n")

        [client] = mcp_runtime.list_clients(tmp_path)

        assert client["enabled"] is default_on
        if not default_on:
            with pytest.raises(RuntimeError, match="disabled"):
                mcp_runtime._launch_plan(tmp_path, "search")


class TestFallbackVision:
    @pytest.mark.parametrize("raw,default_off,_default_on", _CASES)
    def test_supports_vision(self, tmp_path: Path, raw: str, default_off: bool, _default_on: bool) -> None:
        store = ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path)

        public = store._public_fallback({"provider": "openai", "supports_vision": yaml.safe_load(raw)})

        assert public["supports_vision"] is default_off


class TestScriptToolSpec:
    @pytest.mark.parametrize("raw,default_off,_default_on", _CASES)
    def test_async_mode(self, tmp_path: Path, raw: str, default_off: bool, _default_on: bool) -> None:
        value = repr(yaml.safe_load(raw))
        _write(tmp_path / "tools" / "slow_job.py", (
            "SPEC = {'name': 'slow_job', 'description': 'd', "
            f"'input_schema': {{'type': 'object', 'properties': {{}}}}, 'async_mode': {value}}}\n"
        ))

        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")

        assert registry.get_spec("slow_job")["async_mode"] is default_off


class TestRuntimeSwitches:
    @pytest.mark.parametrize("raw,_default_off,default_on", _CASES)
    def test_main_session_enabled(self, raw: str, _default_off: bool, default_on: bool) -> None:
        config = {"engine": {"child_session": {"main_session_enabled": yaml.safe_load(raw)}}}

        assert main_session_enabled(config) is default_on

    @pytest.mark.parametrize("config", [{}, {"engine": None}, {"engine": {"child_session": "broken"}}])
    def test_a_missing_or_malformed_section_keeps_the_default(self, config: dict[str, Any]) -> None:
        assert main_session_enabled(config) is True

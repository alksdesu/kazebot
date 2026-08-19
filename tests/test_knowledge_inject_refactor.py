from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_knowledge_match_keyword_and_scan_text_semantics() -> None:
    """Protect the shared matcher against behavior drift during the refactor."""
    # Why: the matcher is being moved into the knowledge plugin and the deleted
    # standalone matcher module must not remain the public dependency. How:
    # import the helpers from engine.builtin.knowledge_inject before exercising the
    # same regex, literal, and scan-depth cases. Purpose: keep behavior unchanged
    # while proving the new plugin owns the shared keyword logic.
    from engine.builtin.knowledge_inject import build_scan_text, compile_keyword, match_keywords

    assert match_keywords([compile_keyword("/hello/i")], "HeLLo world") is True
    assert match_keywords([compile_keyword("Broken[")], "a broken[ keyword") is True
    assert match_keywords([compile_keyword("Needle")], "haystack needle") is True
    assert match_keywords([compile_keyword("")], "needle") is False

    history = [
        {"role": "user", "content": "old user"},
        {"role": "assistant", "content": "old assistant"},
        {"role": "system", "content": "ignored system"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent assistant"},
    ]
    scan_text = build_scan_text("current instruction", history, 1)
    assert scan_text == "current instruction\nrecent user\nrecent assistant"


def test_knowledge_inject_sets_existing_extra_keys_and_rebuilds_prompt(tmp_path: Path) -> None:
    """Verify the unified injector preserves the old prompt injection contract."""
    # Why: run_ai_node and downstream code still read the existing skill_* and
    # memory_* ctx.extra keys. How: create a tiny workspace, run KnowledgeInjector,
    # and compare its rebuilt messages with the shared assembly helper. Purpose:
    # ensure the unified plugin changes ownership, not prompt layout or key names.
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Demo skill\n"
        "enabled: true\n"
        "strategy: normal\n"
        "keywords: [demo]\n"
        "scan_depth: 0\n"
        "---\n"
        "Demo skill body.\n",
        encoding="utf-8",
    )

    memory_dir = tmp_path / "data" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "book.yaml").write_text(
        "book: book\n"
        "entries:\n"
        "  - id: always\n"
        "    content: Constant memory body.\n"
        "    constant: true\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    from engine.builtin.knowledge_inject import KnowledgeInjector
    from engine.inference.message_assembly import assemble_messages_with_injections

    node = SimpleNamespace(
        id="node-a",
        skill_access=SimpleNamespace(mode="all", allow=None),
        memory_access=SimpleNamespace(mode="all", allow=None),
    )
    system_prompt = [{"role": "system", "content": "System prompt."}]
    history = [{"role": "user", "content": "previous turn"}]
    ctx = SimpleNamespace(
        node=node,
        rctx=SimpleNamespace(workspace_root=tmp_path),
        messages=[{"role": "system", "content": "placeholder"}],
        extra={
            "runtime_cfg": {"skills": {"max_budget_chars": 0}, "memory": {"max_budget_chars": 0}},
            "instruction_text": "please use demo",
            "history": history,
            "attachments": None,
            "system_prompt": system_prompt,
            "apply_injection": True,
        },
    )

    result = asyncio.run(KnowledgeInjector().handle(ctx))

    assert result is not None
    assert result.modified is True
    assert ctx.extra["skill_static_messages"] == []
    assert ctx.extra["skill_dynamic_messages"]
    assert ctx.extra["memory_static_messages"]
    assert ctx.extra["memory_dynamic_messages"] == []

    expected_messages, expected_block_mode = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=system_prompt,
        history=history,
        instruction="please use demo",
        attachments=None,
        skill_static=ctx.extra["skill_static_messages"],
        skill_dynamic=ctx.extra["skill_dynamic_messages"],
        memory_static=ctx.extra["memory_static_messages"],
        memory_dynamic=ctx.extra["memory_dynamic_messages"],
    )
    assert ctx.messages == expected_messages
    assert ctx.extra["is_block_mode"] is expected_block_mode


def test_loader_registers_knowledge_tools_from_plugin_meta(tmp_path: Path) -> None:
    """Knowledge tools should be declared by PLUGIN_META and installed by loader."""
    # Why: skill and memory CRUD tools are being removed from registry.py's hard-
    # coded list. How: create a real ToolRegistry, run built-in auto-discovery with
    # that registry, and verify the six knowledge tools appear and survive reload.
    # Purpose: make plugin-owned tool registration executable instead of relying on
    # implicit imports from toolbox.builtins.
    from engine.builtin.loader import auto_discover_and_register
    from engine.hooks import HookRegistry
    from toolbox.registry import ToolRegistry

    tool_registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    assert tool_registry.get_spec("save_memory") is None
    assert tool_registry.get_spec("create_or_update_skill") is None

    hook_registry = HookRegistry()
    handlers = auto_discover_and_register(hook_registry, tool_registry=tool_registry)

    assert "knowledge_inject" in handlers
    assert hook_registry.list_hooks()["before_prompt_build"] == ["knowledge_inject"]
    for name in (
        "save_memory",
        "list_memories",
        "delete_memory",
        "create_or_update_skill",
        "list_skills",
        "delete_skill",
    ):
        spec = tool_registry.get_spec(name)
        assert spec is not None
        assert spec["name"] == name
        assert isinstance(spec["input_schema"], dict)

    assert tool_registry.reload() >= 1
    assert tool_registry.get_spec("save_memory") is not None

    result = asyncio.run(
        tool_registry.execute(
            name="list_skills",
            arguments={},
            ctx=SimpleNamespace(workspace_root=tmp_path),
        )
    )
    # [AutoC 2026-07-09] knowledge 工具统一返回 ok/data 结构（_tool_ok）：
    # {"ok": True, "data": {"result": "0 skills", "skills": []}}。不再做全等断言，
    # 只校验关键字段，以免因展示包装字段变化而误报。
    assert result["ok"] is True
    assert result["data"]["skills"] == []
    assert result["data"]["result"] == "0 skills"

"""输出规则：strategy: output 的 skill 不进提示词，模型的回复命中 trigger 时这次回复作废，带着规则让这一步重来。"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, echo, stored, turn  # noqa: E402
from _file_edits_fake import FakeFileEdits  # noqa: E402
from engine.builtin import knowledge_inject, output_rules  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    build_knowledge_context,
    create_or_update_skill,
    list_skills,
    load_skill_catalog,
    output_rule_problem,
    parse_output_rule,
)
from engine.builtin.output_rules import (  # noqa: E402
    MAX_SEGMENT_CHARS,
    MAX_TOTAL_CHARS,
    OutputRuleGuard,
    node_output_rules,
    reply_segments,
    task_fires,
)
from engine.inference.ai_step import STEP_LIMIT_NOTICE  # noqa: E402
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from engine.node import Node, SkillAccess, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH, ACTION_PREEMPTED  # noqa: E402
from providers.base import ProviderResponse, ToolCall  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

EVENT = "output_rule_triggered"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目录缓存按工作区存两秒、触发次数按任务 id 存在进程里，用例之间不能串
    monkeypatch.setattr(knowledge_inject._SkillCache, "_entries", {})
    monkeypatch.setattr(output_rules, "_TASK_FIRES", OrderedDict())
    monkeypatch.setattr(knowledge_inject, "_WARNED_OUTPUT_RULES", OrderedDict())


def write_skill(workspace: Path, name: str, body: str = "", **meta: Any) -> Path:
    path = workspace / "config" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = yaml.safe_dump({"name": name, **meta}, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
    return path


def rule(workspace: Path, name: str, trigger: str, body: str = "", **meta: Any) -> Path:
    return write_skill(workspace, name, body, strategy="output", trigger=trigger, **meta)


def node(*, node_id: str = "node-1", tool_mode: str = "native", skills: SkillAccess | None = None, **extra: Any) -> Node:
    return Node(
        id=node_id, type="ai", prompt="Work carefully.", tool_mode=tool_mode,
        tool_access=ToolAccess(mode="all"), skill_access=skills or SkillAccess(), extra=dict(extra),
    )


def node_run(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    *,
    max_steps: int = 8,
    task_context: dict[str, Any] | None = None,
    handlers: dict[str, list[Any]] | None = None,
    **node_options: Any,
) -> NodeRun:
    hooks = {"after_llm_call": [OutputRuleGuard()], **(handlers or {})}
    return NodeRun(
        monkeypatch, workspace,
        registry=ScriptedRegistry(tools={"probe": echo("probe output")}),
        node=node(**node_options),
        runtime_cfg={"engine": {"max_steps": max_steps}},
        task_context=task_context,
        handlers=hooks,
    )


def finish(text: str, call_id: str = "f") -> ProviderResponse:
    return turn(call("finish", {"text": text}, call_id=call_id))


def json_turn(text: str, name: str, arguments: dict[str, Any]) -> ProviderResponse:
    block = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    return ProviderResponse(ok=True, text=f"{text}\n<<<TOOL_CALL>>>\n{block}\n<<<END_TOOL_CALL>>>")


def fired(run: NodeRun) -> list[dict[str, Any]]:
    return run.rctx.events.of(EVENT)  # type: ignore[attr-defined]


class TestTheRetry:
    def test_a_matching_text_discards_the_reply_and_retries_with_the_rule(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-forbidden", "FORBIDDEN", "不要在回复里提到被禁止的词。", scope="text")
        run = node_run(monkeypatch, tmp_path)
        bad = turn(call("probe", {"q": "x"}, call_id="p1"), text="I will say FORBIDDEN here")

        action = run.run(bad, finish("all clean"))

        assert action.action == ACTION_FINISH and action.result["text"] == "all clean"
        assert run.registry.executed_names() == []
        hint = run.requests[1]["messages"][-1]
        assert hint == {
            "role": "user",
            "content": "你刚才的输出触发了规则「no-forbidden」：不要在回复里提到被禁止的词。请按规则重新回答。",
            "_retry_hint": True,
        }
        assert not any("FORBIDDEN" in str(message.content) for message in stored(tmp_path))
        assert not any("你刚才的输出触发了规则" in str(message.content) for message in stored(tmp_path))
        [event] = fired(run)
        assert event["rule"] == "no-forbidden" and event["scope"] == "text"
        assert event["fires"] == 1 and event["max_fires"] == 1 and event["step"] == 0
        assert "FORBIDDEN" not in json.dumps(event, ensure_ascii=False)

    def test_the_discarded_reply_never_enters_the_next_request(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-forbidden", "FORBIDDEN")
        run = node_run(monkeypatch, tmp_path)

        run.run(finish("FORBIDDEN answer"), finish("fine"))

        retried = run.requests[1]["messages"]
        assert not any(message.get("role") == "assistant" for message in retried)
        assert not any("FORBIDDEN answer" in json.dumps(message, ensure_ascii=False) for message in retried)

    def test_a_rule_without_a_body_falls_back_to_its_description(self, tmp_path: Path, monkeypatch: Any) -> None:
        write_skill(tmp_path, "terse", "", strategy="output", trigger="BAD", description="别说 BAD！")
        run = node_run(monkeypatch, tmp_path)

        run.run(finish("BAD"), finish("ok"))

        assert run.requests[1]["messages"][-1]["content"] == "你刚才的输出触发了规则「terse」：别说 BAD！请按规则重新回答。"

    def test_a_reply_that_matches_nothing_goes_through(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-forbidden", "FORBIDDEN")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(turn(call("probe", {"q": "x"}, call_id="p1")), finish("done"))

        assert action.result["text"] == "done"
        assert run.registry.executed_names() == ["probe"]
        assert fired(run) == []


class TestScope:
    def test_tool_arguments_are_checked(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-rm", r"rm\s+-rf", "不要删目录。", scope="tool_args")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(
            turn(call("probe", {"cmd": "rm -rf build"}, call_id="p1")),
            turn(call("probe", {"cmd": "ls"}, call_id="p2")),
            finish("done"),
        )

        assert action.result["text"] == "done"
        assert [arguments for _name, arguments, _ctx in run.registry.executed] == [{"cmd": "ls"}]
        [event] = fired(run)
        assert event["scope"] == "tool_args" and event["tool_name"] == "probe"

    def test_nested_argument_strings_are_checked(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-rm", r"rm\s+-rf", scope="tool_args")
        run = node_run(monkeypatch, tmp_path)

        run.run(turn(call("probe", {"steps": [{"run": "echo ok"}, {"run": "rm -rf /"}]}, call_id="p1")), finish("ok"))

        assert run.registry.executed_names() == []
        assert len(fired(run)) == 1

    def test_finish_and_reply_arguments_count_as_tool_arguments(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-password", "密码是", scope="tool_args")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(finish("密码是 123456"), finish("这个不能告诉你"))

        assert action.result["text"] == "这个不能告诉你"
        assert fired(run)[0]["tool_name"] == "finish"

    def test_a_text_rule_ignores_tool_arguments(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "secret-text", "SECRET", scope="text")
        run = node_run(monkeypatch, tmp_path)

        run.run(turn(call("probe", {"q": "SECRET"}, call_id="p1"), text="looking it up"), finish("done"))

        assert run.registry.executed_names() == ["probe"]
        assert fired(run) == []

    def test_a_tool_args_rule_ignores_the_text(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "secret-args", "SECRET", scope="tool_args")
        run = node_run(monkeypatch, tmp_path)

        run.run(turn(call("probe", {"q": "fine"}, call_id="p1"), text="SECRET plan"), finish("done"))

        assert run.registry.executed_names() == ["probe"]
        assert fired(run) == []

    def test_both_is_the_default(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "secret", "SECRET")
        run = node_run(monkeypatch, tmp_path)

        run.run(
            turn(call("probe", {"q": "fine"}, call_id="p1"), text="SECRET plan"),
            turn(call("probe", {"q": "SECRET"}, call_id="p2")),
            finish("done"),
        )

        assert run.registry.executed_names() == ["probe"]
        assert [event["scope"] for event in fired(run)] == ["text"]

    def test_json_mode_reads_the_calls_out_of_the_text(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-rm", r"rm\s+-rf", scope="tool_args")
        run = node_run(monkeypatch, tmp_path, tool_mode="json")

        action = run.run(
            json_turn("先清理一下。", "probe", {"cmd": "rm -rf build"}),
            json_turn("看看目录。", "probe", {"cmd": "ls"}),
            json_turn("", "finish", {"text": "done"}),
        )

        assert action.result["text"] == "done"
        assert [arguments for _name, arguments, _ctx in run.registry.executed] == [{"cmd": "ls"}]

    def test_json_mode_text_scope_does_not_see_the_tool_block(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "secret-text", "SECRET", scope="text")
        run = node_run(monkeypatch, tmp_path, tool_mode="json")

        run.run(json_turn("正在查。", "probe", {"q": "SECRET"}), json_turn("", "finish", {"text": "done"}))

        assert run.registry.executed_names() == ["probe"]
        assert fired(run) == []


class TestMaxFires:
    def test_the_default_is_one_retry_per_task(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-bad", "BAD")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(finish("BAD one"), finish("BAD two"))

        assert action.result["text"] == "BAD two"
        assert [event["fires"] for event in fired(run)] == [1]

    def test_a_higher_cap_retries_that_many_times(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-bad", "BAD", max_fires=2)
        run = node_run(monkeypatch, tmp_path)

        action = run.run(finish("BAD one"), finish("BAD two"), finish("BAD three"))

        assert action.result["text"] == "BAD three"
        assert [(event["fires"], event["max_fires"]) for event in fired(run)] == [(1, 2), (2, 2)]

    def test_each_rule_has_its_own_count(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "a-rule", "ALPHA")
        rule(tmp_path, "b-rule", "BETA")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(finish("ALPHA"), finish("BETA"), finish("ALPHA BETA"))

        assert action.result["text"] == "ALPHA BETA"
        assert [event["rule"] for event in fired(run)] == ["a-rule", "b-rule"]

    def test_the_count_belongs_to_the_task_not_to_the_loop_state(self, tmp_path: Path) -> None:
        rctx = SimpleNamespace(task_id="task-9")
        first, second = SimpleNamespace(rctx=rctx), SimpleNamespace(rctx=rctx)

        task_fires(first)["config/skills/x/SKILL.md"] += 1

        assert task_fires(second)["config/skills/x/SKILL.md"] == 1
        asyncio.run(OutputRuleGuard().forget(SimpleNamespace(rctx=rctx)))
        assert task_fires(second)["config/skills/x/SKILL.md"] == 0

    def test_a_loop_state_without_a_task_keeps_its_own_count(self) -> None:
        first, second = SimpleNamespace(rctx=SimpleNamespace(task_id="")), SimpleNamespace(rctx=SimpleNamespace(task_id=""))

        task_fires(first)["k"] += 1

        assert task_fires(first)["k"] == 1 and task_fires(second)["k"] == 0


class TestWhereItDoesNotApply:
    def test_system_tasks_are_left_alone(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-bad", "BAD")
        run = node_run(monkeypatch, tmp_path, task_context={"is_system_task": True})

        action = run.run(finish("BAD summary"))

        assert action.result["text"] == "BAD summary"
        assert fired(run) == []

    def test_an_invalid_regex_is_skipped(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "broken", "(unclosed")
        rule(tmp_path, "works", "BAD")
        run = node_run(monkeypatch, tmp_path)

        action = run.run(finish("(unclosed BAD"), finish("(unclosed"))

        assert action.result["text"] == "(unclosed"
        assert [event["rule"] for event in fired(run)] == ["works"]

    def test_a_disabled_rule_does_nothing(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-bad", "BAD", enabled=False)
        run = node_run(monkeypatch, tmp_path)

        assert run.run(finish("BAD")).result["text"] == "BAD"

    def test_a_failed_response_is_not_checked(self, tmp_path: Path) -> None:
        rule(tmp_path, "no-bad", "BAD")
        ls = SimpleNamespace(rctx=SimpleNamespace(workspace_root=tmp_path, task_context={}, task_id="t"), node=node())
        ctx = SimpleNamespace(extra={"loop_state": ls}, response=ProviderResponse(ok=False, text="BAD", error="boom"), step=0)

        assert asyncio.run(OutputRuleGuard().handle(ctx)) is None

    def test_rules_follow_node_ids(self, tmp_path: Path) -> None:
        rule(tmp_path, "only-other", "BAD", node_ids=["other.node"])
        rule(tmp_path, "everyone", "BAD")

        assert [item.name for item in node_output_rules(tmp_path, node())] == ["everyone"]
        assert [item.name for item in node_output_rules(tmp_path, node(node_id="other.node"))] == ["everyone", "only-other"]

    def test_rules_follow_the_node_skill_access(self, tmp_path: Path) -> None:
        rule(tmp_path, "no-bad", "BAD")

        assert node_output_rules(tmp_path, node(skills=SkillAccess(mode="none"))) == []
        assert node_output_rules(tmp_path, node(skills=SkillAccess(mode="allowlist", allow=["other"]))) == []
        allowed = node_output_rules(tmp_path, node(skills=SkillAccess(mode="allowlist", allow=["no-bad"])))
        assert [item.name for item in allowed] == ["no-bad"]


class TestTheLastStep:
    def test_a_violation_on_the_last_step_wraps_up_without_the_reply(self, tmp_path: Path, monkeypatch: Any) -> None:
        rule(tmp_path, "no-bad", "BAD")
        run = node_run(monkeypatch, tmp_path, max_steps=3)
        probes = [turn(call("probe", {"round": index}, call_id=f"p{index}")) for index in range(2)]

        action = run.run(*probes, finish("BAD final answer"))

        assert action.action == ACTION_FINISH
        assert action.result["step_limit_reached"] is True
        assert action.result["text"].startswith("任务还没有完成。已经执行过的工具：probe ×2。")
        assert action.result["text"].endswith(STEP_LIMIT_NOTICE)
        assert "BAD" not in action.result["text"]

    def test_a_soft_interrupt_stops_instead_of_retrying(self, tmp_path: Path, monkeypatch: Any) -> None:
        class SoftInterrupt:
            name = "soft_interrupt"
            priority = 0

            async def handle(self, ctx: Any) -> None:
                ctx.extra["loop_state"].preempt_after_step = True

        rule(tmp_path, "no-bad", "BAD")
        run = node_run(monkeypatch, tmp_path, handlers={"before_step": [SoftInterrupt()]})

        action = run.run(finish("BAD"))

        assert action.action == ACTION_PREEMPTED
        assert len(fired(run)) == 1
        assert len(run.requests) == 1


class TestScanBounds:
    def test_text_past_the_segment_limit_is_not_scanned(self) -> None:
        formatter = create_tool_formatter("native")
        late = ProviderResponse(ok=True, text="x" * MAX_SEGMENT_CHARS + "BAD")
        early = ProviderResponse(ok=True, text="BAD" + "x" * MAX_SEGMENT_CHARS)

        [late_segment] = reply_segments(late, formatter, {"text"})
        [early_segment] = reply_segments(early, formatter, {"text"})

        assert len(late_segment.text) == MAX_SEGMENT_CHARS and "BAD" not in late_segment.text
        assert early_segment.text.startswith("BAD")

    def test_all_segments_together_stay_under_the_total_limit(self) -> None:
        formatter = create_tool_formatter("native")
        response = ProviderResponse(ok=True, text="t" * MAX_SEGMENT_CHARS, tool_calls=[
            ToolCall(id=f"c{index}", name="probe", arguments={"a": "a" * MAX_SEGMENT_CHARS, "b": "b" * 10})
            for index in range(5)
        ])

        segments = reply_segments(response, formatter, {"text", "tool_args"})

        assert sum(len(segment.text) for segment in segments) == MAX_TOTAL_CHARS
        assert segments[0].where == "text"

    def test_only_the_wanted_scopes_are_collected(self) -> None:
        formatter = create_tool_formatter("native")
        response = ProviderResponse(ok=True, text="hello", tool_calls=[ToolCall(id="c", name="probe", arguments={"q": "x", "n": 3})])

        assert [segment.where for segment in reply_segments(response, formatter, {"text"})] == ["text"]
        assert [(segment.where, segment.text) for segment in reply_segments(response, formatter, {"tool_args"})] == [
            ("tool_args", "x"),
        ]


class TestTheSkillCatalog:
    def test_output_skills_are_never_injected(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "demo", "Demo skill body.", strategy="normal", keywords=["demo"])
        rule(tmp_path, "guard", "BAD", "Guard body.", keywords=["demo"])
        rule(tmp_path, "indexed", "BAD", "Index body.")
        guard_node = SimpleNamespace(
            id="node-a", aliases=(), extra={},
            skill_access=SkillAccess(mode="all"), memory_access=SimpleNamespace(mode="none", allow=None),
        )

        skill_static, skill_dynamic, _memory_static, _memory_dynamic = build_knowledge_context(
            tmp_path, guard_node, "please use demo", [], {},
        )

        rendered = json.dumps(skill_static + skill_dynamic, ensure_ascii=False)
        assert "Demo skill body." in rendered
        for hidden in ("guard", "Guard body.", "indexed", "Index body."):
            assert hidden not in rendered

    def test_the_catalog_keeps_the_output_strategy_and_its_settings(self, tmp_path: Path) -> None:
        rule(tmp_path, "guard", "B(A)D", "body", scope="tool_args", max_fires=3)
        write_skill(tmp_path, "odd", "body", strategy="weird")

        catalog = {item["name"]: item for item in load_skill_catalog(tmp_path, _use_cache=False)}

        assert catalog["guard"]["strategy"] == "output"
        assert catalog["guard"]["output_rule"]["pattern"].search("xBADx")
        assert (catalog["guard"]["output_rule"]["scope"], catalog["guard"]["output_rule"]["max_fires"]) == ("tool_args", 3)
        assert catalog["odd"]["strategy"] == "normal" and catalog["odd"]["output_rule"] is None

    def test_a_broken_trigger_is_logged_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        rule(tmp_path, "broken", "(unclosed")

        with caplog.at_level(logging.WARNING, logger="engine.builtin.knowledge_inject"):
            for _ in range(3):
                [item] = load_skill_catalog(tmp_path, _use_cache=False)

        assert item["output_rule"]["pattern"] is None
        assert sum("not a valid regex" in record.getMessage() for record in caplog.records) == 1

    @pytest.mark.parametrize(("meta", "expected"), [
        ({"trigger": "x"}, ("both", 1)),
        ({"trigger": "x", "scope": "TEXT"}, ("text", 1)),
        ({"trigger": "x", "scope": "everything"}, ("both", 1)),
        ({"trigger": "x", "max_fires": 0}, ("both", 1)),
        ({"trigger": "x", "max_fires": "2"}, ("both", 1)),
        ({"trigger": "x", "max_fires": 2.0}, ("both", 2)),
        ({"trigger": "x", "max_fires": True}, ("both", 1)),
    ])
    def test_bad_settings_fall_back_to_the_defaults(self, meta: dict[str, Any], expected: tuple[str, int]) -> None:
        parsed = parse_output_rule(meta, where="config/skills/x/SKILL.md")

        assert (parsed["scope"], parsed["max_fires"]) == expected
        assert parsed["pattern"] is not None

    def test_a_missing_trigger_disables_the_rule(self) -> None:
        assert parse_output_rule({}, where="config/skills/y/SKILL.md")["pattern"] is None

    @pytest.mark.parametrize(("meta", "fragment"), [
        ({}, "non-empty trigger"),
        ({"trigger": "  "}, "non-empty trigger"),
        ({"trigger": "(x"}, "not a valid regex"),
        ({"trigger": "x", "scope": "all"}, "scope must be one of"),
        ({"trigger": "x", "max_fires": 0}, "max_fires"),
        ({"trigger": "x", "max_fires": "3"}, "max_fires"),
    ])
    def test_the_write_side_names_what_is_wrong(self, meta: dict[str, Any], fragment: str) -> None:
        assert fragment in output_rule_problem(meta)

    def test_a_complete_rule_has_no_problem(self) -> None:
        assert output_rule_problem({"trigger": "x", "scope": "tool_args", "max_fires": 2}) == ""


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：写 skill 走 write_file 的认领和送审，认领交给 FakeSupervisor，送审一律放行。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused", session_id="sess-skill", run_id="run-1", worker_id="worker-1",
            workspace_root=workspace_root, http=None, registry=None,  # type: ignore[arg-type]
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto", "reason": "test"}

    async def check_cancelled(self) -> bool:
        return False


class TestTheSkillTools:
    def test_an_output_skill_is_written_with_its_settings(self, tmp_path: Path) -> None:
        result = asyncio.run(create_or_update_skill({
            "name": "no-rm", "description": "别删目录", "strategy": "output",
            "trigger": r"rm\s+-rf", "scope": "tool_args", "max_fires": 2,
        }, _Ctx(tmp_path)))

        assert result["ok"] is True
        text = (tmp_path / "config" / "skills" / "no-rm" / "SKILL.md").read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---\n")[1])
        assert meta["strategy"] == "output" and meta["trigger"] == r"rm\s+-rf"
        assert meta["scope"] == "tool_args" and meta["max_fires"] == 2

    def test_the_settings_can_come_from_the_content(self, tmp_path: Path) -> None:
        content = "---\nstrategy: output\ntrigger: BAD\n---\n\n不要说 BAD。\n"

        result = asyncio.run(create_or_update_skill({"name": "guard", "content": content}, _Ctx(tmp_path)))

        assert result["ok"] is True
        [item] = load_skill_catalog(tmp_path, _use_cache=False)
        assert item["strategy"] == "output" and item["body"] == "不要说 BAD。"

    @pytest.mark.parametrize(("arguments", "fragment"), [
        ({"strategy": "output"}, "non-empty trigger"),
        ({"strategy": "output", "trigger": "(x"}, "not a valid regex"),
        ({"strategy": "output", "trigger": "x", "scope": "anywhere"}, "scope must be one of"),
        ({"strategy": "output", "trigger": "x", "max_fires": 0}, "max_fires"),
        ({"strategy": "output", "trigger": "x", "max_fires": "many"}, "max_fires"),
        ({"strategy": "output", "trigger": ["x"]}, "regex string"),
        ({"strategy": "normal", "trigger": "x"}, "only apply to a skill with strategy output"),
    ])
    def test_a_broken_rule_is_refused_and_nothing_is_written(
        self, tmp_path: Path, arguments: dict[str, Any], fragment: str,
    ) -> None:
        result = asyncio.run(create_or_update_skill({"name": "guard", **arguments}, _Ctx(tmp_path)))

        assert result["ok"] is False and fragment in result["error"]
        assert not (tmp_path / "config" / "skills" / "guard").exists()

    def test_the_schema_offers_the_output_strategy(self) -> None:
        spec = next(tool for tool in knowledge_inject.PLUGIN_META["tools"] if tool["name"] == "create_or_update_skill")
        properties = spec["input_schema"]["properties"]

        assert "output" in properties["strategy"]["enum"]
        assert properties["scope"]["enum"] == ["text", "tool_args", "both"]
        assert properties["max_fires"]["type"] == "integer" and properties["trigger"]["type"] == "string"

    def test_list_skills_reports_the_rule(self, tmp_path: Path) -> None:
        rule(tmp_path, "guard", "BAD", scope="text", max_fires=2)
        rule(tmp_path, "broken", "(x")

        result = asyncio.run(list_skills({}, _Ctx(tmp_path)))

        skills = {item["name"]: item for item in result["data"]["skills"]}
        assert skills["guard"]["strategy"] == "output"
        assert (skills["guard"]["trigger"], skills["guard"]["scope"], skills["guard"]["max_fires"]) == ("BAD", "text", 2)
        assert "rule_error" not in skills["guard"]
        assert "not a valid regex" in skills["broken"]["rule_error"]

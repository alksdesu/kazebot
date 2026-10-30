"""隔离副本里的任务看到的说明：按 supervisor 写进 task_context 的隔离信息，在动态区渲染成固定的一段；
不隔离的任务提示词一字不变，重建和打断注入时也带着它；{{project_root}} 是任务的项目根，coder 的交付文件指到实例根。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, echo, turn  # noqa: E402
from engine.builtin.knowledge_inject import _rebuild_prompt_messages  # noqa: E402
from engine.builtin.preempt import _inject_preempt_message  # noqa: E402
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.inference.message_assembly import assemble_initial_messages, assemble_messages_with_injections  # noqa: E402
from engine.isolation_notice import (  # noqa: E402
    ISOLATION_CLOSE,
    ISOLATION_OPEN,
    NOT_ISOLATED_MODE,
    isolation_parts,
    nested_isolation_refusal,
    task_isolation,
)
from engine.node import Node, load_node  # noqa: E402
from engine.project_root import ISOLATION_CONTEXT_KEY  # noqa: E402
from engine.prompt import assemble_prompt  # noqa: E402
from toolbox._common import ORIGIN_ROOT_ENV, ORIGIN_VENV_ENV  # noqa: E402

BASE = "0123456789abcdef0123456789abcdef01234567"
_INTERPRETER = "Scripts/python.exe" if os.name == "nt" else "bin/python"
_PERSONA = {"role": "system", "content": "persona"}
_NODE_TURN = {"role": "system", "content": "node turn notes"}
_SKILL_TURN = {"role": "system", "content": "[SKILL:ACTIVE]\nhow\n[/SKILL:ACTIVE]"}
_MEMORY_TURN = {"role": "system", "content": "[MEMORY:ACTIVE]\nrecent\n[/MEMORY:ACTIVE]"}
_HISTORY = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
_PROMPTS = {
    "string": [_PERSONA, _NODE_TURN],
    "block": [_PERSONA, {"role": "user", "content": "fixed rules"}, {"role": "history"}],
}


def _isolation(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """supervisor 给新建副本的子任务写的那份说明；原仓库带 .git，找虚拟环境找到它为止。"""
    repo = tmp_path / "repo"
    worktree = tmp_path / "repo-worktrees" / "repo-key" / "wt-1"
    worktree.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "mode": "worktree", "worktree": "wt-1", "path": str(worktree), "worktree_root": str(worktree),
        "origin": str(repo), "repo": str(repo), "branch": "clonoth/wt-1", "base": BASE,
        "workspace_repo": True, "inherited": False, "reused": False,
        "env": {"origin_root": ORIGIN_ROOT_ENV, "origin_venv": ORIGIN_VENV_ENV},
    }
    info.update(overrides)
    return info


def _lines(info: dict[str, Any], workspace: Path) -> list[str]:
    [block] = isolation_parts(info, workspace_root=workspace)
    return block.split("\n")


def _dynamic(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message["content"]) for message in messages if message.get("_dynamic"))


class TestRendering:
    def test_a_new_worktree_gets_the_whole_notice(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path)
        workspace = tmp_path / "instance"
        workspace.mkdir()

        lines = _lines(info, workspace)

        instance = workspace.resolve()
        assert lines == [
            ISOLATION_OPEN,
            "你在隔离副本里工作：改的是原仓库的一份 git worktree，不是原仓库本身。",
            f"- 副本里的项目根：{info['path']}（登记号 wt-1，分支 clonoth/wt-1，基于提交 {BASE[:12]}）",
            f"- 原仓库：{info['repo']}",
            "- 文件工具的相对路径和命令的工作目录都在副本里；不要用绝对路径去改原仓库里的文件，"
            "那样改的不在副本里，也不会算进交回的 diff。",
            "- 副本从基准提交检出：原仓库里还没提交的改动不在这里，.venv、node_modules 和被 gitignore 的文件也没有。",
            f"- 原仓库里没找到虚拟环境，命令的环境变量里没有 {ORIGIN_VENV_ENV}，要用 Python 依赖时先确认装在哪；"
            f"命令的环境变量 {ORIGIN_ROOT_ENV} 是原仓库里对应项目根的目录，只读不改。",
            f"- config/、data/ 在实例根 {instance}，不在副本里；交付给用户的文件写到 {instance / 'data' / 'attachments'} 下，"
            "用绝对路径，不要写进副本。",
            "- 不要切分支、不要 reset、不要 push，也不用自己提交：结束时会按基准提交算出副本里的全部改动"
            "（含未提交和未跟踪的文件），连同结果交给委派方，审过再合回。",
            "- 你再派出的子任务也在这个副本里做，不能再要求隔离。",
            ISOLATION_CLOSE,
        ]

    def test_a_project_root_below_the_worktree_root_names_both(self, tmp_path: Path) -> None:
        base = _isolation(tmp_path)
        root = Path(base["path"])
        sub = root / "services" / "api"
        sub.mkdir(parents=True)
        origin = Path(base["repo"]) / "services" / "api"
        origin.mkdir(parents=True)

        lines = _lines({**base, "path": str(sub), "worktree_root": str(root), "origin": str(origin)}, tmp_path)

        assert f"- 副本里的项目根：{sub}（副本根目录 {root}，登记号 wt-1，分支 clonoth/wt-1，基于提交 {BASE[:12]}）" in lines
        assert f"- 原仓库：{base['repo']}；你的项目根对应原仓库里的 {origin}" in lines

    def test_the_origin_venv_is_named_when_the_repository_has_one(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path)
        venv = Path(info["repo"]) / ".venv"
        (venv / _INTERPRETER).parent.mkdir(parents=True)
        (venv / _INTERPRETER).write_bytes(b"")
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

        lines = _lines(info, tmp_path)

        found = venv.resolve()
        assert (
            f"- Python 用原仓库的虚拟环境 {found}（解释器 {found / _INTERPRETER}），命令的环境变量 {ORIGIN_VENV_ENV} 也指向它；"
            f"命令的环境变量 {ORIGIN_ROOT_ENV} 是原仓库里对应项目根的目录，只读不改。"
        ) in lines

    def test_the_variable_names_come_from_the_notice(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path, env={"origin_root": "MY_ORIGIN", "origin_venv": "MY_VENV"})

        block = "\n".join(_lines(info, tmp_path))

        assert "MY_ORIGIN" in block and "MY_VENV" in block
        assert ORIGIN_ROOT_ENV not in block and ORIGIN_VENV_ENV not in block

    def test_a_grandchild_is_told_whose_worktree_it_works_in(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path, inherited=True, previous={"worktree": "old-1", "state": "merged"})

        lines = _lines(info, tmp_path)

        assert lines[1] == "你在隔离副本里工作：这个副本属于派你来的任务（登记号 wt-1），你在里面接着做，改的不是原仓库。"
        assert (
            "- 不要切分支、不要 reset、不要 push，也不用自己提交：你的改动留在这个副本里，"
            "由派你来的任务收尾时一起算出 diff 交回。"
        ) in lines
        # 副本主人当初换过副本的经历，对孙任务没有意义
        assert not any("old-1" in line for line in lines)
        assert lines[-2] == "- 你再派出的子任务也在这个副本里做，不能再要求隔离。"

    def test_a_continued_task_is_told_its_earlier_changes_are_still_there(self, tmp_path: Path) -> None:
        lines = _lines(_isolation(tmp_path, reused=True), tmp_path)

        assert lines[2] == "- 这是你上次用的那个副本，之前的改动都还在。"

    @pytest.mark.parametrize("state, label", [
        ("abandoned", "已经放弃"),
        ("expired", "过了保留期"),
        ("removed", "已经清理"),
        ("failed", "没建成"),
        ("merging", "正在合回"),
        ("missing", "在登记里已经查不到"),
        ("", "用不了了"),
        ("strange", "用不了了"),
    ])
    def test_a_replaced_worktree_is_explained(self, tmp_path: Path, state: str, label: str) -> None:
        lines = _lines(_isolation(tmp_path, previous={"worktree": "old-1", "state": state}), tmp_path)

        assert lines[2] == f"- 原来的副本 old-1 {label}，这是另建的，基于原仓库现在的提交：旧副本里没合回的改动不在这里。"

    def test_a_merged_worktree_without_a_carry_record_says_merging_does_not_commit(self, tmp_path: Path) -> None:
        lines = _lines(_isolation(tmp_path, previous={"worktree": "old-1", "state": "merged"}), tmp_path)

        assert lines[2] == (
            "- 原来的副本 old-1 已经合回，这是另建的，基于原仓库现在的提交：合回只改原仓库的工作区、不提交，它合回的改动不在这里。"
        )

    def test_merged_files_carried_into_the_base_are_explained(self, tmp_path: Path) -> None:
        previous = {"worktree": "old-1", "state": "merged", "carry": {"status": "carried", "count": 3, "error": ""}}

        lines = _lines(_isolation(tmp_path, previous=previous), tmp_path)

        assert lines[2] == (
            "- 原来的副本 old-1 已经合回，这是另建的：合回只改原仓库的工作区、不提交，所以它合回过的 3 个文件"
            "按原仓库工作区里现在的内容带进了基准提交，你在这之上接着做；没合回的改动不在这里。"
        )
        assert (
            "- 副本从基准提交检出：除了上面说的带进来的文件，原仓库里还没提交的改动不在这里，"
            ".venv、node_modules 和被 gitignore 的文件也没有。"
        ) in lines

    def test_merged_files_already_committed_need_no_carrying(self, tmp_path: Path) -> None:
        previous = {"worktree": "old-1", "state": "removed", "carry": {"status": "committed", "count": 2, "error": ""}}

        lines = _lines(_isolation(tmp_path, previous=previous), tmp_path)

        assert lines[2] == (
            "- 原来的副本 old-1 已经清理，这是另建的，基于原仓库现在的提交：它合回过的 2 个文件在原仓库里已经提交（或已撤回），"
            "不用另外带；没合回的改动不在这里。"
        )
        assert "- 副本从基准提交检出：原仓库里还没提交的改动不在这里，.venv、node_modules 和被 gitignore 的文件也没有。" in lines

    @pytest.mark.parametrize("count, files", [(3, " 3 个"), (0, ""), (True, ""), ("3", "")])
    def test_merged_files_that_could_not_be_carried_say_why_and_what_to_do(
        self, tmp_path: Path, count: Any, files: str,
    ) -> None:
        previous = {"worktree": "old-1", "state": "merged", "carry": {"status": "failed", "count": count, "error": "boom"}}

        lines = _lines(_isolation(tmp_path, previous=previous), tmp_path)

        assert lines[2] == (
            f"- 原来的副本 old-1 已经合回，这是另建的，基于原仓库现在的提交：它合回过的{files}文件没能带进来（boom），"
            "这些改动在原仓库的工作区里、不在这里；要接着改它们，先核对这里的内容，需要的话重做，"
            "或者请上级先把原仓库里的改动提交再派。"
        )
        assert "- 副本从基准提交检出：原仓库里还没提交的改动不在这里，.venv、node_modules 和被 gitignore 的文件也没有。" in lines

    def test_a_grandchild_never_hears_of_its_owner_s_carried_files(self, tmp_path: Path) -> None:
        previous = {"worktree": "old-1", "state": "merged", "carry": {"status": "carried", "count": 3, "error": ""}}

        lines = _lines(_isolation(tmp_path, inherited=True, previous=previous), tmp_path)

        assert not any("old-1" in line or "带进" in line for line in lines)

    def test_linked_directories_are_listed(self, tmp_path: Path) -> None:
        lines = _lines(_isolation(tmp_path, links=["node_modules", " web/node_modules ", "", 3]), tmp_path)

        assert (
            "- 副本里的 node_modules、web/node_modules 链到原仓库的同名目录，可以直接用；不要往里装包或改文件，那会改到原仓库。"
        ) in lines

    @pytest.mark.parametrize("raw", [None, {}, "worktree", ["worktree"], {"mode": "copy"}, {"worktree": "wt-1"}])
    def test_nothing_is_rendered_outside_a_worktree(self, raw: Any) -> None:
        assert isolation_parts(raw, workspace_root=Path(".")) == []

    def test_the_mode_is_read_the_way_the_supervisor_reads_it(self, tmp_path: Path) -> None:
        assert len(isolation_parts({**_isolation(tmp_path), "mode": " WorkTree "}, workspace_root=tmp_path)) == 1


def _reminder(**previous: Any) -> dict[str, Any]:
    """supervisor 给不隔离、接着一段隔离过的对话的子任务写的提醒。"""
    return {"mode": NOT_ISOLATED_MODE, "previous": {"worktree": "old-1", "state": "finished", "inherited": False, **previous}}


class TestReminder:
    def test_a_conversation_isolated_before_is_told_this_run_is_not(self, tmp_path: Path) -> None:
        [block] = isolation_parts(_reminder(), workspace_root=tmp_path)

        assert block.split("\n") == [
            ISOLATION_OPEN,
            "你这次不在隔离副本里：文件工具和命令直接作用于原仓库。",
            "- 这段对话之前在隔离副本 old-1 里做过（那个副本还留着等合回）：历史里的副本路径、"
            "在副本里改过的文件都指那个副本，不是这次的工作目录。",
            "- 那个副本里没合回的改动不在原仓库里，不要凭历史以为它们已经在了；要接着那份改动，"
            "请委派方带 isolation: worktree 再派，不要在原仓库里凭记忆重做。",
            ISOLATION_CLOSE,
        ]

    def test_a_worktree_it_only_followed_its_dispatcher_into_is_said_so(self, tmp_path: Path) -> None:
        [block] = isolation_parts(_reminder(inherited=True, state="merged"), workspace_root=tmp_path)

        assert "- 这段对话之前跟着派你来的任务在隔离副本 old-1 里做过（那个副本已经合回）" in block

    @pytest.mark.parametrize("state, label", [
        ("ready", "还有任务在用"),
        ("missing", "在登记里已经查不到"),
        ("strange", "现在用不了"),
        ("", "现在用不了"),
    ])
    def test_the_state_of_the_old_worktree_is_named(self, tmp_path: Path, state: str, label: str) -> None:
        [block] = isolation_parts(_reminder(state=state), workspace_root=tmp_path)

        assert f"（那个副本{label}）" in block

    @pytest.mark.parametrize("raw", [
        {"mode": NOT_ISOLATED_MODE},
        {"mode": NOT_ISOLATED_MODE, "previous": {}},
        {"mode": NOT_ISOLATED_MODE, "previous": {"worktree": "  "}},
        {"mode": NOT_ISOLATED_MODE, "previous": "old-1"},
    ])
    def test_nothing_is_said_without_an_old_worktree(self, raw: Any) -> None:
        assert isolation_parts(raw, workspace_root=Path(".")) == []

    def test_a_reminded_task_does_not_count_as_isolated(self) -> None:
        assert task_isolation({ISOLATION_CONTEXT_KEY: _reminder()}) == {}

    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_it_goes_where_the_notice_would(self, tmp_path: Path, mode: str) -> None:
        messages = _assemble(tmp_path, mode, isolation=_reminder())

        dynamic = _dynamic(messages)
        assert "你这次不在隔离副本里" in dynamic and "old-1" in dynamic
        assert not any("old-1" in str(message["content"]) for message in messages if not message.get("_dynamic"))


class TestHelpers:
    def test_only_a_real_notice_counts(self) -> None:
        notice = {"mode": "worktree", "worktree": "wt-1"}

        assert task_isolation(None) == {} and task_isolation({}) == {}
        assert task_isolation({ISOLATION_CONTEXT_KEY: {}}) == {}
        assert task_isolation({ISOLATION_CONTEXT_KEY: "worktree"}) == {}
        copied = task_isolation({ISOLATION_CONTEXT_KEY: notice})
        assert copied == notice and copied is not notice

    def test_the_nested_refusal_names_the_worktree(self) -> None:
        assert nested_isolation_refusal({"worktree": "wt-1"}) == (
            "没有派出：你已经在隔离副本 wt-1 里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派。"
        )
        assert nested_isolation_refusal({}) == (
            "没有派出：你已经在隔离副本里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派。"
        )


def _assemble(workspace: Path, mode: str, **kwargs: Any) -> list[dict[str, Any]]:
    messages, _ = assemble_messages_with_injections(
        workspace_root=workspace, system_prompt=[dict(message) for message in _PROMPTS[mode]],
        history=list(_HISTORY), instruction="now", skill_dynamic=[_SKILL_TURN], memory_dynamic=[_MEMORY_TURN],
        **kwargs,
    )
    return messages


def _interrupted(root: Path, info: dict[str, Any]) -> SimpleNamespace:
    """打断时的循环状态：上一轮的动态区还在，supervisor 送来一句新的话。"""
    history = [{"role": "user", "content": "old request"}]
    return SimpleNamespace(
        node=Node(id="coder", type="ai"), runtime_cfg={}, preempt_inject_info={"message": "follow-up"},
        rctx=SimpleNamespace(
            workspace_root=root, task_context={ISOLATION_CONTEXT_KEY: info}, session_id="branch",
            child_session_id="", task_id="task", conversation_store=ConversationStore(root / "data" / "conversations"),
            consume_preempt=AsyncMock(), emit_event=AsyncMock(),
        ),
        messages=[dict(_PERSONA), *history, {"role": "user", "content": "outdated turn", "_dynamic": True}],
        history=history, system_prompt=[_PERSONA, _NODE_TURN], is_block_mode=False,
    )


class TestAssembly:
    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_a_task_outside_a_worktree_is_assembled_as_before(self, tmp_path: Path, mode: str) -> None:
        plain = _assemble(tmp_path, mode)

        for raw in (None, {}, {"mode": "copy"}):
            assert _assemble(tmp_path, mode, isolation=raw) == plain
        assert ISOLATION_OPEN not in str(plain)

    def test_in_string_mode_it_follows_the_node_notes_and_precedes_skills_and_memory(self, tmp_path: Path) -> None:
        turn_block = _dynamic(_assemble(tmp_path, "string", isolation=_isolation(tmp_path)))

        assert (
            turn_block.index("node turn notes") < turn_block.index(ISOLATION_OPEN)
            < turn_block.index("[SKILL:ACTIVE]") < turn_block.index("[MEMORY:ACTIVE]")
        )

    def test_in_block_mode_it_opens_the_turn_block(self, tmp_path: Path) -> None:
        turn_block = _dynamic(_assemble(tmp_path, "block", isolation=_isolation(tmp_path)))

        assert turn_block.startswith(f"以下是本轮动态上下文，每轮可能变化。\n\n{ISOLATION_OPEN}\n")
        assert turn_block.index(ISOLATION_CLOSE) < turn_block.index("[SKILL:ACTIVE]")

    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_it_never_touches_the_cached_prefix(self, tmp_path: Path, mode: str) -> None:
        plain = _assemble(tmp_path, mode)
        isolated = _assemble(tmp_path, mode, isolation=_isolation(tmp_path))

        assert [message for message in isolated if not message.get("_dynamic")] == [
            message for message in plain if not message.get("_dynamic")
        ]

    def test_the_skeleton_carries_it_and_the_project_root(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path)
        node = Node(id="coder", type="ai", prompt="project={{project_root}}\ninstance={{workspace_root}}")

        messages, _, system_prompt = assemble_initial_messages(
            workspace_root=tmp_path, runtime_cfg={}, node=node, instruction="now", history=[],
            task_context={ISOLATION_CONTEXT_KEY: info}, project_root=Path(info["path"]),
        )

        assert system_prompt[0]["content"] == f"project={Path(info['path']).resolve()}\ninstance={tmp_path.resolve()}"
        assert info["path"] in _dynamic(messages)

    def test_the_knowledge_rebuild_keeps_it(self, tmp_path: Path) -> None:
        info = _isolation(tmp_path)
        ctx = SimpleNamespace(
            messages=[], rctx=SimpleNamespace(workspace_root=tmp_path, task_context={ISOLATION_CONTEXT_KEY: info}),
            extra={"system_prompt": [_PERSONA, _NODE_TURN], "history": list(_HISTORY), "instruction_text": "now"},
        )

        asyncio.run(_rebuild_prompt_messages(ctx))

        assert ISOLATION_OPEN in _dynamic(ctx.messages) and info["path"] in _dynamic(ctx.messages)

    def test_an_interruption_keeps_it(self, tmp_path: Path) -> None:
        loop = _interrupted(tmp_path, _isolation(tmp_path))

        asyncio.run(_inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop))

        turn_block = _dynamic(loop.messages)
        assert "outdated turn" not in turn_block
        assert turn_block.index("node turn notes") < turn_block.index(ISOLATION_OPEN)
        assert loop.messages[-1]["content"] == "follow-up"

    def test_the_model_sees_it_only_in_a_worktree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        info = _isolation(tmp_path)
        isolated = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}),
            task_context={ISOLATION_CONTEXT_KEY: info},
        )
        isolated.run(turn(call("finish", {"text": "ok"}, call_id="f")))
        plain_root = tmp_path / "plain"
        plain_root.mkdir()
        plain = NodeRun(monkeypatch, plain_root, registry=ScriptedRegistry(tools={"probe": echo()}))
        plain.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert ISOLATION_OPEN in _dynamic(isolated.requests[0]["messages"])
        assert info["path"] in _dynamic(isolated.requests[0]["messages"])
        assert ISOLATION_OPEN not in str(plain.requests[0]["messages"])


def _prompt_text(workspace: Path, node: Node, **options: Any) -> str:
    return "\n\n".join(
        message["content"] for message in assemble_prompt(workspace, node, **options)
        if isinstance(message.get("content"), str)
    )


class TestProjectRoot:
    def test_outside_a_worktree_it_is_the_workspace_root(self, tmp_path: Path) -> None:
        node = Node(id="n", type="ai", prompt="project={{project_root}}\ninstance={{workspace_root}}")

        [message] = assemble_prompt(tmp_path, node)

        root = tmp_path.resolve()
        assert message["content"] == f"project={root}\ninstance={root}"

    def test_in_a_worktree_only_the_project_root_moves(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        node = Node(id="n", type="ai", prompt="project={{project_root}}\ninstance={{workspace_root}}")

        [message] = assemble_prompt(tmp_path, node, project_root=worktree)

        assert message["content"] == f"project={worktree.resolve()}\ninstance={tmp_path.resolve()}"

    def test_block_prompts_render_it_too(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        node = Node(id="n", type="ai", prompt=[{"role": "system", "content": "project={{project_root}}"}, {"role": "history"}])

        blocks = assemble_prompt(tmp_path, node, project_root=worktree)

        assert blocks[0] == {"role": "system", "content": f"project={worktree.resolve()}"}

    def test_the_coder_works_in_the_worktree_and_delivers_to_the_instance_root(self, tmp_path: Path) -> None:
        coder = load_node(tmp_path, "bootstrap.coder")
        assert coder is not None
        worktree = tmp_path / "wt"
        worktree.mkdir()

        text = _prompt_text(tmp_path, coder, project_root=worktree)

        instance = tmp_path.resolve()
        assert f"Current Workspace: {worktree.resolve()}\n" in text
        assert f"Instance Root (config/ and data/ live here): {instance}\n" in text
        assert f"`{instance}/data/attachments/<name>.txt`" in text
        assert "{{" not in text

    def test_outside_a_worktree_the_coder_s_two_roots_are_the_same(self, tmp_path: Path) -> None:
        coder = load_node(tmp_path, "bootstrap.coder")
        assert coder is not None

        text = _prompt_text(tmp_path, coder)

        root = tmp_path.resolve()
        assert f"Current Workspace: {root}\nInstance Root (config/ and data/ live here): {root}\n" in text

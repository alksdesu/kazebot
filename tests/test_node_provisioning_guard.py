"""create_agent 与 dispatch 的授权回归。

create_agent 曾直接 write_text 落 config/nodes/*.yaml 并改调用者的 delegate_targets，
全程不问策略——而同样内容走 write_file 时，policy 对 config/nodes/** 是 approval_required，
对 QQ 非管理员更是硬 deny。它又在 qq.orchestrator 的白名单里，等于群里任何人
都能让 bot 落一份新的节点定义（含 prompt、模型、tool_access）并给自己扩委派图。

dispatch 则是反过来：给模型看的目标来自 node.delegate_targets，执行时却从一张
进程内全局的反查表拿目标，谁注册过都查得到，查不到还原样退回字符串——白名单
只管住了「看得见什么」，管不住「敢写什么」。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.agent_manage import create_agent  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_TEMPLATE = {
    "id": "tmpl",
    "type": "ai",
    "name": "模板",
    "tool_access": {"mode": "all"},
    "delegate_targets": [],
}


class _Ctx(ToolContext):
    """离线 ToolContext：记录送审的 op，按预设 safety_level 回应。"""

    def __init__(self, workspace_root: Path, *, safety: str = "deny") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            conversation_key="qq_group:abcdef",
            platform_auth={"platform": "qq", "user_id": "30003", "is_admin": False},
        )
        self._safety = safety
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self._safety, "reason": "qq non-admin users cannot modify files"}

    async def check_cancelled(self) -> bool:
        return False


def _seed(root: Path, *, caller: str | None = None) -> None:
    nodes = root / "config" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "tmpl.yaml").write_text(yaml.safe_dump(_TEMPLATE), encoding="utf-8")
    if caller:
        (nodes / f"{caller}.yaml").write_text(
            yaml.safe_dump({"id": caller, "type": "ai", "delegate_targets": ["existing.node"]}),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_denied_create_agent_writes_no_node_file(tmp_path: Path) -> None:
    _seed(tmp_path)
    ctx = _Ctx(tmp_path)

    result = await create_agent({"name": "pwn", "template": "tmpl"}, ctx)

    assert result["ok"] is False
    assert not (tmp_path / "config" / "nodes" / "pwn.yaml").exists()


@pytest.mark.asyncio
async def test_create_agent_reports_write_file_on_the_node_path(tmp_path: Path) -> None:
    # 报的路径要是 config/nodes/**，policy 才匹配得到那条 approval_required。
    _seed(tmp_path)
    ctx = _Ctx(tmp_path)

    await create_agent({"name": "pwn", "template": "tmpl"}, ctx)

    assert len(ctx.requested) == 1
    op, params = ctx.requested[0]
    assert op == "write_file"
    assert params["path"] == "config/nodes/pwn.yaml"


@pytest.mark.asyncio
async def test_allowed_create_agent_still_works(tmp_path: Path) -> None:
    _seed(tmp_path)
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is True
    written = yaml.safe_load((tmp_path / "config" / "nodes" / "helper.yaml").read_text(encoding="utf-8"))
    assert written["id"] == "helper"


@pytest.mark.asyncio
async def test_denied_caller_update_leaves_delegate_targets_alone(tmp_path: Path) -> None:
    """节点建得成、扩委派图不一定。两次写入各自送审，不能一次放行管两件事。"""
    _seed(tmp_path, caller="boss")
    ctx = _Ctx(tmp_path, safety="auto")

    # 第一次（建节点）放行，第二次（改 boss 的委派清单）拒绝。
    original = ctx.request_op

    async def _deny_second(op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        out = await original(op, parameters)
        if len(ctx.requested) >= 2:
            return {"safety_level": "deny", "reason": "delegate graph change denied"}
        return out

    ctx.request_op = _deny_second  # type: ignore[method-assign]

    result = await create_agent(
        {"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx,
    )

    assert result["ok"] is True
    assert result["data"]["caller_updated"] is False
    assert result["data"]["caller_denied"]
    boss = yaml.safe_load((tmp_path / "config" / "nodes" / "boss.yaml").read_text(encoding="utf-8"))
    assert boss["delegate_targets"] == ["existing.node"]


@pytest.mark.asyncio
async def test_caller_update_is_a_separate_guarded_write(tmp_path: Path) -> None:
    _seed(tmp_path, caller="boss")
    ctx = _Ctx(tmp_path, safety="auto")

    await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert [op for op, _ in ctx.requested] == ["write_file", "write_file"]
    assert ctx.requested[1][1]["path"] == "config/nodes/boss.yaml"
    assert ctx.requested[1][1]["delegate_target_added"] == "helper"
    boss = yaml.safe_load((tmp_path / "config" / "nodes" / "boss.yaml").read_text(encoding="utf-8"))
    assert boss["delegate_targets"] == ["existing.node", "helper"]


def _write(root: Path, folder: str, filename: str, data: dict[str, Any]) -> Path:
    path = root / folder / f"{filename}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(("folder", "filename", "aliases"), [
    ("engine/system_nodes", "helper", []),
    ("config/nodes", "helper.v2", ["helper"]),
])
async def test_a_name_that_already_resolves_to_a_node_is_refused(
    tmp_path: Path, folder: str, filename: str, aliases: list[str],
) -> None:
    # 同名系统节点会顶掉新文件；被别的节点声明成旧 id 的名字，建了还会把写着旧 id 的会话引过来。
    _seed(tmp_path)
    _write(tmp_path, folder, filename, {"id": filename, "type": "ai", "aliases": aliases})
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is False
    assert f"{folder}/{filename}.yaml" in result["error"]
    assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()
    assert ctx.requested == []


@pytest.mark.asyncio
async def test_a_template_outside_the_node_folders_is_never_read(tmp_path: Path) -> None:
    # 模板名是模型给的参数：拼路径时不能借 ../ 把附件里的 yaml 当模板抄进新节点。
    _seed(tmp_path)
    _write(tmp_path, "data/attachments", "evil", {"type": "ai", "tool_access": {"mode": "all"}})
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "../../data/attachments/evil"}, ctx)

    assert result["ok"] is False
    assert "template not found" in result["error"]
    assert ctx.requested == []


@pytest.mark.asyncio
async def test_the_caller_named_by_its_old_id_is_updated_in_the_file_that_defines_it(tmp_path: Path) -> None:
    _seed(tmp_path)
    boss = _write(tmp_path, "config/nodes", "boss.v2", {"id": "boss.v2", "type": "ai", "aliases": ["boss"]})
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["data"]["caller_updated"] is True
    assert ctx.requested[1][1]["path"] == "config/nodes/boss.v2.yaml"
    assert yaml.safe_load(boss.read_text(encoding="utf-8"))["delegate_targets"] == ["helper"]
    assert not (tmp_path / "config" / "nodes" / "boss.yaml").exists()

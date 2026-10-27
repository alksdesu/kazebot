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

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _approval_grant_fixture import events_path, logged_events, make_state, qq_auth, start_task  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
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
    assert result["data"]["caller_denied"] == "delegate graph change denied"
    assert result["data"]["caller_error"] == ""
    # 只看 data.result 的模型也得知道派不到新节点
    assert "delegate graph change denied" in result["data"]["result"]
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
async def test_a_broken_template_is_reported_by_place_not_by_its_line(tmp_path: Path) -> None:
    # 节点能单配 api_key，模板坏的那一行可能就是它；原因原样交给模型。
    _seed(tmp_path)
    (tmp_path / "config" / "nodes" / "tmpl.yaml").write_text(
        "id: tmpl\ntype: ai\napi_key: sk-SECRET-template: x\n", encoding="utf-8",
    )
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is False
    assert result["error"] == (
        "failed to read template config/nodes/tmpl.yaml: YAML 读不出来：第 3 行第 28 列：mapping values are not allowed here"
    )
    assert "sk-SECRET" not in result["data"]["result"]
    assert ctx.requested == []
    assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()


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


# 节点能单配 api_key、base_url 里能夹账号密码；模型读不到这些文件，审批 details 却会进事件流、控制台和调试工具。
_SECRET_TEMPLATE = {
    "id": "tmpl",
    "type": "ai",
    "api_key": "sk-TEMPLATESECRET0001",
    "base_url": "https://u:hunter2@r.example/v1",
    "fallback": {"api_key": "sk-NESTEDSECRET00003"},
    "max_tokens": 4096,
    "prompt": "示例 sk-PROMPTSECRET00002",
}
_TEMPLATE_SECRETS = ("TEMPLATESECRET", "NESTEDSECRET", "PROMPTSECRET", "hunter2")


@pytest.mark.asyncio
async def test_the_node_preview_hides_the_template_secrets(tmp_path: Path) -> None:
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", _SECRET_TEMPLATE)
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is True
    preview = ctx.requested[0][1]["content_preview"]
    for secret in _TEMPLATE_SECRETS:
        assert secret not in preview
    assert "api_key: '[已隐藏]'" in preview
    assert "fallback:\n  api_key: '[已隐藏]'" in preview
    assert "base_url: https://[已隐藏]@r.example/v1" in preview
    # 键还在、max_tokens 这种上限照给：审批人看得出配了什么
    assert "max_tokens: 4096" in preview
    assert "prompt: 示例 ******0002" in preview


@pytest.mark.asyncio
async def test_the_new_node_keeps_the_real_values(tmp_path: Path) -> None:
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", _SECRET_TEMPLATE)
    ctx = _Ctx(tmp_path, safety="auto")

    await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    written = yaml.safe_load((tmp_path / "config" / "nodes" / "helper.yaml").read_text(encoding="utf-8"))
    assert written["api_key"] == "sk-TEMPLATESECRET0001"
    assert written["fallback"] == {"api_key": "sk-NESTEDSECRET00003"}
    assert written["base_url"] == "https://u:hunter2@r.example/v1"
    assert written["prompt"] == "示例 sk-PROMPTSECRET00002"
    # content_len 报的是落盘那份的长度，不是打过码的预览
    real = yaml.safe_dump(written, default_flow_style=False, allow_unicode=True)
    assert ctx.requested[0][1]["content_len"] == len(real)


@pytest.mark.asyncio
async def test_an_empty_key_stays_visible_in_the_preview(tmp_path: Path) -> None:
    # 空着的照实给审批人看，分得清是没配还是藏起来了。
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", {"id": "tmpl", "type": "ai", "api_key": ""})
    ctx = _Ctx(tmp_path, safety="auto")

    await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert "api_key: ''" in ctx.requested[0][1]["content_preview"]


@pytest.mark.asyncio
async def test_a_numeric_key_or_password_is_hidden_in_the_preview(tmp_path: Path) -> None:
    # 纯数字的 key 和口令在 YAML 里读出来是数，按字符串认会原样进预览。
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", {
        "id": "tmpl", "type": "ai", "api_key": 31415926, "db_password": 2718.28, "max_tokens": 4096,
    })
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is True
    preview = ctx.requested[0][1]["content_preview"]
    assert "31415926" not in preview
    assert "2718.28" not in preview
    assert "api_key: '[已隐藏]'" in preview
    assert "db_password: '[已隐藏]'" in preview
    assert "max_tokens: 4096" in preview
    written = yaml.safe_load((tmp_path / "config" / "nodes" / "helper.yaml").read_text(encoding="utf-8"))
    assert (written["api_key"], written["db_password"]) == (31415926, 2718.28)


@pytest.mark.asyncio
async def test_the_details_name_the_template_file(tmp_path: Path) -> None:
    # 预览打了码，审批人靠这一项去对照抄的是哪份文件。
    _seed(tmp_path)
    ctx = _Ctx(tmp_path, safety="auto")

    await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    params = ctx.requested[0][1]
    assert params["template"] == "config/nodes/tmpl.yaml"
    assert params["create_agent"] is True
    assert params["memory_book"] == "helper"


@pytest.mark.asyncio
@pytest.mark.parametrize(("definition", "said"), [
    ({"id": "tmpl", "kind": "skill"}, "kind 写的是 skill，节点只认 node"),
    ({"id": "tmpl", "type": "bogus"}, "type 写的是 bogus，只认 ai、tool"),
])
async def test_a_template_that_is_not_a_usable_node_is_refused(
    tmp_path: Path, definition: dict[str, Any], said: str,
) -> None:
    # 照着抄出来的节点一样加载不了，落盘只会多一份坏文件，还白白问一次审批。
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", definition)
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is False
    assert result["error"] == f"template config/nodes/tmpl.yaml is not a usable node definition: {said}"
    assert ctx.requested == []
    assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()


@pytest.mark.asyncio
async def test_the_caller_preview_hides_its_secrets_too(tmp_path: Path) -> None:
    _seed(tmp_path)
    boss = _write(tmp_path, "config/nodes", "boss", {
        "id": "boss", "type": "ai", "api_key": "sk-BOSSSECRET000004", "delegate_targets": ["existing.node"],
    })
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["data"]["caller_updated"] is True
    preview = ctx.requested[1][1]["content_preview"]
    assert "BOSSSECRET" not in preview
    assert "api_key: '[已隐藏]'" in preview
    assert "- helper" in preview
    written = yaml.safe_load(boss.read_text(encoding="utf-8"))
    assert written["api_key"] == "sk-BOSSSECRET000004"
    assert written["delegate_targets"] == ["existing.node", "helper"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "said"), [
    ("id: boss\ntype: ai\napi_key: sk-BOSSSECRET000004: x\n", "：YAML 读不出来：第 3 行"),
    ("", "：文件是空的"),
    ("- existing.node\n", "：顶层不是 key: value 的映射"),
    ("id: boss\nkind: skill\n", "不是能用的节点定义：kind 写的是 skill"),
    ("id: boss\ntype: ai\ndelegate_targets: existing.node\n", "delegate_targets 不是列表"),
    ("id: boss\ntype: ai\ndelegate_targets:\n  first: existing.node\n", "delegate_targets 不是列表"),
])
async def test_a_caller_that_cannot_be_updated_says_why(tmp_path: Path, text: str, said: str) -> None:
    # 新节点已经落盘，调用方改不成要照实说；原因只带位置和问题，坏掉的那一行可能就是密钥。
    _seed(tmp_path)
    boss = tmp_path / "config" / "nodes" / "boss.yaml"
    boss.write_text(text, encoding="utf-8")
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["ok"] is True
    assert (tmp_path / "config" / "nodes" / "helper.yaml").exists()
    data = result["data"]
    assert data["caller_updated"] is False
    assert data["caller_denied"] == ""
    assert data["caller_error"].startswith("调用方节点 boss（config/nodes/boss.yaml）")
    assert said in data["caller_error"]
    assert said in data["result"]
    assert "BOSSSECRET" not in json.dumps(result, ensure_ascii=False)
    # 一处都改不了，就不该再拿它去问一次审批
    assert len(ctx.requested) == 1
    assert boss.read_text(encoding="utf-8") == text


@pytest.mark.asyncio
async def test_a_caller_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    _seed(tmp_path)
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "ghost"}, ctx)

    assert result["ok"] is True
    assert result["data"]["caller_updated"] is False
    assert result["data"]["caller_error"] == "节点未找到：ghost"
    assert "节点未找到：ghost" in result["data"]["result"]
    assert len(ctx.requested) == 1


@pytest.mark.asyncio
async def test_a_caller_that_already_lists_the_node_is_left_alone(tmp_path: Path) -> None:
    _seed(tmp_path)
    boss = _write(tmp_path, "config/nodes", "boss", {"id": "boss", "type": "ai", "delegate_targets": ["helper"]})
    before = boss.read_bytes()
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["data"]["caller_updated"] is True
    assert result["data"]["caller_error"] == ""
    assert "boss already lists it in delegate_targets" in result["data"]["result"]
    assert len(ctx.requested) == 1
    assert boss.read_bytes() == before


@pytest.mark.asyncio
async def test_a_caller_file_that_cannot_be_written_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path, caller="boss")
    write = config_layout.write_config_text

    def refuse_boss(path: Path, content: str, **kwargs: Any) -> str:
        if Path(path).name == "boss.yaml":
            raise PermissionError(13, "Permission denied")
        return write(path, content, **kwargs)

    monkeypatch.setattr(config_layout, "write_config_text", refuse_boss)
    ctx = _Ctx(tmp_path, safety="auto")

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["ok"] is True
    assert result["data"]["caller_updated"] is False
    assert result["data"]["caller_error"] == "config/nodes/boss.yaml 写不进去：Permission denied"


@pytest.mark.asyncio
async def test_a_caller_edited_while_waiting_for_approval_is_not_overwritten(tmp_path: Path) -> None:
    # 送审可能等上好几分钟，这期间在控制台改的那一版不能被整份盖掉。
    _seed(tmp_path, caller="boss")
    boss = tmp_path / "config" / "nodes" / "boss.yaml"
    edited = yaml.safe_dump({"id": "boss", "type": "ai", "delegate_targets": ["existing.node", "manual.edit"]})
    ctx = _Ctx(tmp_path, safety="auto")
    original = ctx.request_op

    async def edit_while_waiting(op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        out = await original(op, parameters)
        if parameters.get("delegate_target_added"):
            boss.write_text(edited, encoding="utf-8")
        return out

    ctx.request_op = edit_while_waiting  # type: ignore[method-assign]

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["ok"] is True
    assert result["data"]["caller_updated"] is False
    assert result["data"]["caller_error"] == "config/nodes/boss.yaml 在等审批的时候被改过，没有写，免得盖掉那次改动"
    assert boss.read_text(encoding="utf-8") == edited


@pytest.mark.asyncio
async def test_a_failed_policy_check_on_the_caller_does_not_hide_the_new_node(tmp_path: Path) -> None:
    # 新节点已经落盘：异常往外抛的话模型以为整个没建成，再建一次只会撞上 id 已被占用。
    _seed(tmp_path, caller="boss")
    before = (tmp_path / "config" / "nodes" / "boss.yaml").read_bytes()
    ctx = _Ctx(tmp_path, safety="auto")
    original = ctx.request_op

    async def unreachable_on_the_caller(op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if parameters.get("delegate_target_added"):
            raise RuntimeError("http://127.0.0.1:8765/v1/ops/request unreachable")
        return await original(op, parameters)

    ctx.request_op = unreachable_on_the_caller  # type: ignore[method-assign]

    result = await create_agent({"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx)

    assert result["ok"] is True
    assert (tmp_path / "config" / "nodes" / "helper.yaml").exists()
    assert result["data"]["caller_updated"] is False
    # 没有 supervisor 写的原因时只报异常类型，异常文本里的内部地址不交给模型
    assert result["data"]["caller_error"] == "config/nodes/boss.yaml 送审没走完：RuntimeError"
    assert (tmp_path / "config" / "nodes" / "boss.yaml").read_bytes() == before


class _SupervisorCtx(_Ctx):
    """送审走真的 SupervisorState：策略判定、审批记录和事件流都是真的，审批人一律驳回。"""

    def __init__(self, workspace_root: Path, state: SupervisorState, session_id: str, task_id: str) -> None:
        super().__init__(workspace_root)
        self.platform_auth = qq_auth()
        self._state = state
        self._sid = session_id
        self._task_id = task_id
        self.approvals: list[str] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        out = self._state.request_operation(
            session_id=self._sid,
            op=op,
            parameters={**parameters, "platform_auth": dict(self.platform_auth or {})},
            task_id=self._task_id,
        )
        if out.approval_id:
            self.approvals.append(out.approval_id)
        return {"safety_level": out.safety_level.value, "reason": out.reason, "approval_id": out.approval_id}

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        return {"status": "denied"}


@pytest.mark.asyncio
async def test_the_secret_never_reaches_the_approval_or_the_event_log(tmp_path: Path) -> None:
    # config/nodes/** 对管理员也要审批：details 原样存进审批记录、写进 events.jsonl，控制台和调试工具都从这两处读。
    state = make_state(tmp_path)
    sid, task = start_task(state, auth=qq_auth())
    _seed(tmp_path)
    _write(tmp_path, "config/nodes", "tmpl", _SECRET_TEMPLATE)
    ctx = _SupervisorCtx(tmp_path, state, sid, task.task_id)

    result = await create_agent({"name": "helper", "template": "tmpl"}, ctx)

    assert result["ok"] is False
    assert not (tmp_path / "config" / "nodes" / "helper.yaml").exists()
    assert len(ctx.approvals) == 1
    details = state.approvals[ctx.approvals[0]].details
    assert details["template"] == "config/nodes/tmpl.yaml"
    assert "api_key: '[已隐藏]'" in details["content_preview"]
    requested = logged_events(tmp_path, "approval_requested")
    assert [row["payload"]["approval_id"] for row in requested] == ctx.approvals
    logged = events_path(tmp_path).read_text(encoding="utf-8")
    for secret in _TEMPLATE_SECRETS:
        assert secret not in json.dumps(details, ensure_ascii=False)
        assert secret not in logged

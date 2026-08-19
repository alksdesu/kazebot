"""非管理员读工作区文件的兜底。

「服务端策略」那一页能把任意路径点成自动放行。没有这一层的话，群友诱导模型
read_file 就能读走 .env 里的密钥、源码里的鉴权逻辑、config 里的管理员 QQ 号。
逐条列敏感前缀补不全，所以这里一律拒绝。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState

ALLOW_EVERYTHING = {
    "version": 1,
    "extra_roots": [],
    "read_file": {"default": "auto", "rules": []},
    "write_file": {"default": "auto", "rules": []},
    "execute_command": {
        "default": "auto", "deny_patterns": [],
        "sensitive_patterns": [], "sensitive_path_patterns": [],
    },
    "restart": {"default": "auto"},
}


def _state(workspace: Path) -> SupervisorState:
    """策略全部放行，模拟管理员在控制台把档位都点成「自动放行」。"""
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    (workspace / "data" / "policy.yaml").write_text(
        yaml.safe_dump(ALLOW_EVERYTHING, sort_keys=False), encoding="utf-8",
    )
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-read-guard"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _read(state: SupervisorState, path: str, *, is_admin: bool):
    sid = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:1")
    return state.request_operation(
        session_id=sid,
        op="read_file",
        parameters={"path": path, "platform_auth": {"platform": "qq", "is_admin": is_admin}},
    )


LEAKY_PATHS = [
    ".env",                       # 全部密钥
    ".env.bak.1787167784",        # 备份同样是密钥
    "config/qq.yaml",             # 管理员 QQ 号与群号
    "config/runtime.yaml",
    "supervisor/state.py",        # 鉴权逻辑本身，读了就知道往哪绕
    "tools/remote_exec.py",       # 工具脚本里的基础设施细节
    "engine/system_nodes/system.dream.yaml",
    "data/config.yaml",
    "README.md",                  # 连普通文件也不给：白名单比黑名单可靠
]


@pytest.mark.parametrize("path", LEAKY_PATHS)
def test_a_group_member_cannot_read_anything_even_when_policy_allows_all(tmp_path, path):
    assert _read(_state(tmp_path), path, is_admin=False).safety_level.value == "deny"


@pytest.mark.parametrize("path", [".env", "supervisor/state.py", "README.md"])
def test_an_admin_is_governed_by_policy_alone(tmp_path, path):
    """管理员不受这层限制：收紧与否由他自己在策略页决定。"""
    assert _read(_state(tmp_path), path, is_admin=True).safety_level.value == "auto"


def test_the_permissive_policy_really_is_permissive(tmp_path):
    """反证：同一份策略下管理员读得到，所以上面的 deny 来自鉴权层而非策略。"""
    state = _state(tmp_path)

    assert _read(state, ".env", is_admin=True).safety_level.value == "auto"
    assert _read(state, ".env", is_admin=False).safety_level.value == "deny"


def test_non_qq_channels_are_not_affected(tmp_path):
    """这条规则只针对 QQ：Web 控制台与 CLI 有各自的入口鉴权。"""
    state = _state(tmp_path)
    sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")

    out = state.request_operation(
        session_id=sid, op="read_file", parameters={"path": ".env"},
    )

    assert out.safety_level.value == "auto"

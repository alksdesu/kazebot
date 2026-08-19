"""插件侧权限接线检查：管理员命令门，以及鉴权相对发送动作的位置。

顺序无法用行为测试表达（拒绝路径永远看不到发送），所以用 AST 静态检查兜住，
防止未来重构把门挪到消息已经发出去之后。
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_PLUGIN = _ROOT / "adapters" / "onebot" / "__init__.py"
_SOURCE = _PLUGIN.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

_SEND_CALLS = ("_send_text_and_attachments", "_send_attachments", "_send_forward_nodes")

_ADMIN_QQ = 10001
_MEMBER_QQ = 30003


def _function(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _call_lines(scope: ast.AST, callee: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if target == callee:
            lines.append(node.lineno)
    return lines


def test_cross_session_gate_precedes_every_send() -> None:
    scope = _function("_forward_bridge_execute")
    gate_lines = _call_lines(scope, "forward_delivery_deny_reason")
    send_lines = [line for callee in _SEND_CALLS for line in _call_lines(scope, callee)]

    assert gate_lines, "cross-session gate is missing from _forward_bridge_execute"
    assert send_lines, "expected _forward_bridge_execute to still perform sends"
    assert min(gate_lines) < min(send_lines)


def test_file_paths_gate_precedes_file_resolution() -> None:
    """非管理员的显式路径要在解析落盘路径前就拒，否则错误信息会泄露文件是否存在。"""
    scope = _function("_forward_bridge_execute")
    gate_lines = _call_lines(scope, "forward_file_paths_deny_reason")
    resolve_lines = _call_lines(scope, "_forward_bridge_resolve_files")

    assert gate_lines and resolve_lines
    assert min(gate_lines) < min(resolve_lines)


def test_secret_blacklist_runs_inside_file_resolution() -> None:
    assert _call_lines(_function("_forward_bridge_resolve_files"), "forward_file_deny_reason")


def _platform_updates_literals() -> list[ast.Dict]:
    found: list[ast.Dict] = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(t, ast.Name) and t.id == "platform_updates" for t in node.targets):
            found.append(node.value)
    return found


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def test_every_platform_updates_registers_trigger_user() -> None:
    """会话登记缺 user_id 就反查不到发起人，鉴权会按会话类型出现盲区。"""
    literals = _platform_updates_literals()

    assert literals
    for literal in literals:
        assert "user_id" in _dict_keys(literal)


class _StubPresetNotFound(ValueError):
    def __init__(self, preset_ref: str, available: list[str]) -> None:
        self.preset_ref = preset_ref
        self.available = list(available)
        super().__init__(f"没找到画师串预设“{preset_ref}”")


def _drawtools_reply(
    runtime,
    monkeypatch: pytest.MonkeyPatch,
    user_id: int,
    text: str,
    *,
    unknown_preset: str | None = None,
) -> str | None:
    switched: list[str] = []
    available = ["可爱风（id: cute）"]

    def _switch(ref: str) -> dict:
        if unknown_preset is not None and ref == unknown_preset:
            raise _StubPresetNotFound(ref, available)
        switched.append(ref)
        return {"id": "cute", "name": "可爱风"}

    monkeypatch.setattr(
        runtime, "_load_drawtools_preset_manager",
        lambda: (
            lambda: [{"id": "cute", "name": "可爱风", "model": "nai", "scale": 5, "steps": 28}],
            _switch,
            _StubPresetNotFound,
        ),
    )
    reply = asyncio.run(
        runtime._maybe_handle_drawtools_command(
            event=SimpleNamespace(user_id=user_id), user_text=text,
        )
    )
    return None if reply is None else f"{reply}\x00{','.join(switched)}"


def test_member_cannot_switch_draw_preset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """切换预设写全局 settings.yaml，改掉所有人的默认生图风格。"""
    runtime = load_runtime(monkeypatch, tmp_path)

    reply = _drawtools_reply(runtime, monkeypatch, _MEMBER_QQ, "/切换画师串 可爱风")

    assert reply is not None
    body, switched = reply.split("\x00")
    assert "管理员" in body
    assert switched == "", "preset must not be switched for a non-admin"


def test_admin_can_switch_draw_preset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))

    reply = _drawtools_reply(runtime, monkeypatch, _ADMIN_QQ, "/切换画师串 可爱风")

    assert reply is not None
    body, switched = reply.split("\x00")
    assert "已切换" in body
    assert switched == "可爱风"


@pytest.mark.parametrize("text", ["/画师串列表", "/生图帮助"])
def test_read_only_draw_commands_stay_open_to_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str,
) -> None:
    """只读命令不该被一起收紧，否则普通成员看不到可用预设。"""
    runtime = load_runtime(monkeypatch, tmp_path)

    reply = _drawtools_reply(runtime, monkeypatch, _MEMBER_QQ, text)

    assert reply is not None
    assert "管理员" not in reply.split("\x00")[0]


def test_admin_switch_reports_unknown_preset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """写错预设名必须硬失败并列出可用项，不能谎报“已切换”。"""
    runtime = load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))

    reply = _drawtools_reply(
        runtime, monkeypatch, _ADMIN_QQ, "/切换画师串 不存在的串", unknown_preset="不存在的串",
    )

    assert reply is not None
    body, switched = reply.split("\x00")
    assert "没找到" in body
    assert "可爱风" in body
    assert switched == "", "unknown preset must not be recorded as switched"

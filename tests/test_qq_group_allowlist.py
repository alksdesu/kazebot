"""群白名单在入站与出站上的一致性回归测试。

入站 _is_group_allowed 是 fail-closed（空名单 = 不响应任何群），出站曾各自写成
`if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS`，空名单时反而放行任意群 ——
而空名单正是未配置时的默认状态（.env.example 是占位符，非数字项被静默丢弃）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_IN_LIST = 111
_NOT_IN_LIST = 222
_SESSION = 'sess-1'
_ADMIN = 10001


class _Bot:
    self_id = '42'

    async def call_api(self, api: str, **kwargs: Any):
        if api == 'get_group_list':
            return [{'group_id': _IN_LIST, 'group_name': 'A'}, {'group_id': _NOT_IN_LIST, 'group_name': 'B'}]
        return []


def _runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, groups: frozenset[int]):
    runtime = load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, allowed_groups=groups, admin_users=frozenset({_ADMIN}))
    return runtime


# --- 入站：本来就是 fail-closed，钉住它别被改坏 ---


def test_inbound_rejects_when_allowlist_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset())

    assert runtime._is_group_allowed(_IN_LIST) is False


def test_inbound_accepts_listed_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    assert runtime._is_group_allowed(_IN_LIST) is True
    assert runtime._is_group_allowed(_NOT_IN_LIST) is False


@pytest.mark.parametrize('gid', ['111', 111.0])
def test_inbound_coerces_numeric_forms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gid: Any) -> None:
    """OneBot 事件里 group_id 有时是字符串，不能因此判成不允许。"""
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    assert runtime._is_group_allowed(gid) is True


@pytest.mark.parametrize('gid', [None, '', 'abc'])
def test_inbound_rejects_unparsable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gid: Any) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    assert runtime._is_group_allowed(gid) is False


# --- 出站：修复前空名单会放行任意群 ---


def test_outbound_candidates_empty_when_allowlist_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """空名单时主动群聊候选必须为空，而不是 Bot 加入的所有群。"""
    runtime = _runtime(monkeypatch, tmp_path, frozenset())

    candidates = asyncio.run(runtime._group_target_candidates(_Bot()))

    assert candidates == []


def test_outbound_candidates_only_listed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    candidates = asyncio.run(runtime._group_target_candidates(_Bot()))

    assert [c.target_id for c in candidates] == [_IN_LIST]


def test_outbound_explicit_group_id_denied_when_allowlist_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """管理员写「发到群 222」，空名单时必须拒绝。"""
    runtime = _runtime(monkeypatch, tmp_path, frozenset())

    target, err = asyncio.run(
        runtime._resolve_proactive_target(
            _Bot(), runtime.capability.Requester(user_id=_ADMIN, listed_admin=True),
            'group', f'群:{_NOT_IN_LIST}', capability_key="proactive",
        )
    )

    assert target is None
    assert err


def test_outbound_explicit_group_id_allowed_when_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    target, err = asyncio.run(
        runtime._resolve_proactive_target(
            _Bot(), runtime.capability.Requester(user_id=_ADMIN, listed_admin=True),
            'group', f'群:{_IN_LIST}', capability_key="proactive",
        )
    )

    assert err == ''
    assert target is not None and target.target_id == _IN_LIST


def test_outbound_unlisted_group_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _runtime(monkeypatch, tmp_path, frozenset({_IN_LIST}))

    target, err = asyncio.run(
        runtime._resolve_proactive_target(
            _Bot(), runtime.capability.Requester(user_id=_ADMIN, listed_admin=True),
            'group', f'群:{_NOT_IN_LIST}', capability_key="proactive",
        )
    )

    assert target is None
    assert err


def test_forward_bridge_current_group_denied_when_allowlist_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """qq_forward 的「发到本群」分支同样不能在空名单时放行。"""
    runtime = _runtime(monkeypatch, tmp_path, frozenset())
    monkeypatch.setitem(
        runtime._session_targets, _SESSION,
        {'type': 'group', 'group_id': _NOT_IN_LIST, 'user_id': _ADMIN},
    )

    target, err = asyncio.run(
        runtime._forward_bridge_resolve_target(_Bot(), _SESSION, 'current', '')
    )

    assert target is None
    assert err


# --- 一致性：同一个群号，入站与出站结论必须相同 ---


@pytest.mark.parametrize('groups', [frozenset(), frozenset({_IN_LIST}), frozenset({_IN_LIST, _NOT_IN_LIST})])
def test_inbound_and_outbound_agree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, groups: frozenset) -> None:
    runtime = _runtime(monkeypatch, tmp_path, groups)

    for gid in (_IN_LIST, _NOT_IN_LIST):
        inbound = runtime._is_group_allowed(gid)
        target, _err = asyncio.run(
            runtime._resolve_proactive_target(
                _Bot(), runtime.capability.Requester(user_id=_ADMIN, listed_admin=True),
                'group', f'群:{gid}', capability_key="proactive",
            )
        )
        assert inbound is (target is not None), f'group {gid} disagrees with allowlist {sorted(groups)}'

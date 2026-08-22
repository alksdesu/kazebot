"""工作区根解析的契约：不设 env 必须与解耦前逐字相同。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from workspace import resolve_workspace_root  # noqa: E402

_ENV_NAMES = ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_falls_back_to_caller_default() -> None:
    assert resolve_workspace_root(_ROOT) == _ROOT


def test_fallback_is_resolved(tmp_path: Path) -> None:
    nested = tmp_path / "a" / ".." / "b"
    (tmp_path / "b").mkdir(parents=True)
    assert resolve_workspace_root(nested) == (tmp_path / "b").resolve()


def test_env_overrides_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    assert resolve_workspace_root(_ROOT) == tmp_path.resolve()


def test_legacy_alias_still_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(tmp_path))
    assert resolve_workspace_root(_ROOT) == tmp_path.resolve()


def test_primary_name_wins_over_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path / "primary"))
    monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(tmp_path / "alias"))
    assert resolve_workspace_root(_ROOT) == (tmp_path / "primary").resolve()


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_is_not_a_workspace(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # 空值当成没设：否则 .env 里留一行空的 CLONOTH_WORKSPACE= 就把根挪到了进程 cwd。
    monkeypatch.setenv("CLONOTH_WORKSPACE", blank)
    assert resolve_workspace_root(_ROOT) == _ROOT


def test_user_home_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_WORKSPACE", "~/clonoth-ws")
    resolved = resolve_workspace_root(_ROOT)
    assert "~" not in str(resolved)
    assert resolved == (Path.home() / "clonoth-ws").resolve()


def test_every_caller_agrees_on_one_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """各调用方的回落值不同，设了 env 之后必须收敛到同一个根。

    值不一致时附件、记忆、admin token 会静默落到两个地方。
    """
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    fallbacks = [
        _ROOT,
        _ROOT / "engine",
        _ROOT / "tools" / "drawtools",
        Path("relative/somewhere"),
    ]
    assert {resolve_workspace_root(f) for f in fallbacks} == {tmp_path.resolve()}

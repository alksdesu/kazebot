"""QQ 行为快照逐项比对：拆平台的每一步都不能让 QQ 这一侧的可观察行为漂移。

快照在多平台改造之前录制；有意改行为时设 QQ_BEHAVIOR_SNAPSHOT_RECORD=<路径> 重录再人工核对差异。
"""
from __future__ import annotations

import json
import os
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

from _qq_snapshot import SECTIONS, NapcatLayout, record_section  # noqa: E402

_SNAPSHOT = _TESTS / "snapshots" / "qq_behavior.json"
_RECORD_TO = os.environ.get("QQ_BEHAVIOR_SNAPSHOT_RECORD", "").strip()


def _differences(expected: Any, actual: Any, path: str) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        found: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                found.append(f"{path}.{key}: 快照里有，现在没有")
            elif key not in expected:
                found.append(f"{path}.{key}: 快照里没有，现在多出 {actual[key]!r}")
            else:
                found.extend(_differences(expected[key], actual[key], f"{path}.{key}"))
        return found
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        found = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            found.extend(_differences(left, right, f"{path}[{index}]"))
        return found
    return [] if expected == actual else [f"{path}: 快照 {expected!r}，现在 {actual!r}"]


def _record(section: str, recorded: Any) -> None:
    target = Path(_RECORD_TO)
    document = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    document[section] = recorded
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize("section", SECTIONS)
def test_qq_behavior_matches_the_snapshot(section: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    actual = json.loads(json.dumps(record_section(section, monkeypatch, tmp_path, NapcatLayout), ensure_ascii=False))
    if _RECORD_TO:
        _record(section, actual)
        return

    expected = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))[section]

    assert _differences(expected, actual, section) == []

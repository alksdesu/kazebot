"""#19 匿名映射：单次备选正则性能修复 + 可选 TTL 回收。

单次正则彻底解决逐条 re.sub 在超过 512 条编译缓存后每次重编译的问题；缓存必须在新登记
时失效。TTL 回收默认关闭（全量表是掩码与反解的正确性依赖），开启时必须在 next 计数器
恢复之后剪枝，否则别名会被复用、历史记忆里同一个人换别名。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime  # noqa: E402


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return load_runtime(monkeypatch, tmp_path)


def _load_with_retention(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, days: float) -> Any:
    # config.py 在 import 期读环境变量，保留期必须在 load_runtime 之前设好。
    monkeypatch.setenv("ONEBOT_ANON_MAP_RETENTION_DAYS", str(days))
    return load_runtime(monkeypatch, tmp_path)


def _write_map(runtime: Any, payload: dict[str, Any]) -> Path:
    path = Path(runtime.ANON_MAP_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---- A: 单次备选正则 ----

def test_empty_map_leaves_text_untouched(runtime: Any) -> None:
    assert runtime._anonymize_text_for_ai("12345 元") == "12345 元"


def test_all_known_ids_replaced_and_cache_invalidates(runtime: Any) -> None:
    u1 = runtime._anonymize_user_id("100001")
    u2 = runtime._anonymize_user_id("100002")
    u3 = runtime._anonymize_user_id("100003")
    g1 = runtime._anonymize_group_id("900001")

    out = runtime._anonymize_text_for_ai("100001 100002 100003 群900001")
    assert {u1, u2, u3, g1} <= set(out.split()) or all(a in out for a in (u1, u2, u3, g1))
    assert "100001" not in out and "900001" not in out

    # 登记第 5 个后立刻能被替换 —— 缓存失效被钉死。
    u5 = runtime._anonymize_user_id("100005")
    assert runtime._anonymize_text_for_ai("100005") == u5


def test_longest_key_wins(runtime: Any) -> None:
    runtime._anonymize_user_id("12345")
    long = runtime._anonymize_user_id("123456")
    assert runtime._anonymize_text_for_ai("123456") == long


def test_group_wins_over_user_on_identical_id(runtime: Any) -> None:
    runtime._anonymize_user_id("555555")
    group_alias = runtime._anonymize_group_id("555555")
    assert runtime._anonymize_text_for_ai("555555") == group_alias


# ---- B: TTL 回收 ----

def test_ttl_disabled_keeps_stale_entries(runtime: Any) -> None:
    old = time.time() - 400 * 86400
    _write_map(runtime, {
        "version": 1, "user_next": 1,
        "users": {"123456": {"alias": "UserA", "last_seen": old}},
        "groups": {},
    })

    runtime._load_anon_map()

    assert runtime._anon_users.get("123456") == "UserA"


def test_ttl_prunes_stale_without_rewinding_counter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_with_retention(monkeypatch, tmp_path, 30)
    old = time.time() - 400 * 86400
    fresh = time.time()
    # 无 user_next 的旧文件：靠 max_idx 恢复计数器，剪枝顺序错就会暴露。
    _write_map(runtime, {
        "version": 1,
        "users": {
            "111111": {"alias": "UserA", "last_seen": fresh},
            "222222": {"alias": "UserE", "last_seen": old},
        },
        "groups": {},
    })

    runtime._load_anon_map()

    assert "222222" not in runtime._anon_users  # 陈旧条目被清
    assert runtime._anon_users.get("111111") == "UserA"  # 新鲜条目保留
    # 计数器不能因剪枝回退到 UserA+1，否则新人会复用被清掉的 UserE。
    assert runtime._anon_user_next == 5
    assert runtime._anonymize_user_id("333333") == "UserF"


def test_ttl_preserves_enrolled_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_with_retention(monkeypatch, tmp_path, 30)
    from engine import memory_subjects

    memory_subjects.record_interaction(tmp_path, "UserA")
    old = time.time() - 400 * 86400
    _write_map(runtime, {
        "version": 1,
        "users": {"123456": {"alias": "UserA", "last_seen": old}},
        "groups": {},
    })

    runtime._load_anon_map()

    assert runtime._anon_users.get("123456") == "UserA"
    assert runtime._resolve_at_alias_to_real("UserA") == "123456"


def test_save_persists_real_last_seen_not_write_time(runtime: Any) -> None:
    runtime._anonymize_user_id("123456")
    old = time.time() - 400 * 86400
    runtime._anon_user_last_seen["123456"] = old
    runtime._anon_map_dirty = True

    asyncio.run(runtime._save_anon_map())

    data = json.loads(Path(runtime.ANON_MAP_FILE).read_text(encoding="utf-8"))
    assert data["users"]["123456"]["last_seen"] == pytest.approx(old)

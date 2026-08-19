"""runtime.yaml 的声明与代码读取必须对得上。

声明了却没有任何读取方的键最有迷惑性：`min_messages: 4` 读起来像「少于 4 条消息就不
提取」的安全阀，实际什么也不做，调参的人会一直调一个不存在的旋钮。反向的漏声明同样坑：
被读的 idle_delay_sec 不在配置文件里，想改正常路径的延迟就无从下手。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SKIP_DIRS = {".venv", "__pycache__", "_research", "tests", "node_modules", ".git"}

# shell/tui 目前不读 runtime.yaml；这五项是留着待接线的意图声明，不当死配置报。
_KNOWN_UNWIRED = {
    "shell.tui.theme",
    "shell.tui.show_thinking",
    "shell.tui.auto_scroll",
    "shell.tui.node_panel",
    "shell.tui.max_message_height",
}


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source_blob() -> str:
    chunks: list[str] = []
    for path in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def _leaf_paths(node: dict, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in (node or {}).items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.extend(_leaf_paths(value, dotted))
        else:
            out.append((dotted, key))
    return out


def test_every_declared_key_has_a_reader(config: dict, source_blob: str) -> None:
    """判据宽松到「裸 leaf 名出现过就算」——大量在用的键走 dict-chain 读法，
    只找 dotted 字面量会误报一堆。宽判据下仍然零命中的才是真死配置。"""
    dead: list[str] = []
    for dotted, leaf in _leaf_paths(config):
        if dotted == "version" or dotted in _KNOWN_UNWIRED:
            continue
        if dotted in source_blob or f'"{leaf}"' in source_blob or f"'{leaf}'" in source_blob:
            continue
        dead.append(dotted)

    assert dead == [], f"声明了但没有任何读取方: {dead}"


@pytest.mark.parametrize("dotted", [
    "memory.auto_extract.idle_delay_sec",
    "memory.auto_extract.idle_fallback_delay_sec",
    "memory.auto_extract.min_increment",
    "engine.compact.threshold_tokens",
    "engine.compact.hard_threshold_tokens",
    "engine.compact.keep_recent_tokens",
    "memory.max_budget_chars",
])
def test_keys_read_by_code_are_declared(config: dict, dotted: str) -> None:
    """被 get_int/get_bool 按 dotted path 读取的键必须能在配置文件里找到。"""
    node = config
    for part in dotted.split("."):
        assert isinstance(node, dict) and part in node, f"{dotted} 未在 runtime.yaml 中声明"
        node = node[part]


@pytest.mark.parametrize("dotted", [
    "engine.child_session.max_per_parent",
    "memory.auto_extract.min_messages",
    "memory.dream.min_sessions",
])
def test_removed_dead_keys_stay_removed(config: dict, dotted: str) -> None:
    """这三个没有任何读取方，语义在当前流水线里也没有对应物，别再加回来。"""
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]

    pytest.fail(f"{dotted} 又被声明了，但代码里依然没有读取方")


def test_the_docs_do_not_advertise_removed_keys() -> None:
    for rel in ("docs/configuration.md", "docs/docs/configuration.md"):
        path = _ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for gone in ("max_per_parent", "min_messages", "min_sessions"):
            assert gone not in text, f"{rel} 仍在宣传已删除的 {gone}"

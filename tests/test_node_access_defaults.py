"""节点三层访问控制各自的兜底。

三个字段长得一样，兜底方向相反，改错一个不会报错，只会悄悄放宽或悄悄断掉：

- tool_access  没说清楚一律 none。工具能落到机器、密钥和新智能体上，宁可什么都不给。
- skills       不写就是 all。技能是提示词片段。
- memories     不写就是 all，而且现有节点大多不写这一项 —— 收紧它会当场断掉一批节点。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.node import load_node  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    return tmp_path


def _load(workspace: Path, node_id: str, body: str):
    (workspace / "config" / "nodes" / f"{node_id}.yaml").write_text(body, encoding="utf-8")
    node = load_node(workspace, node_id)
    assert node is not None
    return node


VAGUE = [
    ("missing", "type: ai\n"),
    ("empty_dict", "type: ai\ntool_access: {}\n"),
    ("no_mode", "type: ai\ntool_access:\n  allow: [read_file]\n"),
    ("bogus_mode", "type: ai\ntool_access:\n  mode: everything\n"),
    ("bogus_string", "type: ai\ntool_access: whatever\n"),
]


@pytest.mark.parametrize("node_id,body", VAGUE)
def test_anything_short_of_an_explicit_grant_gives_no_tools(workspace: Path, node_id: str, body: str) -> None:
    assert _load(workspace, node_id, body).tool_access.mode == "none"


def test_only_an_explicit_all_hands_over_everything(workspace: Path) -> None:
    assert _load(workspace, "wide", "type: ai\ntool_access: all\n").tool_access.mode == "all"
    assert _load(workspace, "wide2", "type: ai\ntool_access:\n  mode: all\n").tool_access.mode == "all"


def test_an_allowlist_without_a_mode_grants_nothing(workspace: Path) -> None:
    """写了 allow 却漏了 mode 的节点，那份名单一条都不生效。"""
    node = _load(workspace, "half", "type: ai\ntool_access:\n  allow: [read_file, execute_command]\n")

    assert node.tool_access.mode == "none"
    # 名单本身留着 —— 补上 mode 就能用，别在解析时把它丢了。
    assert node.tool_access.allow == ["read_file", "execute_command"]


def test_the_frontend_default_agrees_with_this() -> None:
    """前端 asMode 把认不出来的值落成 none，和这里必须是同一个方向。

    两边反过来的话，界面会说这个节点没有任何权限，而它实际握着全部工具。
    """
    source = (Path(__file__).resolve().parents[1]
              / "adapters/web/frontend/src/components/settings/pages/NodeGrantsSection.tsx"
              ).read_text(encoding="utf-8")

    assert "String(value || 'none')" in source


def test_skills_and_memories_stay_open_by_default(workspace: Path) -> None:
    """这两个不跟着 tool_access 收紧。

    仓库里大多数节点根本不写 memories，靠的就是这个默认；改成 none 会让它们
    一次记忆都读不到，而且不会报错，只会表现为「机器人突然失忆」。
    """
    node = _load(workspace, "bare", "type: ai\n")

    assert node.skill_access.mode == "all"
    assert node.memory_access.mode == "all"


def test_real_nodes_do_not_lean_on_the_tool_access_fallback() -> None:
    """仓库里的节点都显式写了 tool_access。

    漏写不会报错，只会让那个节点一个工具都调不到 —— 现象是「模型什么也不干」，
    排查起来要绕很远。
    """
    import yaml

    root = Path(__file__).resolve().parents[1]
    missing = [
        path.name
        for folder in ("config/nodes", "engine/system_nodes")
        for path in sorted((root / folder).glob("*.yaml"))
        if isinstance(data := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
        and "tool_access" not in data
    ]

    assert missing == []

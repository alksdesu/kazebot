"""模型把布尔参数写成字符串时按字面意思读："false" 不能因为是非空字符串就算 true，认不出的值照默认值。"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _file_edits_fake import FakeFileEdits  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from engine.builtin.knowledge_inject import create_or_update_skill, save_memory  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from toolbox.builtins.mcp_clients import create_or_update_mcp_client  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

# 包的 __init__ 把 list_dir 这个名字换成了函数，模块得从 sys.modules 里拿
list_dir_module = importlib.import_module("toolbox.builtins.list_dir")

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_OMITTED = object()

# (参数值, 默认 false 的开关读成什么, 默认 true 的开关读成什么)
_CASES = [
    pytest.param("false", False, False, id="false"),
    pytest.param("False", False, False, id="False"),
    pytest.param("no", False, False, id="no"),
    pytest.param("off", False, False, id="off"),
    pytest.param("0", False, False, id="zero-text"),
    pytest.param(False, False, False, id="bool-false"),
    pytest.param(0, False, False, id="int-zero"),
    pytest.param("true", True, True, id="true"),
    pytest.param("yes", True, True, id="yes"),
    pytest.param("on", True, True, id="on"),
    pytest.param("1", True, True, id="one-text"),
    pytest.param(True, True, True, id="bool-true"),
    pytest.param(1, True, True, id="int-one"),
    pytest.param("garbage", False, True, id="unrecognised"),
    pytest.param("", False, True, id="empty"),
    pytest.param(None, False, True, id="null"),
    pytest.param(_OMITTED, False, True, id="omitted"),
]


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：送审一律放行，认领和读戳交给 FakeSupervisor。"""

    def __init__(self, workspace_root: Path, **context: Any) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-bool",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            **context,
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _with(args: dict[str, Any], key: str, raw: Any) -> dict[str, Any]:
    return args if raw is _OMITTED else {**args, key: raw}


def _frontmatter(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


class TestListDirRecursive:
    @pytest.mark.parametrize("raw,default_false,_default_true", _CASES)
    def test_recursive(self, tmp_path: Path, raw: Any, default_false: bool, _default_true: bool) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        result = asyncio.run(list_dir_module.list_dir(_with({"paths": ["."]}, "recursive", raw), _Ctx(tmp_path)))

        names = [entry["name"] for entry in result["data"]["results"][0]["entries"]]
        assert ("src/app.py" in names) is default_false
        assert "main.py" in names


class TestSkillEnabled:
    @pytest.mark.parametrize("raw,_default_false,default_true", _CASES)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        args = _with({"name": "greet", "description": "打招呼", "content": "见面先问好。"}, "enabled", raw)

        result = asyncio.run(create_or_update_skill(args, _Ctx(tmp_path)))

        assert result["ok"] is True
        assert _frontmatter(tmp_path / "config" / "skills" / "greet" / "SKILL.md")["enabled"] is default_true


class TestMemoryFlags:
    @staticmethod
    def _saved(tmp_path: Path, args: dict[str, Any]) -> dict[str, Any]:
        ctx = _Ctx(tmp_path, conversation_key=_CONV_KEY, node_id="system.memory_extractor")
        result = asyncio.run(save_memory({"id": "m1", "book": "people", "content": "张三住在杭州", "keywords": ["张三"], **args}, ctx))
        assert result["ok"] is True, result
        book = tmp_path / "data" / "memory" / conversation_namespace(_CONV_KEY) / "people.yaml"
        [entry] = [item for item in yaml.safe_load(book.read_text(encoding="utf-8"))["entries"] if item["id"] == "m1"]
        return entry

    @pytest.mark.parametrize("raw,default_false,_default_true", _CASES)
    def test_constant(self, tmp_path: Path, raw: Any, default_false: bool, _default_true: bool) -> None:
        assert self._saved(tmp_path, _with({}, "constant", raw))["constant"] is default_false

    @pytest.mark.parametrize("raw,_default_false,default_true", _CASES)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        assert self._saved(tmp_path, _with({}, "enabled", raw))["enabled"] is default_true


class TestAgentPersistent:
    @pytest.mark.parametrize("raw,_default_false,default_true", _CASES)
    def test_persistent(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        nodes = tmp_path / "config" / "nodes"
        nodes.mkdir(parents=True)
        template = {"id": "tmpl", "type": "ai", "name": "模板", "tool_access": {"mode": "all"}, "delegate_targets": []}
        (nodes / "tmpl.yaml").write_text(yaml.safe_dump(template), encoding="utf-8")

        result = asyncio.run(create_agent(_with({"name": "helper", "template": "tmpl"}, "persistent", raw), _Ctx(tmp_path)))

        assert result["ok"] is True, result
        written = yaml.safe_load((nodes / "helper.yaml").read_text(encoding="utf-8"))
        assert written["persistent"] is default_true


class TestMcpClientEnabled:
    @pytest.mark.parametrize("raw,_default_false,default_true", _CASES)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        args = _with({"id": "search", "transport": "stdio", "command": "fake-mcp-server"}, "enabled", raw)

        result = asyncio.run(create_or_update_mcp_client(args, _Ctx(tmp_path)))

        assert result["ok"] is True, result
        saved = yaml.safe_load(config_layout.mcp_path(tmp_path).read_text(encoding="utf-8"))
        assert saved["clients"]["search"]["enabled"] is default_true

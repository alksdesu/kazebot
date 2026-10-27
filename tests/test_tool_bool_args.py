"""模型填的布尔参数：认得的写法照读，"false" 不能因为是非空字符串就算 true；没写、null、空串照默认值；认不出的当场拒掉，什么都不写、不送审。"""
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
from clonoth_runtime import SwitchValueError, parse_switch, switch_arg, switch_refusal  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from engine.builtin.knowledge_inject import create_or_update_skill, save_memory  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from supervisor.scheduler import load_schedules  # noqa: E402
from toolbox.builtins.mcp_clients import create_or_update_mcp_client  # noqa: E402
from toolbox.builtins.schedules import create_schedule  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

# 包的 __init__ 把 list_dir 这个名字换成了函数，模块得从 sys.modules 里拿
list_dir_module = importlib.import_module("toolbox.builtins.list_dir")

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_OMITTED = object()

# 认得的写法和没写的：(参数值, 默认 false 的开关读成什么, 默认 true 的开关读成什么)
_READ = [
    pytest.param("false", False, False, id="false"),
    pytest.param("False", False, False, id="False"),
    pytest.param("no", False, False, id="no"),
    pytest.param("off", False, False, id="off"),
    pytest.param("0", False, False, id="zero-text"),
    pytest.param(False, False, False, id="bool-false"),
    pytest.param(0, False, False, id="int-zero"),
    pytest.param("true", True, True, id="true"),
    pytest.param("yes", True, True, id="yes"),
    pytest.param(" ON ", True, True, id="on"),
    pytest.param("1", True, True, id="one-text"),
    pytest.param(True, True, True, id="bool-true"),
    pytest.param(1, True, True, id="int-one"),
    pytest.param("", False, True, id="empty"),
    pytest.param("  ", False, True, id="blank"),
    pytest.param(None, False, True, id="null"),
    pytest.param(_OMITTED, False, True, id="omitted"),
]

# 认不出的：拒掉，不照默认值执行
_REFUSED = [
    pytest.param("garbage", id="word"),
    pytest.param("是", id="chinese"),
    pytest.param(2, id="two"),
    pytest.param(1.5, id="fraction"),
    pytest.param(["true"], id="list"),
    pytest.param({"on": True}, id="object"),
]


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：送审一律放行并记下来，认领和读戳交给 FakeSupervisor。"""

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
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _with(args: dict[str, Any], key: str, raw: Any) -> dict[str, Any]:
    return args if raw is _OMITTED else {**args, key: raw}


def _frontmatter(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def _refused(result: dict[str, Any], key: str, raw: Any) -> None:
    assert result["ok"] is False, result
    assert result["error"] == switch_refusal(key, raw)
    assert result["data"]["result"] == f"ERROR: {switch_refusal(key, raw)}"


class TestParseSwitch:
    @pytest.mark.parametrize("raw,default_false,default_true", _READ)
    def test_recognised_and_unset_values(self, raw: Any, default_false: bool, default_true: bool) -> None:
        args = _with({}, "flag", raw)

        assert switch_arg(args, "flag", False) is default_false
        assert switch_arg(args, "flag", True) is default_true

    @pytest.mark.parametrize("raw", [None, "", "  "])
    def test_unset_stays_unset_without_a_default(self, raw: Any) -> None:
        assert parse_switch(raw, None, name="flag") is None
        assert switch_arg({}, "flag", None) is None

    @pytest.mark.parametrize("raw,shown", [
        ("sync", '"sync"'),
        ("是", '"是"'),
        (2, "2"),
        (1.5, "1.5"),
        (["true"], '["true"]'),
        ({"on": True}, '{"on": true}'),
    ])
    def test_the_refusal_names_the_switch_and_shows_the_value(self, raw: Any, shown: str) -> None:
        with pytest.raises(SwitchValueError) as refused:
            switch_arg({"once": raw}, "once", False)

        assert str(refused.value) == f"once 只能是 true 或 false，收到的是 {shown}"

    def test_a_long_value_is_cut_in_the_refusal(self) -> None:
        assert switch_refusal("once", "x" * 200) == f'once 只能是 true 或 false，收到的是 "{"x" * 39}...<truncated>'

    def test_a_refusal_is_a_value_error(self) -> None:
        # 有的工具拿 except ValueError 把它和别的参数错误一起接住
        assert issubclass(SwitchValueError, ValueError)


class TestListDirRecursive:
    @pytest.mark.parametrize("raw,default_false,_default_true", _READ)
    def test_recursive(self, tmp_path: Path, raw: Any, default_false: bool, _default_true: bool) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        result = asyncio.run(list_dir_module.list_dir(_with({"paths": ["."]}, "recursive", raw), _Ctx(tmp_path)))

        names = [entry["name"] for entry in result["data"]["results"][0]["entries"]]
        assert ("src/app.py" in names) is default_false
        assert "main.py" in names

    @pytest.mark.parametrize("raw", _REFUSED)
    def test_recursive_refused(self, tmp_path: Path, raw: Any) -> None:
        ctx = _Ctx(tmp_path)

        result = asyncio.run(list_dir_module.list_dir({"paths": ["."], "recursive": raw}, ctx))

        _refused(result, "recursive", raw)
        assert result["data"]["results"] == []
        assert ctx.requested == []


class TestSkillEnabled:
    _ARGS = {"name": "greet", "description": "打招呼", "content": "见面先问好。"}

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        result = asyncio.run(create_or_update_skill(_with(self._ARGS, "enabled", raw), _Ctx(tmp_path)))

        assert result["ok"] is True
        assert _frontmatter(tmp_path / "config" / "skills" / "greet" / "SKILL.md")["enabled"] is default_true

    @pytest.mark.parametrize("raw", _REFUSED)
    def test_enabled_refused(self, tmp_path: Path, raw: Any) -> None:
        ctx = _Ctx(tmp_path)

        result = asyncio.run(create_or_update_skill({**self._ARGS, "enabled": raw}, ctx))

        _refused(result, "enabled", raw)
        assert not (tmp_path / "config" / "skills" / "greet").exists()
        assert ctx.requested == []


class TestMemoryFlags:
    _ARGS = {"id": "m1", "book": "people", "content": "张三住在杭州", "keywords": ["张三"]}

    @staticmethod
    def _ctx(tmp_path: Path) -> _Ctx:
        return _Ctx(tmp_path, conversation_key=_CONV_KEY, node_id="system.memory_extractor")

    @staticmethod
    def _book(tmp_path: Path) -> Path:
        return tmp_path / "data" / "memory" / conversation_namespace(_CONV_KEY) / "people.yaml"

    @classmethod
    def _saved(cls, tmp_path: Path, args: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(save_memory({**cls._ARGS, **args}, cls._ctx(tmp_path)))
        assert result["ok"] is True, result
        [entry] = [item for item in yaml.safe_load(cls._book(tmp_path).read_text(encoding="utf-8"))["entries"] if item["id"] == "m1"]
        return entry

    @pytest.mark.parametrize("raw,default_false,_default_true", _READ)
    def test_constant(self, tmp_path: Path, raw: Any, default_false: bool, _default_true: bool) -> None:
        assert self._saved(tmp_path, _with({}, "constant", raw))["constant"] is default_false

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        assert self._saved(tmp_path, _with({}, "enabled", raw))["enabled"] is default_true

    @pytest.mark.parametrize("key", ["constant", "enabled"])
    @pytest.mark.parametrize("raw", _REFUSED)
    def test_refused_without_writing_a_book(self, tmp_path: Path, key: str, raw: Any) -> None:
        result = asyncio.run(save_memory({**self._ARGS, key: raw}, self._ctx(tmp_path)))

        _refused(result, key, raw)
        assert not self._book(tmp_path).exists()

    @pytest.mark.parametrize("key", ["constant", "enabled"])
    @pytest.mark.parametrize("raw", _REFUSED)
    def test_refused_without_touching_the_saved_entry(self, tmp_path: Path, key: str, raw: Any) -> None:
        before = self._saved(tmp_path, {"constant": False, "enabled": True})

        result = asyncio.run(save_memory({**self._ARGS, "content": "张三搬去了上海", key: raw}, self._ctx(tmp_path)))

        _refused(result, key, raw)
        [after] = yaml.safe_load(self._book(tmp_path).read_text(encoding="utf-8"))["entries"]
        assert after == before


class TestAgentPersistent:
    @staticmethod
    def _template(tmp_path: Path) -> Path:
        nodes = tmp_path / "config" / "nodes"
        nodes.mkdir(parents=True)
        template = {"id": "tmpl", "type": "ai", "name": "模板", "tool_access": {"mode": "all"}, "delegate_targets": []}
        (nodes / "tmpl.yaml").write_text(yaml.safe_dump(template), encoding="utf-8")
        return nodes

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_persistent(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        nodes = self._template(tmp_path)

        result = asyncio.run(create_agent(_with({"name": "helper", "template": "tmpl"}, "persistent", raw), _Ctx(tmp_path)))

        assert result["ok"] is True, result
        written = yaml.safe_load((nodes / "helper.yaml").read_text(encoding="utf-8"))
        assert written["persistent"] is default_true

    @pytest.mark.parametrize("raw", _REFUSED)
    def test_persistent_refused(self, tmp_path: Path, raw: Any) -> None:
        nodes = self._template(tmp_path)
        ctx = _Ctx(tmp_path)

        result = asyncio.run(create_agent({"name": "helper", "template": "tmpl", "persistent": raw}, ctx))

        _refused(result, "persistent", raw)
        assert not (nodes / "helper.yaml").exists()
        assert not (tmp_path / "data" / "memory" / "helper").exists()
        assert ctx.requested == []


class TestScheduleSwitches:
    _ARGS = {"id": "remind", "cron": "0 9 * * *", "text": "喝水"}
    _SCRIPT = {"id": "probe", "cron": "0 9 * * *", "type": "script", "command": "echo hi"}

    @staticmethod
    def _saved(tmp_path: Path, args: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(create_schedule(args, _Ctx(tmp_path)))
        assert result["ok"] is True, result
        [entry] = load_schedules(tmp_path)
        return entry

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        assert self._saved(tmp_path, _with(self._ARGS, "enabled", raw))["enabled"] is default_true

    @pytest.mark.parametrize("raw,default_false,_default_true", _READ)
    def test_once(self, tmp_path: Path, raw: Any, default_false: bool, _default_true: bool) -> None:
        assert self._saved(tmp_path, _with(self._ARGS, "once", raw))["once"] is default_false

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_silent(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        assert self._saved(tmp_path, _with(self._SCRIPT, "silent", raw))["silent"] is default_true

    @pytest.mark.parametrize("key", ["enabled", "once", "silent"])
    @pytest.mark.parametrize("raw", _REFUSED)
    def test_refused_before_any_approval(self, tmp_path: Path, key: str, raw: Any) -> None:
        ctx = _Ctx(tmp_path)

        result = asyncio.run(create_schedule({**self._ARGS, key: raw}, ctx))

        _refused(result, key, raw)
        assert ctx.requested == []
        assert load_schedules(tmp_path) == []

    def test_a_misspelled_once_never_leaves_a_recurring_schedule(self, tmp_path: Path) -> None:
        result = asyncio.run(create_schedule({**self._ARGS, "once": "仅一次"}, _Ctx(tmp_path)))

        assert result["ok"] is False
        assert result["error"] == 'once 只能是 true 或 false，收到的是 "仅一次"'
        assert load_schedules(tmp_path) == []


class TestMcpClient:
    _ARGS = {"id": "search", "transport": "stdio", "command": "fake-mcp-server"}

    @staticmethod
    def _saved(tmp_path: Path) -> dict[str, Any]:
        return yaml.safe_load(config_layout.mcp_path(tmp_path).read_text(encoding="utf-8"))["clients"]["search"]

    @pytest.mark.parametrize("raw,_default_false,default_true", _READ)
    def test_enabled(self, tmp_path: Path, raw: Any, _default_false: bool, default_true: bool) -> None:
        result = asyncio.run(create_or_update_mcp_client(_with(self._ARGS, "enabled", raw), _Ctx(tmp_path)))

        assert result["ok"] is True, result
        assert self._saved(tmp_path)["enabled"] is default_true

    @pytest.mark.parametrize("raw,expected", [
        ("yes", True), ("TRUE", True), (1, True), (True, True),
        ("no", False), ("false", False), (0, False), (False, False),
    ])
    def test_a_recognised_share_sessions_is_saved(self, tmp_path: Path, raw: Any, expected: bool) -> None:
        result = asyncio.run(create_or_update_mcp_client({**self._ARGS, "share_sessions": raw}, _Ctx(tmp_path)))

        assert result["ok"] is True, result
        assert self._saved(tmp_path).get("share_sessions", False) is expected

    @pytest.mark.parametrize("raw", [None, "", "  ", _OMITTED])
    def test_an_unset_share_sessions_keeps_the_current_setting(self, tmp_path: Path, raw: Any) -> None:
        asyncio.run(create_or_update_mcp_client({**self._ARGS, "share_sessions": True}, _Ctx(tmp_path)))

        result = asyncio.run(create_or_update_mcp_client(_with(self._ARGS, "share_sessions", raw), _Ctx(tmp_path)))

        assert result["ok"] is True, result
        assert self._saved(tmp_path)["share_sessions"] is True

    @pytest.mark.parametrize("key", ["enabled", "share_sessions"])
    @pytest.mark.parametrize("raw", _REFUSED)
    def test_refused_before_approval_and_the_saved_client_stays(self, tmp_path: Path, key: str, raw: Any) -> None:
        asyncio.run(create_or_update_mcp_client({**self._ARGS, "share_sessions": True}, _Ctx(tmp_path)))
        before = self._saved(tmp_path)
        ctx = _Ctx(tmp_path)

        result = asyncio.run(create_or_update_mcp_client({**self._ARGS, "command": "other-server", key: raw}, ctx))

        _refused(result, key, raw)
        assert ctx.requested == []
        assert self._saved(tmp_path) == before

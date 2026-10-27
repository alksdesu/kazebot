"""脚本工具读开关：tools/_channel 里那份 parse_bool、parse_switch 和 clonoth_runtime 的读出同一个结论、报同一句错，
各个脚本按自带工具的启动方式都拿得到它们；模型写了认不出的开关，脚本在动手之前就拒。"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
from tools import _channel  # noqa: E402

_TOOLS = _ROOT / "tools"

_SAMPLES: list[Any] = [
    True, False, None, 0, 1, 2, -1, 0.0, 1.0, 1.5, "", " ", "1", "0", "2",
    "true", "TRUE", " True ", "false", "False", "yes", "YES", "no", "No", "y", "n", "on", "ON", "off", "Off",
    "enable", "disabled", "maybe", "是", [], {}, ["true"],
]
_SWITCH_SAMPLES: list[Any] = [*_SAMPLES, "x" * 200, {"on": True}, 1e100]
# 子进程按本地编码输出，拒绝原因里的中文要逐字比对就得让它按 UTF-8 写
_UTF8 = {"PYTHONIOENCODING": "utf-8"}
_OMITTED = object()


def _switch_outcome(module: Any, raw: Any, default: bool | None) -> Any:
    try:
        return module.parse_switch(raw, default, name="flag")
    except module.SwitchValueError as exc:
        return ("refused", str(exc))


class TestTheToolsCopy:
    @pytest.mark.parametrize("default", [None, True, False])
    def test_it_reads_every_sample_like_the_runtime(self, default: bool | None) -> None:
        for raw in _SAMPLES:
            assert _channel.parse_bool(raw, default) == clonoth_runtime.parse_bool(raw, default), raw

    def test_it_knows_the_same_words(self) -> None:
        assert _channel._TRUE_WORDS == clonoth_runtime._TRUE_WORDS
        assert _channel._FALSE_WORDS == clonoth_runtime._FALSE_WORDS

    @pytest.mark.parametrize("default", [None, True, False])
    def test_it_reads_and_refuses_every_switch_like_the_runtime(self, default: bool | None) -> None:
        for raw in _SWITCH_SAMPLES:
            assert _switch_outcome(_channel, raw, default) == _switch_outcome(clonoth_runtime, raw, default), raw

    def test_switch_arg_reads_the_named_key(self) -> None:
        assert _channel.switch_arg({"force": "yes"}, "force", False) is True
        assert _channel.switch_arg({}, "force", True) is True
        with pytest.raises(_channel.SwitchValueError, match="^force 只能是 true 或 false"):
            _channel.switch_arg({"force": "maybe"}, "force", False)


# 脚本工具里装着模型参数和手写配置的变量名；接口回包这类本来就是真假值的不在其列
_SWITCH_SOURCES = frozenset({"args", "arguments", "params", "settings", "storage", "spec", "preset", "config", "cfg"})


def _bool_of_a_get(tree: ast.AST) -> list[int]:
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].func, ast.Attribute) and node.args[0].func.attr == "get"
        and isinstance(node.args[0].func.value, ast.Name) and node.args[0].func.value.id in _SWITCH_SOURCES
    ]


def test_no_script_tool_reads_a_switch_with_bool() -> None:
    # bool(args.get(...)) 会把模型或手写配置里的 "false" 当成开
    found = {
        str(path.relative_to(_ROOT)): lines
        for path in sorted(_TOOLS.rglob("*.py"))
        if (lines := _bool_of_a_get(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    }

    assert found == {}


def _run_script(
    script: Path, workspace: Path, args: dict[str, Any], *, env_extra: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """照注册表起自带工具的样子跑：解释器直接跑脚本，工作目录是工作区，不补 PYTHONPATH。"""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["CLONOTH_WORKSPACE"] = str(workspace)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(args).encode("utf-8"), capture_output=True,
        cwd=str(workspace), env=env, timeout=120,
    )
    # 和注册表一样按 UTF-8 宽松解码：Windows 上子进程按本地编码输出，中文会花，但键和真假值都是 ASCII
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    lines = [line for line in stdout.splitlines() if line.strip()]
    return proc.returncode, (json.loads(lines[-1]) if lines else {}), stderr


def _cleanup_settings(workspace: Path, cleanup_enabled: str) -> None:
    path = workspace / "config" / "drawtools" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"storage": {"cleanup_enabled": cleanup_enabled}}), encoding="utf-8")


class TestRunningTheScripts:
    @pytest.mark.parametrize("cleanup_enabled", ["false", "no", "off"])
    def test_nai_cleanup_honours_a_quoted_off_switch_and_a_quoted_false_force(self, tmp_path: Path, cleanup_enabled: str) -> None:
        _cleanup_settings(tmp_path, cleanup_enabled)

        code, result, stderr = _run_script(_TOOLS / "drawtools" / "nai_cleanup.py", tmp_path, {"force": "false"})

        assert code == 0, stderr
        assert result.get("skipped") is True and result.get("reason") == "cleanup disabled"

    def test_nai_cleanup_runs_when_forced_with_a_quoted_true(self, tmp_path: Path) -> None:
        _cleanup_settings(tmp_path, "false")

        code, result, stderr = _run_script(_TOOLS / "drawtools" / "nai_cleanup.py", tmp_path, {"force": "yes"})

        assert code == 0, stderr
        assert result.get("skipped") is not True
        assert result.get("deleted_count") == 0

    @pytest.mark.parametrize("script,args", [
        pytest.param("read_web.py", {"url": "http://127.0.0.1/", "raw": "false"}, id="read_web"),
        pytest.param("exa_search.py", {"use_autoprompt": "false"}, id="exa_search"),
        pytest.param("stocktool/quote.py", {"refresh": "false"}, id="stocktool"),
    ])
    def test_a_script_gets_its_switch_reader_before_it_turns_the_input_down(self, tmp_path: Path, script: str, args: dict[str, Any]) -> None:
        # 这几个只走不联网就退出的那条路：导不到 _channel 的话进程会先死在 import 上
        _code, result, stderr = _run_script(_TOOLS / script, tmp_path, args)

        assert "ModuleNotFoundError" not in stderr and "ImportError" not in stderr, stderr
        assert result.get("ok") is False

    @pytest.mark.parametrize("raw", [None, "", "  ", _OMITTED])
    def test_nai_cleanup_without_force_honours_the_off_switch(self, tmp_path: Path, raw: Any) -> None:
        _cleanup_settings(tmp_path, "false")

        code, result, stderr = _run_script(
            _TOOLS / "drawtools" / "nai_cleanup.py", tmp_path, {} if raw is _OMITTED else {"force": raw},
        )

        assert code == 0, stderr
        assert result.get("skipped") is True


class TestScriptsRefuseAnUnrecognisedSwitch:
    @pytest.mark.parametrize("script,args,key", [
        pytest.param("read_web.py", {"url": "http://127.0.0.1/"}, "raw", id="read_web"),
        pytest.param("exa_search.py", {}, "use_autoprompt", id="exa_search"),
        pytest.param("stocktool/quote.py", {}, "refresh", id="stocktool-refresh"),
        pytest.param("stocktool/quote.py", {}, "include_raw", id="stocktool-include_raw"),
        pytest.param("drawtools/draw_context.py", {"query": "miku"}, "include_characters", id="draw_context"),
        pytest.param("drawtools/nai_cleanup.py", {}, "force", id="nai_cleanup"),
    ])
    @pytest.mark.parametrize("raw", [pytest.param("是", id="chinese"), pytest.param(2, id="two")])
    def test_refused_with_the_reason(self, tmp_path: Path, script: str, args: dict[str, Any], key: str, raw: Any) -> None:
        code, result, stderr = _run_script(_TOOLS / script, tmp_path, {**args, key: raw}, env_extra=_UTF8)

        assert code == 1, stderr
        assert result["ok"] is False
        assert result["error"] == clonoth_runtime.switch_refusal(key, raw)
        assert result["data"]["result"] == f"ERROR: {clonoth_runtime.switch_refusal(key, raw)}"

    @pytest.mark.parametrize("script,args,key,next_error", [
        pytest.param("read_web.py", {"url": "http://127.0.0.1/"}, "raw", "该地址不允许访问", id="read_web"),
        pytest.param("exa_search.py", {}, "use_autoprompt", "缺少搜索关键词", id="exa_search"),
        pytest.param("stocktool/quote.py", {}, "refresh", "no symbol specified", id="stocktool-refresh"),
        pytest.param("stocktool/quote.py", {}, "include_raw", "no symbol specified", id="stocktool-include_raw"),
    ])
    @pytest.mark.parametrize("raw", ["yes", 0, None])
    def test_a_recognised_or_unset_switch_lets_the_script_move_on(
        self, tmp_path: Path, script: str, args: dict[str, Any], key: str, next_error: str, raw: Any,
    ) -> None:
        # 每个脚本都挑一条不联网就停下的路：停在下一道检查上，说明开关这一关过了
        _code, result, stderr = _run_script(_TOOLS / script, tmp_path, {**args, key: raw}, env_extra=_UTF8)

        assert result["ok"] is False, stderr
        assert result["error"].startswith(next_error), result

    @pytest.mark.parametrize("raw,matched", [
        ("false", False), ("No", False), (0, False),
        ("yes", True), (1, True), (None, True), ("", True), (_OMITTED, True),
    ])
    def test_draw_context_reads_include_characters(self, tmp_path: Path, raw: Any, matched: bool) -> None:
        args = {"query": "画一张 miku"} if raw is _OMITTED else {"query": "画一张 miku", "include_characters": raw}

        code, result, stderr = _run_script(_TOOLS / "drawtools" / "draw_context.py", tmp_path, args, env_extra=_UTF8)

        assert code == 0, stderr
        names = [character["name"] for character in result["data"]["matched_characters"]]
        assert ("初音未来" in names) is matched


def _nai_settings(workspace: Path) -> None:
    path = workspace / "config" / "drawtools" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 密钥指向一个肯定没设的变量：测试机上配了 NOVELAI_API_KEY 也不会真的发出请求
    path.write_text(
        yaml.safe_dump({"api": {"api_key": "", "api_key_env": "CLONOTH_TEST_UNSET_NOVELAI_KEY"}}), encoding="utf-8",
    )


class TestNaiGenerate:
    @pytest.mark.parametrize("key", ["qualityToggle", "autoSmea", "variety_boost", "sm", "sm_dyn", "decrisper"])
    def test_an_unrecognised_switch_is_refused(self, tmp_path: Path, key: str) -> None:
        _nai_settings(tmp_path)

        code, result, stderr = _run_script(
            _TOOLS / "drawtools" / "nai_generate.py", tmp_path, {"prompt": "1girl", key: "是"}, env_extra=_UTF8,
        )

        assert code == 1, stderr
        assert result["error"] == clonoth_runtime.switch_refusal(key, "是")

    @pytest.mark.parametrize("args", [
        pytest.param({"qualityToggle": "no"}, id="false-word"),
        pytest.param({"autoSmea": "YES"}, id="true-word"),
        pytest.param({"variety_boost": 1}, id="one"),
        pytest.param({"sm": None}, id="null"),
        pytest.param({"sm_dyn": ""}, id="empty"),
        # params 里的开关照旧宽松读：nai_generate_from_plan 把配置预设原样转进来，同一个对象分不出谁写的
        pytest.param({"params": {"decrisper": "garbage"}}, id="params-object"),
    ])
    def test_recognised_unset_and_params_values_move_on_to_the_api_key(self, tmp_path: Path, args: dict[str, Any]) -> None:
        _nai_settings(tmp_path)

        code, result, stderr = _run_script(
            _TOOLS / "drawtools" / "nai_generate.py", tmp_path, {"prompt": "1girl", **args}, env_extra=_UTF8,
        )

        assert code == 1, stderr
        assert result["error"].startswith("NovelAI API key is missing"), result


@contextmanager
def _bridge() -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """假的 QQ 转发 Bridge：收下请求体，一律回成功。"""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            body = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestQqForward:
    _REMIND = {"op": "remind", "target_type": "self", "text": "hi"}

    @staticmethod
    def _env(port: int) -> dict[str, str]:
        return {
            **_UTF8,
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "1",
            "CLONOTH_SESSION_ID": "session-test",
        }

    @pytest.mark.parametrize("switches,expected", [
        pytest.param({"include_images": "no", "include_files": "YES", "use_recent": "on"}, (False, True, True), id="words"),
        pytest.param({"include_images": 0, "include_files": 1, "use_recent": "false"}, (False, True, False), id="numbers"),
        pytest.param({"include_images": None, "include_files": "", "use_recent": "  "}, (True, True, False), id="unset"),
        pytest.param({}, (True, True, False), id="omitted"),
    ])
    def test_the_bridge_gets_the_switches_as_read(
        self, tmp_path: Path, switches: dict[str, Any], expected: tuple[bool, bool, bool],
    ) -> None:
        with _bridge() as (port, received):
            code, _result, stderr = _run_script(
                _TOOLS / "qq_forward.py", tmp_path, {**self._REMIND, **switches}, env_extra=self._env(port),
            )

        assert code == 0, stderr
        [payload] = received
        assert (payload["include_images"], payload["include_files"], payload["use_recent"]) == expected

    @pytest.mark.parametrize("key", ["include_images", "include_files", "use_recent"])
    def test_an_unrecognised_switch_is_refused_before_anything_is_sent(self, tmp_path: Path, key: str) -> None:
        with _bridge() as (port, received):
            code, result, stderr = _run_script(
                _TOOLS / "qq_forward.py", tmp_path, {**self._REMIND, key: "最新"}, env_extra=self._env(port),
            )

        assert code == 1, stderr
        assert result["error"] == clonoth_runtime.switch_refusal(key, "最新")
        assert received == []

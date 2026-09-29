"""工具子进程那侧的共享密钥。

工具只吃标准库、导不了 config_layout，tools/_channel.py 是那边唯一的一份 dotenv 解析：
config/secrets.env 垫底，本实例 .env 盖在上面。搜索工具和绘图工具都从这两层取密钥。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _ROOT / "tools"
_DRAWTOOLS = _TOOLS / "drawtools"
for _p in (str(_ROOT), str(_TOOLS), str(_DRAWTOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common as drawtools_common  # noqa: E402
import _channel  # noqa: E402
import config_layout  # noqa: E402
from _channel import Channel, dotenv_values, platform_secret  # noqa: E402
from toolbox._common import safe_subprocess_env  # noqa: E402


def _workspace(root: Path, *, shared: str = "", dotenv: str = "", models: str = "", platform: str = "") -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    if shared:
        (root / "config" / "secrets.env").write_text(shared, encoding="utf-8")
    if dotenv:
        (root / ".env").write_text(dotenv, encoding="utf-8")
    if models:
        (root / "config" / "models.yaml").write_text(models, encoding="utf-8")
    if platform:
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "data" / "platform_secrets.yaml").write_text(platform, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
#  两层 dotenv
# ---------------------------------------------------------------------------

class TestDotenvValues:
    def test_the_shared_file_is_the_bottom_layer(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="ONLY_SHARED=s\nBOTH=from-shared\n", dotenv="BOTH=from-dotenv\nONLY_LOCAL=l\n")

        assert dotenv_values(root) == {"ONLY_SHARED": "s", "BOTH": "from-dotenv", "ONLY_LOCAL": "l"}

    def test_either_file_alone_is_enough(self, tmp_path: Path) -> None:
        assert dotenv_values(_workspace(tmp_path, shared="A=1\n")) == {"A": "1"}
        assert dotenv_values(_workspace(tmp_path / "b", dotenv="B=2\n")) == {"B": "2"}

    def test_a_workspace_with_neither_is_empty(self, tmp_path: Path) -> None:
        assert dotenv_values(tmp_path) == {}

    def test_the_export_prefix_and_quotes_are_handled(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="export EXPORTED=e\nSINGLE='one two'\nDOUBLE=\"three\"\n")

        assert dotenv_values(root) == {"EXPORTED": "e", "SINGLE": "one two", "DOUBLE": "three"}

    def test_comments_and_junk_lines_are_skipped(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="# note\n\nnot-an-assignment\nGOOD=1\n")

        assert dotenv_values(root) == {"GOOD": "1"}

    def test_the_default_root_is_the_working_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 工具是以工作区为 cwd 拉起的子进程，不传 root 就该读那边的两份文件。
        monkeypatch.chdir(_workspace(tmp_path, shared="FROM_CWD=1\n"))

        assert dotenv_values() == {"FROM_CWD": "1"}


# 一行原文，以及三个解析器都该给出的值；None 表示这个键不该出现。
ENV_CASES: tuple[tuple[str, str, str | None], ...] = (
    ("PLAIN", "PLAIN=plain", "plain"),
    ("PADDED", "PADDED=   padded   ", "padded"),
    ("EQUALS", "EQUALS=a=b", "a=b"),
    ("EXPORTED", "export EXPORTED=e", "e"),
    ("EXPORT_SPACED", "export  EXPORT_SPACED = ex ", "ex"),
    ("SQ", r"SQ='it\'s'", "it's"),
    ("SQ_BACKSLASH", r"SQ_BACKSLASH='back\\slash'", "back\\slash"),
    # 单引号里除了 \\ 和 \' 都原样：\n 是反斜杠加 n，不是换行。
    ("SQ_LITERAL", r"SQ_LITERAL='no \n escape'", "no \\n escape"),
    ("SQ_HASH", "SQ_HASH='a#b'", "a#b"),
    ("SQ_SPACE", "SQ_SPACE='two words'", "two words"),
    ("DQ", r'DQ="say \"hi\""', 'say "hi"'),
    ("DQ_NEWLINE", r'DQ_NEWLINE="line\nbreak"', "line\nbreak"),
    ("DQ_TAB", r'DQ_TAB="a\tb"', "a\tb"),
    ("DQ_RETURN", r'DQ_RETURN="a\rb"', "a\rb"),
    ("DQ_BACKSLASH", r'DQ_BACKSLASH="back\\slash"', "back\\slash"),
    ("DQ_HASH", 'DQ_HASH="a#b"', "a#b"),
    # 不带引号时「空白 + #」起是注释；紧贴着的 # 是值的一部分。
    ("COMMENT", "COMMENT=value # 注释", "value"),
    ("HASH_GLUED", "HASH_GLUED=value#note", "value#note"),
    ("TRAILING_QUOTE", "TRAILING_QUOTE=ends-quote'", "ends-quote'"),
    # 空值一律丢掉：.env 里的占位不该挡住共享密钥里的同名项。
    ("EMPTY", "EMPTY=", None),
    ("EMPTY_SQ", "EMPTY_SQ=''", None),
    ("EMPTY_DQ", 'EMPTY_DQ=""', None),
    ("BLANK", "BLANK='   '", None),
)
ENV_TEXT = "\n".join(line for _name, line, _value in ENV_CASES) + "\n"
ENV_EXPECTED = {name: value for name, _line, value in ENV_CASES if value is not None}


def _forced_fallback(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """装不上 python-dotenv 的环境里那条路径。工具只保证吃标准库。"""
    with monkeypatch.context() as patched:
        patched.setitem(sys.modules, "dotenv", None)
        with pytest.raises(ImportError):
            from dotenv import dotenv_values as _unused  # noqa: F401
        return _channel._read_env_file(path)


class TestTheThreeParsersAgree:
    @pytest.fixture
    def env_file(self, tmp_path: Path) -> Path:
        path = tmp_path / ".env"
        path.write_text(ENV_TEXT, encoding="utf-8")
        return path

    def test_the_tool_side_matches_the_table(self, env_file: Path) -> None:
        assert _channel._read_env_file(env_file) == ENV_EXPECTED

    def test_the_standard_library_fallback_matches_the_table(
        self, env_file: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert _forced_fallback(env_file, monkeypatch) == ENV_EXPECTED

    def test_the_supervisor_side_matches_the_table(self, env_file: Path) -> None:
        # 主进程用的是 config_layout.read_env_file；两边对同一行的理解必须一致。
        assert dict(config_layout.read_env_file(env_file)) == ENV_EXPECTED

    def test_all_three_give_the_very_same_mapping(
        self, env_file: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor_side = dict(config_layout.read_env_file(env_file))
        tool_side = _channel._read_env_file(env_file)

        assert tool_side == supervisor_side == _forced_fallback(env_file, monkeypatch)

    @pytest.mark.parametrize("name,line,value", [case for case in ENV_CASES], ids=[case[0] for case in ENV_CASES])
    def test_each_line_on_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, line: str, value: str | None,
    ) -> None:
        path = tmp_path / ".env"
        path.write_text(line + "\n", encoding="utf-8")
        expected = {} if value is None else {name: value}

        assert _channel._read_env_file(path) == expected
        assert dict(config_layout.read_env_file(path)) == expected
        assert _forced_fallback(path, monkeypatch) == expected

    @pytest.mark.parametrize("text", ["", "# 只有注释\n", "\n\n", "没有等号\n"])
    def test_a_file_with_nothing_in_it_is_empty_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str,
    ) -> None:
        path = tmp_path / ".env"
        path.write_text(text, encoding="utf-8")

        assert _channel._read_env_file(path) == {}
        assert dict(config_layout.read_env_file(path)) == {}
        assert _forced_fallback(path, monkeypatch) == {}

    def test_a_missing_file_is_empty_on_both_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        missing = tmp_path / "nope.env"

        assert _channel._read_env_file(missing) == {}
        assert _forced_fallback(missing, monkeypatch) == {}

    def test_the_last_assignment_wins_on_both_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 和进程启动时灌 .env 一致：dotenv 取最后一行。
        path = tmp_path / ".env"
        path.write_text("K=first\nK=second\n", encoding="utf-8")

        assert _channel._read_env_file(path) == {"K": "second"}
        assert dict(config_layout.read_env_file(path)) == {"K": "second"}
        assert _forced_fallback(path, monkeypatch) == {"K": "second"}


class TestAnEmptyPlaceholderDoesNotShadow:
    def test_an_empty_dotenv_value_falls_through_to_the_group_file(self, tmp_path: Path) -> None:
        # 部署脚本先往 .env 写一行「NAPCAT_WEBUI_TOKEN=」占位，它不该挡住共享密钥里配好的那把。
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared\n", dotenv="SOME_API_KEY=\n")

        assert dotenv_values(root) == {"SOME_API_KEY": "k-shared"}

    @pytest.mark.parametrize("placeholder", ["SOME_API_KEY=\n", "SOME_API_KEY=''\n", 'SOME_API_KEY=""\n', "SOME_API_KEY='  '\n"])
    def test_every_spelling_of_empty_falls_through(self, tmp_path: Path, placeholder: str) -> None:
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared\n", dotenv=placeholder)

        assert dotenv_values(root)["SOME_API_KEY"] == "k-shared"

    def test_a_real_dotenv_value_still_shadows_the_group_file(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared\n", dotenv="SOME_API_KEY=k-local\n")

        assert dotenv_values(root)["SOME_API_KEY"] == "k-local"

    def test_it_matches_how_the_main_process_picks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """主进程是 os.getenv(k) or 共享密钥；.env 里的空占位在那边也是取不到值的。"""
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared\n", dotenv="SOME_API_KEY=\n")
        monkeypatch.delenv("SOME_API_KEY", raising=False)

        main_process = config_layout.read_env_file(root / ".env").get("SOME_API_KEY", "") or \
            config_layout.shared_secrets(root).get("SOME_API_KEY", "")

        assert dotenv_values(root)["SOME_API_KEY"] == main_process == "k-shared"


class TestTheFallbackValueRules:
    @pytest.mark.parametrize("raw,expected", [
        (r"'it\'s'", "it's"),
        (r"'back\\slash'", "back\\slash"),
        (r"'no \n escape'", "no \\n escape"),
        (r'"say \"hi\""', 'say "hi"'),
        (r'"line\nbreak"', "line\nbreak"),
        (r'"a\tb"', "a\tb"),
        (r'"back\\slash"', "back\\slash"),
        ("bare # 注释", "bare"),
        ("bare#glued", "bare#glued"),
        ("  padded  ", "padded"),
    ])
    def test_the_value_rules_follow_python_dotenv(self, raw: str, expected: str) -> None:
        assert _channel._env_value(raw) == expected

    def test_the_export_prefix_is_stripped(self) -> None:
        assert _channel._parse_env_text("export K=v\n") == {"K": "v"}

    def test_junk_lines_are_skipped(self) -> None:
        assert _channel._parse_env_text("# note\n\nno-equals\nK=v\n") == {"K": "v"}


class TestTheConfigView:
    def test_a_key_only_in_the_group_file_is_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ONLY_SHARED_API_KEY", raising=False)
        root = _workspace(tmp_path, shared="ONLY_SHARED_API_KEY=k-shared\n")

        assert Channel("image", root=root).env("ONLY_SHARED_API_KEY") == "k-shared"

    def test_the_process_environment_still_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_API_KEY", "k-env")
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared\n")

        assert Channel("image", root=root).env("SOME_API_KEY") == "k-env"

    def test_the_fallback_list_reaches_the_group_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEW_API_KEY", raising=False)
        monkeypatch.delenv("OLD_API_KEY", raising=False)
        root = _workspace(tmp_path, shared="OLD_API_KEY=k-old\n")

        assert Channel("image", root=root).first_env("NEW_API_KEY|OLD_API_KEY") == "k-old"

    def test_a_channel_reference_expands_from_the_group_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ALPHA_API_KEY", raising=False)
        root = _workspace(
            tmp_path,
            shared="ALPHA_API_KEY=k-shared\n",
            models="\n".join([
                "version: 1",
                "provider: openai",
                "openai:",
                "  base_url: https://api.example.com/v1",
                "  api_key: ${ALPHA_API_KEY}",
                "  model: m-alpha",
                "",
            ]),
        )

        main = Channel("image", root=root).main()

        assert (main.provider, main.api_key, main.model) == ("openai", "k-shared", "m-alpha")

    def test_a_platform_secret_falls_back_to_the_group_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("VAULT_SIGNING", raising=False)
        root = _workspace(tmp_path, shared="VAULT_SIGNING=k-shared\n")

        assert platform_secret("vault", "signing_key", "VAULT_SIGNING", root=root) == "k-shared"

    def test_the_platform_secrets_file_still_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_SIGNING", raising=False)
        root = _workspace(tmp_path, shared="VAULT_SIGNING=k-shared\n", platform="vault:\n  signing_key: k-file\n")

        assert platform_secret("vault", "signing_key", "VAULT_SIGNING", root=root) == "k-file"


# ---------------------------------------------------------------------------
#  搜索工具：密钥只写在 config/secrets.env 里也找得到
# ---------------------------------------------------------------------------

class _Upstream:
    """记下每次请求的路径和认证头，回一份能解析的响应。"""

    def __init__(self, body: dict[str, Any]) -> None:
        self.seen: list[tuple[str, str, str]] = []
        seen = self.seen
        payload = json.dumps(body).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append((
                    self.path,
                    self.headers.get("x-api-key") or "",
                    self.headers.get("Authorization") or "",
                ))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_Upstream":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


_EXA_BODY = {"results": [{"title": "标题", "url": "https://example.com/1", "text": "正文"}]}
_X_BODY = {
    "model": "grok",
    "output": [{
        "type": "message",
        "content": [{
            "type": "output_text", "text": "回答",
            "annotations": [{"type": "url_citation", "url": "https://example.com/2"}],
        }],
    }],
    "usage": {},
}


def _run_tool(script: str, workspace: Path, args: dict[str, Any], **env: str) -> dict[str, Any]:
    """按真实链路跑一个工具：cwd 是工作区，环境里的密钥已经被剥掉。"""
    child_env = safe_subprocess_env()
    for name in ("EXA_BASE_URL", "XAI_BASE_URL", "EXA_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
        child_env.pop(name, None)
    child_env.update({"PYTHONIOENCODING": "utf-8", **env})
    done = subprocess.run(
        [sys.executable, str(_TOOLS / script)],
        input=json.dumps(args, ensure_ascii=False), text=True, encoding="utf-8",
        capture_output=True, cwd=workspace, env=child_env, timeout=60, check=False,
    )
    assert done.stdout, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestExaSearch:
    def test_a_key_only_in_the_group_file_is_used(self, tmp_path: Path) -> None:
        with _Upstream(_EXA_BODY) as upstream:
            root = _workspace(tmp_path, shared=f"EXA_API_KEY=k-shared\nEXA_BASE_URL={upstream.url}\n")

            result = _run_tool("exa_search.py", root, {"query": "alpha"})

            assert result["ok"] is True, result
            assert [seen[:2] for seen in upstream.seen] == [("/search", "k-shared")]

    def test_the_instance_dotenv_still_wins(self, tmp_path: Path) -> None:
        with _Upstream(_EXA_BODY) as upstream:
            root = _workspace(
                tmp_path,
                shared=f"EXA_API_KEY=k-shared\nEXA_BASE_URL={upstream.url}\n",
                dotenv="EXA_API_KEY=k-local\n",
            )

            _run_tool("exa_search.py", root, {"query": "alpha"})

            assert upstream.seen[0][1] == "k-local"

    def test_no_key_anywhere_fails_before_any_request(self, tmp_path: Path) -> None:
        with _Upstream(_EXA_BODY) as upstream:
            root = _workspace(tmp_path, shared=f"EXA_BASE_URL={upstream.url}\n")

            result = _run_tool("exa_search.py", root, {"query": "alpha"})

            assert result["ok"] is False
            assert "EXA_API_KEY" in result["error"]
            assert upstream.seen == []


class TestXSearch:
    def test_a_key_only_in_the_group_file_is_used(self, tmp_path: Path) -> None:
        with _Upstream(_X_BODY) as upstream:
            root = _workspace(tmp_path, shared=f"XAI_API_KEY=k-shared\nXAI_BASE_URL={upstream.url}\n")

            result = _run_tool("x_search.py", root, {"query": "alpha"})

            assert result["ok"] is True, result
            assert [(seen[0], seen[2]) for seen in upstream.seen] == [("/responses", "Bearer k-shared")]

    def test_the_instance_dotenv_still_wins(self, tmp_path: Path) -> None:
        with _Upstream(_X_BODY) as upstream:
            root = _workspace(
                tmp_path,
                shared=f"XAI_API_KEY=k-shared\nXAI_BASE_URL={upstream.url}\n",
                dotenv="XAI_API_KEY=k-local\n",
            )

            _run_tool("x_search.py", root, {"query": "alpha"})

            assert upstream.seen[0][2] == "Bearer k-local"

    def test_no_key_anywhere_fails_before_any_request(self, tmp_path: Path) -> None:
        with _Upstream(_X_BODY) as upstream:
            root = _workspace(tmp_path, shared=f"XAI_BASE_URL={upstream.url}\n")

            result = _run_tool("x_search.py", root, {"query": "alpha"})

            assert result["ok"] is False
            assert "XAI_API_KEY" in result["error"]
            assert upstream.seen == []


def _main_block(script: Path, stop_before: str) -> dict[str, Any]:
    """执行工具主块里到 stop_before 赋值为止的定义，拿到里面那几个纯函数。

    整块跑下去会读 stdin 并向上游发请求；只跑定义部分就能对真实的那几个函数下断言。
    """
    tree = ast.parse(script.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
    )
    body: list[ast.stmt] = []
    for statement in main.body:
        targets = getattr(statement, "targets", ())
        if isinstance(statement, ast.Assign) and any(getattr(t, "id", "") == stop_before for t in targets):
            break
        body.append(statement)
    namespace: dict[str, Any] = {"__name__": "__main__", "__file__": str(script)}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(script), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def web_search_main() -> dict[str, Any]:
    return _main_block(_TOOLS / "web_search.py", "raw_input")


class TestWebSearchSourcePicking:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("XAI_API_KEY", "OPENAI_API_KEY", "WEB_SEARCH_PROVIDER", "WEB_SEARCH_SOURCE"):
            monkeypatch.delenv(name, raising=False)

    def test_it_loads_both_layers(self, web_search_main: dict[str, Any], tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="XAI_API_KEY=k-shared\n", dotenv="WEB_SEARCH_PROVIDER=exa\n")

        values = web_search_main["load_dotenv"](root)

        assert values == {"XAI_API_KEY": "k-shared", "WEB_SEARCH_PROVIDER": "exa"}

    def test_a_key_only_in_the_group_file_counts_as_configured(
        self, web_search_main: dict[str, Any], tmp_path: Path,
    ) -> None:
        values = web_search_main["load_dotenv"](_workspace(tmp_path, shared="XAI_API_KEY=k-shared\n"))

        assert web_search_main["has_config_key"](values, "XAI_API_KEY", "OPENAI_API_KEY") is True

    @pytest.mark.parametrize("query", ["帮我查 推特 上的讨论", "look up recent tweets", "x 上有什么新消息"])
    def test_a_twitter_question_goes_to_x_on_a_group_key(
        self, web_search_main: dict[str, Any], tmp_path: Path, query: str,
    ) -> None:
        values = web_search_main["load_dotenv"](_workspace(tmp_path, shared="XAI_API_KEY=k-shared\n"))

        assert web_search_main["normalize_source"]({}, query, values) == "x"

    def test_without_any_key_it_stays_on_auto(self, web_search_main: dict[str, Any], tmp_path: Path) -> None:
        values = web_search_main["load_dotenv"](_workspace(tmp_path))

        assert web_search_main["normalize_source"]({}, "帮我查 推特 上的讨论", values) == "auto"

    def test_an_explicit_source_is_not_overridden(self, web_search_main: dict[str, Any], tmp_path: Path) -> None:
        values = web_search_main["load_dotenv"](_workspace(tmp_path, shared="XAI_API_KEY=k-shared\n"))

        assert web_search_main["normalize_source"]({"source": "exa"}, "推特 热点", values) == "exa"

    def test_the_group_file_can_set_the_default_provider(
        self, web_search_main: dict[str, Any], tmp_path: Path,
    ) -> None:
        values = web_search_main["load_dotenv"](_workspace(tmp_path, shared="WEB_SEARCH_PROVIDER=twitter\n"))

        assert web_search_main["normalize_source"]({}, "随便问问", values) == "x"


# ---------------------------------------------------------------------------
#  绘图工具
# ---------------------------------------------------------------------------

class TestDrawtoolsApiKey:
    @pytest.fixture
    def root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        workspace = _workspace(tmp_path, shared="NOVELAI_API_KEY=k-shared\nALT_API_KEY=k-shared-alt\n")
        monkeypatch.setattr(drawtools_common, "WORKSPACE_ROOT", workspace)
        for name in ("NOVELAI_API_KEY", "ALT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        return workspace

    def test_the_settings_key_wins(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVELAI_API_KEY", "k-env")

        assert drawtools_common.resolve_api_key({"api": {"api_key": "k-settings"}}) == "k-settings"

    def test_the_environment_comes_next(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVELAI_API_KEY", "k-env")

        assert drawtools_common.resolve_api_key({}) == "k-env"

    def test_the_group_file_is_the_last_layer(self, root: Path) -> None:
        assert drawtools_common.resolve_api_key({}) == "k-shared"

    def test_the_named_variable_is_looked_up_in_the_group_file_too(self, root: Path) -> None:
        assert drawtools_common.resolve_api_key({"api": {"api_key_env": "ALT_API_KEY"}}) == "k-shared-alt"

    def test_nothing_configured_is_an_empty_key(self, root: Path) -> None:
        assert drawtools_common.resolve_api_key({"api": {"api_key_env": "NOBODY_SET_THIS"}}) == ""

    def test_a_rotated_group_key_is_picked_up_without_a_restart(self, root: Path) -> None:
        assert drawtools_common.resolve_api_key({}) == "k-shared"

        import config_layout

        config_layout.write_config_text(root / "config" / "secrets.env", "NOVELAI_API_KEY=k-rotated\n")

        assert drawtools_common.resolve_api_key({}) == "k-rotated"

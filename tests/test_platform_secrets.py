"""平台密钥从哪读：data/config.yaml 的 platforms 段 > 环境变量 > 平台自己生成的文件。

bot、工具子进程、诊断包各读一遍，顺序必须一样。工具的环境里剥掉了密钥，只能回 config 和 .env 里找。
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

from _channel import platform_secret as tool_platform_secret  # noqa: E402
from _onebot_harness import body_attr, load_runtime  # noqa: E402
from adapters.onebot.account_ops import NapCatClient, NapCatError  # noqa: E402
from clonoth_runtime import (  # noqa: E402
    CONFIG_META_KEYS,
    PLATFORMS_CONFIG_KEY,
    SECRET_FROM_CONFIG,
    SECRET_FROM_ENV,
    PlatformSecret,
    is_channel_block,
    platform_secret,
    undeclared_platform_secrets,
)
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor, SecretField  # noqa: E402
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.operations.service import OperationsService  # noqa: E402
from toolbox._common import safe_subprocess_env  # noqa: E402

BRIDGE_ENV = "ONEBOT_FORWARD_BRIDGE_TOKEN"
WEBUI_ENV = "NAPCAT_WEBUI_TOKEN"
QQ_SECRET_ENVS = ("ONEBOT_ACCESS_TOKEN", BRIDGE_ENV, "ONEBOT_CONVERSATION_HASH_SECRET", WEBUI_ENV)
QQ_FILE_ENVS = ("ONEBOT_FORWARD_BRIDGE_TOKEN_FILE", "ONEBOT_CONVERSATION_HASH_SECRET_FILE")

VAULT = PlatformDescriptor(
    id="vault",
    display_name="Vault Chat",
    short_name="Vault",
    channels=(ChannelDescriptor("vault_room", CHANNEL_GROUP),),
    config_path="config/vault.yaml",
    secrets=(SecretField("signing_key", "签名密钥", env="VAULT_SIGNING"),),
)


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*QQ_SECRET_ENVS, *QQ_FILE_ENVS, "VAULT_SIGNING", "CLONOTH_QQ_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vault():
    register_platform(VAULT)
    try:
        yield VAULT
    finally:
        unregister_platform(VAULT.id)


def _workspace(root: Path, *, config: str = "", dotenv: str = "") -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    if config:
        (root / "data" / "config.yaml").write_text(config, encoding="utf-8")
    if dotenv:
        (root / ".env").write_text(dotenv, encoding="utf-8")
    return root


def _qq(**secrets: Any) -> str:
    lines = [f"{PLATFORMS_CONFIG_KEY}:", f"  {QQ_NAPCAT.id}:"]
    lines.extend(f"    {name}: {json.dumps(value)}" for name, value in secrets.items())
    return "\n".join(lines) + "\n"


class TestRuntimeLookup:
    def test_the_config_file_wins_over_the_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "from-env")
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="from-config"))

        secret = platform_secret(root, QQ_NAPCAT, "forward_bridge_token")

        assert secret == PlatformSecret("from-config", SECRET_FROM_CONFIG, env_shadowed=True)

    def test_the_same_value_in_both_places_is_not_a_shadow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "same")
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="same"))

        assert platform_secret(root, QQ_NAPCAT, "forward_bridge_token").env_shadowed is False

    def test_the_environment_is_used_when_the_file_is_silent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "  from-env  ")

        secret = platform_secret(_workspace(tmp_path), QQ_NAPCAT, "forward_bridge_token")

        assert secret == PlatformSecret("from-env", SECRET_FROM_ENV)

    def test_nothing_configured_leaves_it_to_the_platform(self, tmp_path: Path) -> None:
        secret = platform_secret(_workspace(tmp_path), QQ_NAPCAT, "forward_bridge_token")

        assert secret == PlatformSecret()
        assert secret.source == ""

    def test_a_reference_in_the_file_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_REF", "expanded")
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="${BRIDGE_REF}"))

        assert platform_secret(root, QQ_NAPCAT, "forward_bridge_token").value == "expanded"

    def test_a_purely_numeric_token_is_kept_as_text(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(access_token=123456))

        assert platform_secret(root, QQ_NAPCAT, "access_token").value == "123456"

    @pytest.mark.parametrize("value", [True, ["a"], {"a": 1}, None, ""])
    def test_a_value_that_is_not_a_token_counts_as_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any,
    ) -> None:
        # True 当成令牌 "True" 会让两端都以为配上了，却永远对不上。
        monkeypatch.setenv(BRIDGE_ENV, "from-env")
        root = _workspace(tmp_path, config=_qq(forward_bridge_token=value))

        assert platform_secret(root, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret("from-env", SECRET_FROM_ENV)

    def test_an_explicit_env_mapping_replaces_the_process_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "from-process")

        secret = platform_secret(
            _workspace(tmp_path), QQ_NAPCAT, "forward_bridge_token", environ={BRIDGE_ENV: "from-mapping"},
        )

        assert secret == PlatformSecret("from-mapping", SECRET_FROM_ENV)

    def test_a_reference_is_also_looked_up_in_the_explicit_mapping(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="${BRIDGE_REF}"))

        secret = platform_secret(root, QQ_NAPCAT, "forward_bridge_token", environ={"BRIDGE_REF": "mapped"})

        assert secret.value == "mapped"

    def test_an_already_loaded_config_is_not_read_again(self, tmp_path: Path) -> None:
        config = {PLATFORMS_CONFIG_KEY: {QQ_NAPCAT.id: {"forward_bridge_token": "handed-in"}}}

        assert platform_secret(tmp_path / "missing", QQ_NAPCAT, "forward_bridge_token", config=config).value == "handed-in"

    def test_another_platforms_section_is_not_read(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config="platforms:\n  vault:\n    forward_bridge_token: wrong-platform\n")

        assert platform_secret(root, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret()

    def test_an_undeclared_name_is_a_caller_bug(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError):
            platform_secret(_workspace(tmp_path), QQ_NAPCAT, "bridge_token")

    def test_misspelled_keys_are_listed(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(forward_bridge_tokn="x", access_token="y", Access_Token="z"))

        assert undeclared_platform_secrets(root, QQ_NAPCAT) == ["Access_Token", "forward_bridge_tokn"]

    def test_the_section_is_a_reserved_top_level_key(self) -> None:
        section = {QQ_NAPCAT.id: {"forward_bridge_token": "x"}}

        assert PLATFORMS_CONFIG_KEY in CONFIG_META_KEYS
        assert not is_channel_block(section)


class TestToolLookup:
    def test_the_config_file_wins_over_dotenv(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="from-config"), dotenv=f"{BRIDGE_ENV}=from-dotenv\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == "from-config"

    def test_dotenv_is_read_when_the_variable_was_stripped(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, dotenv=f"{BRIDGE_ENV}='from-dotenv'\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == "from-dotenv"

    def test_the_process_environment_beats_dotenv_like_in_the_bot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "from-env")
        root = _workspace(tmp_path, dotenv=f"{BRIDGE_ENV}=from-dotenv\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == "from-env"

    def test_a_reference_in_the_file_is_expanded_from_dotenv(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(forward_bridge_token="${BRIDGE_REF}"), dotenv="BRIDGE_REF=expanded\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == "expanded"

    @pytest.mark.parametrize("value,expected", [(123456, "123456"), (True, "from-dotenv"), (["a"], "from-dotenv")])
    def test_values_are_judged_like_in_the_bot(self, tmp_path: Path, value: Any, expected: str) -> None:
        root = _workspace(tmp_path, config=_qq(forward_bridge_token=value), dotenv=f"{BRIDGE_ENV}=from-dotenv\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == expected

    def test_nothing_configured_is_an_empty_string(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=root) == ""
        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", root=root) == ""


class _Bridge:
    """只记下每次请求带来的令牌头，回一个成功。"""

    def __init__(self) -> None:
        self.tokens: list[str | None] = []
        seen = self.tokens

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append(self.headers.get("X-Forward-Token"))
                body = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_Bridge":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestForwardToolToken:
    # 带着消息引用和图片引用：解析它们的循环以前复用了 token 这个变量名，把最后一个引用当成令牌发了出去。
    ARGS = json.dumps({"op": "forward", "target_type": "self", "message_refs": ["m1", "m2"], "recent_refs": ["a7"]})

    def _send(self, workspace: Path, monkeypatch: pytest.MonkeyPatch, bridge: _Bridge, **env: str) -> None:
        # 父进程里配着一个错的令牌：真实链路上 toolbox 会先把它剥掉，工具不该看到它。
        monkeypatch.setenv(BRIDGE_ENV, "stale-parent-token")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        child_env = safe_subprocess_env()
        child_env.update({
            "CLONOTH_WORKSPACE": str(workspace),
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(bridge.server.server_port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "1",
            "CLONOTH_SESSION_ID": "session-test",
            "PYTHONIOENCODING": "utf-8",
        })
        done = subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "qq_forward.py")],
            input=self.ARGS, text=True, encoding="utf-8", capture_output=True,
            cwd=workspace, env=child_env, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr or done.stdout

    def test_the_config_file_token_is_sent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_token="from-config"), dotenv=f"{BRIDGE_ENV}=from-dotenv\n")
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge)

        assert bridge.tokens == ["from-config"]

    def test_a_reference_in_the_config_file_is_expanded_from_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_token="${BRIDGE_REF}"), dotenv="BRIDGE_REF=expanded\n")
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge)

        assert bridge.tokens == ["expanded"]

    def test_the_dotenv_token_is_sent_although_the_variable_was_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path, dotenv=f"{BRIDGE_ENV}=from-dotenv\n")
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge)

        assert bridge.tokens == ["from-dotenv"]

    def test_the_self_signed_file_is_the_last_resort(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path)
        (workspace / "data" / "onebot_forward_bridge_token").write_text("self-signed\n", encoding="utf-8")
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge)

        assert bridge.tokens == ["self-signed"]

    def test_a_moved_token_file_is_found_through_its_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path / "ws")
        moved = tmp_path / "elsewhere" / "bridge"
        moved.parent.mkdir()
        moved.write_text("moved\n", encoding="utf-8")
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge, ONEBOT_FORWARD_BRIDGE_TOKEN_FILE=str(moved))

        assert bridge.tokens == ["moved"]

    def test_no_token_anywhere_sends_no_header(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path)
        with _Bridge() as bridge:
            self._send(workspace, monkeypatch, bridge)

        assert bridge.tokens == [None]


class TestNapCatWebuiToken:
    def test_the_dotenv_file_is_read_again_every_time(self, tmp_path: Path) -> None:
        # 部署脚本在容器首启后才把令牌回填进 .env，supervisor 不重启也得认出来。
        root = _workspace(tmp_path)
        client = NapCatClient(root)
        assert client.configured is False

        (root / ".env").write_text(f"{WEBUI_ENV}='backfilled'\n", encoding="utf-8")

        assert client.configured is True
        assert client._webui_token() == "backfilled"

    def test_the_config_file_wins_over_dotenv(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, config=_qq(napcat_webui_token="from-config"), dotenv=f"{WEBUI_ENV}=from-dotenv\n")

        assert NapCatClient(root)._webui_token() == "from-config"

    def test_the_first_assignment_in_dotenv_wins(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, dotenv=f"{WEBUI_ENV}=first\n{WEBUI_ENV}=second\n")

        assert NapCatClient(root)._webui_token() == "first"

    def test_the_process_environment_is_not_consulted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 多开时每个实例连自己的容器，地址和令牌得出自同一份工作区文件；进程环境里那份可能是别的实例的。
        monkeypatch.setenv(WEBUI_ENV, "from-process")

        assert NapCatClient(_workspace(tmp_path))._webui_token() == ""

    def test_a_missing_token_names_both_places_it_can_live(self, tmp_path: Path) -> None:
        client = NapCatClient(_workspace(tmp_path))

        with pytest.raises(NapCatError) as caught:
            asyncio.run(client._authenticate(None))

        assert WEBUI_ENV in str(caught.value)
        assert "platforms.qq.napcat_webui_token" in str(caught.value)


class TestDiagnosticsRedaction:
    def _secrets(self, root: Path) -> tuple[str, ...]:
        return OperationsService(SimpleNamespace(workspace_root=root))._configured_secrets()

    def test_declared_secrets_are_redacted_whatever_they_are_called(
        self, vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # signing_key、VAULT_SIGNING 都不叫 token/secret，按名字猜的那一套认不出它们。
        monkeypatch.setenv("VAULT_SIGNING", "sign-from-env")
        root = _workspace(tmp_path, config="platforms:\n  vault:\n    signing_key: sign-from-config\n")
        service = OperationsService(SimpleNamespace(workspace_root=root))

        secrets = service._configured_secrets()

        assert {"sign-from-config", "sign-from-env"} <= set(secrets)
        assert "sign-from-config" not in service._clean("request signed with sign-from-config", secrets)

    def test_every_platforms_config_file_is_scanned(self, vault, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (root / "config").mkdir()
        (root / "config" / "vault.yaml").write_text("webhook:\n  password: pw-in-vault-config\n", encoding="utf-8")
        (root / "config" / "qq.yaml").write_text("napcat:\n  webui_token: qq-webui-token\n", encoding="utf-8")

        secrets = set(self._secrets(root))

        assert {"pw-in-vault-config", "qq-webui-token"} <= secrets

    def test_auto_generated_secret_files_are_left_out(self, tmp_path: Path) -> None:
        # 里面可能只是模式标记，收进来会把日志里的这个词也抹成 [已隐藏]。
        root = _workspace(tmp_path)
        (root / "data" / "onebot_conversation_hash_secret").write_text("legacy-unsalted", encoding="utf-8")

        assert "legacy-unsalted" not in self._secrets(root)

    def test_no_empty_string_is_ever_redacted(self, tmp_path: Path) -> None:
        assert "" not in self._secrets(_workspace(tmp_path))


def _adapter(monkeypatch: pytest.MonkeyPatch, workspace: Path, **env: str) -> Any:
    return load_runtime(monkeypatch, workspace, **env)


def _config_module(runtime: Any) -> Any:
    return sys.modules[f"{runtime.__name__}.config"]


class TestAdapterStartup:
    def test_configured_secrets_reach_the_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_token="cfg-bridge", conversation_hash_secret="cfg-salt"))

        config = _config_module(_adapter(monkeypatch, workspace))

        assert config.FORWARD_BRIDGE_TOKEN == "cfg-bridge"
        assert config.CONVERSATION_HASH_SECRET == "cfg-salt"
        assert config.SECRET_CONFIG_WARNINGS == ()
        assert config.FORWARD_BRIDGE_TOKEN_TOOLS_WARNING == ""

    def test_the_auto_files_default_to_the_declared_places(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config_module(_adapter(monkeypatch, _workspace(tmp_path)))

        assert Path(config.FORWARD_BRIDGE_TOKEN_FILE) == tmp_path / "data" / "onebot_forward_bridge_token"
        # 干净工作区首启就把摘要密钥钉在声明的位置。
        assert (tmp_path / "data" / "onebot_conversation_hash_secret").is_file()

    def test_the_state_files_follow_the_declared_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        aliases = tmp_path / "elsewhere" / "aliases.json"

        config = _config_module(_adapter(
            monkeypatch, workspace, ONEBOT_STATE_FILE="private/routes.json", ONEBOT_ANON_MAP_FILE=str(aliases),
        ))

        # 相对写法按工作区算，和 supervisor、迁移脚本认的是同一个文件。
        assert Path(config.ONEBOT_STATE_FILE) == workspace / "private" / "routes.json"
        assert Path(config.ANON_MAP_FILE) == aliases

    def test_the_user_profiles_are_read_where_the_env_moved_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        moved = workspace / "private" / "people.yaml"
        moved.parent.mkdir()
        moved.write_text('users:\n  "10001":\n    display_name: 张三\n', encoding="utf-8")
        runtime = _adapter(monkeypatch, workspace, CLONOTH_QQ_USER_PROFILES_PATH="private/people.yaml")

        body_attr(runtime, "_load_user_profiles")()

        # 相对写法按工作区算，外传黑名单认的也是这个位置。
        assert Path(_config_module(runtime).USER_PROFILES_PATH) == moved
        assert runtime.state._adapter.qq_user_profiles == {"10001": {"display_name": "张三"}}

    def test_a_misspelled_key_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_tokn="typo"))

        warnings = _config_module(_adapter(monkeypatch, workspace)).SECRET_CONFIG_WARNINGS

        assert any("forward_bridge_tokn" in message for message in warnings)

    def test_a_shadowed_environment_variable_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_token="cfg-bridge"))

        warnings = _config_module(_adapter(monkeypatch, workspace, **{BRIDGE_ENV: "env-bridge"})).SECRET_CONFIG_WARNINGS

        assert warnings == (f"{BRIDGE_ENV} is ignored: data/config.yaml platforms.qq.forward_bridge_token takes precedence",)

    def test_a_token_only_in_the_process_environment_is_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # systemd Environment= 或 docker -e 配的令牌，工具子进程拿不到，转发会全被 Bridge 拒掉。
        config = _config_module(_adapter(monkeypatch, _workspace(tmp_path), **{BRIDGE_ENV: "env-only"}))

        assert "platforms.qq.forward_bridge_token" in config.FORWARD_BRIDGE_TOKEN_TOOLS_WARNING
        assert BRIDGE_ENV in config.FORWARD_BRIDGE_TOKEN_TOOLS_WARNING

    def test_a_token_the_workspace_dotenv_also_carries_is_fine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path, dotenv=f"{BRIDGE_ENV}=env-token\n")

        config = _config_module(_adapter(monkeypatch, workspace, **{BRIDGE_ENV: "env-token"}))

        assert config.FORWARD_BRIDGE_TOKEN_TOOLS_WARNING == ""

    def test_the_startup_check_logs_what_it_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        workspace = _workspace(tmp_path, config=_qq(access_tokn="typo"))
        runtime = _adapter(monkeypatch, workspace, **{BRIDGE_ENV: "env-only"})

        with caplog.at_level(logging.WARNING, logger="nonebot.plugin.clonoth_agent"):
            body_attr(runtime, "_warn_startup_config")()

        assert "access_tokn" in caplog.text
        assert "tool subprocesses have it stripped" in caplog.text

    def test_no_secret_value_ever_reaches_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        workspace = _workspace(tmp_path, config=_qq(forward_bridge_token="cfg-bridge-value"))
        runtime = _adapter(monkeypatch, workspace, **{BRIDGE_ENV: "env-bridge-value"})

        with caplog.at_level(logging.DEBUG, logger="nonebot.plugin.clonoth_agent"):
            body_attr(runtime, "_warn_startup_config")()

        assert "cfg-bridge-value" not in caplog.text
        assert "env-bridge-value" not in caplog.text


def test_the_forward_tool_reads_nothing_but_the_declaration() -> None:
    # 令牌的 env 名和文件位置只写在 QQ 描述里；工具里再抄一份，改名时必然漏掉一边。
    source = (_ROOT / "tools" / "qq_forward.py").read_text(encoding="utf-8")

    assert "ONEBOT_FORWARD_BRIDGE_TOKEN" not in source
    assert "onebot_forward_bridge_token" not in source

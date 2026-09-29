"""跟着共享组走的密钥：config/secrets.env。

组里几个实例共用它，端口、地址、平台令牌、路径这类实例身份留在各自的 .env，同名时 .env 的算数。
共享密钥不灌进进程环境 —— 别的实例改了它，这边下一次取值就换上，不用重启。
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
from adapters.onebot import account_ops  # noqa: E402
from supervisor.operations.service import OperationsService  # noqa: E402
from toolbox.builtins.manage_secret import manage_secret  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 POSIX 权限位")

# 组里共用的那种密钥，和实例身份那种。
SHARED_KEY = "EXA_API_KEY"
INSTANCE_KEYS = ("CLONOTH_PORT", "ONEBOT_ACCESS_TOKEN", "FOO_PATH", "PYTHONPATH")


def _workspace(tmp_path: Path, *, shared: str = "", dotenv: str = "") -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    if shared:
        config_layout.shared_secrets_path(tmp_path).write_text(shared, encoding="utf-8")
    if dotenv:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
#  dotenv 的一行怎么读、怎么写
# ---------------------------------------------------------------------------

class TestEnvLineKey:
    @pytest.mark.parametrize("line,key", [
        ("ALPHA=1", "ALPHA"),
        ("  ALPHA = 1", "ALPHA"),
        ("export ALPHA=1", "ALPHA"),
        ("  export ALPHA=1", "ALPHA"),
        ("_ALPHA=1", "_ALPHA"),
    ])
    def test_an_assignment_names_its_key(self, line: str, key: str) -> None:
        assert config_layout.env_line_key(line) == key

    @pytest.mark.parametrize("line", ["", "   ", "# ALPHA=1", "ALPHA", "just text", "1ALPHA=1", "AL-PHA=1"])
    def test_anything_else_names_nothing(self, line: str) -> None:
        assert config_layout.env_line_key(line) == ""


class TestEnvAssignment:
    @pytest.mark.parametrize("value", ["sk-alpha", "1024", "https://api.example.com/v1", "a.b:c/d+e=f@g,h%i~j", ""])
    def test_a_bare_value_needs_no_quotes(self, value: str) -> None:
        assert config_layout.env_assignment("K", value) == f"K={value}"

    @pytest.mark.parametrize("value", ["has space", "with#hash", 'a "quoted" word'])
    def test_spaces_and_hashes_get_single_quotes(self, value: str) -> None:
        # 不加引号的话 # 之后会被当成注释截掉，空格在 systemd 的 EnvironmentFile 里也读不回来。
        assert config_layout.env_assignment("K", value) == f"K='{value}'"

    @pytest.mark.parametrize("value", ["it's", "back\\slash", "both ' and \\"])
    def test_a_quote_or_backslash_is_written_raw(self, value: str) -> None:
        # 两种解析器对引号里的转义理解不一致，加引号反而读回来不是原值。
        assert config_layout.env_assignment("K", value) == f"K={value}"

    def test_what_is_written_reads_back_the_same(self, tmp_path: Path) -> None:
        values = {"BARE": "sk-alpha", "SPACED": "two words", "HASHED": "a#b", "EQUALS": "a=b"}
        path = tmp_path / ".env"
        path.write_text("\n".join(config_layout.env_assignment(k, v) for k, v in values.items()) + "\n", encoding="utf-8")

        assert dict(config_layout.read_env_file(path)) == values


# ---------------------------------------------------------------------------
#  读一份 dotenv
# ---------------------------------------------------------------------------

class TestReadEnvFile:
    def test_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert dict(config_layout.read_env_file(tmp_path / "nope.env")) == {}

    def test_it_parses_like_the_process_start_up_does(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("\n".join([
            "# 注释",
            "PLAIN=alpha",
            "export EXPORTED=beta",
            "QUOTED='two words'",
            'DOUBLED="three words"',
            "",
        ]), encoding="utf-8")

        assert dict(config_layout.read_env_file(path)) == {
            "PLAIN": "alpha", "EXPORTED": "beta", "QUOTED": "two words", "DOUBLED": "three words",
        }

    def test_empty_values_are_dropped(self, tmp_path: Path) -> None:
        # 写了个空值和没写是一回事：留着它会盖住下面一层里真正配上的值。
        path = tmp_path / ".env"
        path.write_text("SET=alpha\nEMPTY=\nBLANK='   '\n", encoding="utf-8")

        assert dict(config_layout.read_env_file(path)) == {"SET": "alpha"}

    def test_the_mapping_cannot_be_written_to(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("SET=alpha\n", encoding="utf-8")

        with pytest.raises(TypeError):
            config_layout.read_env_file(path)["SET"] = "beta"  # type: ignore[index]

    def test_a_rewrite_is_picked_up(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("SET=alpha\n", encoding="utf-8")
        assert config_layout.read_env_file(path)["SET"] == "alpha"

        config_layout.write_config_text(path, "SET=beta\n")

        assert config_layout.read_env_file(path)["SET"] == "beta"

    def test_a_replacement_with_the_same_time_and_size_is_still_noticed(self, tmp_path: Path) -> None:
        # 同目录临时文件替换每次都换 inode：不看 inode 的话这一笔会一直读到旧值。
        path = tmp_path / ".env"
        path.write_text("K=1\n", encoding="utf-8")
        before = path.stat()
        assert config_layout.read_env_file(path)["K"] == "1"

        config_layout.write_config_text(path, "K=2\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
        assert config_layout.read_env_file(path)["K"] == "2"

    def test_the_same_bytes_come_from_the_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / ".env"
        path.write_text("SET=alpha\n", encoding="utf-8")
        first = config_layout.read_env_file(path)

        # 第二次不该再去解析：缓存键是修改时间、大小和 inode。
        import dotenv

        monkeypatch.setattr(dotenv, "dotenv_values", _boom)
        assert config_layout.read_env_file(path) is first


def _boom(*args: Any, **kwargs: Any) -> dict[str, str]:
    raise AssertionError("缓存命中时不该重新解析")


class TestSharedSecrets:
    def test_it_reads_the_group_file(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared=f"{SHARED_KEY}=k-shared\n")

        assert dict(config_layout.shared_secrets(root)) == {SHARED_KEY: "k-shared"}

    def test_a_workspace_without_one_is_empty(self, tmp_path: Path) -> None:
        assert dict(config_layout.shared_secrets(tmp_path)) == {}

    def test_it_sits_under_config(self, tmp_path: Path) -> None:
        assert config_layout.shared_secrets_path(tmp_path) == tmp_path / "config" / "secrets.env"


class TestTheLockFileName:
    def test_a_dotfile_does_not_get_a_second_dot(self, tmp_path: Path) -> None:
        assert config_layout._lock_path(tmp_path / ".env").name == ".env.lock"

    def test_an_ordinary_file_gets_a_hidden_lock(self, tmp_path: Path) -> None:
        assert config_layout._lock_path(tmp_path / "secrets.env").name == ".secrets.env.lock"


# ---------------------------------------------------------------------------
#  改一份 dotenv
# ---------------------------------------------------------------------------

class TestEditEnvFile:
    def test_other_lines_are_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("# 头注释\nKEEP=1\nTARGET=old\n\n# 尾注释\n", encoding="utf-8")

        assert config_layout.edit_env_file(path, {"TARGET": "new"}) is True
        assert path.read_text(encoding="utf-8") == "# 头注释\nKEEP=1\nTARGET=new\n\n# 尾注释\n"

    def test_a_new_key_is_appended(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=1\n", encoding="utf-8")

        config_layout.edit_env_file(path, {"NEW": "v"})

        assert path.read_text(encoding="utf-8") == "KEEP=1\nNEW=v\n"

    def test_only_the_first_of_several_lines_survives(self, tmp_path: Path) -> None:
        # dotenv 取最后一行：留着重复行就改不动这个键。
        path = tmp_path / ".env"
        path.write_text("TARGET=one\nKEEP=1\nTARGET=two\n", encoding="utf-8")

        config_layout.edit_env_file(path, {"TARGET": "new"})

        assert path.read_text(encoding="utf-8") == "TARGET=new\nKEEP=1\n"
        assert dict(config_layout.read_env_file(path))["TARGET"] == "new"

    def test_none_deletes_every_line_of_that_key(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("DROP=one\nKEEP=1\nDROP=two\n", encoding="utf-8")

        assert config_layout.edit_env_file(path, {"DROP": None}) is True
        assert path.read_text(encoding="utf-8") == "KEEP=1\n"

    def test_deleting_a_key_that_is_not_there_changes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=1\n", encoding="utf-8")
        before = path.read_bytes()

        assert config_layout.edit_env_file(path, {"GHOST": None}) is False
        assert path.read_bytes() == before

    def test_writing_the_same_value_again_changes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=1\nTARGET=same\n", encoding="utf-8")
        before = path.stat().st_mtime_ns, path.read_bytes()

        assert config_layout.edit_env_file(path, {"TARGET": "same"}) is False
        assert (path.stat().st_mtime_ns, path.read_bytes()) == before

    def test_deleting_the_only_key_leaves_an_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("ONLY=1\n", encoding="utf-8")

        assert config_layout.edit_env_file(path, {"ONLY": None}) is True
        assert path.read_bytes() == b""

    def test_a_brand_new_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "secrets.env"

        assert config_layout.edit_env_file(path, {"NEW": "v"}) is True
        assert path.read_text(encoding="utf-8") == "NEW=v\n"

    @_POSIX_ONLY
    def test_a_brand_new_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "secrets.env"

        config_layout.edit_env_file(path, {"NEW": "v"})

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_several_keys_in_one_call(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("A=1\nB=2\n", encoding="utf-8")

        assert config_layout.edit_env_file(path, {"A": "9", "B": None, "C": "3"}) is True
        assert path.read_text(encoding="utf-8") == "A=9\nC=3\n"


# ---------------------------------------------------------------------------
#  谁的值算数：os.environ > extra > 共享密钥
# ---------------------------------------------------------------------------

@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """钉住工作区的进程，共享密钥这一层才存在。"""
    root = _workspace(tmp_path, shared=f"{SHARED_KEY}=k-shared\n")
    monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))
    monkeypatch.delenv(SHARED_KEY, raising=False)
    return root


class TestSharedEnvValue:
    def test_it_reads_the_pinned_workspace(self, pinned: Path) -> None:
        assert clonoth_runtime.shared_env_value(SHARED_KEY) == "k-shared"

    def test_an_unpinned_process_has_no_shared_layer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _workspace(tmp_path, shared=f"{SHARED_KEY}=k-shared\n")
        for name in ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.chdir(root)

        assert clonoth_runtime.shared_env_value(SHARED_KEY) == ""

    def test_the_alias_variable_pins_it_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _workspace(tmp_path, shared=f"{SHARED_KEY}=k-shared\n")
        monkeypatch.delenv("CLONOTH_WORKSPACE", raising=False)
        monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(root))

        assert clonoth_runtime.shared_env_value(SHARED_KEY) == "k-shared"

    def test_a_key_nobody_configured_is_empty(self, pinned: Path) -> None:
        assert clonoth_runtime.shared_env_value("NOTHING_HERE") == ""


class TestResolutionOrder:
    def test_the_process_environment_wins(self, pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SHARED_KEY, "k-env")

        assert clonoth_runtime._resolve_env_names(SHARED_KEY, {SHARED_KEY: "k-extra"}) == "k-env"

    def test_the_dotenv_mapping_beats_the_shared_file(self, pinned: Path) -> None:
        assert clonoth_runtime._resolve_env_names(SHARED_KEY, {SHARED_KEY: "k-extra"}) == "k-extra"

    def test_the_shared_file_is_the_bottom_layer(self, pinned: Path) -> None:
        assert clonoth_runtime._resolve_env_names(SHARED_KEY, {}) == "k-shared"

    def test_the_fallback_list_falls_through_to_the_shared_file(self, pinned: Path) -> None:
        assert clonoth_runtime._resolve_env_names(f"NOT_SET|{SHARED_KEY}") == "k-shared"

    @pytest.mark.parametrize("text", ["${" + SHARED_KEY + "}", "$ENV{" + SHARED_KEY + "}"])
    def test_a_config_reference_resolves_through_it(self, pinned: Path, text: str) -> None:
        assert clonoth_runtime.resolve_env_ref(text) == "k-shared"

    def test_a_changed_secrets_file_is_used_without_a_restart(self, pinned: Path) -> None:
        assert clonoth_runtime.shared_env_value(SHARED_KEY) == "k-shared"

        config_layout.write_config_text(config_layout.shared_secrets_path(pinned), f"{SHARED_KEY}=k-rotated\n")

        assert clonoth_runtime.shared_env_value(SHARED_KEY) == "k-rotated"
        assert clonoth_runtime.resolve_env_ref("${" + SHARED_KEY + "}") == "k-rotated"

    def test_it_never_leaks_into_the_process_environment(self, pinned: Path) -> None:
        clonoth_runtime.shared_env_value(SHARED_KEY)

        assert SHARED_KEY not in os.environ


# ---------------------------------------------------------------------------
#  manage_secret
# ---------------------------------------------------------------------------

class _Ctx(ToolContext):
    """离线 ToolContext：记下 request_guard 问过什么，按预设 safety_level 答复。"""

    def __init__(self, workspace_root: Path, *, safety: str = "auto") -> None:
        super().__init__(
            supervisor_url="http://unused", session_id="sess-1", run_id="run-1", worker_id="worker-1",
            workspace_root=workspace_root, http=None, registry=None,  # type: ignore[arg-type]
        )
        self._safety = safety
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.asked.append((op, dict(parameters)))
        return {"safety_level": self._safety, "reason": "policy said no"}

    async def check_cancelled(self) -> bool:
        return False


def _run(args: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    return asyncio.run(manage_secret(args, ctx))


@pytest.fixture
def secret_ctx(tmp_path: Path) -> _Ctx:
    _workspace(tmp_path)
    return _Ctx(tmp_path)


class TestManageSecretTargets:
    @pytest.mark.parametrize("name", INSTANCE_KEYS)
    def test_an_instance_key_only_ever_lands_in_dotenv(self, secret_ctx: _Ctx, tmp_path: Path, name: str) -> None:
        result = _run({"action": "set", "name": name, "value": "v-instance"}, secret_ctx)

        assert result["ok"] is True, result
        assert result["data"]["target"] == ".env"
        assert dict(config_layout.read_env_file(tmp_path / ".env")) == {name: "v-instance"}
        assert not config_layout.shared_secrets_path(tmp_path).exists()

    def test_a_shareable_key_goes_to_the_group_file(self, secret_ctx: _Ctx, tmp_path: Path) -> None:
        result = _run({"action": "set", "name": SHARED_KEY, "value": "k-shared"}, secret_ctx)

        assert result["data"]["target"] == config_layout.SHARED_SECRETS_FILE
        assert dict(config_layout.shared_secrets(tmp_path)) == {SHARED_KEY: "k-shared"}
        # 本实例 .env 里本来没有这个键，就不要凭空加一行盖住共享的那份。
        assert not (tmp_path / ".env").exists()
        assert result["data"]["instance_updated"] is False

    def test_a_shadowed_key_is_updated_in_both_places(self, secret_ctx: _Ctx, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"{SHARED_KEY}=k-old-local\n", encoding="utf-8")

        result = _run({"action": "set", "name": SHARED_KEY, "value": "k-new"}, secret_ctx)

        assert result["data"]["instance_updated"] is True
        assert dict(config_layout.read_env_file(tmp_path / ".env"))[SHARED_KEY] == "k-new"
        assert dict(config_layout.shared_secrets(tmp_path))[SHARED_KEY] == "k-new"

    def test_unset_clears_both_files(self, secret_ctx: _Ctx, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"{SHARED_KEY}=k-local\nKEEP=1\n", encoding="utf-8")
        config_layout.shared_secrets_path(tmp_path).write_text(f"{SHARED_KEY}=k-shared\n", encoding="utf-8")

        result = _run({"action": "unset", "name": SHARED_KEY}, secret_ctx)

        assert result["ok"] is True, result
        assert dict(config_layout.read_env_file(tmp_path / ".env")) == {"KEEP": "1"}
        assert dict(config_layout.shared_secrets(tmp_path)) == {}

    def test_unsetting_something_that_was_never_there_is_not_an_error(
        self, secret_ctx: _Ctx, tmp_path: Path,
    ) -> None:
        result = _run({"action": "unset", "name": "GHOST_API_KEY"}, secret_ctx)

        assert result["ok"] is True
        assert "本来就没有配置" in result["data"]["result"]


class TestManageSecretList:
    def test_it_separates_instance_and_shared(self, secret_ctx: _Ctx, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("CLONOTH_PORT=8770\nBOTH_API_KEY=k1\n", encoding="utf-8")
        config_layout.shared_secrets_path(tmp_path).write_text("BOTH_API_KEY=k1\nONLY_SHARED_KEY=k2\n", encoding="utf-8")

        data = _run({"action": "list"}, secret_ctx)["data"]

        assert data["instance"] == ["CLONOTH_PORT", "BOTH_API_KEY"]
        assert data["shared"] == ["BOTH_API_KEY", "ONLY_SHARED_KEY"]
        assert data["names"] == ["CLONOTH_PORT", "BOTH_API_KEY", "ONLY_SHARED_KEY"]
        assert data["count"] == 3

    def test_the_where_labels_say_which_file_wins(self, secret_ctx: _Ctx, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("CLONOTH_PORT=8770\nBOTH_API_KEY=k1\n", encoding="utf-8")
        config_layout.shared_secrets_path(tmp_path).write_text("BOTH_API_KEY=k9\nONLY_SHARED_KEY=k2\n", encoding="utf-8")

        lines = _run({"action": "list"}, secret_ctx)["data"]["result"].splitlines()

        assert lines == [
            "CLONOTH_PORT — 已配置（本实例 .env）",
            "BOTH_API_KEY — 已配置（本实例 .env，盖过共享的同名项）",
            f"ONLY_SHARED_KEY — 已配置（共享 {config_layout.SHARED_SECRETS_FILE}）",
        ]

    def test_nothing_configured_says_so(self, secret_ctx: _Ctx) -> None:
        data = _run({"action": "list"}, secret_ctx)["data"]

        assert data == {"result": "尚未配置任何密钥。", "names": [], "instance": [], "shared": [], "count": 0}

    @pytest.mark.parametrize("action,args", [
        ("list", {}),
        ("set", {"name": SHARED_KEY, "value": "k-very-secret-value"}),
        ("unset", {"name": SHARED_KEY}),
    ])
    def test_no_value_ever_shows_up_in_a_result(
        self, secret_ctx: _Ctx, tmp_path: Path, action: str, args: dict[str, Any],
    ) -> None:
        (tmp_path / ".env").write_text("CLONOTH_PORT=8770\n", encoding="utf-8")
        config_layout.shared_secrets_path(tmp_path).write_text(f"{SHARED_KEY}=k-very-secret-value\n", encoding="utf-8")

        result = _run({"action": action, **args}, secret_ctx)

        assert "k-very-secret-value" not in str(result)
        assert "8770" not in str(result)


class TestManageSecretGuard:
    def test_listing_asks_about_the_virtual_root(self, secret_ctx: _Ctx) -> None:
        _run({"action": "list"}, secret_ctx)

        assert secret_ctx.asked == [("read_file", {"path": ".secret"})]

    @pytest.mark.parametrize("action,extra", [("set", {"value": "v"}), ("unset", {})])
    def test_writing_asks_about_the_named_key(self, secret_ctx: _Ctx, action: str, extra: dict[str, Any]) -> None:
        _run({"action": action, "name": SHARED_KEY, **extra}, secret_ctx)

        assert secret_ctx.asked == [("write_file", {"path": f".secret/{SHARED_KEY}"})]

    def test_a_denied_guard_stops_the_write(self, tmp_path: Path) -> None:
        _workspace(tmp_path)
        ctx = _Ctx(tmp_path, safety="deny")

        result = _run({"action": "set", "name": SHARED_KEY, "value": "k"}, ctx)

        assert result["ok"] is False
        assert not config_layout.shared_secrets_path(tmp_path).exists()
        assert not (tmp_path / ".env").exists()

    def test_a_denied_guard_stops_the_listing(self, tmp_path: Path) -> None:
        _workspace(tmp_path)
        ctx = _Ctx(tmp_path, safety="deny")

        result = _run({"action": "list"}, ctx)

        assert result["ok"] is False
        assert "data" not in result or "names" not in result["data"]

    def test_a_bad_name_is_refused_before_the_guard_is_asked(self, secret_ctx: _Ctx) -> None:
        result = _run({"action": "set", "name": "lower_case", "value": "v"}, secret_ctx)

        assert result["ok"] is False
        assert secret_ctx.asked == []


# ---------------------------------------------------------------------------
#  NapCat 的 .env：和进程启动时灌 .env 用同一个解析器
# ---------------------------------------------------------------------------

WEBUI_TOKEN_ENV = "NAPCAT_WEBUI_TOKEN"
WEBUI_URL_ENV = "NAPCAT_WEBUI_URL"


@pytest.fixture
def napcat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只有 .env 的工作区。进程环境里那份可能是别的实例的，先清掉。"""
    for name in (WEBUI_TOKEN_ENV, WEBUI_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _dotenv(root: Path, text: str) -> Path:
    path = root / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class TestTheNapCatTokenIsReadAgainEveryTime:
    def test_a_backfilled_token_is_noticed_without_a_restart(self, napcat: Path) -> None:
        # 令牌是容器首启后才回填进 .env 的，supervisor 不重启也得认出来。
        client = account_ops.NapCatClient(napcat)
        assert client.configured is False

        _dotenv(napcat, f"{WEBUI_TOKEN_ENV}=backfilled\n")

        assert client.configured is True
        assert client._webui_token() == "backfilled"

    def test_an_empty_placeholder_is_not_configured(self, napcat: Path) -> None:
        # 部署脚本先写一行占位，那不算配过。
        _dotenv(napcat, f"{WEBUI_TOKEN_ENV}=\n")

        assert account_ops.NapCatClient(napcat).configured is False

    def test_a_removed_token_stops_being_configured(self, napcat: Path) -> None:
        path = _dotenv(napcat, f"{WEBUI_TOKEN_ENV}=backfilled\n")
        client = account_ops.NapCatClient(napcat)
        assert client.configured is True

        config_layout.write_config_text(path, "OTHER=1\n")

        assert client.configured is False

    def test_the_last_assignment_wins(self, napcat: Path) -> None:
        # 和进程启动时灌 .env 一致：dotenv 取最后一行，否则改了一行还以为没生效。
        _dotenv(napcat, f"{WEBUI_TOKEN_ENV}=first\n{WEBUI_TOKEN_ENV}=second\n")

        assert account_ops.NapCatClient(napcat)._webui_token() == "second"

    @pytest.mark.parametrize("line,expected", [
        (f"export {WEBUI_TOKEN_ENV}=exported", "exported"),
        (f"{WEBUI_TOKEN_ENV}=quoted-token # 注释", "quoted-token"),
        (f"{WEBUI_TOKEN_ENV}='two words'", "two words"),
        (f"{WEBUI_TOKEN_ENV}=  padded  ", "padded"),
    ])
    def test_the_line_is_read_like_the_process_start_up_does(
        self, napcat: Path, line: str, expected: str,
    ) -> None:
        _dotenv(napcat, line + "\n")

        assert account_ops.NapCatClient(napcat)._webui_token() == expected

    def test_the_platform_secrets_file_still_wins(self, napcat: Path) -> None:
        (napcat / "data" / "platform_secrets.yaml").write_text(
            "qq:\n  napcat_webui_token: from-config\n", encoding="utf-8",
        )
        _dotenv(napcat, f"{WEBUI_TOKEN_ENV}=from-dotenv\n")

        assert account_ops.NapCatClient(napcat)._webui_token() == "from-config"

    def test_the_process_environment_is_still_not_consulted(
        self, napcat: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 多开时每个实例连自己的容器，令牌只能出自本实例工作区的 .env。
        monkeypatch.setenv(WEBUI_TOKEN_ENV, "from-process")

        assert account_ops.NapCatClient(napcat)._webui_token() == ""


class TestTheNapCatUrlComesFromTheSameFile:
    def test_it_is_read_from_dotenv(self, napcat: Path) -> None:
        _dotenv(napcat, f"{WEBUI_URL_ENV}=http://127.0.0.1:6199\n")

        assert account_ops.NapCatClient(napcat)._base == "http://127.0.0.1:6199"

    def test_a_trailing_slash_is_dropped(self, napcat: Path) -> None:
        _dotenv(napcat, f"{WEBUI_URL_ENV}=http://127.0.0.1:6199/\n")

        assert account_ops.NapCatClient(napcat)._base == "http://127.0.0.1:6199"

    def test_the_last_assignment_wins(self, napcat: Path) -> None:
        _dotenv(napcat, f"{WEBUI_URL_ENV}=http://127.0.0.1:6199\n{WEBUI_URL_ENV}=http://127.0.0.1:6299\n")

        assert account_ops.NapCatClient(napcat)._base == "http://127.0.0.1:6299"

    @pytest.mark.parametrize("line", [
        f"export {WEBUI_URL_ENV}=http://127.0.0.1:6199",
        f"{WEBUI_URL_ENV}=http://127.0.0.1:6199 # 第二个实例",
        f"{WEBUI_URL_ENV}='http://127.0.0.1:6199'",
    ])
    def test_the_line_is_read_like_the_process_start_up_does(self, napcat: Path, line: str) -> None:
        _dotenv(napcat, line + "\n")

        assert account_ops.NapCatClient(napcat)._base == "http://127.0.0.1:6199"

    @pytest.mark.parametrize("text", ["", f"{WEBUI_URL_ENV}=\n", "OTHER=1\n"])
    def test_nothing_configured_falls_back_to_the_default_port(self, napcat: Path, text: str) -> None:
        if text:
            _dotenv(napcat, text)

        assert account_ops.NapCatClient(napcat)._base == "http://127.0.0.1:6099"

    def test_an_explicit_base_url_beats_the_file(self, napcat: Path) -> None:
        _dotenv(napcat, f"{WEBUI_URL_ENV}=http://127.0.0.1:6199\n")

        assert account_ops.NapCatClient(napcat, base_url="http://127.0.0.1:6399")._base == "http://127.0.0.1:6399"

    def test_the_url_and_the_token_come_from_one_file(self, napcat: Path) -> None:
        # 分成两个来源迟早会拿甲的令牌敲乙的容器。
        _dotenv(napcat, f"{WEBUI_URL_ENV}=http://127.0.0.1:6199\n{WEBUI_TOKEN_ENV}=token-6199\n")
        client = account_ops.NapCatClient(napcat)

        assert (client._base, client._webui_token()) == ("http://127.0.0.1:6199", "token-6199")


# ---------------------------------------------------------------------------
#  诊断
# ---------------------------------------------------------------------------

def _ops(root: Path) -> OperationsService:
    return OperationsService(SimpleNamespace(
        workspace_root=root, tasks={}, _lock=threading.RLock(), _task_terminal=lambda _: False,
        feature_config_store=None,
    ))


class TestDiagnosticsSecrets:
    def test_the_group_file_values_are_redacted_too(self, tmp_path: Path) -> None:
        # 共享密钥不灌进进程环境，按 os.environ 收一遍收不到它们。
        root = _workspace(tmp_path, shared="SOME_API_KEY=k-shared-secret\nWEBHOOK_TOKEN=t-shared\n")

        secrets = _ops(root)._configured_secrets()

        assert {"k-shared-secret", "t-shared"} <= set(secrets)
        assert "k-shared-secret" not in _ops(root)._clean("called with k-shared-secret", secrets)

    def test_a_name_that_does_not_look_like_a_secret_is_left_alone(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, shared="EXA_BASE_URL=https://api.example.com\n")

        assert "https://api.example.com" not in set(_ops(root)._configured_secrets())


class TestDiagnoseReportsABrokenModelConfig:
    def _checks(self, root: Path) -> dict[str, dict[str, Any]]:
        report = asyncio.run(_ops(root).diagnose())
        return {check["name"]: check for check in report["checks"]}

    def test_a_broken_file_becomes_a_warning(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        config_layout.models_path(root).write_text("openai: [1, 2\n", encoding="utf-8")

        check = self._checks(root)["模型配置文件"]

        assert check["status"] == "warning"
        assert "上一份能用的配置" in check["detail"]
        assert config_layout.MODELS_FILE in check["action"]

    def test_a_readable_file_raises_no_such_check(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        config_layout.models_path(root).write_text("version: 1\nprovider: openai\n", encoding="utf-8")

        assert "模型配置文件" not in self._checks(root)

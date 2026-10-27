"""哪些环境变量是实例身份、哪些只认进程环境，以及数据根下被占着的那几个名字。

共享组把一个实例的 .env 抄进组里给别人用。端口、监听地址、平台令牌、路径抄过去，组里的实例就会去抢同一个端口、
用同一个令牌登录、把文件写到别人家里 —— 所以 is_instance_env 漏一个就是一次串号。只认进程环境的设置写进共享
密钥文件谁也读不到，manage_secret 报成功就是在骗人。数据根下 stickers 和 shared 两个目录不是实例，实例叫这名字
工作区就落到它们身上。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from adapters.onebot import process_env as qq_process_env  # noqa: E402
from adapters.onebot import provisioning as qq_provisioning  # noqa: E402
from adapters.onebot.live_config import LIVE_KEYS  # noqa: E402
from clonoth_runtime import SYSTEM_MODEL_SLOTS  # noqa: E402
from platforms import protection  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import HOOK_PROCESS_ENV, PlatformDescriptor  # noqa: E402
from platforms.protection import (  # noqa: E402
    BASIS_DECLARED,
    BASIS_INJECTED,
    BASIS_INTERPRETER,
    BASIS_PATH,
    BASIS_PROXY,
    BASIS_SETTING,
    ENV_INSTANCE,
    ENV_PROCESS,
    ENV_SHARED,
    EnvScope,
    env_scope,
    instance_env_names,
    is_instance_env,
    is_process_env,
    is_shareable_env,
    process_env_names,
)
from platforms.registry import PLATFORM_ENV  # noqa: E402
from supervisor import provisioning  # noqa: E402
from supervisor.instances import ports_for  # noqa: E402
from supervisor.share_groups import _make_dir_link  # noqa: E402

DEPLOY = _ROOT / "deploy"
SYSTEMD = DEPLOY / "systemd"
INSTALLER = DEPLOY / "install_provision.sh"
NEW_INSTANCE = DEPLOY / "new_instance.sh"
PROVISION = DEPLOY / "provision_instance.sh"
DATA_ROOT = "/opt/kazebot-data"


def _directives(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _script_code(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class TestDeclaringInstanceEnv:
    @pytest.mark.parametrize("name", ["host", "Host", "2HOST", "_HOST", "HOST-2", "HOST.PORT", ""])
    def test_a_name_that_is_not_upper_snake_is_refused(self, name: str) -> None:
        # 声明写歪了对不上 .env 里真正那一行，等于这个变量没被认成实例身份。
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            PlatformDescriptor(id="x", display_name="X", short_name="X", instance_env=(name,))

    def test_declared_names_are_frozen_into_a_tuple(self) -> None:
        platform = PlatformDescriptor(
            id="x", display_name="X", short_name="X", instance_env=["HOST", "PORT"],
        )

        assert platform.instance_env == ("HOST", "PORT")

    def test_a_platform_may_declare_none(self) -> None:
        assert PlatformDescriptor(id="x", display_name="X", short_name="X").instance_env == ()


class TestTheQqDeclarationMatchesTheHook:
    @pytest.mark.parametrize("idx", [0, 1])
    def test_every_line_the_hook_writes_is_declared(self, idx: int) -> None:
        # 声明漏一行，建组时那一行就跟着配置抄给别的实例：两个实例抢同一个端口、用同一个令牌。
        ports = {port.name: ports_for(idx)[port.name] for port in QQ_NAPCAT.instance_ports}

        written = [line.split("=", 1)[0] for line in qq_provisioning.instance_env(idx, ports)]

        assert set(written) <= set(QQ_NAPCAT.instance_env)

    def test_nothing_is_declared_that_the_hook_never_writes(self) -> None:
        ports = {port.name: ports_for(1)[port.name] for port in QQ_NAPCAT.instance_ports}
        written = {line.split("=", 1)[0] for line in qq_provisioning.instance_env(1, ports)}

        assert set(QQ_NAPCAT.instance_env) == written

    def test_the_root_instance_writes_a_subset(self) -> None:
        # 序号 0 的 NapCat 跑 --network host，没有反向连接令牌那一行。
        ports = {port.name: ports_for(0)[port.name] for port in QQ_NAPCAT.instance_ports}
        written = {line.split("=", 1)[0] for line in qq_provisioning.instance_env(0, ports)}

        assert written == set(QQ_NAPCAT.instance_env) - {"ONEBOT_ACCESS_TOKEN"}


class TestWhatCountsAsInstanceIdentity:
    @pytest.mark.parametrize("name", [
        PLATFORM_ENV,
        "CLONOTH_PORT",
        "CLONOTH_BASE_URL",
        "CLONOTH_HOST",
        "CLONOTH_URL_PREFIX",
        "CLONOTH_WORKSPACE",
        "ONEBOT_WORKSPACE_ROOT",
        config_layout.INSTANCES_FILE_ENV,
        "CLONOTH_ADMIN_TOKEN",
        "CLONOTH_MAINTENANCE_SCOPE",
    ])
    def test_the_core_identity_never_travels(self, name: str) -> None:
        assert is_instance_env(name) is True

    @pytest.mark.parametrize("name", [
        "HOST", "PORT", "ONEBOT_FORWARD_BRIDGE_PORT", "NAPCAT_WEBUI_URL",
        "NAPCAT_WEBUI_TOKEN", "ONEBOT_ACCESS_TOKEN",
    ])
    def test_what_the_qq_hook_writes_never_travels(self, name: str) -> None:
        assert is_instance_env(name) is True

    @pytest.mark.parametrize("name", [
        "ONEBOT_STATE_FILE", "CLONOTH_LOG_PATH", "SKILLS_DIR", "DATA_ROOT", "EXTRA_ROOTS",
    ])
    def test_anything_naming_a_place_never_travels(self, name: str) -> None:
        # 值是路径的变量指着本实例工作区里的某处，抄给别的实例就指到别人家里去了。
        assert is_instance_env(name) is True

    @pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONIOENCODING", "PYTHONUNBUFFERED"])
    def test_the_interpreter_settings_never_travel(self, name: str) -> None:
        # 解释器只在启动时读进程环境：不是实例身份，但也一样不跟着组走。
        assert is_instance_env(name) is False
        assert is_process_env(name) is True
        assert is_shareable_env(name) is False

    @pytest.mark.parametrize("name", [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY", "DRAW_TAG_MODEL", "OPENAI_BASE_URL",
    ])
    def test_a_model_or_tool_key_may_travel(self, name: str) -> None:
        assert is_instance_env(name) is False

    @pytest.mark.parametrize("raw", ["  openai_api_key  ", "OpenAI_Api_Key"])
    def test_a_shared_key_is_not_smuggled_in_by_spelling_it_oddly(self, raw: str) -> None:
        assert is_instance_env(raw) is False

    @pytest.mark.parametrize("raw", ["  clonoth_port ", "Clonoth_Port", "clonoth_workspace"])
    def test_identity_is_recognised_however_it_is_spelled(self, raw: str) -> None:
        # .env 里手写小写的人不少，比对前统一成大写，否则这一行照样被抄走。
        assert is_instance_env(raw) is True

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_an_empty_name_is_not_identity(self, name: str | None) -> None:
        assert is_instance_env(name) is False

    def test_every_platform_secret_and_moved_file_is_identity(self) -> None:
        names = instance_env_names()

        for secret in QQ_NAPCAT.secrets:
            if secret.env:
                assert secret.env in names
            if secret.file_env:
                assert secret.file_env in names
        assert QQ_NAPCAT.config_env in names and QQ_NAPCAT.profiles_env in names
        assert set(QQ_NAPCAT.state_env.values()) <= names

    def test_only_the_platforms_you_hand_in_are_considered(self) -> None:
        other = PlatformDescriptor(
            id="x", display_name="X", short_name="X", instance_env=("X_BRIDGE_TOKEN",),
        )

        assert is_instance_env("X_BRIDGE_TOKEN", [other]) is True
        assert is_instance_env("X_BRIDGE_TOKEN", [QQ_NAPCAT]) is False


_QQ_LIVE_ENV = sorted({env for key in LIVE_KEYS for env in key.env})
_SLOT_ENV = sorted(
    f"{spec.env_prefix}_{suffix}"
    for spec in SYSTEM_MODEL_SLOTS
    for suffix in ("MODEL", "BASE_URL", "API_KEY", "PROVIDER")
)


@pytest.fixture
def fresh_hook_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protection, "_warned_hooks", set())


def _platform_with_hook(module_name: str) -> PlatformDescriptor:
    return PlatformDescriptor(id="x", display_name="X", short_name="X", hooks={HOOK_PROCESS_ENV: module_name})


class TestWhereEachVariableBelongs:
    @pytest.mark.parametrize("name", sorted(protection._CORE_PROCESS_SETTINGS))
    def test_a_core_setting_is_read_from_the_process_only(self, name: str) -> None:
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_SETTING)

    @pytest.mark.parametrize("name", sorted(protection._CORE_INJECTED_ENV))
    def test_runtime_context_is_injected_not_configured(self, name: str) -> None:
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_INJECTED)

    @pytest.mark.parametrize("name", ["HTTP_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"])
    def test_a_proxy_in_either_case_is_a_process_setting(self, name: str) -> None:
        # Linux 上两种大小写是两个变量，urllib 先读小写的：只认大写的话小写那份照样被当成密钥抄走。
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_PROXY)
        assert is_shareable_env(name) is False

    @pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME", "PYTHONUTF8"])
    def test_the_interpreter_reads_its_own(self, name: str) -> None:
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_INTERPRETER)

    @pytest.mark.parametrize("name", sorted(qq_process_env.STARTUP_ENV | qq_process_env.HOST_ENV))
    def test_what_the_qq_side_reads_at_start_up_is_its_own(self, name: str) -> None:
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_SETTING, QQ_NAPCAT.id)

    @pytest.mark.parametrize("name", _QQ_LIVE_ENV)
    def test_a_live_option_fallback_is_a_qq_process_setting(self, name: str) -> None:
        # 控制台平台页那些选项在两份配置文件都没写时兜底读的环境变量，只认适配进程自己的环境。
        assert env_scope(name) == EnvScope(ENV_PROCESS, BASIS_SETTING, QQ_NAPCAT.id)

    @pytest.mark.parametrize("name,expected", [
        ("ONEBOT_FORWARD_BRIDGE_PORT", EnvScope(ENV_INSTANCE, BASIS_DECLARED, QQ_NAPCAT.id)),
        ("CLONOTH_BASE_URL", EnvScope(ENV_INSTANCE, BASIS_DECLARED)),
        ("ONEBOT_IDEMPOTENCY_STORE_FILE", EnvScope(ENV_INSTANCE, BASIS_PATH)),
        ("CLONOTH_ORIGIN_ROOT", EnvScope(ENV_INSTANCE, BASIS_PATH)),
        # NoneBot 自己读的，值是这个实例的 NapCat 接口地址
        ("ONEBOT_API_ROOTS", EnvScope(ENV_INSTANCE, BASIS_PATH)),
        ("ONEBOT_V11_API_ROOTS", EnvScope(ENV_INSTANCE, BASIS_PATH)),
    ])
    def test_identity_is_judged_before_the_process_lists(self, name: str, expected: EnvScope) -> None:
        # 这几个适配进程也是启动时读一次，但值是端口、地址、路径，只能留在本实例 .env。
        assert env_scope(name) == expected

    @pytest.mark.parametrize("name", [
        *_SLOT_ENV,
        "CLONOTH_NATIVE_SEARCH_MODEL",
        "CLONOTH_NATIVE_SEARCH_BASE_URL",
        "CLONOTH_NATIVE_SEARCH_API_KEY",
        "CLONOTH_NATIVE_SEARCH_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "EXA_API_KEY",
        "EXA_BASE_URL",
        "XAI_API_KEY",
        "XAI_BASE_URL",
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_SOURCE",
        "WEB_SEARCH_TOTAL_TIMEOUT_SEC",
        "WEB_SEARCH_CHILD_TIMEOUT_SEC",
        "NOVELAI_API_KEY",
        "DRAW_TAG_MODEL",
    ])
    def test_what_the_secrets_file_serves_is_shared(self, name: str) -> None:
        # 这些都经 dotenv 两层、${VAR} 或共享密钥文件取值，组里别的实例配上的这边读得到。
        assert env_scope(name) == EnvScope(ENV_SHARED)
        assert is_shareable_env(name) is True

    def test_no_process_name_is_swallowed_by_the_identity_rules(self) -> None:
        # 身份先判：名单里的名字带着路径后缀或被平台声明成身份的话，名单里那一行等于没写。
        swallowed = sorted(name for name in process_env_names() if env_scope(name).kind != ENV_PROCESS)

        assert swallowed == []


class TestPlatformProcessEnvHooks:
    def test_a_platform_without_the_hook_only_gets_the_core_lists(self, fresh_hook_warnings: None) -> None:
        other = PlatformDescriptor(id="x", display_name="X", short_name="X")

        assert env_scope("ONEBOT_GROUP_TRIGGER", [other]) == EnvScope(ENV_SHARED)
        assert env_scope("CLONOTH_LOG_LEVEL", [other]) == EnvScope(ENV_PROCESS, BASIS_SETTING)
        assert process_env_names([other]) == process_env_names([])

    def test_what_a_hook_reports_belongs_to_its_platform(
        self, fresh_hook_warnings: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = types.ModuleType("clonoth_process_env_probe")
        module.process_env_names = lambda: frozenset({"X_POLL_SECONDS"})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, module.__name__, module)
        other = _platform_with_hook(module.__name__)

        assert env_scope("X_POLL_SECONDS", [other]) == EnvScope(ENV_PROCESS, BASIS_SETTING, "x")
        assert env_scope("x_poll_seconds", [other]).kind == ENV_PROCESS
        assert "X_POLL_SECONDS" in process_env_names([other])

    def test_a_broken_hook_falls_back_to_the_core_lists_and_warns_once(
        self, fresh_hook_warnings: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def broken() -> frozenset[str]:
            raise RuntimeError("qq.yaml unreadable")

        module = types.ModuleType("clonoth_process_env_broken_probe")
        module.process_env_names = broken  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, module.__name__, module)
        other = _platform_with_hook(module.__name__)

        with caplog.at_level(logging.WARNING, logger=protection.__name__):
            first = env_scope("X_POLL_SECONDS", [other])
            again = env_scope("X_POLL_SECONDS", [other])
            core = env_scope("CLONOTH_LOG_LEVEL", [other])

        assert first == again == EnvScope(ENV_SHARED)
        assert core == EnvScope(ENV_PROCESS, BASIS_SETTING)
        warnings = [record.getMessage() for record in caplog.records if "process_env" in record.getMessage()]
        assert len(warnings) == 1
        assert "qq.yaml unreadable" in warnings[0]

    def test_a_hook_module_that_cannot_be_imported_does_not_break_the_judgement(
        self, fresh_hook_warnings: None, caplog: pytest.LogCaptureFixture,
    ) -> None:
        other = _platform_with_hook("clonoth_no_such_process_env_module")

        with caplog.at_level(logging.WARNING, logger=protection.__name__):
            scope = env_scope("X_POLL_SECONDS", [other])

        assert scope == EnvScope(ENV_SHARED)
        assert any("process_env" in record.getMessage() for record in caplog.records)


class TestNamesTheDataRootHasAlreadyTaken:
    @pytest.mark.parametrize("name", ["stickers", "shared"])
    def test_an_instance_cannot_be_called_one_of_them(self, name: str) -> None:
        with pytest.raises(provisioning.ProvisionError) as caught:
            provisioning.validate_instance_id(name)

        assert name in str(caught.value)

    def test_the_reserved_set_names_the_shared_config_directory(self) -> None:
        assert provisioning.RESERVED_IDS == {"stickers", config_layout.SHARED_DIR}

    def test_the_error_lists_every_reserved_name(self) -> None:
        with pytest.raises(provisioning.ProvisionError) as caught:
            provisioning.validate_instance_id("Alpha")

        message = str(caught.value)
        assert all(name in message for name in provisioning.RESERVED_IDS)

    def test_the_root_side_helper_refuses_the_same_names(self) -> None:
        # root 侧不导 supervisor 的代码，两份清单漂移的话 ingress 会给共享目录开一条路由。
        spec = importlib.util.spec_from_file_location(
            "cloudflared_ingress_reserved", DEPLOY / "cloudflared_ingress.py",
        )
        assert spec and spec.loader
        ingress = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ingress)

        assert set(ingress.RESERVED_IDS) == set(provisioning.RESERVED_IDS)

    @pytest.mark.parametrize("path", [NEW_INSTANCE, INSTALLER, PROVISION], ids=lambda p: p.name)
    def test_the_shell_entry_points_refuse_the_same_names(self, path: Path) -> None:
        body = "\n".join(_script_code(path))
        guard = next(line for line in _script_code(path) if line.startswith("case "))

        assert "stickers|shared)" in guard
        assert "exit 2" in guard
        assert "!= stickers" not in body


class TestTheCodeLinksAnInstanceGets:
    @pytest.fixture
    def links_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows 上普通账号建不了软链，换成目录联接，脚手架其余那几步照跑。"""
        def make(self: Path, target: Path, target_is_directory: bool = False) -> None:
            _make_dir_link(self, Path(target))

        monkeypatch.setattr(Path, "symlink_to", make)

    @pytest.fixture
    def plan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> provisioning.Plan:
        monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(tmp_path / "data" / "instances.yaml"))
        (tmp_path / "data").mkdir()
        monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
        return provisioning.plan_instance(tmp_path, instance_id="beta", label="小号")

    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "code"
        for name in config_layout.CODE_LINKS:
            (root / name).mkdir(parents=True)
        monkeypatch.setattr(provisioning, "_repo_root", lambda: root)
        return root

    def test_the_scaffold_links_exactly_what_the_layout_declares(self) -> None:
        assert provisioning._LINKED == config_layout.CODE_LINKS

    def test_every_linked_name_is_a_real_directory_in_the_repo(self) -> None:
        for name in provisioning._LINKED:
            assert (_ROOT / name).is_dir(), name

    def test_a_new_workspace_gets_every_declared_link(
        self, links_work: None, plan: provisioning.Plan, tmp_path: Path, repo: Path,
    ) -> None:
        # defaults/ 里是仓库自带的节点和 runtime，少这一条链接新实例连提示词都加载不起来。
        provisioning.scaffold(plan, tmp_path)

        for name in config_layout.CODE_LINKS:
            assert config_layout.is_dir_link(plan.workspace / name), name
            assert (plan.workspace / name).resolve() == (repo / name).resolve()

    def test_the_shared_sticker_library_is_linked_too(
        self, links_work: None, plan: provisioning.Plan, tmp_path: Path,
    ) -> None:
        provisioning.scaffold(plan, tmp_path)

        link = plan.workspace / config_layout.STICKERS_LINK
        assert config_layout.is_dir_link(link)
        assert link.resolve() == (tmp_path / "data" / "stickers").resolve()

    def test_a_linked_name_that_is_missing_stops_the_scaffold(
        self, links_work: None, plan: provisioning.Plan, tmp_path: Path, repo: Path,
    ) -> None:
        (repo / config_layout.CODE_LINKS[-1]).rmdir()

        with pytest.raises(provisioning.ProvisionError) as caught:
            provisioning.scaffold(plan, tmp_path)

        assert config_layout.CODE_LINKS[-1] in str(caught.value)


class TestTheDeployedUnitsCanReachTheSharedConfig:
    @pytest.mark.parametrize("unit", ["kazebot@.service", "kazebot-qq@.service"], ids=lambda p: str(p))
    def test_each_per_instance_unit_opens_the_shared_directory(self, unit: str) -> None:
        # 加入共享组后 config/ 是指到这下面的链接，systemd 按真实路径判权限，不放开就一个字节都写不进去。
        assert f"ReadWritePaths=-{DATA_ROOT}/shared" in _directives(SYSTEMD / unit)

    @pytest.mark.parametrize("unit", ["kazebot@.service", "kazebot-qq@.service"], ids=lambda p: str(p))
    def test_a_host_without_the_directory_still_starts(self, unit: str) -> None:
        # 前面那个 - 没了的话，没建过共享目录的机器上单元直接起不来。
        line = next(l for l in _directives(SYSTEMD / unit) if l.endswith(f"{DATA_ROOT}/shared"))

        assert line == f"ReadWritePaths=-{DATA_ROOT}/shared"

    def test_the_installer_creates_the_shared_directory_for_the_owner(self) -> None:
        body = _script_code(INSTALLER)

        assert 'install -d -m 0700 -o "$OWNER" -g "$OWNER" "$DATA_ROOT/shared"' in body

    def test_the_shared_directory_is_owner_only(self) -> None:
        # 组里放着模型密钥，同机别的服务账号不该读得到。
        line = next(l for l in _script_code(INSTALLER) if l.endswith('"$DATA_ROOT/shared"'))

        assert "-m 0700" in line

    def test_the_installer_drops_in_the_same_permission_for_the_single_instance_unit(self) -> None:
        # 单机装出来的主实例走的是不带 @ 的 kazebot-qq.service，它拿不到 @ 单元里那一行。
        body = INSTALLER.read_text(encoding="utf-8")

        assert "kazebot-qq.service.d/20-shared-config.conf" in body
        assert "ReadWritePaths=-$DATA_ROOT/shared" in body

"""由平台声明推导的保护：经聊天外传的黑名单、policy 兜底规则、子进程环境里剥掉的密钥变量。

新平台只要在描述里声明就该自动受保护，所以 QQ 和一个只在这里存在的平台各跑一遍同样的断言。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters.onebot.forward_authz import file_deny_reason  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    ChannelDescriptor,
    PlatformDescriptor,
    SecretField,
    check_workspace_glob,
)
from platforms.protection import (  # noqa: E402
    CORE_OUTBOUND_DENY,
    normalize_relative,
    outbound_deny_patterns,
    outbound_deny_reason,
    policy_floor,
    secret_env_names,
    secret_file_patterns,
)
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402
from toolbox._common import safe_subprocess_env  # noqa: E402

VAULT = PlatformDescriptor(
    id="vault",
    display_name="Vault Chat",
    short_name="Vault",
    channels=(ChannelDescriptor("vault_room", CHANNEL_GROUP),),
    config_path="config/vault.yaml",
    config_env="VAULT_CONFIG_PATH",
    state_files={"live": "vault_live.json"},
    secrets=(
        SecretField("app_secret", "应用密钥", env="VAULT_APP_SECRET"),
        SecretField(
            "signing_key", "签名密钥", env="VAULT_SIGNING", file="data/vault_signing_key", file_env="VAULT_SIGNING_FILE",
        ),
    ),
    secret_files=("data/vault-session.*",),
    owned_files=("data/vault_cache/*",),
    private_files=("config/vault_people.yaml",),
)

QQ_SECRET_ENVS = {
    "ONEBOT_ACCESS_TOKEN", "ONEBOT_FORWARD_BRIDGE_TOKEN", "ONEBOT_CONVERSATION_HASH_SECRET", "NAPCAT_WEBUI_TOKEN",
}
QQ_SECRET_FILES = {"data/napcat-account.*", "data/onebot_forward_bridge_token", "data/onebot_conversation_hash_secret"}

PERMISSIVE_POLICY = "\n".join([
    "version: 1",
    "read_file:",
    "  default: auto",
    "  rules: []",
    "write_file:",
    "  default: auto",
    "  rules: []",
    "execute_command:",
    "  default: auto",
    "",
])


@pytest.fixture
def vault():
    register_platform(VAULT)
    try:
        yield VAULT
    finally:
        unregister_platform(VAULT.id)


def _engine(workspace: Path, policy: str | None = PERMISSIVE_POLICY) -> PolicyEngine:
    """policy 为 None 时不写文件，引擎自己落一份内置默认。"""
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    if policy is not None:
        (workspace / "data" / "policy.yaml").write_text(policy, encoding="utf-8")
    return PolicyEngine(workspace_root=workspace)


class TestDeclarationsAreChecked:
    @pytest.mark.parametrize("kwargs", [
        {"name": "AppSecret", "label": "x"},
        {"name": "app_secret", "label": "  "},
        {"name": "app_secret", "label": "x", "env": "vault_app_secret"},
        {"name": "app_secret", "label": "x", "file_env": "VAULT_FILE"},
        {"name": "app_secret", "label": "x", "file": "config/vault_token"},
        {"name": "app_secret", "label": "x", "file": "/var/lib/vault_token"},
        {"name": "app_secret", "label": "x", "file": "C:/vault_token"},
        {"name": "app_secret", "label": "x", "file": "data/../vault_token"},
        {"name": "app_secret", "label": "x", "file": "data\\vault_token"},
        {"name": "app_secret", "label": "x", "file": "data//vault_token"},
    ])
    def test_a_malformed_secret_is_refused(self, kwargs: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            SecretField(**kwargs)

    @pytest.mark.parametrize("extra", [
        {"secrets": (SecretField("token", "a"), SecretField("token", "b"))},
        {"secrets": (SecretField("token", "a", env="SAME_ENV"), SecretField("other", "b", env="SAME_ENV"))},
        # config/ 下的文件审批就能改，还会被当成可编辑配置展示，密钥不能放那里。
        {"secret_files": ("config/vault-session.json",)},
        {"owned_files": ("data/../escape",)},
        {"private_files": ("/etc/vault_people.yaml",)},
        {"private_files": (" config/vault_people.yaml",)},
    ])
    def test_a_malformed_platform_declaration_is_refused(self, extra: dict) -> None:
        with pytest.raises(ValueError):
            PlatformDescriptor(id="broken", display_name="Broken", short_name="B", **extra)

    def test_asking_for_an_undeclared_secret_is_a_caller_bug(self) -> None:
        with pytest.raises(KeyError):
            QQ_NAPCAT.secret("forward_bridge_tokn")

    @pytest.mark.parametrize("pattern", ["data/onebot_*", "*/.env", "data/cache/onebot_*", ".env.*"])
    def test_ordinary_workspace_globs_pass(self, pattern: str) -> None:
        assert check_workspace_glob(pattern, what="pattern") == pattern

    def test_declarations_are_frozen_into_tuples(self) -> None:
        platform = PlatformDescriptor(
            id="listy", display_name="Listy", short_name="L",
            secrets=[SecretField("token", "t")], secret_files=["data/listy.*"],
        )

        assert isinstance(platform.secrets, tuple)
        assert isinstance(platform.secret_files, tuple)


class TestQqDeclarations:
    def test_the_env_names_are_the_ones_deployments_already_use(self) -> None:
        # 部署脚本和 .env.example 写的就是这些名字，改名等于存量部署的密钥全部失效。
        assert secret_env_names([QQ_NAPCAT]) == QQ_SECRET_ENVS

    def test_the_auto_files_are_the_ones_the_adapter_already_writes(self) -> None:
        assert set(secret_file_patterns(QQ_NAPCAT, environ={})) == QQ_SECRET_FILES

    def test_file_env_names_are_not_treated_as_secrets(self) -> None:
        # 它们只是路径，工具进程要靠它们找到自签的令牌文件。
        names = secret_env_names([QQ_NAPCAT])

        assert "ONEBOT_FORWARD_BRIDGE_TOKEN_FILE" not in names
        assert "ONEBOT_CONVERSATION_HASH_SECRET_FILE" not in names


class TestOutboundDeny:
    @pytest.mark.parametrize("path", [
        ".env",
        ".env.local",
        "deploy/.env",
        "deploy/.env.prod",
        "data/config.yaml",
        "data/.admin_token",
        "data/policy.yaml",
        "data/onebot_anon_map.json",
        "data/onebot_plugin_state.json",
        "data/onebot_reply_attachments.json",
        "data/cache/onebot_images.json",
        "config/qq.yaml",
        "data/qq_live_state.json",
        "data/config.yaml.bak",
        "id_rsa",
        "keys/id_rsa.pub",
        "tls/server.key",
        "tls/cert.pem",
        "tls/client.p12",
        "tls/client.pfx",
    ])
    def test_everything_the_old_hand_written_list_blocked_is_still_blocked(self, path: str) -> None:
        assert file_deny_reason(path), path

    @pytest.mark.parametrize("path", [
        "config/qq_user_profiles.yaml",
        "data/napcat-account.json",
        "data/qq_dead_logins.json",
        "data/onebot_forward_bridge_token",
        "data/onebot_conversation_hash_secret",
        "data/onebot_active_bot.json",
    ])
    def test_the_declared_private_files_are_blocked(self, path: str) -> None:
        assert file_deny_reason(path), path

    @pytest.mark.parametrize("path", ["README.md", "notes/todo.md", "data/memory/notes.md", "config/features.yaml"])
    def test_ordinary_files_still_go_out(self, path: str) -> None:
        assert file_deny_reason(path) == ""

    @pytest.mark.parametrize("path", ["./data/config.yaml", "data\\onebot_anon_map.json", "  .env  ", "././config/qq.yaml"])
    def test_spellings_models_produce_are_normalized_first(self, path: str) -> None:
        assert file_deny_reason(path), path

    def test_an_empty_path_is_not_a_denial(self) -> None:
        assert normalize_relative("  ./ ") == ""
        assert file_deny_reason("") == ""

    def test_the_reason_names_the_file_and_the_platform_it_would_leave_through(self) -> None:
        reason = file_deny_reason("./data/onebot_anon_map.json")

        assert reason.startswith("data/onebot_anon_map.json：")
        assert QQ_NAPCAT.short_name in reason

    @pytest.mark.parametrize("path", [
        "data/vault_signing_key",
        "data/vault-session.json",
        "data/vault_cache/avatars.json",
        "data/vault_live.json",
        "config/vault.yaml",
        "config/vault_people.yaml",
    ])
    def test_another_platforms_files_cannot_leave_through_qq_either(self, vault, path: str) -> None:
        assert file_deny_reason(path), path

    def test_an_unregistered_platforms_files_are_not_guessed_at(self) -> None:
        assert file_deny_reason("data/vault_signing_key") == ""

    def test_a_secret_file_moved_by_env_is_blocked_where_it_now_lives(self, tmp_path: Path) -> None:
        moved = {"VAULT_SIGNING_FILE": "keys/signing"}

        assert outbound_deny_reason("keys/signing", via="Vault", platforms=[VAULT], environ=moved, workspace=tmp_path)
        assert outbound_deny_reason("keys/signing", via="Vault", platforms=[VAULT], environ={}, workspace=tmp_path) == ""

    def test_a_config_file_moved_by_env_is_blocked_where_it_now_lives(self, tmp_path: Path) -> None:
        moved = {"VAULT_CONFIG_PATH": "etc/vault.yaml"}

        assert outbound_deny_reason("etc/vault.yaml", via="Vault", platforms=[VAULT], environ=moved, workspace=tmp_path)

    def test_a_file_moved_outside_the_workspace_is_listed_by_its_absolute_path(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "secrets" / "signing"

        patterns = secret_file_patterns(VAULT, environ={"VAULT_SIGNING_FILE": str(outside)}, workspace=workspace)

        assert outside.resolve().as_posix() in patterns

    def test_the_core_list_leads_and_nothing_is_listed_twice(self) -> None:
        patterns = outbound_deny_patterns(platforms=[QQ_NAPCAT, VAULT], environ={})

        assert patterns[:len(CORE_OUTBOUND_DENY)] == CORE_OUTBOUND_DENY
        assert len(patterns) == len(set(patterns))


class TestPolicyFloor:
    def test_reads_are_floored_only_for_files_that_carry_credentials(self) -> None:
        read, _write = policy_floor(platforms=[QQ_NAPCAT], environ={})

        assert {rule.pattern for rule in read} == QQ_SECRET_FILES
        assert all(QQ_NAPCAT.id in rule.reason for rule in read)

    def test_writes_are_floored_for_everything_the_platform_maintains(self) -> None:
        _read, write = policy_floor(platforms=[QQ_NAPCAT], environ={})
        patterns = {rule.pattern for rule in write}

        assert QQ_SECRET_FILES <= patterns
        assert {f"data/{name}" for name in QQ_NAPCAT.state_files.values()} <= patterns
        assert {"data/onebot_*", "data/cache/onebot_*", "data/qq_dead_logins.json"} <= patterns

    def test_the_config_file_stays_editable_under_approval(self) -> None:
        _read, write = policy_floor(platforms=[QQ_NAPCAT], environ={})
        patterns = {rule.pattern for rule in write}

        assert QQ_NAPCAT.config_path not in patterns
        assert "config/qq_user_profiles.yaml" not in patterns

    @pytest.mark.parametrize("path", [
        "data/onebot_forward_bridge_token",
        "data/onebot_conversation_hash_secret",
        "data/napcat-account.json",
    ])
    def test_a_permissive_policy_file_cannot_open_secret_reads(self, tmp_path: Path, path: str) -> None:
        # 第一个命中的规则赢，兜底排在最前，改 policy.yaml 放不开。
        assert _engine(tmp_path).evaluate_read_file(path=path).safety_level == SafetyLevel.deny

    @pytest.mark.parametrize("path", [
        "data/onebot_anon_map.json",
        "data/onebot_plugin_state.json",
        "data/qq_live_state.json",
        "data/qq_dead_logins.json",
        "data/cache/onebot_images.json",
        "data/napcat-account.json",
        "data/onebot_forward_bridge_token",
    ])
    def test_a_permissive_policy_file_cannot_open_writes_to_platform_files(self, tmp_path: Path, path: str) -> None:
        assert _engine(tmp_path).evaluate_write_file(path=path).safety_level == SafetyLevel.deny

    def test_an_explicit_allow_rule_in_policy_yaml_cannot_open_them(self, tmp_path: Path) -> None:
        # 第一个命中的规则赢：兜底要是排在后面，一条 data/* auto 就把它们全放开了。
        engine = _engine(tmp_path, policy="\n".join([
            "version: 1",
            "read_file:",
            "  default: deny",
            "  rules:",
            "    - {pattern: 'data/*', decision: auto}",
            "write_file:",
            "  default: deny",
            "  rules:",
            "    - {pattern: 'data/*', decision: auto}",
            "",
        ]))

        assert engine.evaluate_read_file(path="data/onebot_forward_bridge_token").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/onebot_anon_map.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_read_file(path="data/onebot_anon_map.json").safety_level == SafetyLevel.auto

    def test_state_files_stay_readable_where_the_policy_allows(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        assert engine.evaluate_read_file(path="data/onebot_anon_map.json").safety_level == SafetyLevel.auto
        assert engine.evaluate_write_file(path="config/qq.yaml").safety_level == SafetyLevel.auto

    def test_commands_that_name_a_secret_file_are_denied(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        assert engine.evaluate_execute_command(
            command="cat data/onebot_forward_bridge_token",
        ).safety_level == SafetyLevel.deny
        assert engine.evaluate_execute_command(command="echo hi").safety_level == SafetyLevel.auto

    def test_the_built_in_default_still_guards_the_account_queue(self, tmp_path: Path) -> None:
        # 内置默认里删掉了手写的 napcat-account 两条，改由平台声明兜住，不能因此放开。
        engine = _engine(tmp_path, policy=None)

        assert engine.evaluate_read_file(path="data/napcat-account.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/napcat-account.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/qq_dead_logins.json").safety_level == SafetyLevel.deny

    def test_the_console_export_does_not_carry_the_floor(self, tmp_path: Path) -> None:
        # 导出的是给控制台编辑的那份文件；带上兜底，存一次就把它们写死进 policy.yaml。
        exported = json.dumps(_engine(tmp_path).export_config())

        assert "onebot_forward_bridge_token" not in exported
        assert "napcat-account" not in exported

    def test_a_secret_file_moved_by_env_is_guarded_where_it_now_lives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ONEBOT_FORWARD_BRIDGE_TOKEN_FILE", "private/bridge")
        engine = _engine(tmp_path)

        assert engine.evaluate_read_file(path="private/bridge").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="private/bridge").safety_level == SafetyLevel.deny

    def test_a_state_file_moved_by_env_is_guarded_where_it_now_lives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ONEBOT_STATE_FILE", "private/routes.json")
        monkeypatch.setenv("ONEBOT_ANON_MAP_FILE", str(tmp_path / "private" / "aliases.json"))
        engine = _engine(tmp_path)

        assert engine.evaluate_write_file(path="private/routes.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="private/aliases.json").safety_level == SafetyLevel.deny
        assert outbound_deny_reason("private/aliases.json", via="QQ", workspace=tmp_path)

    def test_a_platforms_own_data_directory_is_floored(self, vault, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        assert engine.evaluate_write_file(path="data/platforms/vault/aliases.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/platforms/vault/cache/x.json").safety_level == SafetyLevel.deny
        # 别名表、路由这些记着真实 ID，经聊天发出去一样是泄露。
        assert outbound_deny_reason("data/platforms/vault/routes.json", via="Vault")
        assert outbound_deny_reason("data/platforms/qq/anything.json", via="QQ")

    def test_a_newly_registered_platform_is_floored_without_touching_policy(self, vault, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        assert engine.evaluate_read_file(path="data/vault_signing_key").safety_level == SafetyLevel.deny
        assert engine.evaluate_read_file(path="data/vault-session.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/vault_live.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="data/vault_cache/avatars.json").safety_level == SafetyLevel.deny
        assert engine.evaluate_write_file(path="config/vault_people.yaml").safety_level == SafetyLevel.auto


class TestSubprocessEnvironment:
    def test_declared_secret_env_names_are_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in QQ_SECRET_ENVS:
            monkeypatch.setenv(name, "do-not-leak")
        monkeypatch.setenv("ONEBOT_FORWARD_BRIDGE_TOKEN_FILE", "data/elsewhere")
        monkeypatch.setenv("ONEBOT_FORWARD_BRIDGE_PORT", "8769")

        env = safe_subprocess_env()

        assert not QQ_SECRET_ENVS & set(env)
        # 工具靠这两个找到 Bridge 和自签的令牌文件，剥掉就连不上了。
        assert env["ONEBOT_FORWARD_BRIDGE_TOKEN_FILE"] == "data/elsewhere"
        assert env["ONEBOT_FORWARD_BRIDGE_PORT"] == "8769"

    def test_a_newly_registered_platforms_secrets_are_stripped_too(self, vault, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_SIGNING", "do-not-leak")
        monkeypatch.setenv("VAULT_SIGNING_FILE", "data/vault_signing_key")

        env = safe_subprocess_env()

        assert "VAULT_SIGNING" not in env
        assert env["VAULT_SIGNING_FILE"] == "data/vault_signing_key"

    def test_the_admin_token_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 拿着它能调整个管理接口，policy 也在里面，等于工具能自己批自己。
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-secret")

        assert "CLONOTH_ADMIN_TOKEN" not in safe_subprocess_env()

    def test_the_existing_api_key_rules_still_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOMEVENDOR_API_KEY", "sk-x")
        monkeypatch.setenv("NOVELAI_API_KEY", "nai-x")

        env = safe_subprocess_env()

        assert "SOMEVENDOR_API_KEY" not in env
        assert env["NOVELAI_API_KEY"] == "nai-x"

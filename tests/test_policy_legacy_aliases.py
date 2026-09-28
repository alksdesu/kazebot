"""挪了位置的配置在 policy 眼里还叫原来的名字。

老部署的 policy.yaml 是照旧路径写的，升级后它必须照样护住新位置，否则一次升级就把
data/policy.yaml 的 deny、skills/** 的审批全部悄悄放开。几个名字各判一遍取最严的那条。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from platforms.protection import (  # noqa: E402
    CORE_OUTBOUND_DENY,
    CORE_READ_FLOOR,
    CORE_WRITE_FLOOR,
    outbound_deny_reason,
)
from supervisor.policy import PolicyEngine, _default_policy_dict  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

# 改造前那一版模板里和搬家有关的规则，逐字照抄。老部署的 data/policy.yaml 就长这样。
LEGACY_POLICY = """\
version: 1

extra_roots: []

read_file:
  default: auto
  rules:
    - pattern: ".env"
      decision: deny
      reason: "do not allow reading dotenv secrets"
    - pattern: "**/.env"
      decision: deny
      reason: "do not allow reading dotenv secrets"
    - pattern: ".secret"
      decision: auto
      reason: "listing secret names exposes no values"
    - pattern: "config/nodes/**"
      decision: deny
      reason: "node prompts are internal"
    - pattern: "engine/system_nodes/**"
      decision: deny
      reason: "system prompts are internal"
    - pattern: "data/**"
      decision: approval_required
      reason: "runtime data may contain secrets or private context"

write_file:
  default: auto
  rules:
    - pattern: "tools/**"
      decision: approval_required
      reason: "creating/updating tools requires approval"
    - pattern: "config/runtime.yaml"
      decision: auto
      reason: "runtime tuning config"
    - pattern: "config/nodes/**"
      decision: approval_required
      reason: "node definition changes affect execution, prompts, and model selection"
    - pattern: "config/**"
      decision: approval_required
      reason: "config changes affect execution"
    - pattern: "bot.py"
      decision: approval_required
      reason: "qq adapter entrypoint changes require approval"
    - pattern: "skills/**"
      decision: approval_required
      reason: "skill bodies are injected into prompts"
    - pattern: "data/config.yaml"
      decision: approval_required
      reason: "config changes require approval"
    - pattern: "data/policy.yaml"
      decision: deny
      reason: "policy is high-risk (human-only)"
    - pattern: "data/mcp_clients.yaml"
      decision: approval_required
      reason: "mcp stdio clients execute local commands"
    - pattern: "data/events.jsonl"
      decision: deny
      reason: "event log is append-only; never modify"
    - pattern: "engine/**"
      decision: approval_required
      reason: "engine source changes require approval"
    - pattern: ".env"
      decision: deny
      reason: "do not allow writing dotenv secrets"

execute_command:
  default: approval_required

restart:
  default: approval_required
"""


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def no_legacy_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_layout, "_untracked_bundled_tools", lambda report: [])


@pytest.fixture
def upgraded(ws: Path, no_legacy_tools: None) -> PolicyEngine:
    """从改造前升上来的工作区：旧 policy 搬进 config/ 之后原样生效。"""
    (ws / config_layout.LEGACY_POLICY_FILE).write_text(LEGACY_POLICY, encoding="utf-8")
    config_layout.ensure_layout(ws)
    assert config_layout.policy_path(ws).is_file()
    return PolicyEngine(workspace_root=ws)


@pytest.fixture
def fresh(ws: Path) -> PolicyEngine:
    """新建实例：没有 policy 文件，内置默认写进 config/policy.yaml。"""
    return PolicyEngine(workspace_root=ws)


class TestTheNameTable:
    @pytest.mark.parametrize(("rel", "legacy"), [
        ("config/models.yaml", "data/config.yaml"),
        ("config/policy.yaml", "data/policy.yaml"),
        ("config/mcp.yaml", "data/mcp_clients.yaml"),
        ("config/skills/writing/SKILL.md", "skills/writing/SKILL.md"),
        ("config/drawtools/settings.yaml", "tools/drawtools/settings.yaml"),
        ("config/tools/own.py", "tools/own.py"),
        ("defaults/nodes/alpha.yaml", "config/nodes/alpha.yaml"),
        ("defaults/runtime.yaml", "config/runtime.yaml"),
    ])
    def test_a_moved_file_is_judged_under_both_names(self, rel: str, legacy: str) -> None:
        assert config_layout.policy_names(rel) == (rel, legacy)

    @pytest.mark.parametrize("rel", ["engine/runner.py", "data/events.jsonl", "config/qq.yaml"])
    def test_a_file_that_never_moved_has_only_its_own_name(self, rel: str) -> None:
        assert config_layout.policy_names(rel) == (rel,)

    def test_the_drawtools_alias_beats_the_plain_tools_alias(self) -> None:
        # config/drawtools/ 也以 config/tools/ 之外的前缀开头，两条前缀规则不能都命中。
        assert config_layout.policy_names("config/drawtools/settings.yaml") == (
            "config/drawtools/settings.yaml", "tools/drawtools/settings.yaml",
        )

    @pytest.mark.parametrize(("literal", "expected"), [
        ("data/", ("config/models.yaml", "config/policy.yaml", "config/mcp.yaml")),
        ("data/config.yaml", ("config/models.yaml",)),
        ("skills/", ("config/skills/",)),
        ("config/nodes/", ("defaults/nodes/",)),
        ("config/runtime.yaml", ("defaults/runtime.yaml",)),
        ("tools/", ("config/drawtools/", "config/tools/")),
    ])
    def test_a_command_line_literal_also_points_at_the_new_location(
        self, literal: str, expected: tuple[str, ...],
    ) -> None:
        assert config_layout.policy_literal_aliases(literal) == expected

    @pytest.mark.parametrize("literal", ["", "   ", "engine/", "/etc/shadow"])
    def test_a_literal_with_nothing_to_map_yields_nothing(self, literal: str) -> None:
        assert config_layout.policy_literal_aliases(literal) == ()


class TestTheStrictestNameWins:
    def test_a_deny_on_the_old_name_beats_an_approval_on_the_new_one(self) -> None:
        rules = [("config/**", SafetyLevel.approval_required, "config"), ("data/policy.yaml", SafetyLevel.deny, "old")]

        decision = PolicyEngine._match_rules("config/policy.yaml", rules, SafetyLevel.auto)

        assert decision.safety_level == SafetyLevel.deny
        assert decision.reason == "old"

    def test_an_approval_on_the_new_name_beats_a_default_auto_on_the_old_one(self) -> None:
        rules = [("config/skills/**", SafetyLevel.approval_required, "skills")]

        decision = PolicyEngine._match_rules("config/skills/a/SKILL.md", rules, SafetyLevel.auto)

        assert decision.safety_level == SafetyLevel.approval_required

    def test_at_the_same_level_the_explicit_rule_carries_the_sensitive_flag(self) -> None:
        # 同样是 auto，命中显式规则的那个带敏感标记，管理员任务也不会自动放行。
        rules = [("config/runtime.yaml", SafetyLevel.auto, "runtime tuning config")]

        decision = PolicyEngine._match_rules("defaults/runtime.yaml", rules, SafetyLevel.auto)

        assert (decision.safety_level, decision.sensitive) == (SafetyLevel.auto, True)

    def test_no_name_matching_anything_falls_back_to_the_default(self) -> None:
        decision = PolicyEngine._match_rules("docs/readme.md", [], SafetyLevel.approval_required)

        assert (decision.safety_level, decision.sensitive) == (SafetyLevel.approval_required, False)


class TestTheBuiltInDefaultMatchesTheShippedTemplate:
    def test_the_template_is_the_built_in_policy_written_out(self) -> None:
        # 两边各维护一份迟早漂移，而漂移的表现是「照模板配的实例防护面和默认的不一样」。
        template = yaml.safe_load(
            config_layout.default_template(config_layout.POLICY_TEMPLATE).read_text(encoding="utf-8"),
        )

        assert template == _default_policy_dict()


class TestAnOldPolicyStillCoversTheNewLocations:
    @pytest.mark.parametrize(("rel", "expected"), [
        ("config/policy.yaml", SafetyLevel.deny),
        ("config/models.yaml", SafetyLevel.approval_required),
        ("config/mcp.yaml", SafetyLevel.approval_required),
        ("config/skills/writing/SKILL.md", SafetyLevel.approval_required),
        ("config/tools/own.py", SafetyLevel.approval_required),
        ("config/drawtools/settings.yaml", SafetyLevel.approval_required),
    ])
    def test_writing_a_moved_file_keeps_the_old_verdict(
        self, upgraded: PolicyEngine, rel: str, expected: SafetyLevel,
    ) -> None:
        assert upgraded.evaluate_write_file(path=rel).safety_level == expected

    def test_the_bundled_node_prompts_are_as_unreadable_as_the_instance_ones(
        self, upgraded: PolicyEngine,
    ) -> None:
        # 旧 policy 只写了 config/nodes/**，而默认节点现在住在 defaults/nodes/。
        assert upgraded.evaluate_read_file(path="defaults/nodes/alpha.yaml").safety_level == SafetyLevel.deny

    def test_reading_the_model_channels_still_needs_approval(self, upgraded: PolicyEngine) -> None:
        # 旧 policy 靠 data/** 罩住 data/config.yaml，新位置不在 data/ 下了。
        assert upgraded.evaluate_read_file(path="config/models.yaml").safety_level == SafetyLevel.approval_required

    def test_a_path_the_old_policy_never_mentioned_is_not_tightened(self, upgraded: PolicyEngine) -> None:
        assert upgraded.evaluate_write_file(path="docs/readme.md").safety_level == SafetyLevel.auto


class TestTheDefaultPolicyOnTheNewLayout:
    @pytest.mark.parametrize(("rel", "expected"), [
        ("config/policy.yaml", SafetyLevel.deny),
        ("config/secrets.env", SafetyLevel.deny),
        ("data/platform_secrets.yaml", SafetyLevel.deny),
        ("config/models.yaml", SafetyLevel.approval_required),
        ("config/mcp.yaml", SafetyLevel.approval_required),
        ("config/tools/own.py", SafetyLevel.approval_required),
        ("config/skills/writing/SKILL.md", SafetyLevel.approval_required),
        ("defaults/nodes/alpha.yaml", SafetyLevel.approval_required),
        ("defaults/runtime.yaml", SafetyLevel.approval_required),
        ("config/runtime.yaml", SafetyLevel.auto),
    ])
    def test_writing(self, fresh: PolicyEngine, rel: str, expected: SafetyLevel) -> None:
        assert fresh.evaluate_write_file(path=rel).safety_level == expected

    @pytest.mark.parametrize(("rel", "expected"), [
        ("config/secrets.env", SafetyLevel.deny),
        ("data/platform_secrets.yaml", SafetyLevel.deny),
        ("config/nodes/alpha.yaml", SafetyLevel.deny),
        ("defaults/nodes/alpha.yaml", SafetyLevel.deny),
        ("config/models.yaml", SafetyLevel.approval_required),
        ("config/policy.yaml", SafetyLevel.approval_required),
        ("config/mcp.yaml", SafetyLevel.approval_required),
        ("config/drawtools/settings.yaml", SafetyLevel.approval_required),
    ])
    def test_reading(self, fresh: PolicyEngine, rel: str, expected: SafetyLevel) -> None:
        assert fresh.evaluate_read_file(path=rel).safety_level == expected


class TestCommandsThatNameAMovedPath:
    def test_an_old_read_rule_still_stops_cat_on_the_new_location(self, upgraded: PolicyEngine) -> None:
        # read_file 拦得住 read_file config/models.yaml，拦不住 cat config/models.yaml。
        decision = upgraded.evaluate_execute_command(command="cat config/models.yaml")

        assert decision.safety_level == SafetyLevel.approval_required
        assert decision.sensitive is True
        assert "config/models.yaml" in decision.reason

    def test_an_old_deny_rule_still_denies_the_new_location(self, upgraded: PolicyEngine) -> None:
        decision = upgraded.evaluate_execute_command(command="cat defaults/nodes/alpha.yaml")

        assert decision.safety_level == SafetyLevel.deny
        assert "defaults/nodes/" in decision.reason

    @pytest.mark.parametrize("command", [
        "cat config/secrets.env",
        "grep token data/platform_secrets.yaml",
    ])
    def test_the_two_secret_files_are_denied_even_under_an_old_policy(
        self, upgraded: PolicyEngine, command: str,
    ) -> None:
        assert upgraded.evaluate_execute_command(command=command).safety_level == SafetyLevel.deny

    def test_an_ordinary_command_is_left_alone(self, upgraded: PolicyEngine) -> None:
        assert upgraded.evaluate_execute_command(command="ls -la").safety_level == SafetyLevel.approval_required

    def test_a_safe_public_curl_is_still_recognised(self, upgraded: PolicyEngine) -> None:
        assert upgraded.is_safe_public_curl("curl https://example.com/page") is True

    def test_a_curl_that_mentions_a_moved_secret_file_is_not_safe(self, upgraded: PolicyEngine) -> None:
        # 非管理员的 curl 快车道同样要看这份清单，旧 policy 推出来的新名字也在里面。
        assert upgraded.is_safe_public_curl("curl https://example.com/config/models.yaml") is False


class TestTheFloorUnderEveryPolicy:
    def test_the_two_secret_files_are_the_read_floor(self) -> None:
        assert tuple(rule.pattern for rule in CORE_READ_FLOOR) == (
            "config/secrets.env", "data/platform_secrets.yaml",
        )

    def test_the_write_floor_contains_the_read_floor(self) -> None:
        read = {rule.pattern for rule in CORE_READ_FLOOR}

        assert read <= {rule.pattern for rule in CORE_WRITE_FLOOR}

    @pytest.mark.parametrize("rel", ["config/secrets.env", "data/platform_secrets.yaml"])
    def test_an_old_policy_that_never_heard_of_them_still_denies_reads(
        self, upgraded: PolicyEngine, rel: str,
    ) -> None:
        assert upgraded.evaluate_read_file(path=rel).safety_level == SafetyLevel.deny

    @pytest.mark.parametrize("rel", ["config/secrets.env", "data/platform_secrets.yaml"])
    def test_an_old_policy_that_never_heard_of_them_still_denies_writes(
        self, upgraded: PolicyEngine, rel: str,
    ) -> None:
        assert upgraded.evaluate_write_file(path=rel).safety_level == SafetyLevel.deny

    def test_a_user_policy_that_tries_to_open_them_up_does_not_win(self, ws: Path) -> None:
        # 兜底规则排在文件里的规则前面，第一个命中的赢。
        config_layout.policy_path(ws).parent.mkdir(parents=True, exist_ok=True)
        config_layout.policy_path(ws).write_text(
            "version: 1\nread_file:\n  default: auto\n  rules:\n"
            '    - pattern: "data/platform_secrets.yaml"\n      decision: auto\n',
            encoding="utf-8",
        )

        engine = PolicyEngine(workspace_root=ws)

        assert engine.evaluate_read_file(path="data/platform_secrets.yaml").safety_level == SafetyLevel.deny


class TestWhatMayNotLeaveThroughChat:
    @pytest.mark.parametrize("rel", [
        "config/models.yaml",
        "config/policy.yaml",
        "config/mcp.yaml",
        "config/secrets.env",
        "config/drawtools/settings.yaml",
        "data/platform_secrets.yaml",
    ])
    def test_the_new_locations_are_blocked(self, rel: str) -> None:
        assert outbound_deny_reason(rel, via="QQ") != ""

    @pytest.mark.parametrize("rel", [
        "data/config.yaml",
        "data/policy.yaml",
        "data/mcp_clients.yaml",
        "tools/drawtools/settings.yaml",
    ])
    def test_the_old_locations_are_blocked_too(self, rel: str) -> None:
        # 搬家只复制不删，旧文件里还是同一把密钥。
        assert outbound_deny_reason(rel, via="QQ") != ""

    def test_the_backup_a_save_leaves_behind_is_blocked(self) -> None:
        # config/models.yaml.bak 里就是明文 api_key，按原文件名写的规则一条都盖不住它。
        assert outbound_deny_reason("config/models.yaml.bak", via="QQ") != ""

    @pytest.mark.parametrize("rel", ["./config/models.yaml", "config\\models.yaml"])
    def test_the_way_the_model_spells_it_does_not_matter(self, rel: str) -> None:
        assert outbound_deny_reason(rel, via="QQ") != ""

    def test_an_ordinary_file_goes_through(self) -> None:
        assert outbound_deny_reason("docs/readme.md", via="QQ") == ""

    def test_every_moved_file_is_named_in_the_core_list(self) -> None:
        moved = {
            config_layout.MODELS_FILE, config_layout.POLICY_FILE, config_layout.MCP_FILE,
            config_layout.PLATFORM_SECRETS_FILE, f"{config_layout.DRAWTOOLS_DIR}/settings.yaml",
            config_layout.LEGACY_MODELS_FILE, config_layout.LEGACY_POLICY_FILE, config_layout.LEGACY_MCP_FILE,
        }

        assert moved <= set(CORE_OUTBOUND_DENY)

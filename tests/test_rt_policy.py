"""策略的底线：装着明文密钥的配置读写至少要审批，清单只认 config_layout 那一份；写坏的策略文件沿用上一份并说出来。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from supervisor.policy import PolicyEngine, _default_policy_dict  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_PERMISSIVE = (
    "version: 1\n"
    "read_file:\n  default: auto\n  rules: []\n"
    "write_file:\n  default: auto\n  rules: []\n"
    "execute_command:\n  default: auto\n  sensitive_path_patterns: []\n"
)
_STRICT_ENOUGH = {SafetyLevel.approval_required, SafetyLevel.deny}


def _policy(tmp_path: Path, text: str) -> PolicyEngine:
    path = config_layout.policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return PolicyEngine(workspace_root=tmp_path)


def _rewrite(tmp_path: Path, text: str) -> None:
    config_layout.policy_path(tmp_path).write_text(text, encoding="utf-8")


class TestTheSecretFloor:
    def test_the_list_comes_from_config_layout_including_the_old_names(self) -> None:
        names = set(config_layout.secret_bearing_names())

        assert set(config_layout._SECRET_BEARING) <= names
        # 挪进 config/ 之前的旧名字一并护住：老 policy 给旧路径写的规则照样生效，绕旧名字也读不到。
        assert {"data/config.yaml", "data/policy.yaml", "data/mcp_clients.yaml"} <= names

    @pytest.mark.parametrize("name", config_layout.secret_bearing_names())
    def test_a_permissive_policy_cannot_open_a_secret_file(self, tmp_path: Path, name: str) -> None:
        # 老部署的 policy.yaml 写成 default:auto 也一样：新加的密钥文件不能落到 auto 上。
        engine = _policy(tmp_path, _PERMISSIVE)

        for decision in (engine.evaluate_read_file(path=name), engine.evaluate_write_file(path=name)):
            assert decision.safety_level in _STRICT_ENOUGH, (name, decision)
            assert decision.sensitive

    def test_an_auto_rule_for_plugin_settings_is_lifted_to_approval(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, _PERMISSIVE.replace(
            "read_file:\n  default: auto\n  rules: []\n",
            "read_file:\n  default: auto\n  rules:\n    - {pattern: config/plugins.yaml, decision: auto, reason: open}\n",
        ))

        decision = engine.evaluate_read_file(path="config/plugins.yaml")

        assert decision.safety_level == SafetyLevel.approval_required
        assert decision.sensitive and decision.reason != "open"

    def test_a_stricter_rule_keeps_its_level_and_its_reason(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, _PERMISSIVE.replace(
            "read_file:\n  default: auto\n  rules: []\n",
            "read_file:\n  default: auto\n  rules:\n    - {pattern: config/models.yaml, decision: deny, reason: nobody reads keys}\n",
        ))

        decision = engine.evaluate_read_file(path="config/models.yaml")

        assert (decision.safety_level, decision.reason) == (SafetyLevel.deny, "nobody reads keys")


class TestThePluginRules:
    @pytest.mark.parametrize("section", ["read_file", "write_file"])
    def test_the_built_in_default_and_the_example_carry_them(self, section: str) -> None:
        example = yaml.safe_load((_ROOT / "defaults" / "policy.example.yaml").read_text(encoding="utf-8"))
        for source in (_default_policy_dict(), example):
            patterns = {rule["pattern"]: rule["decision"] for rule in source[section]["rules"]}
            assert patterns.get("config/plugins.yaml") == "approval_required"
            assert patterns.get("config/plugins/**") == "approval_required"

    def test_the_plugin_rules_sit_before_the_config_catch_all(self) -> None:
        # 第一个命中的规则赢，排在 config/** 后面就永远轮不到，理由也就成了笼统的那句。
        patterns = [rule["pattern"] for rule in _default_policy_dict()["write_file"]["rules"]]

        assert patterns.index("config/plugins/**") < patterns.index("config/**")
        assert patterns.index("config/plugins.yaml") < patterns.index("config/**")

    def test_writing_a_plugin_package_needs_approval_by_default(self, tmp_path: Path) -> None:
        engine = PolicyEngine(workspace_root=tmp_path)

        decision = engine.evaluate_write_file(path="config/plugins/backup/plugin.py")

        assert decision.safety_level == SafetyLevel.approval_required and decision.sensitive


class TestCommandsTouchingSeveralPaths:
    @pytest.mark.parametrize("command", [
        "cat config/models.yaml config/secrets.env",
        "cat config/secrets.env config/models.yaml",
    ])
    def test_the_strictest_path_decides(self, tmp_path: Path, command: str) -> None:
        # 按排列顺序取第一个命中的话，前面那个要审批的会盖住后面那个该拒绝的。
        engine = PolicyEngine(workspace_root=tmp_path)

        assert engine.evaluate_execute_command(command=command).safety_level == SafetyLevel.deny


class TestABrokenPolicyFile:
    def test_the_last_good_policy_stays_and_the_reason_is_reported(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, _PERMISSIVE)
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto
        assert engine.load_error == ""

        _rewrite(tmp_path, "read_file:\n  default: [this is broken\n")

        # 不退回内置默认：写坏一个字，自定义的规则就会静默变样。
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto
        assert "用不了" in engine.load_error and "config/policy.yaml" in engine.load_error

        _rewrite(tmp_path, _PERMISSIVE.replace("default: auto\n  rules: []\nwrite", "default: deny\n  rules: []\nwrite", 1))

        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.deny
        assert engine.load_error == ""

    def test_a_file_broken_at_startup_falls_back_to_the_built_in_default(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, "- not a mapping\n")

        assert engine.evaluate_read_file(path=".env").safety_level == SafetyLevel.deny
        assert engine.load_error

    def test_keys_nobody_reads_are_applied_and_named(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, _PERMISSIVE + "write_flie:\n  default: deny\n")

        assert engine.evaluate_write_file(path="notes.txt").safety_level == SafetyLevel.auto
        assert "write_flie" in engine.load_error

    def test_a_deleted_file_is_reported(self, tmp_path: Path) -> None:
        engine = _policy(tmp_path, _PERMISSIVE)
        config_layout.policy_path(tmp_path).unlink()

        assert "不见了" in engine.load_error
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto

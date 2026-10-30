"""顶层模块、平台和插件运行时等代码路径的写策略，含存量部署的内置兜底。

单实例部署工作区就是代码目录，这些路径不补规则就落到 default:auto。新部署在内置默认里就有规则；
存量 policy.yaml 没写时由一层兜底补上，排在管理员规则之后，而控制台「一键全自动」不受影响。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.policy import PolicyEngine, _CODE_PATH_WRITE_FLOOR, _default_policy_dict  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_CODE_SAMPLES = [
    "platforms/protection.py",
    "plugin_runtime/host.py",
    "plugin_sdk/anything.py",
    "stickers/imagebed.py",
    "config_layout.py",
    "real_paths.py",
    "file_lock.py",
    "workspace.py",
    "requirements.txt",
    "requirements-dev.txt",
    ".git/config",
    ".venv/lib/python3.11/site-packages/x.pth",
    "__pycache__/config_layout.cpython-311.pyc",
    "engine/__pycache__/runner.cpython-311.pyc",
]

_ALL_AUTO = "\n".join([
    "version: 1",
    "read_file: {default: auto, rules: []}",
    "write_file: {default: auto, rules: []}",
    "execute_command: {default: auto, deny_patterns: [], sensitive_patterns: [], sensitive_path_patterns: []}",
    "restart: {default: auto}",
    "",
])

# 存量部署：有自定义规则但没提到代码路径
_PARTIAL = "\n".join([
    "version: 1",
    "write_file:",
    "  default: auto",
    "  rules:",
    "    - {pattern: notes/**, decision: approval_required, reason: x}",
    "execute_command: {default: approval_required}",
    "",
])

# 管理员明确把某个代码路径放宽
_ADMIN_ALLOWS = "\n".join([
    "version: 1",
    "write_file:",
    "  default: approval_required",
    "  rules:",
    "    - {pattern: platforms/**, decision: auto, reason: admin allows}",
    "execute_command: {default: approval_required}",
    "",
])


def _engine(tmp_path: Path, policy: str | None = None) -> PolicyEngine:
    (tmp_path / "data").mkdir(exist_ok=True)
    if policy is not None:
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "policy.yaml").write_text(policy, encoding="utf-8")
    return PolicyEngine(workspace_root=tmp_path)


class TestTheBuiltInDefault:
    @pytest.mark.parametrize("path", _CODE_SAMPLES)
    def test_a_code_path_is_sensitive(self, tmp_path: Path, path: str) -> None:
        decision = _engine(tmp_path).evaluate_write_file(path=path)
        assert decision.safety_level == SafetyLevel.approval_required, path
        assert decision.sensitive, path

    def test_the_rules_sit_between_the_entrypoint_block_and_the_env_denies(self) -> None:
        patterns = [rule["pattern"] for rule in _default_policy_dict()["write_file"]["rules"]]
        assert patterns.index("main.py") < patterns.index("platforms/**")
        assert patterns.index("**/__pycache__/**") < patterns.index(".env")

    def test_every_floor_pattern_is_in_the_default(self) -> None:
        patterns = {rule["pattern"] for rule in _default_policy_dict()["write_file"]["rules"]}
        for pattern, _reason in _CODE_PATH_WRITE_FLOOR:
            assert pattern in patterns, pattern


class TestTheFallbackForExistingDeployments:
    @pytest.mark.parametrize("path", _CODE_SAMPLES)
    def test_an_existing_policy_without_the_rules_still_gets_them(self, tmp_path: Path, path: str) -> None:
        decision = _engine(tmp_path, _PARTIAL).evaluate_write_file(path=path)
        assert decision.safety_level == SafetyLevel.approval_required, path
        assert decision.sensitive, path

    def test_an_admin_rule_still_wins_over_the_fallback(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _ADMIN_ALLOWS)
        # 管理员放宽的照管理员的
        assert engine.evaluate_write_file(path="platforms/x.py").safety_level == SafetyLevel.auto
        # 没放宽的仍然兜底
        assert engine.evaluate_write_file(path="stickers/x.py").safety_level == SafetyLevel.approval_required

    def test_a_path_the_admin_already_allows_elsewhere_keeps_its_decision(self, tmp_path: Path) -> None:
        # notes/** 是管理员自己的规则，兜底不覆盖它
        assert _engine(tmp_path, _PARTIAL).evaluate_write_file(path="notes/x").safety_level == SafetyLevel.approval_required


class TestTheAllAutoEscapeHatch:
    @pytest.mark.parametrize("path", _CODE_SAMPLES)
    def test_the_fallback_is_off_when_the_class_is_all_auto(self, tmp_path: Path, path: str) -> None:
        # 控制台一键全自动产出的形态：write_file 全放行，代码路径兜底整层不生效
        assert _engine(tmp_path, _ALL_AUTO).evaluate_write_file(path=path).safety_level == SafetyLevel.auto

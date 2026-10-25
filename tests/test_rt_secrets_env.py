"""密钥从哪儿取：系统槽位的专属变量也认共享组的 config/secrets.env；给了 environ 的调用方，${VAR} 也从它取。

实例没有自己的 .env 时不借代码目录那份：多开时那是主实例的端口和令牌。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor, SecretField  # noqa: E402

_SLOT_KEY = "CLONOTH_SUMMARY_API_KEY"
_PROBE = "CLONOTH_RT_SECRETS_PROBE"


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """钉好工作区的进程：共享密钥按 CLONOTH_WORKSPACE 找。"""
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    for name in ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT", _SLOT_KEY, _PROBE, "RTPLAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))
    return root


def _shared(ws: Path, text: str) -> None:
    config_layout.shared_secrets_path(ws).write_text(text, encoding="utf-8")


class TestSystemSlots:
    def test_a_slot_key_kept_in_the_shared_secrets_file_is_found(self, ws: Path) -> None:
        # manage_secret 把它写进 config/secrets.env：进程环境里没有这个变量，以前只查进程环境就读成空。
        _shared(ws, f"{_SLOT_KEY}=sk-from-the-group\n")

        model = clonoth_runtime.resolve_system_model(ws, "summary", default_api_key="sk-main")

        assert model.api_key == "sk-from-the-group"

    def test_the_instance_environment_still_wins(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 两边有同名项时实例 .env 的算数：它在进程环境里。
        _shared(ws, f"{_SLOT_KEY}=sk-from-the-group\n")
        monkeypatch.setenv(_SLOT_KEY, "sk-from-the-instance")

        assert clonoth_runtime.resolve_system_model(ws, "summary").api_key == "sk-from-the-instance"

    def test_nothing_anywhere_follows_the_main_channel(self, ws: Path) -> None:
        assert clonoth_runtime.resolve_system_model(ws, "summary", default_api_key="sk-main").api_key == "sk-main"


class TestPlatformSecretsWithAnExplicitEnvironment:
    @pytest.fixture
    def platform(self) -> PlatformDescriptor:
        return PlatformDescriptor(
            id="rtplat", display_name="测试平台", short_name="RT",
            channels=(ChannelDescriptor("rtplat_group", CHANNEL_GROUP),),
            secrets=(SecretField("token", "连接令牌", env="RTPLAT_TOKEN"),),
        )

    def test_a_reference_in_the_secrets_file_resolves_from_the_given_environment(
        self, ws: Path, platform: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 每次重读 .env 的调用方给的是文件里的现值，进程环境里那份是启动时冻结下来的旧值。
        monkeypatch.setenv(_PROBE, "stale-from-process")

        secret = clonoth_runtime.platform_secret(
            ws, platform, "token",
            secrets={"rtplat": {"token": f"${{{_PROBE}}}"}},
            environ={_PROBE: "fresh-from-file"},
        )

        assert secret.value == "fresh-from-file"

    def test_without_an_environment_the_process_one_is_used(
        self, ws: Path, platform: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_PROBE, "from-process")

        secret = clonoth_runtime.platform_secret(ws, platform, "token", secrets={"rtplat": {"token": f"${{{_PROBE}}}"}})

        assert secret.value == "from-process"

    def test_the_declared_variable_also_comes_from_the_given_environment(
        self, ws: Path, platform: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RTPLAT_TOKEN", "stale-from-process")

        secret = clonoth_runtime.platform_secret(ws, platform, "token", secrets={}, environ={"RTPLAT_TOKEN": "fresh"})

        assert secret.value == "fresh"


class TestAnInstanceWithoutItsOwnEnv:
    def test_the_code_directory_env_is_not_borrowed_and_the_gap_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        code = tmp_path / "code"
        code.mkdir()
        (code / ".env").write_text(f"{_PROBE}=main-instance-value\n", encoding="utf-8")
        monkeypatch.setattr(clonoth_runtime, "_CODE_ROOT", code.resolve())
        monkeypatch.setattr(clonoth_runtime, "_CODE_ENV_FILE", code / ".env")
        instance = tmp_path / "instance"
        instance.mkdir()
        for name in ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT", _PROBE):
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(instance))

        with caplog.at_level(logging.WARNING, logger="clonoth_runtime"):
            assert clonoth_runtime.process_workspace() == instance.resolve()

        assert _PROBE not in os.environ
        assert any(".env" in record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)

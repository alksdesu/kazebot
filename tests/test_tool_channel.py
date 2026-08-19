"""工具子进程的渠道解析。

工具只吃标准库、导不了 clonoth_runtime，所以 tools/_channel.py 是那边唯一的一份。
密钥只能从 .env 文件读：safe_subprocess_env() 会把所有 *_API_KEY 从环境里剥掉。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

from _channel import OPENAI_COMPATIBLE, Channel  # noqa: E402

CONFIG = "\n".join([
    "version: 1",
    "provider: openai",
    "openai:",
    "  base_url: https://relay.example/v1",
    "  api_key: sk-main",
    "  model: gpt-5.4",
    "anthropic:",
    "  base_url: https://api.anthropic.com",
    "  api_key: sk-ant",
    "  model: claude-sonnet-4-6",
])


def _workspace(tmp_path: Path, config: str = CONFIG, dotenv: str = "") -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text(config, encoding="utf-8")
    if dotenv:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    return tmp_path


class TestMainChannel:
    def test_it_reads_the_active_block(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path))
        main = Channel("image").main()
        assert (main.provider, main.base_url, main.api_key) == (
            "openai", "https://relay.example/v1", "sk-main",
        )

    def test_switching_the_active_block_switches_what_tools_see(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path, CONFIG.replace("provider: openai", "provider: anthropic")))
        main = Channel("image").main()
        assert main.provider == "anthropic"
        assert main.base_url == "https://api.anthropic.com"

    def test_a_missing_block_falls_back_without_lying(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path, CONFIG.replace("provider: openai", "provider: ghost")))
        assert Channel("image").main().provider == "openai"

    def test_no_config_file_is_not_fatal(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert Channel("image").main() == ("", "", "", "")

    def test_env_refs_resolve_from_the_dotenv_file(self, tmp_path, monkeypatch) -> None:
        # 环境变量拿不到：*_API_KEY 在子进程环境里已经被剥掉了。
        monkeypatch.delenv("MAIN_KEY", raising=False)
        monkeypatch.chdir(_workspace(
            tmp_path,
            CONFIG.replace("api_key: sk-main", 'api_key: "${MAIN_KEY}"'),
            dotenv="MAIN_KEY=sk-from-dotenv",
        ))
        assert Channel("image").main().api_key == "sk-from-dotenv"


class TestSlotResolution:
    def test_the_slot_block_wins(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path, CONFIG + "\n".join([
            "", "system_models:", "  image:", "    model: from-config",
        ])))
        monkeypatch.setenv("CLONOTH_IMAGE_MODEL", "from-env")
        assert Channel("image").own("model", "MODEL") == "from-config"

    def test_the_slot_env_is_next(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path))
        monkeypatch.setenv("CLONOTH_IMAGE_GPT_MODEL", "gpt-image-9")
        assert Channel("image_gpt").own("model", "MODEL") == "gpt-image-9"

    def test_own_ignores_the_fallbacks(self, tmp_path, monkeypatch) -> None:
        # own 只回答「这一槽自己配了吗」，调用方靠它区分「跟随」和「指定了」。
        monkeypatch.chdir(_workspace(tmp_path))
        monkeypatch.delenv("CLONOTH_IMAGE_BASE_URL", raising=False)
        assert Channel("image").own("base_url", "BASE_URL") == ""

    def test_pick_walks_the_fallbacks_in_order(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path))
        monkeypatch.delenv("CLONOTH_IMAGE_API_KEY", raising=False)
        channel = Channel("image")
        assert channel.pick("api_key", "API_KEY", "", "  ", "second", "third") == "second"

    def test_each_slot_keeps_its_own_namespace(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(_workspace(tmp_path))
        monkeypatch.setenv("CLONOTH_IMAGE_MODEL", "for-read")
        monkeypatch.delenv("CLONOTH_IMAGE_GEMINI_MODEL", raising=False)
        assert Channel("image").own("model", "MODEL") == "for-read"
        assert Channel("image_gemini").own("model", "MODEL") == ""


def test_only_openai_shaped_providers_are_listed_as_compatible() -> None:
    # 这个集合决定 read_image 敢不敢跟随主渠道；anthropic 和 gemini 走自己的端点。
    assert "openai" in OPENAI_COMPATIBLE
    assert "deepseek" in OPENAI_COMPATIBLE
    assert "anthropic" not in OPENAI_COMPATIBLE
    assert "gemini" not in OPENAI_COMPATIBLE


class TestReadImageFollowsTheMainChannel:
    """黑盒跑真脚本。图片路径给一个不存在的，跑到那一步就说明渠道解析已经过了。"""

    def _run(self, cwd: Path) -> dict:
        done = subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "read_image.py")],
            input=json.dumps({"image_path": "no-such-file.png"}),
            capture_output=True, text=True, cwd=str(cwd), timeout=120,
        )
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_an_openai_shaped_main_channel_is_followed(self, tmp_path) -> None:
        out = self._run(_workspace(tmp_path))
        # 渠道解析过了才会走到读图片这一步。
        assert "Image not found" in out["error"]

    def test_an_incompatible_main_channel_is_refused_instead_of_mangled(self, tmp_path) -> None:
        out = self._run(_workspace(tmp_path, CONFIG.replace("provider: openai", "provider: anthropic")))
        assert out["ok"] is False
        assert "anthropic" in out["error"]
        assert "system_models.image" in out["error"]

    def test_an_explicit_slot_url_overrides_the_compatibility_check(self, tmp_path) -> None:
        # 自己指了地址就是自己负责，别拦着。
        config = CONFIG.replace("provider: openai", "provider: anthropic") + "\n".join([
            "", "system_models:", "  image:",
            "    base_url: https://relay.example/v1",
            "    api_key: sk-image",
        ])
        out = self._run(_workspace(tmp_path, config))
        assert "Image not found" in out["error"]

    def test_no_key_anywhere_says_so(self, tmp_path) -> None:
        out = self._run(_workspace(tmp_path, CONFIG.replace("api_key: sk-main", "api_key: ''")))
        assert "No API key" in out["error"]

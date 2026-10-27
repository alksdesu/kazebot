"""config/drawtools 下的配置读不了：不拿默认值顶上，也不拿默认值写回去。

settings.yaml 管着密钥、出图预设、保留期和容量上限，角色库是手写的大段 YAML，写坏一处缩进很常见。
按空表算的话，出图换成默认预设、清理按默认保留期删文件、切换预设把默认值整份写回去盖掉手写到一半的那份。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
_DRAWTOOLS = _ROOT / "tools" / "drawtools"
for _p in (str(_ROOT), str(_DRAWTOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
from plugins.draw_character_inject import _load_character_tags  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402

# 出错的正是写着密钥的那一行：报出来的原因里不能带上它。
_BROKEN_SETTINGS = "api:\n  api_key: [nai-secret-key\n  timeout_sec: 130\n"
_BROKEN_LIBRARY = "characters:\n  - name: [miku\n"
_GOOD_SETTINGS = yaml.safe_dump({
    "params": {
        "selected_preset_id": "cute",
        "presets": [{"id": "cute", "name": "可爱风"}, {"id": "plain", "name": "朴素"}],
    },
}, allow_unicode=True, sort_keys=False)


@pytest.fixture()
def settings_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "settings.yaml"
    monkeypatch.setattr(common, "SETTINGS_PATH", path)
    return path


@pytest.fixture()
def tags_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "character_tags.yaml"
    monkeypatch.setattr(common, "CHARACTER_TAGS_PATH", path)
    return path


class TestLoadingSettings:
    def test_an_unreadable_file_is_refused_not_replaced_by_defaults(self, settings_path: Path) -> None:
        settings_path.write_text(_BROKEN_SETTINGS, encoding="utf-8")

        with pytest.raises(common.DrawtoolsConfigUnreadable) as caught:
            common.load_settings()

        message = str(caught.value)
        assert "settings.yaml 读不了" in message
        assert "（第 3 行第 14 列）" in message
        assert "nai-secret-key" not in message

    @pytest.mark.parametrize("params", ["5", "[cute]", "''"])
    def test_params_that_is_not_a_mapping_is_refused_with_the_reason(self, settings_path: Path, params: str) -> None:
        # 出图预设要补进 params，补不进去时抛的是 AttributeError，谁也看不出是哪一项写错了
        settings_path.write_text(f"api:\n  api_key: k\nparams: {params}\n", encoding="utf-8")

        with pytest.raises(common.DrawtoolsConfigUnreadable) as caught:
            common.load_settings()

        assert "settings.yaml 读不了：params 不是映射" in str(caught.value)

    def test_an_empty_params_gets_the_defaults(self, settings_path: Path) -> None:
        settings_path.write_text("api:\n  api_key: k\nparams:\n", encoding="utf-8")

        assert common.load_settings()["params"]["presets"] == [common.DEFAULT_PRESET]

    def test_a_later_breakage_keeps_the_last_good_settings(self, settings_path: Path) -> None:
        # 常驻进程（控制台、QQ 命令）里写坏一笔，接着用的是写坏之前那份，而不是默认值。
        settings_path.write_text(_GOOD_SETTINGS, encoding="utf-8")
        assert common.load_settings()["params"]["selected_preset_id"] == "cute"

        settings_path.write_text(_BROKEN_SETTINGS, encoding="utf-8")

        assert common.load_settings()["params"]["selected_preset_id"] == "cute"

    def test_the_defaults_are_never_handed_out_by_reference(self, settings_path: Path) -> None:
        settings_path.write_text("api:\n  api_key: k\n", encoding="utf-8")
        first = common.load_settings()
        first["storage"]["retention_days"] = 0
        first["params"]["presets"][0]["params"]["steps"] = 1

        second = common.load_settings()

        assert second["storage"]["retention_days"] == common.DEFAULT_SETTINGS["storage"]["retention_days"] == 7
        assert second["params"]["presets"][0]["params"]["steps"] == common.DEFAULT_PRESET["params"]["steps"] == 28


class TestSwitchingPresets:
    def test_a_broken_file_is_left_alone(self, settings_path: Path) -> None:
        settings_path.write_text(_BROKEN_SETTINGS, encoding="utf-8")
        before = settings_path.read_bytes()

        with pytest.raises(common.DrawtoolsConfigUnreadable) as caught:
            common.set_selected_preset("cute")

        assert "先在控制台的原文编辑器里修好再切换" in str(caught.value)
        assert settings_path.read_bytes() == before

    def test_an_older_copy_in_memory_is_not_written_back_either(self, settings_path: Path) -> None:
        # 沿用上一份能读，写回去不行：那会把手写到一半的新内容整份盖掉。
        settings_path.write_text(_GOOD_SETTINGS, encoding="utf-8")
        common.load_settings()
        settings_path.write_text(_BROKEN_SETTINGS, encoding="utf-8")
        before = settings_path.read_bytes()

        with pytest.raises(common.DrawtoolsConfigUnreadable):
            common.set_selected_preset("plain")

        assert settings_path.read_bytes() == before

    def test_params_that_is_not_a_mapping_is_left_alone(self, settings_path: Path) -> None:
        settings_path.write_text("params: 5\n", encoding="utf-8")
        before = settings_path.read_bytes()

        with pytest.raises(common.DrawtoolsConfigUnreadable) as caught:
            common.set_selected_preset("cute")

        assert "params 不是映射" in str(caught.value)
        assert settings_path.read_bytes() == before

    def test_a_readable_file_still_switches(self, settings_path: Path) -> None:
        settings_path.write_text(_GOOD_SETTINGS, encoding="utf-8")

        assert common.set_selected_preset("plain")["id"] == "plain"
        assert yaml.safe_load(settings_path.read_text(encoding="utf-8"))["params"]["selected_preset_id"] == "plain"


class TestTheCharacterLibrary:
    def test_an_unreadable_library_is_refused(self, tags_path: Path) -> None:
        tags_path.write_text(_BROKEN_LIBRARY, encoding="utf-8")

        with pytest.raises(common.DrawtoolsConfigUnreadable) as caught:
            common.load_character_tags()

        assert "character_tags.yaml 读不了" in str(caught.value)

    def test_a_readable_library_is_unchanged(self, tags_path: Path) -> None:
        tags_path.write_text("characters:\n  - name: miku\n  - just text\n", encoding="utf-8")

        assert common.load_character_tags() == [{"name": "miku"}]


def _library(workspace: Path) -> Path:
    path = workspace / "config" / "drawtools" / "character_tags.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestTheInjectedCharacterLibrary:
    """draw 节点每一步都注入一次角色库：写坏了不能每一步都告警，也不能悄悄当成没有角色。"""

    def test_a_later_breakage_keeps_the_last_good_library(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _library(tmp_path)
        path.write_text("characters:\n  - name: own\n", encoding="utf-8")
        assert _load_character_tags(tmp_path) == [{"name": "own"}]
        path.write_text(_BROKEN_LIBRARY, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="tools._channel"):
            first = _load_character_tags(tmp_path)
            again = _load_character_tags(tmp_path)

        assert first == again == [{"name": "own"}]
        assert len([record for record in caplog.records if "沿用上一份" in record.getMessage()]) == 1

    def test_a_library_never_read_counts_as_empty_and_says_so(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _library(tmp_path).write_text(_BROKEN_LIBRARY, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="tools._channel"):
            assert _load_character_tags(tmp_path) == []
            assert _load_character_tags(tmp_path) == []

        warnings = [record.getMessage() for record in caplog.records if "还没读成过" in record.getMessage()]
        assert len(warnings) == 1
        assert "character_tags.yaml 读不了" in warnings[0]


def _drawtools_config(workspace: Path, name: str, text: str) -> None:
    path = workspace / "config" / "drawtools" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_script(script: str, workspace: Path, args: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    """照注册表起自带工具的样子跑：解释器直接跑脚本，工作目录是工作区，不补 PYTHONPATH。"""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({"CLONOTH_WORKSPACE": str(workspace), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.run(
        [sys.executable, str(_DRAWTOOLS / script)], input=json.dumps(args).encode("utf-8"),
        capture_output=True, cwd=str(workspace), env=env, timeout=120,
    )
    lines = [line for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return proc.returncode, (json.loads(lines[-1]) if lines else {}), proc.stderr.decode("utf-8", errors="replace")


class TestTheScriptsSayWhy:
    def test_nai_generate_stops_before_spending(self, tmp_path: Path) -> None:
        _drawtools_config(tmp_path, "settings.yaml", _BROKEN_SETTINGS)

        code, result, stderr = _run_script("nai_generate.py", tmp_path, {"prompt": "1girl"})

        assert code == 1, stderr
        assert "settings.yaml 读不了" in result["error"]
        assert "nai-secret-key" not in result["error"]

    def test_nai_generate_from_plan_names_the_settings_not_the_plan(self, tmp_path: Path) -> None:
        _drawtools_config(tmp_path, "settings.yaml", _BROKEN_SETTINGS)

        code, result, stderr = _run_script(
            "nai_generate_from_plan.py", tmp_path, {"plan_yaml": "images:\n  - prompt: 1girl\n"},
        )

        assert code == 1, stderr
        assert "settings.yaml 读不了" in result["error"]
        assert "plan_yaml" not in result["error"]

    def test_nai_cleanup_deletes_nothing(self, tmp_path: Path) -> None:
        # 按默认的 7 天保留期，这张很久以前的图会被删掉。
        _drawtools_config(tmp_path, "settings.yaml", _BROKEN_SETTINGS)
        old = tmp_path / "data" / "attachments" / "novelai" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"png")
        os.utime(old, (0, 0))

        code, result, stderr = _run_script("nai_cleanup.py", tmp_path, {"force": True})

        assert code == 1, stderr
        assert "settings.yaml 读不了" in result["error"]
        assert old.exists()

    def test_draw_context_still_lists_the_presets_when_the_library_is_broken(self, tmp_path: Path) -> None:
        _drawtools_config(tmp_path, "character_tags.yaml", _BROKEN_LIBRARY)

        code, result, stderr = _run_script("draw_context.py", tmp_path, {"query": "画一张 miku"})

        assert code == 0, stderr
        data = result["data"]
        assert data["presets"]
        assert data["matched_characters"] == []
        assert "character_tags.yaml 读不了" in data["character_library_problem"]
        assert "没法按角色库匹配" in data["result"]

    def test_draw_context_reports_no_problem_on_a_readable_library(self, tmp_path: Path) -> None:
        code, result, stderr = _run_script("draw_context.py", tmp_path, {"query": "画一张 miku"})

        assert code == 0, stderr
        assert result["data"]["character_library_problem"] == ""
        assert result["data"]["matched_characters"]

    def test_draw_context_fails_with_the_reason_when_settings_are_broken(self, tmp_path: Path) -> None:
        _drawtools_config(tmp_path, "settings.yaml", _BROKEN_SETTINGS)

        code, result, stderr = _run_script("draw_context.py", tmp_path, {"query": "画一张 miku"})

        assert code == 1, stderr
        assert "settings.yaml 读不了" in result["error"]


class TestTheConsoleCleanup:
    def test_it_refuses_instead_of_deleting_by_the_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        package_common = importlib.import_module("tools.drawtools.common")
        package_cleanup = importlib.import_module("tools.drawtools.cleanup")
        broken = tmp_path / "settings.yaml"
        broken.write_text(_BROKEN_SETTINGS, encoding="utf-8")
        monkeypatch.setattr(package_common, "SETTINGS_PATH", broken)
        # 万一放行了，清的也只是替身工作区，不是跑测试那台机器上的图
        monkeypatch.setattr(package_cleanup, "WORKSPACE_ROOT", tmp_path)
        endpoint = next(
            route.endpoint for route in create_admin_router(tmp_path).routes
            if getattr(route, "path", "") == "/drawtools/cleanup"
        )

        with pytest.raises(HTTPException) as caught:
            endpoint()

        assert caught.value.status_code == 400
        assert "settings.yaml 读不了" in caught.value.detail
        assert not (tmp_path / "data").exists()

"""画师串预设匹配：显式写错名硬失败，隐式默认宽松兜底，选中判定大小写一致。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DRAWTOOLS = _ROOT / "tools" / "drawtools"
for _p in (str(_ROOT), str(_DRAWTOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
import format_plan  # noqa: E402
import preset_manager  # noqa: E402

_SETTINGS = {
    "params": {
        "selected_preset_id": "default-v45-full",
        "presets": [
            {"id": "cute", "name": "可爱风", "aliases": ["萌"]},
            {"id": "default-v45-full", "name": "默认 (V4.5 Full)"},
        ],
    },
}


@pytest.fixture()
def settings_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(_SETTINGS, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(common, "SETTINGS_PATH", path)
    return path


def test_switch_unknown_preset_raises_and_leaves_file_untouched(settings_path: Path) -> None:
    before = settings_path.read_text(encoding="utf-8")
    with pytest.raises(common.PresetNotFoundError):
        common.set_selected_preset("不存在的串")
    assert settings_path.read_text(encoding="utf-8") == before


def test_error_lists_available_presets(settings_path: Path) -> None:
    with pytest.raises(common.PresetNotFoundError) as excinfo:
        common.set_selected_preset("不存在的串")
    assert "可爱风（id: cute）" in excinfo.value.available


def test_switch_by_alias_persists_target_id(settings_path: Path) -> None:
    target = common.set_selected_preset("萌")
    assert target.get("id") == "cute"
    written = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert written["params"]["selected_preset_id"] == "cute"


def test_switching_writes_back_only_the_selection(settings_path: Path) -> None:
    # 补上的默认值写进实例那份，升级改了默认也到不了这个实例
    common.set_selected_preset("萌")

    written = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    expected = {**_SETTINGS, "params": {**_SETTINGS["params"], "selected_preset_id": "cute"}}
    assert written == expected


def test_switching_to_the_built_in_preset_adds_no_default_sections(settings_path: Path) -> None:
    settings_path.write_text("api:\n  api_key_env: MY_NAI_KEY\n", encoding="utf-8")

    target = common.set_selected_preset(common.DEFAULT_PRESET["id"])

    assert target["id"] == common.DEFAULT_PRESET["id"]
    written = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert written == {"api": {"api_key_env": "MY_NAI_KEY"}, "params": {"selected_preset_id": common.DEFAULT_PRESET["id"]}}


def test_switching_without_an_own_file_copies_the_template_with_the_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # 实例还没有自己那份时预设只在模板里，照模板那份写，切过去的预设下次才找得到
    example = tmp_path / "settings.example.yaml"
    example.write_text(yaml.safe_dump(_SETTINGS, allow_unicode=True, sort_keys=False), encoding="utf-8")
    own = tmp_path / "settings.yaml"
    monkeypatch.setattr(common, "SETTINGS_PATH", own)
    monkeypatch.setattr(common, "SETTINGS_EXAMPLE_PATH", example)

    common.set_selected_preset("cute")

    written = yaml.safe_load(own.read_text(encoding="utf-8"))
    assert written == {**_SETTINGS, "params": {**_SETTINGS["params"], "selected_preset_id": "cute"}}
    assert yaml.safe_load(example.read_text(encoding="utf-8")) == _SETTINGS


def test_selected_preset_falls_back_when_ref_unknown(settings_path: Path) -> None:
    settings = common.load_settings()
    preset = common.selected_preset(settings, "不存在")
    assert preset.get("id") == "default-v45-full"


def test_normalize_image_task_rejects_unknown_preset(settings_path: Path) -> None:
    settings = common.load_settings()
    with pytest.raises(common.PresetNotFoundError):
        format_plan.normalize_image_task({"preset": "不存在"}, 1, settings)


def test_normalize_image_task_resolves_alias(settings_path: Path) -> None:
    settings = common.load_settings()
    task = format_plan.normalize_image_task({"preset": "萌"}, 1, settings)
    assert task["preset_id"] == "cute"


def test_list_presets_selected_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    data = {
        "params": {
            "selected_preset_id": "CUTE",
            "presets": [
                {"id": "cute", "name": "可爱风", "aliases": ["萌"]},
                {"id": "default-v45-full", "name": "默认 (V4.5 Full)"},
            ],
        },
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(common, "SETTINGS_PATH", path)
    presets = preset_manager.list_presets()
    cute = next(p for p in presets if p["id"] == "cute")
    assert cute["selected"] is True

"""runtime 配置：代码里的兜底和 defaults/runtime.yaml 对得上，写坏的一层沿用上一份能用的并且说出来。

开关一律按同一张词表读：环境变量、配置文件、模型渠道里的三态开关不再各认各的。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
from clonoth_runtime import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG,
    RETIRED_RUNTIME_KEYS,
    RuntimeConfigError,
    check_runtime_text,
    env_bool,
    get_bool,
    load_runtime_config,
    parse_bool,
    runtime_config_error,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _leaves(data: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _leaves(value, f"{dotted}.")
        else:
            yield dotted, value


def _lookup(data: Any, dotted: str) -> tuple[bool, Any]:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cursor = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return out


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时代码目录：仓库那份 defaults/runtime.yaml 不参与，默认层只剩代码里的兜底。"""
    root = tmp_path / "code"
    (root / "defaults").mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    return root


class TestTheCodeDefaults:
    def test_every_key_both_sides_declare_has_the_same_value(self) -> None:
        # defaults/runtime.yaml 读不了时才轮到代码里这份兜底；两边不一致，同一个部署读得到和读不到时行为不一样。
        bundled = yaml.safe_load((_ROOT / "defaults" / "runtime.yaml").read_text(encoding="utf-8"))

        drifted = {
            dotted: (value, found_value)
            for dotted, value in _leaves(DEFAULT_RUNTIME_CONFIG)
            for found, found_value in [_lookup(bundled, dotted)]
            if found and found_value != value
        }

        assert drifted == {}


class TestABrokenLayer:
    def test_the_last_good_values_stay_in_force_and_the_reason_is_kept(
        self, ws: Path, code_root: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        own = _write(config_layout.runtime_path(ws), "engine:\n  max_steps: 70\n")
        assert load_runtime_config(ws)["engine"]["max_steps"] == 70
        assert runtime_config_error(ws) == ""

        with caplog.at_level(logging.WARNING, logger="clonoth_runtime"):
            _write(own, "engine: [this is not valid\n")
            assert load_runtime_config(ws)["engine"]["max_steps"] == 70

        error = runtime_config_error(ws)
        assert "config/runtime.yaml" in error and "沿用上一份能用的" in error
        assert any("runtime.yaml" in record.getMessage() for record in caplog.records)

        _write(own, "engine:\n  max_steps: 71\n# fixed\n")
        assert load_runtime_config(ws)["engine"]["max_steps"] == 71
        assert runtime_config_error(ws) == ""

    def test_a_layer_broken_from_the_start_counts_as_unwritten(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.runtime_path(ws), "- a list instead of a mapping\n")

        merged = load_runtime_config(ws)

        assert merged["engine"]["max_steps"] == DEFAULT_RUNTIME_CONFIG["engine"]["max_steps"]
        assert "这一层按没写算" in runtime_config_error(ws)

    def test_a_section_written_as_a_scalar_is_not_merged_over_the_section(self, ws: Path, code_root: Path) -> None:
        # 照样盖上去的话，engine 变成 5，读 engine.max_steps 的地方全都会崩。
        _write(config_layout.runtime_path(ws), "engine: 5\n")

        assert isinstance(load_runtime_config(ws)["engine"], dict)
        assert "engine" in runtime_config_error(ws)

    def test_a_section_with_everything_commented_out_keeps_its_defaults(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.runtime_path(ws), "engine:\n  # max_steps: 10\n")

        assert load_runtime_config(ws)["engine"]["max_steps"] == DEFAULT_RUNTIME_CONFIG["engine"]["max_steps"]
        assert runtime_config_error(ws) == ""

    def test_the_health_check_entry_point_never_raises(self, ws: Path, code_root: Path) -> None:
        (config_layout.runtime_path(ws)).mkdir(parents=True)

        assert "config/runtime.yaml" in runtime_config_error(ws)


class TestTheEditorCheck:
    @pytest.mark.parametrize("text", ["engine: [unclosed\n", "- a\n- b\n", "engine: 5\n", "engine:\n  retry: nope\n"])
    def test_what_the_runtime_could_not_use_is_refused(self, code_root: Path, text: str) -> None:
        with pytest.raises(RuntimeConfigError):
            check_runtime_text(text)

    def test_a_refusal_is_a_value_error_the_console_turns_into_a_400(self) -> None:
        assert issubclass(RuntimeConfigError, ValueError)

    @pytest.mark.parametrize("text", ["", "engine:\n  max_steps: 10\n", "engine:\n"])
    def test_a_usable_file_passes_quietly(self, code_root: Path, text: str) -> None:
        assert check_runtime_text(text) == []

    def test_a_cron_that_never_fires_passes_with_a_reminder(self, code_root: Path) -> None:
        text = "memory:\n  dream:\n    cron: '0-59/5 19 * * *'\nmaintenance:\n  data_cleanup:\n    cron: '17 * * *'\n"

        assert check_runtime_text(text) == [
            "memory.dream.cron 分钟段的 0-59/5 永远命中不了，整条不会触发",
            "maintenance.data_cleanup.cron 不是「分 时 日 月 周」五段，不会触发",
        ]

    @pytest.mark.parametrize("cron", ["", "0 19 * * *"])
    def test_an_empty_or_working_cron_passes_quietly(self, code_root: Path, cron: str) -> None:
        assert check_runtime_text(yaml.safe_dump({"memory": {"dream": {"cron": cron}}})) == []

    def test_a_retired_key_passes_with_a_reminder(self, code_root: Path) -> None:
        dotted = sorted(RETIRED_RUNTIME_KEYS)[0]

        warnings = check_runtime_text(yaml.safe_dump(_nested(dotted, 1)))

        assert len(warnings) == 1 and dotted in warnings[0]


class TestSwitches:
    @pytest.mark.parametrize("raw", [True, 1, "1", "true", "TRUE", " yes ", "y", "on", "On"])
    def test_words_for_on(self, raw: Any) -> None:
        assert parse_bool(raw) is True

    @pytest.mark.parametrize("raw", [False, 0, "0", "false", "No", "n", "off", " OFF "])
    def test_words_for_off(self, raw: Any) -> None:
        assert parse_bool(raw) is False

    @pytest.mark.parametrize("raw", [None, "", "  ", "maybe", "2", [], {}])
    def test_anything_else_is_the_default(self, raw: Any) -> None:
        assert parse_bool(raw) is None
        assert parse_bool(raw, True) is True
        assert parse_bool(raw, False) is False

    def test_an_unreadable_config_value_falls_back_to_the_default(self) -> None:
        # 和 get_int、get_float 读坏值时一样：认不出来就按默认，不是一律当成关。
        assert get_bool({"a": {"b": "maybe"}}, "a.b", True) is True
        assert get_bool({"a": {"b": "off"}}, "a.b", True) is False
        assert get_bool({}, "a.b", True) is True

    @pytest.mark.parametrize(("value", "expected"), [("on", True), ("n", False), ("", True), ("garbage", True)])
    def test_an_environment_switch_reads_the_same_words(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool,
    ) -> None:
        monkeypatch.setenv("CLONOTH_RT_SWITCH_PROBE", value)
        assert env_bool("CLONOTH_RT_SWITCH_PROBE", True) is expected

    def test_an_unset_environment_switch_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLONOTH_RT_SWITCH_PROBE", raising=False)
        assert env_bool("CLONOTH_RT_SWITCH_PROBE", False) is False

    def test_a_model_channel_tristate_uses_the_same_words(self) -> None:
        from supervisor.config_store import _tristate

        assert [_tristate(raw) for raw in (None, "", "garbage", "y", "off", True, 0)] == [
            None, None, None, True, False, True, False,
        ]


class TestTheCopyIsIndependent:
    def test_a_caller_mutating_the_result_does_not_poison_the_cache(self, ws: Path, code_root: Path) -> None:
        first = load_runtime_config(ws)
        first["engine"]["max_steps"] = -1

        assert load_runtime_config(ws)["engine"]["max_steps"] == DEFAULT_RUNTIME_CONFIG["engine"]["max_steps"]
        assert clonoth_runtime._deep_merge({"a": {"b": 1}}, {"a": None}) == {"a": {"b": 1}}

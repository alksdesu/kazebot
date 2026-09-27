"""全平台共用的功能设置：声明只写一份，平台只补自己的 env 兜底，模板和声明逐项对得上。"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _yaml_paths import leaf_paths  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import GLOBAL_ONLY, LAYER_GLOBAL, SOURCE_GLOBAL  # noqa: E402
from adapters._shared.yaml_loader import load_yaml  # noqa: E402

_EXAMPLE = _ROOT / "config" / "features.example.yaml"
_STICKER_LIBRARY = frozenset({
    "sticker_auto_tag", "sticker_auto_accept", "sticker_pending_limit", "sticker_library_limit",
    "sticker_pending_ttl_days",
})


class TestDeclarations:
    def test_every_key_can_be_set_globally(self) -> None:
        assert [key.name for key in features.FEATURE_KEYS if LAYER_GLOBAL not in key.layers] == []

    def test_no_key_reads_env(self) -> None:
        """env 兜底是各平台早就有的老设置，挂在全局声明上就分不清是哪个平台的。"""
        offenders = [key.name for key in features.FEATURE_KEYS if key.env or key.derive_default is not None]

        assert offenders == []

    def test_only_the_sticker_library_is_global_only(self) -> None:
        assert {key.name for key in features.FEATURE_KEYS if key.layers == GLOBAL_ONLY} == _STICKER_LIBRARY

    def test_adopting_keeps_everything_but_env(self) -> None:
        adopted = features.adopt("enable_echo", env=("SOME_PLATFORM_ECHO",))

        assert adopted.env == ("SOME_PLATFORM_ECHO",)
        assert dataclasses.replace(adopted, env=()) == features.FEATURE_KEYS_BY_NAME["enable_echo"]

    def test_adopting_an_unknown_name_fails_loudly(self) -> None:
        """平台键表在导入时就调它：拼错名字应当起不来，而不是少一个键。"""
        with pytest.raises(KeyError):
            features.adopt("enable_ecoh")


class TestLocation:
    def test_the_file_sits_under_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)

        assert features.features_config_path(tmp_path) == tmp_path / "config" / "features.yaml"

    def test_the_env_override_is_read_on_every_lookup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
        config = features.new_features_config(tmp_path)
        assert config.path() == tmp_path / "config" / "features.yaml"

        monkeypatch.setenv(features.FEATURES_CONFIG_ENV, str(tmp_path / "elsewhere.yaml"))

        assert config.path() == tmp_path / "elsewhere.yaml"


class TestExampleFileStaysInSync:
    """features.example.yaml 是通用键唯一的键位说明，和声明表脱钩就等于文档骗人。"""

    def _example(self) -> dict[str, Any]:
        loaded = load_yaml(_EXAMPLE.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def test_the_example_lists_exactly_the_declared_keys(self) -> None:
        leaves = set(leaf_paths(self._example())) - {"version"}

        assert sorted(leaves) == sorted(key.path for key in features.FEATURE_KEYS)

    def test_the_example_values_are_the_declared_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """示例文件必须就是当前默认行为，否则复制一份就等于悄悄改了配置。"""
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
        config = features.new_features_config(tmp_path)
        defaults = dict(config.current())

        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text(_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        config.invalidate()

        assert dict(config.current()) == defaults
        sources = config.state_payload()["sources"]
        # 三态开关的示例值是 null，本来就等于没写。
        unset = {key.name for key in features.FEATURE_KEYS if key.default is None}
        assert {name for name, source in sources.items() if source != SOURCE_GLOBAL} == unset

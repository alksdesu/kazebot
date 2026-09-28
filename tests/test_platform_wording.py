"""平台名后面接中文时隔不隔空格：规矩只有 platforms/wording.py 一份，后端各处文案都走它。

中英之间空一格，中文之间空一格就是错字。控制台的 nameWith 是同一条规矩，两边拼出来的句子才一样。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.platform_api as platform_api  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import SOURCE_ENV, SOURCE_LEGACY  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    ID_NUMERIC,
    STATE_LIVE,
    ChannelDescriptor,
    PlatformDescriptor,
)
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from platforms.wording import name_with  # noqa: E402
from supervisor.platform_accounts import PlatformAccounts  # noqa: E402
from supervisor.provisioning import ProvisionError, normalize_account  # noqa: E402
from supervisor.sticker_tagging import StickerTaggingService  # noqa: E402

# 汉字名、有配置文件：能说到「它的配置文件叫什么」的那几句用它。
VAULT = PlatformDescriptor(
    id="vault",
    display_name="保险库",
    short_name="库",
    channels=(ChannelDescriptor("vault_room", CHANNEL_GROUP),),
    id_shape=ID_NUMERIC,
    config_path="config/vault.yaml",
    state_files={STATE_LIVE: "vault_live.json"},
)
# 汉字名、没有热载配置。
PAPERLESS = PlatformDescriptor(
    id="paperless",
    display_name="无纸台",
    short_name="无纸",
    channels=(ChannelDescriptor("paperless_room", CHANNEL_GROUP),),
)
# 拉丁名对照：同一句话在它这里要空一格。
INKWELL = PlatformDescriptor(
    id="inkwell",
    display_name="InkWell",
    short_name="Ink",
    channels=(ChannelDescriptor("inkwell_room", CHANNEL_GROUP),),
    id_shape=ID_NUMERIC,
)


@pytest.fixture(autouse=True)
def _registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
    monkeypatch.delenv(QQ_NAPCAT.config_env, raising=False)
    for platform in (VAULT, PAPERLESS, INKWELL):
        register_platform(platform)
    try:
        yield
    finally:
        for platform in (VAULT, PAPERLESS, INKWELL):
            unregister_platform(platform.id)


def _refused(call: Callable[[], Any]) -> str:
    with pytest.raises((HTTPException, ProvisionError)) as caught:
        call()
    error = caught.value
    return str(error.detail if isinstance(error, HTTPException) else error)


class TestNameWith:
    @pytest.mark.parametrize(("name", "expected"), [
        ("NapCat", "NapCat 没有热载配置"),
        ("QQ · NapCat", "QQ · NapCat 没有热载配置"),
        ("OneBot11", "OneBot11 没有热载配置"),
        ("保险库", "保险库没有热载配置"),
        ("主号（备）", "主号（备）没有热载配置"),
        ("\U00020BB7", "\U00020BB7没有热载配置"),
        ("网关\U00020BB7", "网关\U00020BB7没有热载配置"),
        ("", "没有热载配置"),
    ])
    def test_it_spaces_latin_and_joins_chinese(self, name: str, expected: str) -> None:
        assert name_with(name, "没有热载配置") == expected

    def test_an_empty_sentence_leaves_no_trailing_space(self) -> None:
        assert name_with("NapCat", "") == "NapCat"
        assert name_with("", "") == ""

    def test_emoji_and_other_non_chinese_signs_take_the_space(self) -> None:
        assert name_with("网关🚀", "没有热载配置") == "网关🚀 没有热载配置"
        assert name_with("gateway-1", "没有热载配置") == "gateway-1 没有热载配置"


class TestRefusals:
    """四个 404 和一个建实例校验，中文名不多空格，拉丁名照旧空一格。"""

    def test_a_platform_without_a_hot_reloaded_config(self, tmp_path: Path) -> None:
        assert _refused(lambda: platform_api.config_file(tmp_path, PAPERLESS)) == "无纸台没有热载配置"
        assert _refused(lambda: platform_api.config_file(tmp_path, INKWELL)) == "InkWell 没有热载配置"

    def test_a_platform_without_a_console_page(self) -> None:
        assert _refused(lambda: platform_api.config_schema(VAULT)) == "保险库没有控制台配置界面"
        assert _refused(lambda: platform_api.config_schema(INKWELL)) == "InkWell 没有控制台配置界面"

    def test_a_platform_without_trigger_rules(self, tmp_path: Path) -> None:
        payload: dict[str, Any] = {"messages": []}

        assert _refused(lambda: platform_api.trigger_dry_run(tmp_path, VAULT, payload)) == "保险库没有触发判定"
        assert _refused(lambda: platform_api.trigger_dry_run(tmp_path, INKWELL, payload)) == "InkWell 没有触发判定"

    def test_a_platform_without_account_operations(self, tmp_path: Path) -> None:
        accounts = PlatformAccounts(tmp_path)

        assert _refused(lambda: accounts.ops(VAULT)) == "保险库没有账号运维"
        assert _refused(lambda: accounts.ops(INKWELL)) == "InkWell 没有账号运维"

    def test_an_account_that_does_not_fit_the_platform(self) -> None:
        assert _refused(lambda: normalize_account(VAULT, "abc")) == "保险库的账号写法不对：abc"
        assert _refused(lambda: normalize_account(QQ_NAPCAT, "abc")) == "QQ · NapCat 的账号写法不对：abc"


class TestTaggingFallbackText:
    @pytest.fixture
    def service(self, tmp_path: Path) -> StickerTaggingService:
        return StickerTaggingService(tmp_path, features.new_features_config(tmp_path))

    @pytest.mark.parametrize(("platform_id", "expected"), [
        ("vault", "保险库的环境变量（关）"),
        ("qq", "QQ · NapCat 的环境变量（关）"),
    ])
    def test_an_env_fallback(self, service: StickerTaggingService, platform_id: str, expected: str) -> None:
        fallback = features.PlatformFallback(platform_id, SOURCE_ENV, False)

        assert service._fallback_text(fallback) == expected

    @pytest.mark.parametrize(("platform_id", "expected"), [
        ("vault", "保险库的 vault.yaml 老位置（开）"),
        ("qq", "QQ · NapCat 的 qq.yaml 老位置（开）"),
    ])
    def test_an_old_line_fallback(self, service: StickerTaggingService, platform_id: str, expected: str) -> None:
        fallback = features.PlatformFallback(platform_id, SOURCE_LEGACY, True)

        assert service._fallback_text(fallback) == expected

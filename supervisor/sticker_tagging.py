"""表情包自动打标，全系统只跑一份，放在 supervisor 里。

开关听全局文件；全局没写时认各平台 bot 公布的老位置和 env 兜底，bot 停着也照认。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from platforms.registry import features_module, get_platform, platform_config_path
from stickers.store import StickerStore
from stickers.tagger import StickerTagger, TaggerConfig

from .platform_api import published_states
from .sticker_api import library_store

logger = logging.getLogger(__name__)

AUTO_TAG = "sticker_auto_tag"


class StickerTaggingService:
    def __init__(self, workspace_root: Path, features_config: Any) -> None:
        self.workspace_root = Path(workspace_root)
        self._features_config = features_config
        # 开关的来由、开库失败都只在变了的时候说：打标每分钟醒一次，每轮都喊会把日志刷没。
        self._reported: Any = None
        self._store_error = ""
        self.tagger = StickerTagger(self.workspace_root, config=self._tagger_config, open_store=self._open_store)

    def switch(self) -> Any:
        """打标眼下开不开、是谁定的。"""
        return features_module().central_switch(
            AUTO_TAG, self._features_config, published_states(self.workspace_root),
        )

    def start(self) -> None:
        self.tagger.start()

    async def stop(self) -> None:
        await self.tagger.stop()

    def _tagger_config(self) -> TaggerConfig:
        switch = self.switch()
        self._report(switch)
        return TaggerConfig(enabled=switch.enabled)

    def _open_store(self) -> StickerStore | None:
        try:
            store = library_store(self.workspace_root)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self._store_error:
                logger.warning("表情包库打不开，打标先停着：%s", error)
                self._store_error = error
            return None
        self._store_error = ""
        return store

    def _report(self, switch: Any) -> None:
        if switch == self._reported:
            return
        self._reported = switch
        state = "开着" if switch.enabled else "关着"
        if switch.set_globally or not switch.fallbacks:
            logger.info("表情包自动打标%s（%s）", state, "全局设置" if switch.set_globally else "默认")
            return
        missing = f"{self._features_config.path().name} 没写 {features_module().FEATURE_KEYS_BY_NAME[AUTO_TAG].path}"
        reasons = "、".join(self._fallback_text(fallback) for fallback in switch.fallbacks)
        if switch.enabled:
            logger.info("表情包自动打标开着：%s，按%s", missing, reasons)
            return
        # 全局页上这一项显示的是默认值「开」，不说清楚的话没人想得到是平台那边关的。
        logger.warning("表情包自动打标关着：%s，按%s。要打标就在控制台表情包图库页打开，写进全局设置", missing, reasons)

    def _fallback_text(self, fallback: Any) -> str:
        platform = get_platform(fallback.platform)
        value = "开" if fallback.value else "关"
        if fallback.from_env:
            return f"{platform.display_name} 的环境变量（{value}）"
        path = platform_config_path(platform, self.workspace_root)
        return f"{platform.display_name} 的 {path.name if path is not None else '配置文件'} 老位置（{value}）"

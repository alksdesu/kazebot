"""给表情包打标签。

标签是检索的全部依据，没打上的图对模型不可描述。后台慢慢跑，别和聊天抢 API 配额。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .store import (
    CAPTION_FAILED,
    CAPTION_PENDING,
    CAPTION_PROMPT_VERSION,
    CAPTION_RUNNING,
    StickerStore,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

# 一次给多少张。打标是纯等待，但每张都是一次计费请求，慢慢来。
_BATCH = 5
# 两次请求之间至少隔这么久，免得把中转站的频率限制打满连带影响聊天。
_MIN_INTERVAL = 1.5
_IDLE_SLEEP = 60.0
# 连着失败这么多次才判死。网络抖一下不该让一张图永久进失败堆。
_MAX_ATTEMPTS = 3
_TIMEOUT = 60.0
_TAG_MIN, _TAG_MAX = 3, 8
_TAG_MAX_LEN = 12

_TYPE_TAGS = ("表情包", "照片")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

PROMPT = (
    "为这张图片生成 5 到 7 个简短中文特征标签；图中若有可辨认的文字，"
    "再额外生成 1 个只含该文字原文的标签。\n"
    "规则：\n"
    "1. 必须有且只有一个类型标签，只能是「表情包」或「照片」二选一。\n"
    "2. 其余标签用 2 到 6 个字，描述情绪、动作、主体或画风。\n"
    "3. 不要生成露骨、色情、性化未成年人或推断隐私的标签。\n"
    "只输出一个 JSON 字符串数组，例如 [\"表情包\",\"开心\",\"猫\",\"今天也要加油\"]。"
    "不要输出解释，不要输出 Markdown。"
)
SYSTEM_PROMPT = "你是图像标签生成器，只输出 JSON 数组。"


@dataclass(frozen=True)
class TaggerConfig:
    enabled: bool = True
    batch: int = _BATCH
    min_interval: float = _MIN_INTERVAL
    idle_sleep: float = _IDLE_SLEEP


def parse_tags(raw: str) -> list[str]:
    """从模型回复里抠出标签数组。

    模型经常无视"不要 Markdown"，也经常在数组前后带一句话，所以逐层退让地找。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    payload: Any = None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("["), text.rfind("]")
        if 0 <= start < end:
            try:
                payload = json.loads(text[start : end + 1])
            except (TypeError, ValueError):
                payload = None
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if str(item or "").strip()]


def normalize_tags(tags: Sequence[str], *, fallback: str = "") -> list[str]:
    """去重、限长、保证只有一个类型标签。"""
    seen: list[str] = []
    type_tag = ""
    for raw in tags:
        tag = re.sub(r"\s+", "", str(raw or "")).strip("，,、；;")
        if not tag or len(tag) > _TAG_MAX_LEN:
            continue
        if tag in _TYPE_TAGS:
            # 只留第一个：两个类型标签会让检索里的类型权重翻倍。
            if not type_tag:
                type_tag = tag
            continue
        if tag not in seen:
            seen.append(tag)
    ordered = ([type_tag] if type_tag else []) + seen
    if len(ordered) < _TAG_MIN and fallback:
        name = fallback.strip()
        if name and name not in ordered:
            ordered.append(name)
    return ordered[:_TAG_MAX]


class StickerTagger:
    def __init__(
        self,
        workspace_root: Path,
        *,
        config: Callable[[], TaggerConfig],
        open_store: Callable[[], StickerStore | None],
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self._config = config
        self._open_store = open_store
        self._task: asyncio.Task[None] | None = None
        self._attempts: dict[str, int] = {}
        self._last_call = 0.0
        # 渠道没配好时每轮都喊一遍就把日志刷没了，只在状态变化时说。
        self._last_channel_error = ""

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._task = None
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            config = self._config()
            done = 0
            if config.enabled:
                try:
                    done = await self._sweep(config)
                except Exception:
                    logger.warning("表情包打标出错", exc_info=True)
            # 有活就立刻接着干，没活才睡；不然新收的图要等一整轮。
            await asyncio.sleep(0.0 if done else max(config.idle_sleep, 1.0))

    async def _sweep(self, config: TaggerConfig) -> int:
        store = self._open_store()
        if store is None:
            return 0
        pending = store.due_for_caption(limit=max(int(config.batch), 1))
        if not pending:
            self._attempts.clear()
            return 0

        from tools._channel import resolve_vision_channel

        vision = resolve_vision_channel(root=self.workspace_root)
        if not vision.usable:
            reason = vision.error or "看图渠道没配好"
            if reason != self._last_channel_error:
                logger.warning("表情包打标停用：%s", reason)
                self._last_channel_error = reason
            return 0
        self._last_channel_error = ""

        done = 0
        for row in pending:
            path = self.workspace_root / row.rel_path
            if not row.rel_path or not path.is_file():
                store.mark_caption(row.sha256, CAPTION_FAILED, error="文件不在了")
                continue
            gap = config.min_interval - (asyncio.get_running_loop().time() - self._last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_call = asyncio.get_running_loop().time()
            store.mark_caption(row.sha256, CAPTION_RUNNING)
            try:
                tags = await self._describe(vision, path)
            except Exception as exc:
                self._record_failure(store, row.sha256, str(exc))
                continue
            # 先判空再归一化：fallback 是给"给了但不够"补位的，不能让它把
            # "一个标签都没给"也伪装成成功，那样图片会带个没意义的名字进检索。
            if not tags:
                self._record_failure(store, row.sha256, "模型没给出标签")
                continue
            cleaned = normalize_tags(tags, fallback=row.name)
            if not cleaned:
                self._record_failure(store, row.sha256, "标签全被归一化丢弃")
                continue
            store.set_auto_tags(row.sha256, cleaned, version=CAPTION_PROMPT_VERSION)
            self._attempts.pop(row.sha256, None)
            done += 1
        return done

    def _record_failure(self, store: StickerStore, sha256: str, reason: str) -> None:
        attempts = self._attempts.get(sha256, 0) + 1
        self._attempts[sha256] = attempts
        if attempts >= _MAX_ATTEMPTS:
            store.mark_caption(sha256, CAPTION_FAILED, error=reason)
            self._attempts.pop(sha256, None)
        else:
            # 留在待办里下轮再试，别让一次超时把图永久判死。
            store.mark_caption(sha256, CAPTION_PENDING, error=reason)

    async def _describe(self, vision: Any, path: Path) -> list[str]:
        import httpx

        from tools._image import build_image_part, family_for_base_url

        part = await asyncio.to_thread(
            build_image_part, path, family_for_base_url(vision.base_url),
        )
        payload = {
            "model": vision.model,
            "temperature": 0.2,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": part.data_url()}},
                    {"type": "text", "text": PROMPT},
                ]},
            ],
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                vision.endpoint(),
                headers={"Authorization": f"Bearer {vision.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ValueError("模型返回里没有 choices")
        content = ((choices[0] or {}).get("message") or {}).get("content")
        return parse_tags(content if isinstance(content, str) else "")

"""命令目录与本地命令：帮助、模型、绘图、表情、清记忆、死信。"""
from __future__ import annotations

import logging
import asyncio
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from nonebot.adapters.onebot.v11 import Bot, Event
from engine.memory_hit_cache import prune_namespace_hits
from engine.memory_namespace import conversation_namespace
from ..config import CLONOTH_WORKSPACE, CUSTOM_FACE_METADATA_PATH, CUSTOM_FACE_NAMES_PATH
from adapters._shared import capability
from adapters._shared.send_guard import AmbiguousClaim
from ..send_contract import target_from_idempotency_key
from ..emoji_handler import (
    count_duplicate_face_names,
    duplicated_detail_names,
    format_custom_face_detail_line,
)
from stickers.store import STATE_LIBRARY, STATE_PENDING

from . import common, emotes, identity, media, models, permissions, proactive, routing, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


_HELP_RE = common._cmd_re(("帮助", "命令", "命令列表", "菜单", "help", "commands"))


# (能力, 用法, 说明)。能力为空 = 谁都能用。这张表是 /帮助 的唯一数据源，
# 加命令不更新它，用户就永远不知道它存在。
_COMMAND_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("", "/生图 <描述>", "直接出图，不走闲聊"),
    ("", "/生图帮助", "绘图的参数与写法"),
    ("", "/画师串列表", "可用画风预设"),
    ("draw_preset", "/切换画师串 <名>", "改所有人的默认画风"),
    ("model", "/当前模型", "看主模型是哪个"),
    ("model", "/切换模型 <名>", "改全局主模型"),
    ("custom_face", "/表情包帮助", "收藏表情的全部命令"),
    ("clear_memory", "/清除群记忆 [群名]", "清掉群的长期记忆"),
    ("dream", "/整理记忆", "立刻整理一遍记忆：合并重复、清掉过期"),
    ("proactive", "/主动发送帮助", "借 bot 发消息、文件、合并转发"),
    ("send_dead_letter", "/发送死信", "查看并放行卡住的投递（只能私聊）"),
    ("approval", "/审批 同意 <ID>", "处理审批（只能私聊）"),
)


def _help_text(event: Any) -> str:
    """按调用者实际拿得到的能力过滤命令表。"""
    lines = [f"{usage} —— {desc}" for cap, usage, desc in _COMMAND_CATALOG if not cap or permissions._can(cap, event)]
    if not lines:
        return "你当前没有可用命令。"
    return "【可用命令】\n" + "\n".join(lines) + "\n\n功能入口：/群协作帮助、/任务、/提醒列表、/资料。\n命令都要以 / 开头，不带斜杠的话我会当成普通聊天。"


async def _maybe_handle_help_command(*, event: Event, user_text: str) -> str | None:
    """总命令表。不设权限门 —— 列出来的本来就只有你能用的那些。"""
    if not _HELP_RE.match((user_text or "").strip()):
        return None
    return _help_text(event)


async def _maybe_handle_private_only_in_group(*, event: Event, user_text: str) -> str | None:
    """群里命中「只能私聊」的命令时说清楚，别默默丢给模型烧一轮。

    没这项能力的人拿到 None，照常走聊天 —— 提示本身也会暴露命令存在。
    """
    if not _DEAD_LETTER_RE.match((user_text or "").strip()):
        return None
    if not permissions._can("send_dead_letter", event):
        return None
    return "死信清单里有真实群号和 QQ 号，只在私聊里出。私聊我发 /发送死信。"


def _drawtools_help_text() -> str:
    return (
        "【NovelAI 生图命令】\n"
        "1) /生图 画一个穿着西装的帅哥\n"
        "   直接进入绘图节点并生成图片。\n"
        "2) /生图 初音未来，画风用可爱风\n"
        "   可在需求里指定画风/预设。\n"
        "3) /生图 给我初音未来的NAI提示词，不要画\n"
        "   只输出绘图 tag / prompt，不调用生图。\n"
        "4) /画师串列表\n"
        "   查看可用绘图预设。\n"
        "5) /切换画师串 可爱风\n"
        "   切换默认绘图预设。\n"
        "支持别名：/画图、/绘图、/nai、/draw。"
    )


def _load_drawtools_preset_manager():
    drawtools_dir = Path(CLONOTH_WORKSPACE) / "tools" / "drawtools"
    if str(drawtools_dir) not in sys.path:
        sys.path.insert(0, str(drawtools_dir))
    from preset_manager import PresetNotFoundError, list_presets, switch_preset  # type: ignore
    return list_presets, switch_preset, PresetNotFoundError


async def _maybe_handle_drawtools_command(*, event: Event, user_text: str) -> str | None:
    """处理 QQ 侧绘图帮助/预设命令；返回回复文本，None 表示不是命令。

    帮助与列表对所有人开放；切换预设会写全局 settings.yaml 改掉所有人的默认
    生图风格，与 /切换模型 同属全局配置修改，因此限管理员。
    """
    text = (user_text or "").strip()
    if not text:
        return None
    if common._DRAW_HELP_RE.match(text):
        return _drawtools_help_text()
    if common._DRAW_PRESET_LIST_RE.match(text):
        try:
            list_presets, _switch_preset, _preset_not_found = _load_drawtools_preset_manager()
            presets = list_presets()
        except Exception as exc:
            common.logger.warning("list draw presets failed: %s", exc, exc_info=True)
            return f"读取绘图预设失败：{exc}"
        if not presets:
            return "当前没有可用绘图预设。"
        lines = []
        for item in presets:
            mark = "✅ " if item.get("selected") else "   "
            lines.append(f"{mark}{item.get('name') or item.get('id')}（id: {item.get('id')}，model: {item.get('model')}，CFG: {item.get('scale')}，steps: {item.get('steps')}）")
        return "【可用画师串/绘图预设】\n" + "\n".join(lines) + "\n切换：/切换画师串 <名称或id>"
    switch_match = common._DRAW_PRESET_SWITCH_RE.match(text)
    if switch_match:
        if not permissions._can("draw_preset", event):
            return permissions._capability_denial(
                "draw_preset", event, "切换默认画师串会影响所有人，仅限 Clonoth 管理员使用。",
            )
        preset_ref = switch_match.group(1).strip()
        try:
            _list_presets, switch_preset, preset_not_found = _load_drawtools_preset_manager()
        except Exception as exc:
            common.logger.warning("load draw preset manager failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：{exc}"
        try:
            preset = switch_preset(preset_ref)
        except preset_not_found as exc:
            return f"切换失败：{exc}。可用：" + "、".join(exc.available) + "\n可发送 /画师串列表 查看。"
        except Exception as exc:
            common.logger.warning("switch draw preset failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：{exc}"
        return f"已切换默认画师串：{preset.get('name') or preset.get('id')}（id: {preset.get('id')}）"
    return None


async def _maybe_handle_model_command(
    *,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 QQ 管理员 /切换模型 <模型名> 命令，切换全局主模型。

    返回回复文本；None 表示不是本命令。切换通过 Supervisor 的
    POST /v1/config/openai 实时写入 data/config.yaml；Engine 每次处理任务
    前都会重新拉取该配置（fetch_openai_secret），因此无需重启服务。
    """
    text = (user_text or "").strip()
    if not text:
        return None

    if state._adapter.client is None:
        # 不是本命令时返回 None；确实是本命令但客户端未就绪时才提示。
        if common._MODEL_SWITCH_RE.match(text) or common._MODEL_SHOW_RE.match(text):
            return "Clonoth Agent 尚未初始化，请稍后重试。"
        return None

    allowed = permissions._can("model", event)

    # 查看当前模型 / 帮助（仅管理员，避免向普通用户暴露底层模型名）。
    if common._MODEL_SHOW_RE.match(text):
        if not allowed:
            return permissions._capability_denial("model", event, "模型命令仅限 Clonoth 管理员使用。")
        try:
            cfg = await state._adapter.client.get_openai_config()
        except Exception as exc:
            common.logger.warning("get openai config failed: %s", exc, exc_info=True)
            return f"❌ 获取当前模型失败：{exc}"
        return (
            f"ℹ️ 当前 model → {cfg.model or '(未设置)'}\n"
            "切换：/切换模型 <模型名>"
        )

    switch_match = common._MODEL_SWITCH_RE.match(text)
    if switch_match:
        if not allowed:
            # 不向非管理员暴露切换能力。
            return permissions._capability_denial("model", event, "模型命令仅限 Clonoth 管理员使用。")
        model_name = switch_match.group(1).strip()
        # 去掉可能的包裹引号/反引号。
        model_name = model_name.strip("`\"'").strip()
        if not model_name:
            return "用法：/切换模型 <模型名>"
        try:
            out = await state._adapter.client.update_openai_config(model=model_name)
        except Exception as exc:
            common.logger.warning("switch global model failed: %s", exc, exc_info=True)
            return f"❌ 切换失败：{exc}"
        new_model = ""
        try:
            new_model = str((out or {}).get("openai", {}).get("model") or "").strip()
        except Exception:
            new_model = ""
        common.logger.info("QQ admin %s switched global model -> %s", getattr(event, "user_id", ""), new_model or model_name)
        return f"✅ model → {new_model or model_name}"

    return None


_STICKER_HELP = (
    "表情包库命令：\n"
    "1) /表情包 统计\n"
    "2) /表情包 列表 或 /表情包 列表 30\n"
    "3) /表情包 待审 —— 看还没过筛的\n"
    "4) /表情包 通过 名字 —— 待审转入库\n"
    "5) /表情包 丢弃 名字 —— 丢掉并记住，同一张不会再收\n"
    "6) /表情包 删除 名字 —— 彻底删，之后还能被重新收\n"
    "7) /表情包 改名 旧名 新名\n"
    "8) /表情包 标签 名字 开心,猫\n"
    "9) /表情包 重打标 或 /表情包 重打标 失败\n"
    "10) /表情包 备份\n"
    "提示：AI 发表情包和发收藏表情用的是同一个 [表情:名称]；开关在控制台的表情包页。"
)


def _sticker_line(row: Any) -> str:
    tags = "、".join(row.tags[:5]) or "还没打标"
    return f"{row.name}（{tags}）"


def _find_sticker(store: Any, token: str) -> Any:
    """先按名字找，找不到再当哈希前缀。名字是给人用的，哈希是给出问题时兜底的。"""
    row = store.by_name(token)
    if row is not None:
        return row
    token = token.strip().lower()
    if len(token) < 6:
        return None
    return next((item for item in store.browse(limit=500) if item.sha256.startswith(token)), None)


async def _maybe_handle_sticker_command(
    *, event: Event, user_text: str,
) -> str | None:
    """处理表情包库管理命令；返回回复文本，None 表示不是命令。"""
    match = common._STICKER_RE.match((user_text or "").strip())
    if match is None:
        return None
    rest = (match.group(1) or "").strip()
    if not rest or rest in ("帮助", "help", "?", "？"):
        return _STICKER_HELP

    store = emotes._sticker_store_handle()
    if store is None:
        return "表情包库打不开，去看看 bot 日志。"
    head, _, tail = rest.partition(" ")
    tail = tail.strip()

    if head in ("统计", "状态"):
        counts = store.counts()
        return (
            f"在库 {counts['library']} 张，其中 {counts['usable']} 张可用；"
            f"待审 {counts['pending']}，已弃 {counts['discarded']}，"
            f"等打标 {counts['awaiting_caption']}。"
        )

    if head in ("列表", "待审"):
        state = STATE_LIBRARY if head == "列表" else STATE_PENDING
        try:
            limit = min(max(int(tail), 1), 50) if tail else 20
        except ValueError:
            limit = 20
        rows = store.browse(state=state, limit=limit)
        if not rows:
            return "待审池是空的。" if state == STATE_PENDING else "图库里还没有图。"
        return "\n".join([f"共 {len(rows)} 张：", *(_sticker_line(row) for row in rows)])

    allowed = permissions._can("custom_face", event)
    if not allowed:
        return permissions._capability_denial("custom_face", event, "表情包库的写操作仅限 Clonoth 管理员。")

    if head == "重打标":
        queued = store.reset_captions(only_failed=tail in ("失败", "failed"))
        return f"已把 {queued} 张排进打标队列，后台慢慢跑。"

    if head == "备份":
        from stickers.backup import export_library

        try:
            result = await asyncio.to_thread(export_library, Path(CLONOTH_WORKSPACE))
        except Exception as exc:
            common.logger.warning("表情包备份失败", exc_info=True)
            return f"备份失败：{exc}"
        return f"已备份 {result.stickers} 条记录、{result.images} 张图到 {result.path.name}。"

    if not tail:
        return f"这个命令要带名字。{_STICKER_HELP}"

    if head == "改名":
        old, _, new = tail.partition(" ")
        row = _find_sticker(store, old.strip())
        if row is None:
            return f"没找到：{old}"
        if not new.strip():
            return "要给新名字。"
        return f"已改名为：{store.rename(row.sha256, new.strip())}"

    if head == "标签":
        name, _, raw = tail.partition(" ")
        row = _find_sticker(store, name.strip())
        if row is None:
            return f"没找到：{name}"
        tags = [item.strip() for item in re.split(r"[,，、\s]+", raw) if item.strip()]
        if not tags:
            return "要给至少一个标签。"
        store.set_manual_tags(row.sha256, tags, override=True)
        return f"{row.name} 的标签已改为：{'、'.join(tags)}（人工标签覆盖自动标签）"

    row = _find_sticker(store, tail)
    if row is None:
        return f"没找到：{tail}"

    if head == "通过":
        from stickers.collect import accept_pending

        if not accept_pending(Path(CLONOTH_WORKSPACE), store, row.sha256):
            return f"{row.name} 不在待审池里。"
        return f"{row.name} 已入库，打完标就能用。"

    if head == "丢弃":
        from stickers.collect import drop_sticker_file

        drop_sticker_file(Path(CLONOTH_WORKSPACE), row.rel_path)
        store.discard(row.sha256)
        return f"已丢弃 {row.name}，同一张图不会再被收进来。"

    if head == "删除":
        from stickers.collect import drop_sticker_file

        drop_sticker_file(Path(CLONOTH_WORKSPACE), row.rel_path)
        store.forget(row.sha256)
        return f"已删除 {row.name}。它再出现在群里还会被重新收。"

    return _STICKER_HELP


async def _maybe_handle_custom_face_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理 QQ 侧收藏表情管理命令；返回回复文本，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None

    allowed = permissions._can("custom_face", event)

    if common._CUSTOM_FACE_HELP_RE.match(text):
        if not allowed:
            return permissions._capability_denial("custom_face", event, "表情包命令仅限 Clonoth 管理员使用。")
        return (
            "【表情包管理命令示例（仅管理员）】\n"
            "1) /同步表情列表\n"
            "   从 NapCat 收藏同步已命名表情到本地文件。\n"
            "2) /表情列表 或 /表情列表 50\n"
            "   查看 AI 当前可用的表情名称。\n"
            "3) /表情详情列表 或 /表情详情列表 50\n"
            "   查看收藏详情（含未命名项）与序号。\n"
            "4) /收藏表情 开心\n"
            "   收藏当前消息/引用/最近的一张图片，并命名为“开心”。\n"
            "5) /命名表情 3 开心\n"
            "   给第 3 个收藏表情命名/改名（也可用 md5/resId/文件名定位）。\n"
            "6) /删除表情 开心\n"
            "   删除名为“开心”的收藏表情。\n"
            "提示：AI 发送表情用 [表情:名称]；未命名表情不会给 AI 使用。"
        )

    # 以下写操作命令仅限管理员：同步 / 收藏 / 命名 / 删除。
    if common._CUSTOM_FACE_SYNC_RE.match(text):
        if not allowed:
            return permissions._capability_denial("custom_face", event, "同步表情列表仅限 Clonoth 管理员使用。")
        try:
            names = await emotes._sync_custom_face_names_file(bot)
        except Exception as exc:
            common.logger.warning("sync custom face names failed: %s", exc, exc_info=True)
            return "同步表情列表失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not names:
            return f"已同步，但没有发现已命名收藏表情。未命名表情不会写入 AI 表情列表文件：{CUSTOM_FACE_NAMES_PATH}"
        duplicates = count_duplicate_face_names(state._adapter.custom_face_metadata)
        total = len(state._adapter.custom_face_metadata)
        dup_note = ""
        if duplicates:
            dup_desc = "、".join(f"{name}(x{count})" for name, count in list(duplicates.items())[:20])
            dup_note = (
                f"\n⚠ 检测到 {len(duplicates)} 组同名表情：{dup_desc}\n"
                "同名表情已全部保留，AI 名称列表仅显示一个基础名；发送时会在同名表情中随机选择一个。"
            )
        return (
            f"已同步 {total} 个已命名收藏表情（AI 可见基础名 {len(names)} 个）。\n"
            f"AI 名称文件：{CUSTOM_FACE_NAMES_PATH}\n"
            f"内部元数据文件：{CUSTOM_FACE_METADATA_PATH}\n"
            "AI 只看到名称文件里的基础名，md5/resId/emojiId 保存在元数据文件里。"
            + dup_note
        )

    list_match = common._CUSTOM_FACE_LIST_RE.match(text)
    if list_match:
        if not allowed:
            return permissions._capability_denial("custom_face", event, "表情列表仅限 Clonoth 管理员使用。")
        limit = int(list_match.group(1) or "30")
        limit = max(1, min(100, limit))
        names = emotes._load_custom_face_names_file()
        if not names:
            return f"AI 表情列表文件为空：{CUSTOM_FACE_NAMES_PATH}\n可发送 /同步表情列表 从 NapCat 写入已命名表情；未命名表情不会写入。"
        shown = names[:limit]
        lines = [f"・{name}" for name in shown]
        more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情列表 {min(len(names), 100)}”查看更多。"
        return "AI 可用收藏表情：\n" + "\n".join(lines) + more + "\n模型可用格式：[表情:名称]\n（此处不显示定位序号，命名/删除请先发“表情详情列表”）"

    detail_match = common._CUSTOM_FACE_DETAIL_LIST_RE.match(text)
    if detail_match:
        if not allowed:
            return permissions._capability_denial("custom_face", event, "表情详情列表仅限 Clonoth 管理员使用。")
        limit = int(detail_match.group(1) or "50")
        limit = max(1, min(100, limit))
        try:
            items = await state._adapter.emoji.list_custom_face_details(bot, state._adapter.bqbs, count=max(limit, 48))
        except Exception as exc:
            common.logger.warning("list custom face details failed: %s", exc, exc_info=True)
            return "获取收藏表情详情失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not items:
            return "当前没有可识别的收藏表情。"
        shown = items[:limit]
        dups = duplicated_detail_names(items)
        lines = [format_custom_face_detail_line(item, dups) for item in shown]
        more = "" if len(items) <= limit else f"\n……还有 {len(items) - limit} 个，可发送“表情详情列表 {min(len(items), 100)}”查看更多。"
        return "收藏表情详情（含未命名项）：\n" + "\n".join(lines) + more + "\n未命名表情可用：命名表情 <序号> <新名字>；序号会随收藏列表变动，同名项请用括号里的 md5 定位。"

    rename_match = common._CUSTOM_FACE_RENAME_RE.match(text)
    if rename_match:
        if not allowed:
            return permissions._capability_denial("custom_face", event, "命名/重命名表情仅限 Clonoth 管理员使用。")
        target = rename_match.group(1).strip()
        desc = rename_match.group(2).strip()
        if not target or not desc:
            return "请指定要命名的表情和新名字，例如：命名表情 3 开心"
        # 若定位词是纯基础名且存在多个同名，不直接改，列序号让管理员指定具体一个。
        try:
            siblings = await state._adapter.emoji.find_custom_faces_by_base_name(bot, target, state._adapter.bqbs)
        except Exception as exc:
            common.logger.warning("find custom faces (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{target}”，为避免改错，请指定要命名哪一个：\n"
                + "\n".join(lines)
                + f"\n请改用序号，例如：命名表情 {siblings[0]['index']} {desc}"
                + "\n也可用 md5 或 resId 精确定位。"
            )
        try:
            face = await state._adapter.emoji.resolve_custom_face(bot, target, state._adapter.bqbs)
        except Exception as exc:
            common.logger.warning("resolve custom face (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{target}，但当前 OneBot/NapCat 只返回图片 URL，没有 emojiId/resId/md5，无法命名。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 set_custom_face_desc。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{target}\n可先发送“表情详情列表 50”查看序号，再用“命名表情 <序号> <新名字>”。"
        ok, message = await emotes._set_custom_face_desc(bot, face, desc)
        return message

    delete_match = common._CUSTOM_FACE_DELETE_RE.match(text)
    if delete_match:
        if not allowed:
            return permissions._capability_denial("custom_face", event, "删除表情仅限 Clonoth 管理员使用。")
        name = delete_match.group(1).strip()
        if not name:
            return "请指定要删除的表情名称，例如：删除表情 开心"
        # 若输入是纯基础名（不是序号/md5/resId 这类唯一定位），且存在多个同名，
        # 则不直接删除，改为列出同名项的序号，让管理员明确指定删哪个。
        try:
            siblings = await state._adapter.emoji.find_custom_faces_by_base_name(bot, name, state._adapter.bqbs)
        except Exception as exc:
            common.logger.warning("find custom faces (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{name}”，为避免误删，请指定要删除哪一个：\n"
                + "\n".join(lines)
                + "\n请改用序号删除，例如：删除表情 " + str(siblings[0]["index"])
                + "\n也可用 md5 或 resId 精确删除。"
            )
        try:
            face = await state._adapter.emoji.resolve_custom_face(bot, name, state._adapter.bqbs)
        except Exception as exc:
            common.logger.warning("resolve custom face (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{name}，但当前 OneBot/NapCat 只返回图片 URL，没有 resId，无法删除。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 delete_custom_face。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{name}"
        res_id = face.get("resId") or face.get("res_id") or face.get("id")
        if not res_id:
            return f"找到了表情 {name}，但没有 resId，无法删除。"
        try:
            await bot.call_api("delete_custom_face", res_id=str(res_id))
            state._adapter.emoji.invalidate_custom_face_cache(bot)
            try:
                await emotes._sync_custom_face_names_file(bot)
            except Exception:
                common.logger.warning("sync custom face names after delete failed", exc_info=True)
            return f"已删除收藏表情：{name}"
        except Exception as exc:
            common.logger.warning("delete_custom_face failed: %s", exc, exc_info=True)
            return "删除收藏表情失败：当前 OneBot 实现可能不支持 delete_custom_face，或 resId 已失效。"

    add_match = common._CUSTOM_FACE_ADD_RE.match(text)
    if add_match:
        if not allowed:
            return permissions._capability_denial("custom_face", event, "收藏表情仅限 Clonoth 管理员使用。")
        alias = add_match.group(1).strip()
        if not alias:
            return "请给表情起一个名字，例如：收藏表情 开心"
        attachments, errors = await media._custom_face_command_attachments(
            bot=bot,
            event=event,
            conversation_key=conversation_key,
            current_attachments=current_attachments,
        )
        if not attachments:
            suffix = "\n" + "\n".join(dict.fromkeys(errors)) if errors else ""
            return "没有找到可收藏的图片。请在同一条消息里带图，或引用/紧接着回复一张图片：收藏表情 名称" + suffix
        return await emotes._add_custom_face_from_attachment(bot, alias, attachments[0])

    return None


# 命令别名：群聊里直接清当前群；私聊里列可清理群名或按群名清指定群。
_CLEAR_GROUP_MEMORY_RE = common._cmd_re(
    ("清除群记忆", "清空群记忆", "清理群记忆", "清除群聊记忆", "清空群聊记忆", "清理群聊记忆"), common._CMD_REST,
)


def _parse_clear_group_memory_command(text: str) -> Optional[str]:
    """识别 /清除群记忆 命令。返回目标群引用（可为空字符串表示当前群/需列表）。

    返回 None 表示不是本命令。返回 "" 表示命令无参数；返回非空字符串表示
    管理员显式指定了群名/群号。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _CLEAR_GROUP_MEMORY_RE.match(raw)
    if match is None:
        return None
    return match.group(1).strip()


def _conversation_memory_namespace_for_group(group_id: int) -> str:
    """计算 QQ 群对应的 memory namespace 目录名。

    落盘用的是稳定哈希 key 的摘要，不是真实群号的摘要：engine 侧收到的
    conversation_key 已经被 _stable_conversation_key 换成 qq_group:<digest>，
    knowledge_inject 再对它做一次 SHA256。少算这一层就会去删一个从不存在的目录。
    这里用 _scoped_stable_key 而非 _stable_conversation_key，后者会顺带
    给群分配匿名别名。
    """
    return conversation_namespace(routing._scoped_stable_key(f"qq_group:{int(group_id)}"))


def _clear_group_memory_namespace(group_id: int) -> tuple[bool, int]:
    """删除指定群的 memory namespace 目录及其命中缓存条目。

    返回 (是否存在过目录, 删除的 memory book 文件数)。删除后只摘掉这个 namespace
    的命中记录，不再整份删掉全局 .hit_cache.json 抹平其他群的命中。
    """
    namespace = _conversation_memory_namespace_for_group(group_id)
    if not namespace:
        # 空串拼出来就是 data/memory 根目录，删下去是所有会话的记忆。
        return False, 0
    mem_root = Path(CLONOTH_WORKSPACE) / "data" / "memory"
    ns_dir = mem_root / namespace
    deleted_books = 0
    existed = ns_dir.exists() and ns_dir.is_dir()
    if existed:
        try:
            deleted_books = sum(1 for _ in ns_dir.glob("*.yaml"))
        except Exception:
            deleted_books = 0
        import shutil as _shutil
        try:
            _shutil.rmtree(ns_dir)
        except Exception:
            common.logger.exception("remove memory namespace failed: %s", ns_dir)
            existed = False
    # engine 侧的 catalog 缓存靠它自己 2 秒的 TTL 过期。这里不能 import 它来 invalidate：
    # engine worker 是独立进程，import 只会在 bot 进程里新建一份空缓存对象，
    # 操作它等于什么都没做（而且会把整条 engine 依赖链拉进 NoneBot 进程）。
    _prune_hit_cache_for_namespace(namespace)
    return existed, deleted_books


def _prune_hit_cache_for_namespace(namespace: str) -> None:
    """删掉这个 namespace 在 .hit_cache.json 里的命中记录。

    命中缓存是全局一个文件，整份删掉会把其他群的「最近命中」一起抹平，
    之后 14 天扫除只能按 updated_at 判龄，老而常被召回的条目会被误删。
    """
    try:
        prune_namespace_hits(Path(CLONOTH_WORKSPACE), namespace)
    except Exception:
        common.logger.debug("prune hit cache failed", exc_info=True)


async def _clearable_group_targets(bot: Bot) -> list[models.ProactiveTarget]:
    """列出管理员可清理记忆的群（复用主动群目标候选逻辑）。"""
    return await proactive._group_target_candidates(bot)


async def _clear_group_memory_list_text(bot: Bot) -> str:
    """生成私聊场景下"可清理群记忆"的群名列表提示。"""
    groups = await _clearable_group_targets(bot)
    names = [identity._sanitize_name(t.label, max_len=30) for t in groups if t.label]
    body = "、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"
    return (
        "【可清理群记忆的群】\n"
        + body
        + "\n\n用法：/清除群记忆 <群名>\n"
        "例如：/清除群记忆 " + (names[0] if names else "某群")
        + "\n提示：群名可用 get_group_list 中的群名或配置别名；也可用 /清除群记忆 群号"
    )


async def _maybe_handle_clear_group_memory_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 /清除群记忆 命令。

    - 群聊中无参数：清空当前群记忆。
    - 私聊中无参数：返回可清理群列表 + 用法。
    - 任意场景带参数（群名/群号）：解析目标群并清空其记忆。
    没有这项能力的人一律拒绝，且不暴露命令能力细节。
    """
    target_ref = _parse_clear_group_memory_command(user_text)
    if target_ref is None:
        return None
    if not permissions._can("clear_memory", event):
        return permissions._capability_denial("clear_memory", event, "该请求不可用。")
    requester = permissions._requester(event)

    # 无参数：群聊直接清当前群；私聊给出可清理列表。
    if not target_ref:
        group_id = permissions._as_qq_id(getattr(event, "group_id", None))
        if group_id is not None:
            existed, count = _clear_group_memory_namespace(group_id)
            if existed:
                return f"已清空当前群的记忆（删除 {count} 个记忆本）。"
            return "当前群没有可清理的记忆（记忆目录为空或从未生成）。"
        scope_denied = capability.scope_deny_reason("clear_memory", requester, None)
        if scope_denied:
            # 私聊里没有「本群」，群主的作用域为空；这份清单也不该给他看。
            return scope_denied
        return await _clear_group_memory_list_text(bot)

    # 带参数：解析目标群（复用主动发送的目标解析，支持群名/别名/显式群号）。
    target, error = await proactive._resolve_proactive_target(
        bot, requester, "group", target_ref, capability_key="clear_memory",
    )
    if target is None:
        # 附带可清理群列表，方便管理员纠正群名。名单之外的人拿不到这份清单。
        if not capability.may_see_target_roster(requester):
            return error or "目标群解析失败。"
        list_hint = await _clear_group_memory_list_text(bot)
        return (error or "目标群解析失败。") + "\n\n" + list_hint
    scope_denied = proactive._target_scope_denied("clear_memory", requester, target)
    if scope_denied:
        return scope_denied
    existed, count = _clear_group_memory_namespace(int(target.target_id))
    label = identity._sanitize_name(target.label, max_len=40)
    if existed:
        return f"已清空群「{label}」的记忆（删除 {count} 个记忆本）。"
    return f"群「{label}」没有可清理的记忆（记忆目录为空或从未生成）。"


_DREAM_RE = common._cmd_re(("整理记忆", "记忆整理", "整理一下记忆", "dream"))


async def _maybe_handle_dream_command(
    *,
    event: Event,
    user_text: str,
    conversation_key: str,
) -> str | None:
    """处理 /整理记忆：手动跑一次记忆整理。

    整理要几分钟，这里只等到「已受理」；摘要由引擎跑完后推回本会话。
    """
    if not _DREAM_RE.match((user_text or "").strip()):
        return None
    if not permissions._can("dream", event):
        return permissions._capability_denial("dream", event, "该请求不可用。")
    if state._adapter.client is None:
        return "Clonoth Agent 尚未初始化，请稍后重试。"

    try:
        out = await state._adapter.client.run_dream_now(notify_conversation_key=conversation_key)
    except Exception as exc:
        common.logger.warning("trigger dream failed: %s", exc, exc_info=True)
        return f"❌ 触发整理失败：{exc}"

    status = str((out or {}).get("status") or "")
    if status == "started":
        return "已开始整理记忆，跑完把摘要发这儿。通常要几分钟。"
    if status == "busy":
        return "上一轮整理还在跑，等它结束再来。"
    return "引擎没接住这次触发（任务通道不可用），看一下服务状态。"


_DEAD_LETTER_RE = common._cmd_re(("发送死信", "投递死信"), common._CMD_REST)


_DEAD_LETTER_CLEAR_RE = re.compile(r"^(?:清理|清除|放行)\s*(\S+)$")


_DEAD_LETTER_ALL_WORDS = frozenset({"全部", "所有", "all"})


_DEAD_LETTER_LIST_LIMIT = 20


def _parse_dead_letter_command(text: str) -> tuple[str, str] | None:
    """解析 /发送死信 命令，返回 (动作, 参数)；不是本命令返回 None。"""
    match = _DEAD_LETTER_RE.match(str(text or "").strip())
    if not match:
        return None
    tail = match.group(1).strip()
    if not tail:
        return ("list", "")
    clear_match = _DEAD_LETTER_CLEAR_RE.match(tail)
    if not clear_match:
        # 「发送死信吧」这类尾串不是编号，回用法提示而不是拿它去查记录。
        return ("unknown", tail)
    arg = clear_match.group(1).strip()
    if arg in _DEAD_LETTER_ALL_WORDS:
        return ("clear_all", "")
    return ("clear", arg)


def _dead_letter_target_label(key: str) -> str:
    """把幂等 key 还原成人类可读的目标标签，不查 bot API（NapCat 断线也要能打印）。"""
    target = target_from_idempotency_key(key)
    if target.startswith("group:"):
        return f"群{target.split(':', 1)[1]}"
    if target.startswith("private:"):
        return f"私聊{target.split(':', 1)[1]}"
    return "未知目标"


def _format_dead_letter_lines(claims: list[AmbiguousClaim]) -> str:
    lines = []
    for claim in claims:
        when = dt.datetime.fromtimestamp(claim.updated).strftime("%m-%d %H:%M")
        lines.append(f"{claim.handle} {_dead_letter_target_label(claim.key)} {when} {claim.last_error[:60]}")
    return "\n".join(lines)


async def _maybe_handle_dead_letter_command(*, event: Event, user_text: str) -> str | None:
    """处理 /发送死信 命令：无参数列出死信，带「清理 <编号>」放行一条。

    只挂在私聊入口：清单里带真实群号，群里打出来等于把别的群号广播给群成员。
    """
    parsed = _parse_dead_letter_command(user_text)
    if parsed is None:
        return None
    if not permissions._can("send_dead_letter", event):
        return permissions._capability_denial("send_dead_letter", event, "该请求不可用。")
    action, arg = parsed
    if action == "unknown":
        return "用法：/发送死信 查看卡住的投递；/发送死信 清理 <编号> 放行一条；/发送死信 清理 全部。"
    if action == "list":
        claims = await state._adapter.outbound_idempotency.ambiguous_claims(limit=_DEAD_LETTER_LIST_LIMIT)
        total = await state._adapter.outbound_idempotency.ambiguous_count()
        if not claims:
            return "当前没有卡住的投递记录。"
        header = f"【卡住的投递（共 {total} 条，显示前 {_DEAD_LETTER_LIST_LIMIT} 条）】"
        usage = (
            "放行：/发送死信 清理 <编号>；全部放行：/发送死信 清理 全部\n"
            "放行等于承认这条消息平台可能已经收到过，之后同样内容再发一次就会重复。"
        )
        return f"{header}\n{_format_dead_letter_lines(claims)}\n\n{usage}"
    if action == "clear_all":
        count = await state._adapter.outbound_idempotency.clear_all_ambiguous()
        common.logger.warning(
            "onebot_dead_letter_cleared",
            extra={"idempotency_key": "*", "operator": str(getattr(event, "user_id", "")), "count": count},
        )
        return f"已放行 {count} 条。其中任意一条都可能已经送到过，重发会重复。"
    candidates = [
        claim for claim in await state._adapter.outbound_idempotency.ambiguous_claims(limit=0)
        if claim.handle.startswith(arg)
    ]
    if not candidates:
        return "没有这个编号的记录，可能已到期自动放行。"
    if len(candidates) > 1:
        return "编号不唯一，请多输几位。"
    claim = candidates[0]
    if not await state._adapter.outbound_idempotency.clear_ambiguous(claim.key):
        return "没有这个编号的记录，可能已到期自动放行。"
    common.logger.warning(
        "onebot_dead_letter_cleared",
        extra={"idempotency_key": claim.key, "operator": str(getattr(event, "user_id", ""))},
    )
    label = _dead_letter_target_label(claim.key)
    return f"已放行 {claim.handle}（{label}）。这条消息平台可能已经收到过，同样内容再发一次就会重复。"

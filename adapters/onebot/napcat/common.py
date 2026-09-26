"""QQ 适配器各模块共用的常量、命令正则与 logger。"""
from __future__ import annotations

import datetime as dt
import logging
import re
from types import MappingProxyType
from typing import Mapping
from nonebot import get_driver
from ..faces import face_display_name


logger = logging.getLogger("nonebot.plugin.clonoth_agent")


driver = get_driver()


_CST = dt.timezone(dt.timedelta(hours=8))


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")


_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")


_AT_QQ_RE = re.compile(r"\[(?:CQ:)?at[,:][^\]]*?qq=([^,\]\s]+)", re.IGNORECASE)


# 命令一律 / 开头。裸词会把「画图软件推荐哪个」「可用表情」这类日常句子当命令截胡，
# 而 / 不在可剥离前缀里，群聊私聊拿到的都是同一个字面量。全角 ／ 一并收：
# 中文输入法下打出来的是另一个码位，用户看不出区别。
_CMD_PREFIX = r"[/／]"


# 尾参形态。命令只在这里分叉，别名表各自维护。
_CMD_NO_ARG = r"\s*$"


_CMD_OPT_COUNT = r"(?:\s+(\d+))?\s*$"


_CMD_ONE_ARG = r"\s+(.+?)\s*$"


_CMD_TWO_ARGS = r"\s+(\S+)\s+(.+?)\s*$"


# 吞掉剩余全部内容。中文别名后面常常不带空格（/生图画只猫），所以不能要求词边界，
# 代价是 /drawhelp 会被 draw 分支吃掉 —— 靠分派链里 help 先判来保证，顺序不能动。
_CMD_REST = r"\s*(.*)$"


def _cmd_re(aliases: tuple[str, ...], tail: str = _CMD_NO_ARG) -> "re.Pattern[str]":
    """把别名表编成「/别名 + 尾参」的命令正则。"""
    body = "|".join(re.escape(alias) for alias in aliases)
    return re.compile(rf"^{_CMD_PREFIX}(?:{body}){tail}", re.IGNORECASE)


def _strip_command_prefix(text: str) -> str:
    """剥掉引导斜杠交给集合式命令解析；不是命令返回空串。

    首段里再出现斜杠的一律不认：群里贴 /var/log/x 这类路径很常见，
    而开了前缀触发信号之后它们照样会走到命令链上来。
    """
    raw = str(text or "").strip()
    if not raw or raw[0] not in "/／":
        return ""
    body = raw[1:].lstrip()
    head = body.split(None, 1)[0] if body else ""
    if not head or "/" in head or "／" in head:
        return ""
    return body


_CUSTOM_FACE_LIST_RE = _cmd_re(("表情列表", "收藏表情列表", "emoji列表", "可用表情"), _CMD_OPT_COUNT)


_CUSTOM_FACE_DETAIL_LIST_RE = _cmd_re(("表情详情列表", "收藏表情详情", "表情管理列表"), _CMD_OPT_COUNT)


_CUSTOM_FACE_SYNC_RE = _cmd_re(("同步表情列表", "刷新表情列表", "更新表情列表"))


_STICKER_RE = _cmd_re(("表情包", "表情包库", "图库"), _CMD_REST)


_CUSTOM_FACE_HELP_RE = _cmd_re(("表情包帮助", "表情帮助", "表情包命令", "表情命令帮助"))


_CUSTOM_FACE_ADD_RE = _cmd_re(("收藏表情", "添加表情", "保存表情", "表情收藏"), _CMD_ONE_ARG)


_CUSTOM_FACE_RENAME_RE = _cmd_re(("命名表情", "重命名表情", "改名表情"), _CMD_TWO_ARGS)


_CUSTOM_FACE_DELETE_RE = _cmd_re(("删除表情", "移除表情", "取消收藏表情"), _CMD_ONE_ARG)


_DRAW_DIRECT_RE = _cmd_re(
    ("生图", "画图", "绘图", "nai生图", "novelai生图", "draw", "nai", "novelai"), _CMD_REST,
)


_DRAW_HELP_RE = _cmd_re(("生图帮助", "画图帮助", "绘图帮助", "画师串帮助", "drawhelp", "naihelp"))


_DRAW_PRESET_LIST_RE = _cmd_re(
    ("画师串列表", "绘图预设列表", "生图预设列表", "画风列表", "drawpresets", "presets"),
)


_DRAW_PRESET_SWITCH_RE = _cmd_re(
    ("切换画师串", "切换绘图预设", "切换生图预设", "切换画风", "setdrawpreset", "preset"), _CMD_ONE_ARG,
)


_MODEL_SWITCH_RE = _cmd_re(
    ("切换模型", "切换主模型", "切模型", "设置模型", "setmodel", "switchmodel"), _CMD_ONE_ARG,
)


# 去掉了「模型帮助」：它和「当前模型」是同一个分支，只回一行模型名，叫帮助属于误导。
_MODEL_SHOW_RE = _cmd_re(("当前模型", "查看模型", "model", "showmodel"))


DRAW_NODE_ID = "draw.novelai_planner"


# 2026-07-09: 旧的"5~12 位数字一律当作 QQ 号"兜底匿名正则已废弃（会误伤金额/验证码/
# 日期等普通数字）。现改为只对采集入口登记的已知真实 ID 做精确匿名，不再保留该正则。
# 2026-05-03 修改原因：QQ 端需要把 Clonoth 任务生命周期映射为离散 React 阶段。
# 做法是集中维护阶段到 emoji_id 的映射和阶段顺序，目的在于后续回调只声明
# 目标阶段，避免 stream_delta 高频到达时重复调用 OneBot React API。
# 模型可用的表态 emoji。
_REACT_MODEL_EMOJI_IDS = (
    "4", "5", "9", "14", "63", "66", "76", "79", "99", "182", "201", "264", "271", "319",
)


# 名字只在 faces 表里写一份：这里和收到表情时显示的必须是同一个词。
_REACT_MODEL_EMOJIS: Mapping[str, str] = MappingProxyType({
    eid: face_display_name(eid) for eid in _REACT_MODEL_EMOJI_IDS
})


assert all(_REACT_MODEL_EMOJIS.values())


_SEARCH_PROGRESS_FIRST_NOTICE = "已收到联网搜索请求，正在检索网页资料，可能需要几秒钟……"


_SEARCH_PROGRESS_STILL_RUNNING_NOTICE = "还在联网搜索中（已等 {waited} 秒），我会拿到结果后马上整理回复。"


_SEARCH_PROGRESS_STILL_RUNNING_DELAY_SEC = 20.0


_SEARCH_PROGRESS_KEYWORDS = ("web_search", "exa_search", "x_search")

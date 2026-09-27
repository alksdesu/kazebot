"""平台输出说明：按能力声明和运行期目录生成，随入站交给引擎放进提示词，不进对话历史。

稳定的几段进可缓存的前缀，每轮会变的（按概率抽的表情包清单）进动态区。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from adapters._shared import outbound
from platforms.descriptor import PlatformDescriptor

GUIDE_VERSION = 1
SECTION_SPLIT = "split"
SECTION_REACTIONS = "reactions"
SECTION_EMOJI = "emoji"


@dataclass(frozen=True)
class GuideSection:
    key: str
    text: str
    # 每轮会变的段放进动态区；稳定的段进缓存前缀，内容一变缓存就整段失效。
    volatile: bool = False

    def payload(self) -> dict[str, Any]:
        return {"key": self.key, "text": self.text, "volatile": self.volatile}


@dataclass(frozen=True)
class OutputGuide:
    sections: tuple[GuideSection, ...] = ()

    @classmethod
    def of(cls, *sections: Optional[GuideSection]) -> "OutputGuide":
        return cls(tuple(section for section in sections if section is not None and section.text.strip()))

    def section(self, key: str) -> Optional[GuideSection]:
        return next((section for section in self.sections if section.key == key), None)

    def payload(self) -> dict[str, Any]:
        """交给 submit_inbound / preempt_task 的形状。一段都没有也照发：打断时它表示把任务原有的说明清掉。"""
        return {"version": GUIDE_VERSION, "sections": [section.payload() for section in self.sections]}


def split_section(platform: PlatformDescriptor) -> Optional[GuideSection]:
    """分条说明。平台一次只能发一条时不给：[SPLIT] 会被并回一条，教了也白教。"""
    most = platform.capabilities.max_split_parts
    if most == 1:
        return None
    marker = outbound.SPLIT_MARKER
    lines = [
        f"【{platform.short_name}回复分条】",
        "普通换行只在一个消息气泡内排版。想分成独立消息时，"
        f"用 {marker} 分隔直接发给用户的回复正文（包括 finish.text、reply.text）；平台会分条发送，标记不会显示。",
        f"例如“收到{marker}我看看”会发成两条。按语气和信息量决定，通常一条就够，"
        "确需分条时以 2–3 条为宜，不要逐句刷屏。",
    ]
    if most > 0:
        lines.append(f"最多 {most} 条，多出来的会并进最后一条。")
    lines.extend([
        "代码、列表、引用等需要连续阅读的内容不要拆散，保留普通换行。",
        f"当前任务要求简短接话或只回一句时，只发一条，不使用 {marker}。",
    ])
    return GuideSection(SECTION_SPLIT, "\n".join(lines))


def reaction_section(platform: PlatformDescriptor, kind: str, catalog: Mapping[str, str]) -> Optional[GuideSection]:
    """表态说明，只列驱动认的目录：目录外的 ID 会被丢掉。驱动关了表态就传空目录。"""
    if kind not in platform.capabilities.reactions or not catalog:
        return None
    items = "、".join(f"{ref}={name}" for ref, name in catalog.items())
    return GuideSection(SECTION_REACTIONS, "\n".join([
        f"【{platform.short_name}表情表态】",
        "想对用户这条消息表态时，在回复里写 [REACT:ID]，平台会把对应表情贴到用户那条消息上。",
        f"只能使用下列 ID：{items}。",
        "标记本身不会出现在回复文本里；表态是点缀，一条回复最多 1 个。",
    ]))


def emoji_section(
    platform: PlatformDescriptor,
    *,
    native_label: str = "",
    native_names: Sequence[str] = (),
    native_hidden: int = 0,
    stickers: Sequence[str] = (),
) -> Optional[GuideSection]:
    """表情说明：平台原生表情按名单给全，表情包这一轮抽中了才给。

    表情包清单每轮按概率抽，整段放动态区；按有没有抽中切换位置的话，两头的缓存都会被打穿。
    """
    capabilities = platform.capabilities
    names = list(native_names) if capabilities.native_emoji else []
    entries = list(stickers) if capabilities.images is not None else []
    if not names and not entries:
        return None
    lines = ["【可用表情】", "在回复里写 [表情:名称] 就能发。"]
    if names:
        more = f"（另有 {native_hidden} 个未展示）" if native_hidden > 0 else ""
        lines.append(f"{native_label}（只能用这些名字）：{'、'.join(names)}{more}")
    if entries:
        lines.append("表情包，一行一张，括号里是它的标签：")
        # 一行一张：挤成一行靠顿号分隔的话，标签自带顿号或名字带括号时就分不清归属了。
        lines.extend(f"  {entry}" for entry in entries)
        lines.extend([
            "配不配表情包你自己定，不合适就别写 —— 每句话都配图很烦人。",
            "只想甩一张图不说话时，整条回复就写一个 [表情:名称]，别硬凑话。",
            # 名单外的词也能用：想表达的情绪未必对得上某张图的名字，但多半对得上它的某个标签。
            "名单里挑不出想要的，也可以直接写想表达的情绪，比如 [表情:无语]，会去标签里找。",
        ])
    return GuideSection(SECTION_EMOJI, "\n".join(lines), volatile=True)

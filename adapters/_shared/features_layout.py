"""全平台共用设置在控制台上的布局：全局页的分区，以及平台页拼自己分区时引用的片段。"""
from __future__ import annotations

from .console_schema import (
    SPACING_LOOSE,
    Block,
    Node,
    block,
    field,
    fixed,
    grid,
    loose,
    note,
    paragraph,
    section,
)

TRIGGER_CONDITIONS = block(
    "触发条件",
    grid(
        field("signal_all"),
        field("signal_at"),
        field("signal_reply"),
        field("signal_name", field("name_words"), field("name_anywhere", sub=True)),
        field("signal_prefix"),
        field("signal_keyword", field("keyword_words")),
        field("signal_random", field("random_probability")),
    ),
    loose(
        field("trigger_prefixes", spacing=SPACING_LOOSE),
        note("前缀同时用于剥离正文，但 `/` 与 `／` 例外 —— `/draw` 这类命令要求字面斜杠，剥掉会失配。"),
    ),
    hint="命中任意一条即回应",
)

LLM_INTENT = block(
    "接话意愿判断",
    grid(
        field("llm_intent_enabled", field("llm_intent_node_id"), field("llm_intent_context_messages")),
        fixed(
            "并发上限",
            paragraph("判定会占住 engine worker，占满了真实对话就得排队。"),
            field("llm_intent_max_inflight"),
            field("llm_intent_timeout_sec"),
        ),
    ),
    hint="前面全不命中时才问模型",
)

COOLDOWN = block(
    "冷却",
    grid(
        fixed(
            "发言间隔",
            paragraph("刚说过话的群里先歇一会儿。0 表示不限制。"),
            field("cooldown_group_sec"),
            field("cooldown_user_sec"),
        ),
        field("cooldown_exempt_at"),
    ),
    hint="命中之后仍可能不说话",
)

APPROVAL = block(
    "审批",
    grid(
        field(
            "approval_reply_unique_fallback",
            paragraph(
                "默认关：这条兜底分不清引用的是它、还是另一张已经处理过的卡片，是误批的直接成因。"
                "开了也仍然要求只剩一条待审批、且那张卡片确实发给了本人。"
            ),
        ),
        field(
            "approval_bare_verb_unique",
            paragraph(
                "默认开：收到卡片后直接回「同意」就能批。只在你名下恰好剩一条待审批、且那张卡片确实发给你时成立；"
                "有多条会让你指定，一条都没有时就当普通聊天。"
            ),
        ),
    ),
    hint="审批卡片怎么认回来",
)

REPLY_TO_TRIGGER = field("reply_to_trigger")
HISTORY_TEXT_LIMIT = field("history_text_limit")

PREEMPT = field("enable_preempt")
ECHO = field(
    "enable_echo",
    field("echo_threshold"),
    field("echo_max_length"),
    field("echo_window_sec"),
    field("echo_cooldown_sec"),
    paragraph(
        "必须是不同的人发的，同一个人连刷不算。命令、@、回复和长文本一律不跟；"
        "表情包按图片本身比对，三个人发三张不同的图不会被当成复读。"
    ),
)

IMAGE_INPUT = field(
    "enable_image_input",
    field("max_images_per_turn"),
    field("image_max_bytes"),
    field("image_wait_after_text_sec"),
    paragraph("有的平台会把「文字＋图」拆成两条发过来，收到纯文本后等这么久看有没有图跟上。"),
)
FILE_INPUT = field(
    "enable_file_input",
    field("max_files_per_turn"),
    field("file_max_bytes"),
    field("file_extra_allowed_extensions"),
    paragraph("可执行文件与脚本类型不受这里影响，填了也不会收。"),
)

# 平台可以在「发图」这一格后面接自己的项（比如表情重名时先查哪边），所以给的是一串节点而不是整格。
STICKER_PICKING: tuple[Node, ...] = (
    paragraph("清单按概率整份摊给模型，发不发、发哪张由它自己定。库比上限大就先给发得最少的。"),
    field("sticker_send_probability"),
    field("sticker_prompt_limit"),
    field("sticker_repeat_window_sec"),
)
STICKER_COMBAT = field("sticker_combat", field("sticker_burst_probability"))
STICKER_CANDIDATES = note(
    "只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。"
    "打标走「机器人 › 表情包图库」里显示的那条看图渠道，刚收进来的图要等一会儿才可用。"
)
STICKER_SENDING_HINT = "什么时候把表情包摊给模型挑"
STICKER_SENDING_PATHS: tuple[str, ...] = (
    "extensions.sticker.send_probability",
    "extensions.sticker.prompt_limit",
    "extensions.sticker.repeat_window_sec",
    "extensions.sticker.combat",
    "extensions.sticker.burst_probability",
)
STICKER_LIBRARY_PATHS: tuple[str, ...] = (
    "extensions.sticker.auto_accept",
    "extensions.sticker.auto_tag",
    "extensions.sticker.pending_limit",
    "extensions.sticker.library_limit",
    "extensions.sticker.pending_ttl_days",
)


def queue_block(*extra: Node) -> Block:
    """消息队列那一块。平台页在后面接运行期实况，全局页没有进程可看。"""
    return block(
        "消息队列",
        grid(
            field("enable_queue", field("queue_workers"), field("queue_interval")),
            field("queue_wait_for_reply", field("queue_reply_timeout")),
        ),
        *extra,
        hint="开着的时候群消息排队处理，不再并发",
    )


def history_block(*extra: Node) -> Block:
    return block(
        "群历史",
        grid(
            fixed(
                "缓存条数",
                paragraph("每个群留多少条最近消息。0 表示不缓存。"),
                field("group_history_max"),
            ),
            fixed(
                "未送达余量",
                paragraph("还没送到 Engine 的行可以多占几倍位置，硬上限 = 缓存条数 × 本值。1.0 表示不给余量，满了就丢。"),
                field("group_history_undelivered_ratio"),
            ),
        ),
        *extra,
        hint="给模型看的上下文，不是平台上的聊天记录",
    )


FEATURE_SECTIONS = (
    section(
        "trigger", "触发时机",
        TRIGGER_CONDITIONS, LLM_INTENT, COOLDOWN,
        icon="timer",
        note="所有平台默认的开口规则。平台页的「触发时机」可以单独改其中任意一项，试听也在那里。",
        paths=("trigger",),
    ),
    section(
        "approval", "审批",
        APPROVAL,
        icon="verified_user",
        note="审批卡片发出去之后，怎么认出回复的是哪一张。",
        paths=("approval",),
    ),
    section(
        "reply", "回复",
        block(
            "回复格式",
            grid(
                REPLY_TO_TRIGGER,
                fixed("历史长度", paragraph("群历史里每条消息压成一行，最多留这么多字，超出的截掉。"), HISTORY_TEXT_LIMIT),
            ),
            hint="消息发出去之前的处理",
        ),
        icon="format_align_left",
        note="回复时引不引用原消息，以及群历史里每条留多长。",
        paths=("output.reply_to_trigger", "output.history_text_limit"),
    ),
    section(
        "interaction", "群互动",
        block("群内互动", grid(PREEMPT, ECHO), hint="除了说话之外还能做什么"),
        note("这几项只在群里成立，私聊不受影响。"),
        icon="chat",
        note="跟复读、抢答这些说话之外的动作。",
        paths=("extensions.preempt", "extensions.echo"),
    ),
    section(
        "input", "输入处理",
        block("能接收什么", grid(IMAGE_INPUT, FILE_INPUT), hint="发来的东西里，哪些交给模型看"),
        icon="attach_file",
        note="发来的图片和文件哪些收、收多少。",
        paths=("input",),
    ),
    section(
        "sticker", "表情包挑选",
        block("发送", grid(fixed("发图", *STICKER_PICKING), STICKER_COMBAT), hint=STICKER_SENDING_HINT),
        STICKER_CANDIDATES,
        icon="mood",
        note="什么时候把表情包清单摊给模型、怎么挑。收不收、从哪收在各平台页里设。",
        paths=STICKER_SENDING_PATHS,
    ),
    section(
        "link", "队列与群历史",
        queue_block(), history_block(),
        icon="lan",
        note="消息排不排队、每个群缓存多少条历史。改完两秒内对齐，不用重启。",
        paths=("queue", "history"),
    ),
    section(
        "sticker_library", "表情包图库",
        block(
            "入库",
            grid(
                field("sticker_auto_accept"),
                field("sticker_auto_tag"),
                fixed(
                    "容量",
                    paragraph("待审池满了就不再收新的；图库超出上限时最旧的先出局。"),
                    field("sticker_pending_limit"),
                    field("sticker_library_limit"),
                    field("sticker_pending_ttl_days"),
                ),
            ),
            hint="收进来的图怎么进图库、存多少",
        ),
        icon="photo_library",
        note="全平台共用一份图库，这几项只在这里设，平台页里改不了。",
        paths=STICKER_LIBRARY_PATHS,
    ),
)

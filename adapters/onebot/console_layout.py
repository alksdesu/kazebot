"""QQ · NapCat 在控制台上的九个分区。不分平台的那几块引用共享布局，这里只拼 QQ 自己的部分。"""
from __future__ import annotations

from typing import Any

from adapters._shared.console_schema import (
    SPACING_PANEL,
    block,
    build_schema,
    custom,
    field,
    fixed,
    grid,
    note,
    panel,
    paragraph,
    section,
)
from adapters._shared.features_layout import (
    APPROVAL,
    COOLDOWN,
    ECHO,
    FILE_INPUT,
    HISTORY_TEXT_LIMIT,
    IMAGE_INPUT,
    LLM_INTENT,
    PREEMPT,
    REPLY_TO_TRIGGER,
    STICKER_CANDIDATES,
    STICKER_COMBAT,
    STICKER_PICKING,
    STICKER_SENDING_HINT,
    STICKER_SENDING_PATHS,
    TRIGGER_CONDITIONS,
    history_block,
    queue_block,
)
from .live_config import LIVE_KEYS

SECTIONS = (
    section(
        "account", "账号与连接",
        custom("workspace"),
        icon="account_circle",
        note="这一个实例用哪个号在说话，以及多开与记忆归属。换号会重启 NapCat，期间 bot 不可用。",
    ),
    section(
        "channels", "信道",
        block(
            "群",
            panel(field("allowed_groups", spacing=SPACING_PANEL), custom("groupWarning")),
            hint="不在名单里的群，消息一律不处理",
        ),
        block(
            "私聊",
            panel(field("allowed_private_users", spacing=SPACING_PANEL)),
            grid(field("allow_private_friends"), field("private_denied_reply")),
            hint="私聊按名单与好友关系两条独立放行",
        ),
        note(
            "备注只存在这台浏览器里，不写进平台配置、也不会进模型的上下文 —— 群名往往带真实身份。"
            "换个浏览器或清掉站点数据，备注就没了，名单本身不受影响。"
        ),
        icon="inbox",
        note="bot 只在名单里的群和私聊里出现。两份名单都是白名单，不在名单里的消息连处理都不会处理。",
        paths=("channels",),
    ),
    section(
        "trigger", "触发时机",
        TRIGGER_CONDITIONS,
        LLM_INTENT,
        COOLDOWN,
        block("试听", custom("audition"), hint="判定由 bot 那份代码算，不是界面自己猜的"),
        note(
            "真实判定由 bot 进程执行。改动要点「应用」写入配置文件之后才生效，顶部状态条会显示 bot"
            " 是否已经读到这一份。试听用的是编辑中的配置，所以它显示的是「应用之后会怎样」。"
        ),
        icon="timer",
        note="决定 bot 在群里什么时候说话。多个条件之间是「或」——命中任意一条就会回应，再由冷却决定是否真的开口。",
        paths=("trigger",),
    ),
    section(
        "permissions", "权限",
        block(
            "管理员",
            panel(field("admin_users", spacing=SPACING_PANEL), custom("adminWarning")),
            hint="名单里的 QQ 号在所有会话里都算管理员",
        ),
        block("这些人能做什么", custom("capabilities"), hint="每项各自决定放到哪一档"),
        APPROVAL,
        note(
            "群主与群管理员的身份由 QQ 上报，只在群消息里成立 —— 同一个人在私聊里就是普通用户。"
            "影响面超出单个群的那几项（改全局配置、借 bot 的身份发给任意人）只能给名单里的人，"
            "它们的档位里没有群主可选；标了「只在本群」的项给出去之后，目标会被夹在他自己那个群里。"
            "名单还兼着私聊放通和审批收件人，把能力调成「不开放」不会把人从名单里去掉。"
        ),
        icon="verified_user",
        note="谁能对 bot 下管理命令。管理员权限跨所有群和私聊生效，不区分场景。",
        paths=("permissions", "approval"),
    ),
    section(
        "input", "输入处理",
        block(
            "能接收什么",
            grid(
                IMAGE_INPUT,
                FILE_INPUT,
                field(
                    "enable_forward_msg_input",
                    field("forward_msg_max_depth"),
                    field("forward_msg_max_messages"),
                    field("forward_msg_text_limit"),
                    field("forward_msg_timeout_sec"),
                    field("enable_forward_msg_media", sub=True),
                    field("forward_msg_expand_for_trigger", sub=True),
                    paragraph("后一项关掉，转发消息就只有 @ 或回复才理。"),
                ),
            ),
            hint="群友发来的东西里，哪些交给模型看",
        ),
        note("看图走的是「渠道」页系统槽位里的读图渠道，没配模型时图片收得到也看不了。改完点顶部的「应用」。"),
        icon="attach_file",
        note="群友发来的图片、文件和合并转发，哪些收、收多少。",
        paths=("input",),
    ),
    section(
        "output", "输出格式",
        block(
            "回复格式",
            grid(
                REPLY_TO_TRIGGER,
                field("strip_asterisk_styles"),
                field("strip_underscore_styles"),
                fixed(
                    "长度上限",
                    paragraph("超长的回复会被截断，避免被 QQ 拒收。"),
                    field("message_limit"),
                    HISTORY_TEXT_LIMIT,
                ),
                field(
                    "enable_image_forward_merge",
                    field("image_forward_merge_threshold"),
                    paragraph(
                        "默认关是有原因的：合并转发会长时间占住那条唯一的反向 WS，期间发消息、贴表情全部排队。"
                        "发现卡顿就关掉，即时生效。"
                    ),
                ),
            ),
            hint="消息发出去之前的处理",
        ),
        block(
            "表态",
            grid(
                field("enable_reactions"),
                fixed("表情提示", paragraph("告诉模型有哪些收藏表情可用。0 表示不告诉它。"), field("face_prompt_limit")),
            ),
            hint="告诉模型有哪些收藏表情可用",
        ),
        note("这些是 QQ 这一侧的发送处理，改完点顶部的「应用」。bot 是谁、说话什么风格在「机器人 › 人格」里改。"),
        icon="format_align_left",
        note="消息发出去之前的处理：分条、去掉 Markdown 标记、长度上限、表态。",
        paths=("output", "extensions.reactions"),
    ),
    section(
        "interaction", "群互动",
        block(
            "群内互动",
            grid(field("enable_auto_like", field("auto_like_times")), PREEMPT, ECHO),
            hint="除了说话之外还能做什么",
        ),
        note("这几项只在群里成立，私聊不受影响。改完点顶部的「应用」。"),
        icon="chat",
        note="除了说话之外还能做什么：跟复读、抢答、给资料卡点赞。",
        # auto_like_times 和 auto_like 是同级键，不是它的子路径，得各列一条。
        paths=("extensions.echo", "extensions.preempt", "extensions.auto_like", "extensions.auto_like_times"),
    ),
    section(
        "sticker", "表情包",
        block(
            "收集",
            grid(
                field(
                    "sticker_collect",
                    paragraph("严格只收 QQ 标了表情包的，宽松再放行看着不像截图的，全收连群友随手发的照片都要。"),
                    field("sticker_strategy"),
                    field("sticker_groups"),
                ),
            ),
            hint="群里刷过的表情包攒进图库的规则",
        ),
        block(
            "发送",
            grid(
                fixed(
                    "发图",
                    *STICKER_PICKING,
                    paragraph("模型写的表情名同时对得上收藏表情和图库里的图时，先发哪一边的。"),
                    field("sticker_lookup_order"),
                ),
                STICKER_COMBAT,
            ),
            hint=STICKER_SENDING_HINT,
        ),
        STICKER_CANDIDATES,
        icon="mood",
        note="在 QQ 这一侧怎么收表情包、怎么发。图库本身在「机器人 › 表情包图库」里管。",
        paths=(
            "extensions.sticker.collect",
            "extensions.sticker.strategy",
            "extensions.sticker.groups",
            "extensions.sticker.lookup_order",
            *STICKER_SENDING_PATHS,
        ),
    ),
    section(
        "link", "链路",
        queue_block(custom("queueFacts")),
        history_block(custom("historyFacts")),
        block(
            "转发桥",
            grid(field("enable_forward_bridge", field("forward_bridge_max_messages"))),
            custom("bridgeFacts"),
            hint="本地 HTTP 口子，供别的进程借 bot 发消息",
        ),
        block("固定配置", custom("environment"), hint="改这些要重启进程，控制台只读"),
        block("危险操作", custom("restartEngine"), hint="唯一会中断正在进行的工作的操作"),
        note(
            "队列、群历史、转发桥这几项改完同样是热生效 —— bot 会在两秒内重建 worker、deque 与"
            "监听端口，不需要重启。上面那个重启按钮是给 engine worker 卡住时用的。"
        ),
        icon="lan",
        note="队列、群历史、转发桥这几个改完立刻重建运行期对象的参数，以及固定配置与危险操作。",
        paths=("queue", "history", "forward_bridge"),
    ),
)


def schema() -> dict[str, Any]:
    """控制台 schema。平台注册表的 console_schema 钩子指到这里，supervisor 不必等 bot 上报就能给出。"""
    return build_schema(SECTIONS, LIVE_KEYS)

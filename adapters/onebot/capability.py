"""QQ 管理能力的授权判定。

八个管理命令门共用这一份档位表：每项能力授予到哪一档、群主拿到之后管得着哪个群。
不 import NoneBot，规则可在没有平台依赖的环境下回归测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 授权档位，由窄到宽。值是「这项能力最宽给到谁」，不是「谁在用」。
OFF = "off"
LISTED = "admin"
OWNER = "owner"
GROUP_ADMIN = "group_admin"

GRANTS: tuple[str, ...] = (OFF, LISTED, OWNER, GROUP_ADMIN)

# 请求者身份档，越大权越高。
RANK_MEMBER = 0
RANK_GROUP_ADMIN = 1
RANK_OWNER = 2
RANK_LISTED = 3

# 档位 → 放行所需的最低身份档。off 用一个谁都够不着的数，名单里的人也一样被挡住 ——
# 「这项能力我不想要」是运营者的合法选择，留后门反而让开关不可信。
_MIN_RANK: Mapping[str, int] = {
    OFF: RANK_LISTED + 1,
    LISTED: RANK_LISTED,
    OWNER: RANK_OWNER,
    GROUP_ADMIN: RANK_GROUP_ADMIN,
}

_ROLE_RANK: Mapping[str, int] = {
    "owner": RANK_OWNER,
    "admin": RANK_GROUP_ADMIN,
}

CROSS_GROUP_DENIED = "你的权限只在本群有效，不能对其他群或私聊执行这个操作。"

# 「关掉」的其他写法。写 no 的意图明确，不认就等于把他想关的门按默认档打开；
# on / yes / true 相反 —— 对应哪一档没有答案，只能回落默认。
_OFF_WORDS = frozenset({"no", "false"})


@dataclass(frozen=True)
class Capability:
    """一项管理能力。key 同时是 qq.yaml 的键名和控制台的字段名。"""

    key: str
    name: str
    desc: str
    # 配置能开到的最宽档位。超出的值会被夹回来 —— 全局能力给了群主就等于给了所有群。
    widest: str
    # True = 群主/群管拿到之后只能作用于自己那个群，目标要另外夹（scope_deny_reason）。
    group_scoped: bool = False
    default: str = LISTED


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "approval", "审批",
        "工具调用需要人工放行时，私发给有这项权限的人。没有人有这项权限时一律自动拒绝。",
        widest=LISTED,
    ),
    Capability(
        "model", "切换模型",
        "查看和切换当前对话模型。改的是全局配置，所有会话一起变。",
        widest=LISTED,
    ),
    Capability(
        "draw_preset", "切换画师串",
        "改绘图预设。同样是全局的。",
        widest=LISTED,
    ),
    Capability(
        "custom_face", "表情包管理",
        "同步、命名、删除 bot 的收藏表情。改的是 bot 账号自己的表情库。",
        widest=LISTED,
    ),
    Capability(
        "cross_session", "跨会话投递",
        "借 bot 的身份往任意人或群发消息。没有这项权限的人只能把内容发回自己或本群。",
        widest=LISTED,
    ),
    Capability(
        "clear_memory", "清空群记忆",
        "清掉某个群的对话记忆。给到群主时只能清他自己那个群。",
        widest=GROUP_ADMIN, group_scoped=True,
    ),
    Capability(
        "dream", "整理记忆",
        "手动触发一次记忆整理：合并重复、清掉过期。整理的是全部会话和人物档案，不限本群，所以不给群主。",
        widest=LISTED,
    ),
    Capability(
        "proactive", "主动发送",
        "让 bot 主动发消息、发文件、发合并转发。给到群主时目标只能是他自己那个群。",
        widest=GROUP_ADMIN, group_scoped=True,
    ),
    Capability(
        "send_dead_letter", "发送死信",
        "查看和放行卡住的投递记录。清单里带真实群号/QQ 号，只能给管理员名单。",
        widest=LISTED,
    ),
)

BY_KEY: Mapping[str, Capability] = {cap.key: cap for cap in CAPABILITIES}


def clamp(capability_key: str, grant: Any) -> str:
    """把配置里的档位收进这项能力允许的范围。认不出的值回落默认档。"""
    cap = BY_KEY.get(capability_key)
    if cap is None:
        return LISTED
    # dry-run 的档位从 JSON 来，`false` 到这里仍然是真布尔，yaml 那侧的字符串路径管不到它。
    if grant is False:
        return OFF
    # `true` 没有对应档位，按这项能力的默认档处理，而不是当成「开到最宽」。
    if grant is True:
        return cap.default
    token = str(grant if grant is not None else "").strip().lower()
    if token in _OFF_WORDS:
        return OFF
    if token not in _MIN_RANK:
        return cap.default
    if _MIN_RANK[token] < _MIN_RANK[cap.widest]:
        return cap.widest
    return token


@dataclass(frozen=True)
class Requester:
    """一次命令请求的身份。group_role 只在群消息里有，私聊恒为空串。"""

    user_id: int | None
    listed_admin: bool
    group_id: int | None = None
    group_role: str = ""

    @property
    def rank(self) -> int:
        if self.listed_admin:
            return RANK_LISTED
        # 群角色只在群里成立：私聊没有「本群」，群主在私聊里就是个普通用户。
        if self.group_id is None:
            return RANK_MEMBER
        return _ROLE_RANK.get(str(self.group_role or "").strip().lower(), RANK_MEMBER)


def allows(capability_key: str, grant: Any, requester: Requester) -> bool:
    """身份档够不够这项能力的门槛。目标范围另判，见 scope_deny_reason。"""
    return requester.rank >= _MIN_RANK[clamp(capability_key, grant)]


def scope_deny_reason(capability_key: str, requester: Requester, target_group_id: Any) -> str:
    """作用域夹紧；空串表示允许。

    群主的权限只在自己群里成立。少了这一层，「把清空群记忆给群主」就变成了
    「让群主清掉 bot 待过的每一个群」—— 他只要在自己群里带上别人的群号。
    """
    if requester.listed_admin:
        return ""
    cap = BY_KEY.get(capability_key)
    if cap is None or not cap.group_scoped:
        return ""
    if requester.group_id is None:
        return CROSS_GROUP_DENIED
    try:
        target = int(target_group_id)
    except (TypeError, ValueError):
        return CROSS_GROUP_DENIED
    return "" if target == requester.group_id else CROSS_GROUP_DENIED


def may_see_target_roster(requester: Requester) -> bool:
    """能不能看整份目标清单。清单里是 bot 待过的每个群和能私聊的每个人，
    群主的权限只到自己那个群，这份清单本身就超出他该知道的范围。"""
    return requester.listed_admin


def catalog() -> list[dict[str, Any]]:
    """公布给控制台的能力清单。档位选项按这项能力实际允许的范围给，
    免得界面上摆出一个选了也会被夹回去的选项。"""
    return [
        {
            "key": cap.key,
            "name": cap.name,
            "desc": cap.desc,
            "default": cap.default,
            "group_scoped": cap.group_scoped,
            "grants": [g for g in GRANTS if _MIN_RANK[g] >= _MIN_RANK[cap.widest]],
        }
        for cap in CAPABILITIES
    ]

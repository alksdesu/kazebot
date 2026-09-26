"""QQ 行为快照的录制逻辑：会话键与记忆命名空间、别名、分条、表情解析顺序、权限判定、触发判定。

改造前后跑同一份录制、逐项比对；取函数和进程内状态只经 Layout，换代码布局时只换 Layout。
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Protocol

import pytest

from _onebot_harness import body_attr, load_runtime, set_live_config

GROUP = 700001
OTHER_GROUP = 700002
ADMIN = 10001
MEMBER = 30003
FRIEND = 30004
STRANGER = 40004
BOT = "10000"
OTHER_BOT = "20000"
EVENT_TIME = 1_700_000_000
HASH_SECRET = "5" * 64
ROLL_SALT = b"qq-behavior-snapshot"
CLOCK_START = 1_800_000_000.0
# 已部署实例的密钥文件里写的就是这串；照字面录，常量被改了快照才会报。
LEGACY_SECRET_MARKER = "legacy-unsalted"
FACES: tuple[dict[str, Any], ...] = (
    {"desc": "开心", "md5": "aaaaaaaa1111", "resId": "r1", "url": "http://faces.invalid/1"},
    {"desc": "无语", "md5": "bbbbbbbb2222", "resId": "r2", "url": "http://faces.invalid/2"},
)
MEMBERS: tuple[dict[str, Any], ...] = (
    {"user_id": ADMIN, "nickname": "管理员", "card": "群管家", "role": "admin"},
    {"user_id": MEMBER, "nickname": "测试用户", "card": "", "role": "member"},
    {"user_id": FRIEND, "nickname": "路人乙", "card": "乙", "role": "member"},
)
SECTIONS = ("conversation_keys", "aliases", "emoji_order", "reply_split", "permissions", "triggers")


class Layout(Protocol):
    runtime: Any

    def fn(self, name: str) -> Any: ...

    def alias_numbering(self) -> tuple[Callable[[str, int], str], Callable[[str, str], int]]: ...

    async def render_emojis(self, text: str, bot: Any, bqbs: list[str]) -> list[dict[str, Any]]: ...

    def bind_at_alias(self, resolver: Callable[[str], str]) -> None: ...

    def close(self) -> None: ...


class NapcatLayout:
    """napcat 子包布局：函数散在各模块里，进程内状态挂在 state._adapter 上。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def fn(self, name: str) -> Any:
        return body_attr(self.runtime, name)

    def alias_numbering(self) -> tuple[Callable[[str, int], str], Callable[[str, str], int]]:
        # 函数内导入：录制逻辑也要在改造前的树上跑，那边没有这个模块。
        from adapters._shared.aliases import alias_from_index, alias_to_index

        return alias_from_index, alias_to_index

    @property
    def _adapter(self) -> Any:
        return self.runtime.state._adapter

    async def render_emojis(self, text: str, bot: Any, bqbs: list[str]) -> list[dict[str, Any]]:
        return await self._adapter.emoji.process_emojis(text, bot, bqbs)

    def bind_at_alias(self, resolver: Callable[[str], str]) -> None:
        self._adapter.emoji.at_alias_resolver = resolver

    def close(self) -> None:
        self._adapter.outbound_idempotency.close()
        if self._adapter.sticker_store is not None:
            self._adapter.sticker_store.close()


LayoutFactory = Callable[[Any], Layout]


class RecordingBot:
    """只记不发的 NapCat：收藏表情和群成员按固定数据回，其余接口记下调用后回空。"""

    def __init__(self, self_id: str = BOT) -> None:
        self.self_id = self_id
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append(api)
        if api == "fetch_custom_face_detail":
            return [dict(face) for face in FACES]
        if api == "get_group_member_list":
            return [dict(member) for member in MEMBERS]
        return {}

    async def send_group_msg(self, *, group_id: Any, message: Any) -> dict[str, str]:
        return self._record("group", group_id, message)

    async def send_private_msg(self, *, user_id: Any, message: Any) -> dict[str, str]:
        return self._record("private", user_id, message)

    def _record(self, kind: str, target: Any, message: Any) -> dict[str, str]:
        self.sent.append({"kind": kind, "target": target, "segments": plain(list(message))})
        return {"message_id": str(900 + len(self.sent))}


class _Clock:
    """冷却按秒计，判定读的 time.time() 换成手动拨的钟，reason 里的剩余秒数才可复现。"""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def plain(value: Any) -> Any:
    """转成 JSON 往返后不变的形状，比对才能逐项对上。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return plain(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(item) for item in value)
    return value


def outcome(call: Callable[[], Any]) -> Any:
    """抛异常也是行为：记下异常类型，而不是让整段录制中断。"""
    try:
        return plain(call())
    except Exception as error:
        return {"raises": type(error).__name__}


def _v11() -> Any:
    return sys.modules["nonebot.adapters.onebot.v11"]


def group_event(
    message_id: int,
    text: str,
    *,
    user_id: Any = MEMBER,
    group_id: Any = GROUP,
    role: str = "member",
    nickname: str = "测试用户",
    card: str = "",
    at_bot: bool = False,
    at_users: Iterable[Any] = (),
    to_me: bool = False,
    reply_to_bot: bool = False,
    anonymous: Mapping[str, Any] | None = None,
) -> SimpleNamespace:
    segment = _v11().MessageSegment
    segments = [segment.at(BOT)] if at_bot else []
    segments.extend(segment.at(str(uid)) for uid in at_users)
    if text:
        segments.append(segment.text(text))
    message = _v11().Message(segments)
    reply = (
        SimpleNamespace(message_id=message_id - 1, sender=SimpleNamespace(user_id=int(BOT)))
        if reply_to_bot else None
    )
    return SimpleNamespace(
        group_id=group_id, user_id=user_id, message_id=message_id, time=EVENT_TIME, self_id=int(BOT),
        message_type="group", sub_type="anonymous" if anonymous else "normal",
        raw_message=text, to_me=to_me, reply=reply, anonymous=dict(anonymous) if anonymous else None,
        sender=SimpleNamespace(user_id=user_id, nickname=nickname, card=card, role=role),
        get_message=lambda: message,
    )


def private_event(message_id: int, text: str, *, user_id: Any = MEMBER, sub_type: str = "friend") -> SimpleNamespace:
    message = _v11().Message([_v11().MessageSegment.text(text)] if text else [])
    return SimpleNamespace(
        user_id=user_id, message_id=message_id, time=EVENT_TIME, self_id=int(BOT),
        message_type="private", sub_type=sub_type, raw_message=text, to_me=True, reply=None, anonymous=None,
        sender=SimpleNamespace(user_id=user_id, nickname="私聊用户", card="", role=""),
        get_message=lambda: message,
    )


def open_runtime(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, layout_factory: LayoutFactory, *, salted: bool = True,
) -> Layout:
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", raising=False)
    if salted:
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", HASH_SECRET)
    else:
        monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET", raising=False)
        secret_file = workspace / "data" / "onebot_conversation_hash_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(LEGACY_SECRET_MARKER, encoding="utf-8")
    return layout_factory(load_runtime(monkeypatch, workspace))


def record_section(
    section: str, monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory,
) -> dict[str, Any]:
    recorders: dict[str, Callable[[pytest.MonkeyPatch, Path, LayoutFactory], dict[str, Any]]] = {
        "conversation_keys": record_conversation_keys,
        "aliases": record_aliases,
        "emoji_order": record_emoji_order,
        "reply_split": record_reply_split,
        "permissions": record_permissions,
        "triggers": record_triggers,
    }
    return plain(recorders[section](monkeypatch, root, layout_factory))


def record_conversation_keys(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mode, salted in (("salted", True), ("legacy", False)):
        layout = open_runtime(monkeypatch, root / f"keys-{mode}", layout_factory, salted=salted)
        try:
            result[mode] = _keys_per_scope(layout)
        finally:
            layout.close()
    return result


def _keys_per_scope(layout: Layout) -> dict[str, Any]:
    fn = layout.fn
    rows: dict[str, Any] = {}
    # 未登录的空作用域必须先录：一旦认过账号就回不去了。
    for scope in ("", BOT, OTHER_BOT):
        if scope:
            fn("_adopt_bot_scope")(SimpleNamespace(self_id=scope))
        bot = SimpleNamespace(self_id=scope or None)
        row: dict[str, Any] = {}
        for real in (f"qq_group:{GROUP}", f"qq_private:{MEMBER}", f"qq_group:{OTHER_GROUP}"):
            stable = fn("_stable_conversation_key")(real)
            row[real] = {"stable": stable, "real": fn("_real_conversation_key")(stable)}
        row["memory_namespace"] = fn("_conversation_memory_namespace_for_group")(GROUP)
        row["sticker_group"] = fn("_sticker_conversation_key")({"type": "group", "group_id": GROUP}, bot)
        row["sticker_private"] = fn("_sticker_conversation_key")({"type": "private", "user_id": MEMBER}, bot)
        rows[scope or "unscoped"] = row
    return rows


_ANONYMIZE_TEXTS = (
    f"{MEMBER} 在群 {GROUP} 里 @{ADMIN}，还有陌生号 1234567 和 {MEMBER}0",
    f"[at:{FRIEND}] 回复 {OTHER_GROUP}",
    "没有号码的一句话",
)
_ALIAS_INDEXES = (0, 1, 25, 26, 27, 51, 52, 701, 702, 18277, 18278)
_AT_TOKENS = ("UserA", "userb", "@UserC", "UserZZ", str(MEMBER), "测试用户", "all")


def record_aliases(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    layout = open_runtime(monkeypatch, root / "aliases", layout_factory)
    fn = layout.fn
    try:
        users = [MEMBER, FRIEND, ADMIN, MEMBER, STRANGER, str(FRIEND), " 30005 "]
        groups = [GROUP, OTHER_GROUP, GROUP, str(OTHER_GROUP)]
        alias_from_index, alias_to_index = layout.alias_numbering()
        index_to_alias = {str(index): alias_from_index("User", index) for index in _ALIAS_INDEXES}
        result: dict[str, Any] = {
            "users": [outcome(lambda u=u: fn("_anonymize_user_id")(u)) for u in users],
            "groups": [outcome(lambda g=g: fn("_anonymize_group_id")(g)) for g in groups],
            "index_to_alias": index_to_alias,
            "alias_to_index": {alias: alias_to_index("User", alias) for alias in index_to_alias.values()},
            "anonymized_text": [fn("_anonymize_text_for_ai")(text) for text in _ANONYMIZE_TEXTS],
            "at_resolution": {token: outcome(lambda t=token: fn("_resolve_at_alias_to_real")(t)) for token in _AT_TOKENS},
        }
        first_anon = group_event(501, "匿名一", anonymous={"id": 11, "name": "匿名甲", "flag": "f1"})
        second_anon = group_event(502, "匿名二", anonymous={"id": 12, "name": "匿名乙"})
        carded = group_event(503, "有名片", user_id=FRIEND, nickname="路人乙", card="乙")
        result["event_aliases"] = [
            fn("_event_user_alias")(event) for event in (first_anon, second_anon, first_anon, carded)
        ]
        stable_group = fn("_stable_conversation_key")(f"qq_group:{GROUP}")
        stable_private = fn("_stable_conversation_key")(f"qq_private:{MEMBER}")
        subject_cases = (
            ("direct_group", group_event(511, "UserA 你还记得测试用户吗", user_id=FRIEND, at_users=(MEMBER,)),
             "UserA 你还记得测试用户吗", stable_group, True),
            ("private", private_event(512, "记得我吗"), "记得我吗", stable_private, True),
            ("indirect", group_event(513, "路过", user_id=STRANGER), "路过", stable_group, False),
            ("sticky_followup", group_event(514, "再说一句", user_id=FRIEND), "再说一句", stable_group, True),
        )
        result["memory_subjects"] = {
            case_id: outcome(lambda e=event, t=text, k=key, d=direct: fn("_collect_memory_subjects")(e, t, k, d))
            for case_id, event, text, key, direct in subject_cases
        }
        return result
    finally:
        layout.close()


_EMOJI_CASES = (
    ("mixed", "开头[表情:开心]中间[at:UserA]、[at:30003,UserB]结尾[CQ:at,qq=10001]"),
    ("faces", "[表情:无语][表情:不存在][emoji:开心][QQ_EMOJI:1][收藏表情: aaaaaaaa ]"),
    ("at_all", "[at:all] [at:全体成员] [at:@UserA] [at:谁也不是]"),
    ("markdown", "**[表情:开心]** `代码` [链接](https://example.invalid) ## 标题 _保留_"),
    ("platform_markers", "[REACT:66]表态<:x:123>[QQ表情:微笑]剩下的字"),
    ("empty", ""),
)


def record_emoji_order(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    layout = open_runtime(monkeypatch, root / "emoji", layout_factory)
    fn = layout.fn
    try:
        for user_id in (MEMBER, FRIEND):
            fn("_anonymize_user_id")(user_id)
        layout.bind_at_alias(fn("_resolve_at_alias_to_real"))
        bot = RecordingBot()
        result: dict[str, Any] = {
            case_id: asyncio.run(layout.render_emojis(text, bot, [])) for case_id, text in _EMOJI_CASES
        }
        result["api_calls"] = list(bot.calls)
        return result
    finally:
        layout.close()


_SPLIT_CASES = (
    ("split_marker", "group", "第一段[SPLIT]第二段[SPLIT]  [SPLIT]第三段"),
    ("newlines", "group", "普通换行\n保留排版\n\n段落二"),
    ("markdown", "private", "**粗体** *斜体* `代码` [链接](https://example.invalid)\n# 标题"),
    ("long", "group", "长" * 4400),
    ("mentions", "group", "[at:UserA] 你好[SPLIT][at:30003,UserB]、[CQ:at,qq=10001] 都来"),
    ("faces", "group", "看这个[表情:开心][SPLIT]再来一个[表情:不存在]"),
    ("platform_markers", "private", "[REACT:66]带表态<:x:123>[QQ表情:微笑]"),
    ("empty", "group", ""),
    ("only_split", "private", "[SPLIT][SPLIT]"),
)


def _split_target(kind: str) -> dict[str, Any]:
    if kind == "group":
        return {
            "type": "group", "group_id": GROUP, "conversation_key": f"qq_group:{GROUP}",
            "reply_message_id": 99, "reply_sender_id": MEMBER,
        }
    return {"type": "private", "user_id": MEMBER, "conversation_key": f"qq_private:{MEMBER}", "reply_message_id": 98}


def record_reply_split(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    layout = open_runtime(monkeypatch, root / "split", layout_factory)
    fn = layout.fn
    try:
        set_live_config(layout.runtime, reply_to_trigger=True)
        for user_id in (MEMBER, FRIEND):
            fn("_anonymize_user_id")(user_id)
        layout.bind_at_alias(fn("_resolve_at_alias_to_real"))
        result: dict[str, Any] = {}
        for case_id, kind, text in _SPLIT_CASES:
            bot = RecordingBot()
            sent_any = asyncio.run(fn("_send_split_text")(bot, _split_target(kind), text))
            result[case_id] = {"sent_any": sent_any, "messages": bot.sent, "api_calls": bot.calls}
        return result
    finally:
        layout.close()


def _requester_events() -> dict[str, SimpleNamespace]:
    return {
        "listed_admin_in_group": group_event(601, "a", user_id=ADMIN, nickname="管理员"),
        "owner": group_event(602, "b", user_id=MEMBER, role="owner"),
        "group_admin": group_event(603, "c", user_id=FRIEND, role="admin"),
        "member": group_event(604, "d", user_id=STRANGER),
        "anonymous_owner": group_event(605, "e", user_id=MEMBER, role="owner", anonymous={"id": 21, "name": "匿名丙"}),
        "other_group_owner": group_event(606, "f", user_id=MEMBER, role="owner", group_id=OTHER_GROUP),
        "private_admin": private_event(607, "g", user_id=ADMIN),
        "private_member": private_event(608, "h", user_id=MEMBER),
        "unparsable_user": group_event(609, "i", user_id="abc"),
    }


def record_permissions(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    layout = open_runtime(monkeypatch, root / "permissions", layout_factory)
    fn = layout.fn
    # 取判定函数实际引用的那一份，不假设能力模块落在哪个包里。
    capability = fn("_can").__globals__["capability"]
    try:
        set_live_config(
            layout.runtime,
            admin_users=frozenset({ADMIN}),
            allowed_groups=frozenset({GROUP}),
            allowed_private_users=frozenset({FRIEND}),
            allow_private_friends=True,
            grant_clear_memory=capability.GROUP_ADMIN,
            grant_proactive=capability.OWNER,
            grant_dream=capability.OFF,
            grant_model=capability.LISTED,
        )
        keys = sorted(capability.BY_KEY)
        requesters: dict[str, Any] = {}
        for case_id, event in _requester_events().items():
            requester = fn("_requester")(event)
            requesters[case_id] = {
                "rank": requester.rank,
                "listed_admin": requester.listed_admin,
                "group_role": requester.group_role,
                "can": {key: fn("_can")(key, event) for key in keys},
                "denial": {key: fn("_capability_denial")(key, event, "fallback") for key in keys},
                "scope": {
                    key: {
                        str(target): capability.scope_deny_reason(key, requester, target)
                        for target in (GROUP, OTHER_GROUP, None)
                    }
                    for key in keys if capability.BY_KEY[key].group_scoped
                },
            }
        grants = (capability.OFF, capability.LISTED, capability.OWNER, capability.GROUP_ADMIN, True, False, "no", "bogus", None)
        events = _requester_events()
        grant_table = {
            key: {
                str(grant): {
                    "clamped": capability.clamp(key, grant),
                    "allows": {
                        case_id: capability.allows(key, grant, fn("_requester")(event))
                        for case_id, event in events.items()
                    },
                }
                for grant in grants
            }
            for key in ("clear_memory", "model")
        }
        channels = {
            "group_allowed": {
                repr(group_id): outcome(lambda g=group_id: fn("_is_group_allowed")(g))
                for group_id in (GROUP, OTHER_GROUP, str(GROUP), 0)
            },
            "admin_user": {
                repr(user_id): outcome(lambda u=user_id: fn("_is_admin_user")(u))
                for user_id in (ADMIN, str(ADMIN), MEMBER, None, "abc")
            },
            "private_allowed": {
                f"{user_id!r}/{sub_type}": outcome(
                    lambda u=user_id, s=sub_type: fn("_is_private_allowed")(private_event(700, "x", user_id=u, sub_type=s))
                )
                for user_id, sub_type in (
                    (ADMIN, "group"), (FRIEND, "group"), (MEMBER, "friend"), (STRANGER, "group"), (None, "friend"), ("abc", "friend"),
                )
            },
            "private_origin_allowed": {
                repr(user_id): outcome(lambda u=user_id: fn("_is_private_origin_allowed")(u))
                for user_id in (ADMIN, FRIEND, MEMBER, STRANGER)
            },
        }
        set_live_config(layout.runtime, allow_private_friends=False)
        channels["private_allowed_friends_off"] = {
            f"{user_id!r}/{sub_type}": outcome(
                lambda u=user_id, s=sub_type: fn("_is_private_allowed")(private_event(701, "y", user_id=u, sub_type=s))
            )
            for user_id, sub_type in ((MEMBER, "friend"), (FRIEND, "friend"), (ADMIN, "friend"))
        }
        return {"requesters": requesters, "grants": grant_table, "channels": channels}
    finally:
        layout.close()


@dataclasses.dataclass(frozen=True)
class TriggerCase:
    case_id: str
    text: str
    event: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    advance_sec: float = 0.0


@dataclasses.dataclass(frozen=True)
class TriggerScenario:
    scenario_id: str
    live: Mapping[str, Any]
    cases: tuple[TriggerCase, ...]


_NAMED = {
    "signal_name": True, "name_words": ["咪啪", "小咪"], "name_anywhere": True,
    "signal_prefix": True, "trigger_prefixes": ["/问", "#"],
    "signal_keyword": True, "keyword_words": ["天气", "下雨"],
}
_ANON = {"id": 31, "name": "匿名丁"}
# 白名单为空时一律不放行，rule 层要先放进这两个群才看得到触发与冷却。
_TRIGGER_BASE: dict[str, Any] = {"allowed_groups": frozenset({GROUP, OTHER_GROUP})}
_TRIGGER_SCENARIOS: tuple[TriggerScenario, ...] = (
    TriggerScenario("default", {}, (
        TriggerCase("plain", "在吗"),
        TriggerCase("at_segment", "帮我看看", {"at_bot": True}),
        TriggerCase("at_stripped_to_me", "帮我看看呀", {"to_me": True}),
        TriggerCase("reply_bot", "接着说", {"reply_to_bot": True}),
        TriggerCase("reply_and_to_me", "接着说呀", {"reply_to_bot": True, "to_me": True}),
        TriggerCase("default_prefix", "/问 今天"),
        TriggerCase("name_off", "咪啪在吗"),
        TriggerCase("other_group_at", "别的群", {"group_id": OTHER_GROUP, "at_bot": True}),
        TriggerCase("anonymous_at", "匿名来问", {"at_bot": True, "anonymous": _ANON}),
    )),
    TriggerScenario("named", _NAMED, (
        TriggerCase("name_start", "咪啪在吗"),
        TriggerCase("name_middle", "我觉得咪啪很可爱"),
        TriggerCase("alt_name", "小咪，出来"),
        TriggerCase("prefix", "/问 今天几号"),
        TriggerCase("prefix_hash", "#查一下"),
        TriggerCase("prefix_leading_space", " /问 带空格"),
        TriggerCase("keyword", "明天天气如何"),
        TriggerCase("keyword_other", "外面下雨了"),
        TriggerCase("nothing", "无关的话"),
    )),
    TriggerScenario("name_at_start_only", {"signal_name": True, "name_words": ["咪啪"], "name_anywhere": False}, (
        TriggerCase("middle", "我觉得咪啪很可爱"),
        TriggerCase("comma", "咪啪，你好"),
        TriggerCase("glued", "咪啪你好"),
    )),
    TriggerScenario("all_signal", {"signal_all": True}, (
        TriggerCase("plain", "随便一句"),
        TriggerCase("empty", ""),
        TriggerCase("at", "@一下", {"at_bot": True}),
    )),
    TriggerScenario("legacy_all", {"group_trigger": "all"}, (
        TriggerCase("plain", "随便一句"),
    )),
    TriggerScenario("legacy_prefix", {"group_trigger": "prefix"}, (
        TriggerCase("bang", "!你好"),
        TriggerCase("plain", "你好"),
        TriggerCase("fullwidth_slash", "／问"),
    )),
    TriggerScenario("at_off", {"signal_at": False, "signal_reply": True}, (
        TriggerCase("at_segment", "at 关了", {"at_bot": True}),
        TriggerCase("to_me", "to_me 关了", {"to_me": True}),
        TriggerCase("reply", "回复还开着", {"reply_to_bot": True}),
    )),
    TriggerScenario("random", {"signal_random": True, "random_probability": 0.5}, tuple(
        TriggerCase(f"roll_{index}", f"随机第{index}句") for index in range(12)
    )),
    TriggerScenario("group_cooldown", {
        "signal_keyword": True, "keyword_words": ["天气"],
        "cooldown_group_sec": 600, "cooldown_user_sec": 0, "cooldown_exempt_at": True,
    }, (
        TriggerCase("first", "天气1"),
        TriggerCase("other_user_blocked", "天气2", {"user_id": FRIEND}, advance_sec=1),
        TriggerCase("at_exempt", "天气3", {"at_bot": True}, advance_sec=1),
        TriggerCase("other_group", "天气4", {"group_id": OTHER_GROUP}, advance_sec=1),
        TriggerCase("after_window", "天气5", advance_sec=600),
    )),
    TriggerScenario("user_cooldown", {
        "signal_keyword": True, "keyword_words": ["天气"],
        "cooldown_group_sec": 0, "cooldown_user_sec": 300, "cooldown_exempt_at": False,
    }, (
        TriggerCase("first", "天气1"),
        TriggerCase("same_user_blocked", "天气2", advance_sec=10),
        TriggerCase("other_user", "天气3", {"user_id": FRIEND}),
        TriggerCase("at_not_exempt", "天气4", {"at_bot": True}, advance_sec=10),
        TriggerCase("after_window", "天气5", advance_sec=300),
    )),
    TriggerScenario("llm_intent", {"llm_intent_enabled": True, "cooldown_group_sec": 30}, (
        TriggerCase("undetermined", "无关的话"),
        TriggerCase("at_records_cooldown", "at 一下", {"at_bot": True}),
        TriggerCase("intent_in_cooldown", "无关的话2", advance_sec=1),
        TriggerCase("intent_after_window", "无关的话3", advance_sec=30),
    )),
    TriggerScenario("allowlist", {"allowed_groups": frozenset({GROUP})}, (
        TriggerCase("home_group", "本群", {"at_bot": True}),
        TriggerCase("other_group", "外群", {"group_id": OTHER_GROUP, "at_bot": True}),
    )),
)


def record_triggers(monkeypatch: pytest.MonkeyPatch, root: Path, layout_factory: LayoutFactory) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scenario in _TRIGGER_SCENARIOS:
        layout = open_runtime(monkeypatch, root / f"trigger-{scenario.scenario_id}", layout_factory)
        try:
            result[scenario.scenario_id] = _run_trigger_scenario(monkeypatch, layout, scenario)
        finally:
            layout.close()
    return result


def _run_trigger_scenario(monkeypatch: pytest.MonkeyPatch, layout: Layout, scenario: TriggerScenario) -> dict[str, Any]:
    fn = layout.fn
    set_live_config(layout.runtime, **{**_TRIGGER_BASE, **scenario.live})
    clock = _Clock(CLOCK_START)
    monkeypatch.setitem(fn("_trigger_input").__globals__, "time", clock)
    monkeypatch.setitem(fn("_trigger_roll").__globals__, "_TRIGGER_ROLL_SALT", ROLL_SALT)
    bot = RecordingBot()
    rows: dict[str, Any] = {}
    for index, case in enumerate(scenario.cases):
        clock.now += case.advance_sec
        event = group_event(1000 + index, case.text, **case.event)
        rows[case.case_id] = asyncio.run(_judge(fn, bot, event))
    return rows


async def _judge(fn: Callable[[str], Any], bot: RecordingBot, event: SimpleNamespace) -> dict[str, Any]:
    trigger_text = await fn("_group_trigger_text")(bot, event)
    # 先单独判一次再走 rule：rule 触发后会记冷却，顺序反过来就判不到冷却前的结论。
    decision = fn("_group_trigger_decision_once")(event, bot, trigger_text)
    explicit = await fn("_agent_group_rule")(bot, event)
    intent = None if explicit else await fn("_intent_group_rule")(bot, event)
    return {
        "text": trigger_text,
        "stripped": fn("_strip_trigger_prefix")(trigger_text),
        "decision": decision.as_dict(),
        "rule": explicit,
        "intent_rule": intent,
        "roll": round(fn("_trigger_roll")(event), 6),
    }

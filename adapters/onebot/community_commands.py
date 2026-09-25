from __future__ import annotations

from clonoth_sdk.command_text import canonicalize_quiet_command as canonicalize

COMMANDS = frozenset({
    "/群协作帮助", "/群规", "/群资料", "/常见问题", "/旁听", "/安静", "/恢复", "/安静状态",
    "/活动列表", "/投票列表", "/投票创建", "/活动创建", "/报名", "/退出活动", "/确认", "/名单",
    "/投票", "/活动提醒", "/结束活动", "/取消活动",
})
CONTROL_COMMANDS = frozenset({"/旁听", "/安静", "/恢复", "/安静状态"})


async def handle(client, actor, text, reply_ref=""):
    text = canonicalize(text)
    head = text.strip().replace("／", "/", 1).split(maxsplit=1)
    if not head or head[0] not in COMMANDS:
        return None
    result = await client.request_feature("POST", "/v1/community/commands", actor=actor,
                                          body={"text": text, "reply_ref": reply_ref, "display_name": actor.get("display_name", "")})
    return result.get("result")

from __future__ import annotations

from typing import Any

from engine.todo_list import latest_todos_from_messages, reminder_text, unfinished_items

from .result import hook_result

PLUGIN_META = {
    "handler_class": "TodoReminder",
    "hook_points": [
        ("before_step", "handle"),
    ],
    # 排在抢占注入和压缩之后：提醒要贴在这一步消息的最末尾
    "priority": 10,
}

TODO_REMINDER_KEY = "_todo_reminder"


class TodoReminder:
    """启用了 update_todos 的节点：每步推理前维护一条待办提醒，换掉上一条，清单做完了就撤掉。

    任务挂起再恢复时 loop state 是新的，第一步先从本任务最近一次成功的 update_todos 调用重建清单。
    """

    name = "todo_reminder"
    priority = 10

    async def handle(self, ctx: Any) -> Any | None:
        ls = ctx.extra.get("loop_state")
        if ls is None or not getattr(ls, "todo_enabled", False):
            return None
        if not ls.todos_restored:
            ls.todos_restored = True
            if not ls.todos:
                restored = latest_todos_from_messages(ls.messages, str(getattr(ls.rctx, "task_id", "") or ""))
                if restored:
                    ls.todos = restored
        kept = [message for message in ls.messages if not message.get(TODO_REMINDER_KEY)]
        changed = len(kept) != len(ls.messages)
        if unfinished_items(ls.todos):
            kept.append({
                "role": "user",
                "content": reminder_text(ls.todos),
                "_dynamic": True,
                TODO_REMINDER_KEY: True,
            })
            changed = True
        if not changed:
            return None
        # 原地换内容：HookContext.messages 和别的 handler 手里拿的是同一个列表
        ls.messages[:] = kept
        return hook_result(modified=True)

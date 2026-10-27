"""隔离副本在任务 input 和入站消息里用的键。engine 也要读的几个定义在 engine.project_root。"""
from __future__ import annotations

from engine.project_root import PROJECT_ORIGIN_KEY, PROJECT_ROOT_KEY, WORKTREE_KEY

# 收尾时要算 diff 的任务：建副本的那个子任务，或接着用这个副本的任务
WORKTREE_OWNER_KEY = "_worktree_owner"
# 副本还在建：值是建它要用的说明；建好或建失败后置成 None。带着它的任务不会被领走
WORKTREE_PENDING_KEY = "_worktree_pending"
# 收尾时算出的副本结果，交回父节点用；不进瘦身快照，重启后路由重做时现算
WORKTREE_OUTCOME_KEY = "_worktree_outcome"
# 异步派出的子任务记下派它的任务在哪个副本里：结果回到派发方的会话时接着在那个副本里做
CALLER_WORKTREE_KEY = "_caller_worktree"
# 入站消息顶层的内部键，HTTP 入站带不进来：新建的入口任务接着哪个副本做，值是 {"worktree": 登记号, "owner": 是否主人}
WORKTREE_FROM_KEY = "_worktree_from"
# 异步结果回调的入站消息里带的副本结果
CHILD_WORKTREE_PAYLOAD_KEY = "child_worktree"
# 收尾算 diff 的路由线程已经起过了
OUTCOME_WORKER_KEY = "_worktree_outcome_worker_started"

# 副本建好之后才写上、重启回放还要用的键：task_store 瘦身快照时保留
SNAPSHOT_INPUT_KEYS = (PROJECT_ROOT_KEY, PROJECT_ORIGIN_KEY, WORKTREE_KEY, WORKTREE_OWNER_KEY, WORKTREE_PENDING_KEY)

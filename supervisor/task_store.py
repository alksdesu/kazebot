"""Task 生命周期 mixin —— 创建、领取、完成、取消。"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from typing import Any

from clonoth_runtime import get_str, load_runtime_config

from ._helpers import _now
from .types import RouteStatus, Task, TaskKind, TaskStatus


logger = logging.getLogger(__name__)


class TaskStoreMixin:
    """提供 Task 创建 / 领取 / 完成 / 取消等方法。

    运行时 self 是 SupervisorState 实例。
    """

    # ---- 基础工具 ----

    def _default_entry_node(self) -> str:
        cfg = load_runtime_config(self.workspace_root)
        return get_str(cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip() or "bootstrap.shell_orchestrator"

    def _entry_node_pinned(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("entry_node_pinned")) and bool(str(payload.get("entry_node_id") or "").strip())

    def _node_yaml(self, node_id: str) -> dict[str, Any]:
        """按 id 找节点定义。文件名不一定等于 id，engine 内建目录优先。"""
        import yaml

        target = (node_id or "").strip()
        if not target:
            return {}
        for nodes_dir in (
            self.workspace_root / "engine" / "system_nodes",
            self.workspace_root / "config" / "nodes",
        ):
            if not nodes_dir.exists():
                continue
            for path in sorted(nodes_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if isinstance(data, dict) and str(data.get("id") or path.stem) == target:
                    return data
        return {}

    def _deref_env(self, value: Any) -> str:
        text = str(value or "").strip()
        for head in ("$ENV{", "${"):
            if text.startswith(head) and text.endswith("}") and len(text) > len(head) + 1:
                return os.environ.get(text[len(head):-1].strip(), "").strip()
        return text

    def _vision_node_is_configured(self, node_id: str) -> bool:
        """视觉入口节点有没有自己的模型或地址。

        没有就别绕：engine/model.py 里空值一律回落全局，这个节点解析出来就是主渠道那个
        看不了图的模型，只是顺带把工具和委派也砍光了 —— 比不绕更糟。
        读不到节点定义时按「配了」算，维持原来的绕。
        """
        node = self._node_yaml(node_id)
        if not node:
            return True
        return bool(self._deref_env(node.get("model")) or self._deref_env(node.get("base_url")))

    def _warn_vision_node_unconfigured(self, node_id: str) -> None:
        seen = getattr(self, "_vision_unconfigured_warned", None)
        if seen is None:
            seen = set()
            self._vision_unconfigured_warned = seen
        if node_id in seen:
            return
        seen.add(node_id)
        logger.warning(
            "vision routing: %s has no model/base_url of its own, keeping images on the main channel. "
            "Set QQ_VISION_MODEL / QQ_VISION_API_KEY / QQ_VISION_BASE_URL to make the detour meaningful.",
            node_id,
        )

    def _vision_routing_applies(self, setting: Any, node_id: str = "") -> bool:
        """带图消息要不要绕去视觉入口节点。

        auto：主渠道自己收得下图就别绕 —— 绕过去等于把工具和委派全砍掉，
        换来的还常常是同一个模型。读不到配置时按绕算，那是这里的历史行为。
        显式 true 不看节点配没配：那是「把图挡在工具外面」，隔离本身就是目的。
        """
        if isinstance(setting, bool):
            return setting
        mode = str(setting or "").strip().lower()
        if mode in {"false", "0", "no", "off"}:
            return False
        if mode in {"true", "1", "yes", "on"}:
            return True
        store = getattr(self, "config_store", None)
        if store is None:
            return True
        try:
            if store.supports_vision():
                return False
        except Exception:
            logger.warning("vision routing: cannot read provider config, routing as before", exc_info=True)
            return True
        if node_id and not self._vision_node_is_configured(node_id):
            self._warn_vision_node_unconfigured(node_id)
            return False
        return True

    def _route_entry_node_for_capabilities(
        self,
        *,
        cfg: dict[str, Any],
        payload: dict[str, Any],
        selected_entry_node: str,
        default_node: str,
        session_override: str,
    ) -> str:
        """Route multimodal inputs to a configured vision-capable entry node."""
        # A pinned entry node comes from an explicit user command, so image capability routing must not move it.
        if payload.get("dispatch_origin") or session_override or self._entry_node_pinned(payload):
            return selected_entry_node
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        has_image_attachment = any(
            isinstance(a, dict) and str(a.get("type") or "") == "image"
            for a in attachments
        )
        hints = payload.get("route_hints") if isinstance(payload.get("route_hints"), dict) else {}
        has_image_hint = bool(hints.get("has_image"))
        if not (has_image_attachment or has_image_hint):
            return selected_entry_node

        routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
        vision = routing.get("vision") if isinstance(routing.get("vision"), dict) else {}
        channel = str(payload.get("channel") or hints.get("channel") or "").strip()
        platform = str(hints.get("platform") or channel.split("_", 1)[0] or "").strip()
        channels = vision.get("channels") if isinstance(vision.get("channels"), dict) else {}
        channel_cfg = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
        platform_cfg = channels.get(platform) if isinstance(channels.get(platform), dict) else {}
        # 目标节点要先算出来：auto 判定看的就是这个节点配没配。
        node_id = str(
            channel_cfg.get("entry_node_id")
            or platform_cfg.get("entry_node_id")
            or vision.get("entry_node_id")
            or ""
        ).strip()
        if not self._vision_routing_applies(vision.get("enabled", "auto"), node_id):
            return selected_entry_node
        return node_id or selected_entry_node or default_node

    def _task_terminal(self, task: Task) -> bool:
        return task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}

    def _to_positive_int(self, value: Any) -> int | None:
        try:
            v = int(value)
            return v if v > 0 else None
        except Exception:
            return None

    def _reset_task_route_state_locked(self, task: Task) -> None:
        """Persist a stable intent identity for a newly produced completion phase."""
        task.route_schema_version = 1
        task.route_generation = max(0, int(task.route_generation or 0)) + 1
        task.delivery_id = f"task:{task.task_id}:completion:{task.route_generation}:v1"
        task.route_status = RouteStatus.pending
        task.routed_at = None
        task.route_error = ""
        task.post_work_schema_version = 0
        task.post_work_id = ""
        task.post_work_status = ""
        task.post_work_error = ""
        task.post_work_phases = []
        raw_input = task.input if isinstance(task.input, dict) else {}
        task_context = raw_input.get("task_context")
        try:
            base_count = int(raw_input.get("base_count") or 0)
        except (TypeError, ValueError):
            base_count = 0
        task.route_context = {
            "runtime_session_id": task.session_id,
            "route_session_id": str(
                raw_input.get("_route_session_id")
                or raw_input.get("parent_session_id")
                or task.session_id
                or ""
            ),
            "parent_session_id": str(raw_input.get("parent_session_id") or ""),
            "branch_session_id": str(raw_input.get("branch_session_id") or ""),
            "base_count": base_count,
            "conversation_key": str(
                task_context.get("conversation_key")
                if isinstance(task_context, dict)
                else ""
            ),
        }

    # ---- 内部 task 创建 ----

    def _event_task_snapshot(
        self, event_type: str, task: Task, *, component: str = "supervisor",
    ) -> dict[str, Any]:
        return self.eventlog.append(
            session_id=task.session_id,
            component=component,
            type_=event_type,
            payload=self._event_payload_for_task(task, event_type=event_type),
        )

    # [2026-07-16] 只在 task_created 快照保留完整 input；后续生命周期
    # 事件（started/completed/suspended/resumed 等）的 input 与 created 一致。
    _INPUT_FULL_EVENT_TYPES = frozenset({"task_created"})

    def _event_payload_for_task(
        self, task: Task, *, event_type: str = "",
    ) -> dict[str, Any]:
        """Build a task snapshot payload safe for long-term event storage."""
        payload = task.model_dump(mode="json")
        result = payload.get("result")
        if isinstance(result, dict) and "dispatch_input" in result:
            # Why: dispatch_input can contain a full child prompt or compacted
            # conversation text, and the same stale result used to be repeated in
            # task_started/task_suspended snapshots. How: copy the serialized
            # result and remove only dispatch_input from the event payload while
            # leaving the live Task object untouched. Purpose: reduce future
            # events.jsonl growth and replay memory without changing routing,
            # which reads task.result before or outside event serialization.
            slim_result = dict(result)
            slim_result.pop("dispatch_input", None)
            slim_result["dispatch_input_omitted"] = True
            payload["result"] = slim_result
        # [2026-07-16] Why: task_started/task_completed/task_suspended/
        # task_resumed 等事件重复写入与 task_created 相同的 input（实测每条
        # ~1KB）How: 除 task_created 外，把 input 替换为只
        # 保留路由/回放必需轻量字段的瘦身版本并标记 _input_slimmed。
        # Purpose: 回放仍可从 task_created 拿完整 input，后续事件体积大幅缩减，
        # 不影响活 Task 对象与路由。
        if event_type and event_type not in self._INPUT_FULL_EVENT_TYPES:
            raw_input = payload.get("input")
            if isinstance(raw_input, dict) and raw_input:
                slim_input: dict[str, Any] = {"_input_slimmed": True}
                for _k in (
                    "task_context", "_dispatch_origin", "dispatch_context_mode",
                    "parent_session_id", "branch_session_id", "base_count",
                    "_route_session_id", "_branch_finalized", "child_session_id",
                    "_compact_dispatch_pending", "_system_task", "_async_dispatch",
                ):
                    if _k in raw_input:
                        slim_input[_k] = raw_input[_k]
                payload["input"] = slim_input
        return payload

    def _apply_task_snapshot(self, payload: dict[str, Any]) -> None:
        # [2026-07-16] Why: 非 task_created 事件的 input 已被瘦身
        # （只留 _input_slimmed + 少量路由字段）。回放时若直接用瘦身快照覆盖，
        # 会丢失已从 task_created 恢复的完整 instruction 等。How: 检测到
        # _input_slimmed 且已有同 task_id 的完整 input 时，保留旧 input。
        # Purpose: 事件回放后 Task.input 仍为完整值，路由/诊断不受影响。
        try:
            _new_input = payload.get("input")
            if isinstance(_new_input, dict) and _new_input.get("_input_slimmed"):
                _tid = payload.get("task_id")
                _prev = self.tasks.get(_tid) if _tid else None
                _prev_input = getattr(_prev, "input", None) if _prev else None
                if isinstance(_prev_input, dict) and _prev_input:
                    payload = dict(payload)
                    payload["input"] = _prev_input
            t = Task.model_validate(payload)
        except Exception:
            return
        existed = t.task_id in self.tasks
        self.tasks[t.task_id] = t
        if not existed:
            self._task_order.append(t.task_id)
        cur = self.session_generations.get(t.session_id, 0)
        if t.session_generation > cur:
            self.session_generations[t.session_id] = t.session_generation

    def _apply_cancel_requested(self, session_id: str, payload: dict[str, Any]) -> None:
        sid = str(session_id or payload.get("session_id") or "")
        if not sid:
            return
        self._cancelled_sessions.add(sid)
        gen = self._to_positive_int(payload.get("session_generation"))
        if gen is not None and gen > self.session_generations.get(sid, 0):
            self.session_generations[sid] = gen

    def _create_task_locked(
        self,
        *,
        session_id: str,
        session_generation: int,
        kind: TaskKind,
        node_id: str | None = None,
        tool_name: str | None = None,
        input_data: dict[str, Any] | None = None,
        continuation: dict[str, Any] | None = None,
        source_inbound_seq: int | None = None,
        caller_task_id: str | None = None,
        batch_id: str | None = None,
        batch_index: int = 0,
    ) -> Task:
        now = _now()
        task_input = dict(input_data or {})
        # [2026-05-29 方案C第一步] 为什么：task_created 事件在 Task 创建时立即
        # 持久化，调用方如果在 _create_task_locked 返回后才补 dispatch 元数据，SDK
        # 无法从事件 payload.input.task_context 中读取结构化路由字段。怎么改：在
        # 创建 Task 之前就把 _dispatch_origin、dispatch_context_mode、
        # parent_conversation_key、route_conversation_key 复制进 task_context。
        # 目的：下游 EventRouter 可直接从 task_created 事件判断子 session 属于哪个
        # 父频道，不再反解析 agent: 字符串。
        dispatch_origin = task_input.get("_dispatch_origin")
        if isinstance(dispatch_origin, dict) and dispatch_origin:
            task_context = task_input.get("task_context")
            if not isinstance(task_context, dict):
                task_context = {}
                task_input["task_context"] = task_context
            dispatch_mode = str(
                task_input.get("dispatch_context_mode")
                or dispatch_origin.get("context_mode")
                or task_context.get("dispatch_context_mode")
                or ""
            ).strip()
            parent_conv_key = str(
                dispatch_origin.get("parent_conversation_key")
                or task_context.get("parent_conversation_key")
                or task_context.get("route_conversation_key")
                or ""
            ).strip()
            if dispatch_mode:
                task_input.setdefault("dispatch_context_mode", dispatch_mode)
                dispatch_origin.setdefault("context_mode", dispatch_mode)
                task_context.setdefault("dispatch_context_mode", dispatch_mode)
            if parent_conv_key:
                dispatch_origin.setdefault("parent_conversation_key", parent_conv_key)
                task_context.setdefault("parent_conversation_key", parent_conv_key)
                task_context.setdefault("route_conversation_key", parent_conv_key)

        task = Task(
            task_id=str(uuid.uuid4()),
            session_id=session_id,
            session_generation=session_generation,
            kind=kind,
            node_id=node_id,
            tool_name=tool_name,
            input=task_input,
            continuation=dict(continuation or {}),
            source_inbound_seq=source_inbound_seq,
            caller_task_id=caller_task_id,
            batch_id=batch_id,
            batch_index=batch_index,
            status=TaskStatus.pending,
            cancel_requested=False,
            worker_id=None,
            created_at=now,
            updated_at=now,
            lease_expires_at=None,
            result={},
        )
        self.tasks[task.task_id] = task
        self._task_order.append(task.task_id)
        self._event_task_snapshot("task_created", task)
        return task

    def _cancel_session_tasks_locked(self, session_id: str) -> None:
        now = _now()
        for task in self.tasks.values():
            if task.session_id != session_id or self._task_terminal(task):
                continue
            if task.status == TaskStatus.pending:
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = now
                task.lease_expires_at = None
                # [Fork/Merge 2026-05-12] pending 入口分支被 session cancel 直接终结时也要清理。
                # 原因：此路径不会经过 complete_task/task_router。做法：调用幂等 finalize。
                # 目的：避免未启动分支在主 session 取消后遗留 JSONL 与映射。
                self._finalize_branch_task_locked(task, merge=True)
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.suspended:
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.waiting_for_task_id = None
                task.updated_at = now
                self._finalize_branch_task_locked(task, merge=True)
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.running and not task.cancel_requested:
                task.cancel_requested = True
                task.updated_at = now
                self._event_task_snapshot("task_cancel_requested", task)

    def _find_last_context_ref_locked(self, session_id: str, node_id: str, *, context_key: str | None = None) -> str:
        """查找同一 session 中指定 node_id 最近一次完成的 task 的 context_ref。

        如果提供了 context_key，则按 (session_id, node_id, context_key) 精确匹配，
        用于区分同一 node_id 的多个并发实例。否则按 (session_id, node_id) 匹配（默认行为）。

        DEPRECATED — Child Session 隔离（Phase D）
        新 dispatch 路径使用 get_or_create_child_session() 替代本方法。
        当前仍被 task_router 的兼容期 fallback 代码调用，待 child session
        稳定运行后可删除。
        """
        for tid in reversed(self._task_order):
            t = self.tasks.get(tid)
            if (t is not None
                and t.session_id == session_id
                and t.node_id == node_id
                and t.status == TaskStatus.completed
                and t.result.get("context_ref")):
                if context_key:
                    if t.input.get("_context_key") == context_key:
                        return str(t.result["context_ref"])
                else:
                    return str(t.result["context_ref"])
        return ""

    def _create_entry_task_for_inbound_locked(self, *, inbound_seq: int, session_id: str, payload: dict[str, Any]) -> Task | None:
        text = str(payload.get("text") or "").strip()
        has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
        # [AutoC 2026-06-04] Why: dispatch_result.text is now the raw child result and
        # can legitimately be empty when a child only returns summary. How: treat a
        # dispatch_result summary as sufficient inbound content for task creation.
        # Purpose: runner can still deliver the summary to the LLM through the new
        # LLM-only English prefix while ConversationStore keeps text empty.
        has_dispatch_summary = (
            str(payload.get("message_type") or "").strip() == "dispatch_result"
            and bool(str(payload.get("summary") or "").strip())
        )
        if not text and not has_attachments and not has_dispatch_summary:
            return None
        # 优先级: session 覆盖（AI switch） > 前端指定 > session 记录的 > 全局默认
        default_node = self._default_entry_node()
        session_override = self.session_entry_overrides.get(session_id, "").strip()
        session_info = self.sessions.get(session_id)
        # Why: after a supervisor restart, session_entry_overrides is deliberately
        # cleared. How: read the persisted SessionInfo.entry_node_id before using
        # the global runtime default. Purpose: callbacks keep the node binding
        # selected when the session was first routed or switched.
        session_recorded = (session_info.entry_node_id if session_info else "").strip() if session_info else ""
        payload_entry = str(payload.get("entry_node_id") or "").strip()
        # [2026-05-28] dispatch 场景下跳过 session_override，用 payload 指定的 entry_node。
        # 为什么：dispatch inbound 的 entry_node_id 由调用方精确指定，不应被
        #   session 级覆盖（如 AI switch_node）干扰。
        # 怎么改：检查 dispatch_origin 存在时走简化路径。
        # 目的：确保 dispatch 的目标节点始终准确。
        if payload.get("dispatch_origin"):
            entry_node = payload_entry or default_node
        elif self._entry_node_pinned(payload):
            entry_node = payload_entry
        else:
            entry_node = (
                session_override
                or payload_entry
                or session_recorded
                or default_node
            )
        cfg = load_runtime_config(self.workspace_root)
        entry_node = self._route_entry_node_for_capabilities(
            cfg=cfg,
            payload=payload,
            selected_entry_node=entry_node,
            default_node=default_node,
            session_override=session_override,
        )
        if session_info and not session_info.entry_node_id:
            session_info.entry_node_id = entry_node
            self._session_store.update_entry_node(session_id, entry_node)
        # Record the actual entry node used for this session (for getActiveNode API)
        self.session_last_entry_node[session_id] = entry_node
        generation = self._current_session_generation_locked(session_id) or 1
        if not self.session_generations.get(session_id):
            self.session_generations[session_id] = generation
        self._cancelled_sessions.discard(session_id)
        # dispatch 的 fork 模式下，将调用方历史复制到目标 session。

        # 目的：fork 模式下目标节点启动时能看到调用方的对话上下文。
        _fork_from = str(payload.get("dispatch_fork_from_session") or "").strip()
        if _fork_from:
            from engine.conversation_store import ConversationStore
            _fork_store = ConversationStore(self.workspace_root / "data" / "conversations")
            if _fork_store.message_count(session_id) == 0:
                try:
                    _fork_store.fork(_fork_from, session_id)
                except Exception as _fork_err:
                    logger.warning("dispatch fork from %s to %s failed: %s", _fork_from[:12], session_id[:12], _fork_err)
        # [Fork/Merge 2026-05-12] 每条 inbound 都创建独立入口分支。
        branch_session_id, fork_meta = self._create_entry_branch_locked(session_id, inbound_seq)
        branch_base_count = int(fork_meta.get("base_count") or 0)
        self._cancelled_sessions.discard(branch_session_id)
        # 收集当前活跃 task 摘要，注入给入口节点 AI 判断
        active_tasks_summary = self._active_tasks_summary_locked(session_id)
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
        use_context = bool(payload.get("use_context", True))
        platform_auth = dict(payload.get("platform_auth") or {}) if isinstance(payload.get("platform_auth"), dict) else {}
        route_hints = dict(payload.get("route_hints") or {}) if isinstance(payload.get("route_hints"), dict) else {}
        memory_hints = dict(payload.get("memory_hints") or {}) if isinstance(payload.get("memory_hints"), dict) else {}
        # Step 2（2026-04-16）：主节点切 ConversationStore。
        # flag 开启时不再注入 context_ref，engine 侧 runner.py 会从 data/conversations/{session_id}.jsonl
        # 加载 history，不再依赖 node_contexts snapshot。
        # flag 关闭时走旧路径，调用 _find_last_context_ref_locked 注入 snapshot ref。
        # _find_last_context_ref_locked 本体保留（兼容期回退路径仍需要）。
        main_use_conv = bool(cfg.get("engine", {}).get("child_session", {}).get("main_session_enabled", True))
        if main_use_conv:
            last_ctx_ref = ""
        else:
            # 查找入口节点上一轮的 context_ref，使对话上下文（含工具调用）跨轮次连续
            last_ctx_ref = self._find_last_context_ref_locked(session_id, entry_node)
        _dispatch_origin = payload.get("dispatch_origin")
        if not isinstance(_dispatch_origin, dict):
            _dispatch_origin = {}
        _dispatch_context_mode = str(payload.get("dispatch_context_mode") or "").strip()
        _dispatch_parent_conv_key = str(_dispatch_origin.get("parent_conversation_key") or "").strip()

        inbound_metadata: dict[str, Any] = {}
        _inbound_message_type = str(payload.get("message_type") or "").strip()
        if _inbound_message_type:
            inbound_metadata["inbound_message_type"] = _inbound_message_type
        _inbound_summary = str(payload.get("summary") or "").strip()
        if _inbound_summary:

            inbound_metadata["inbound_summary"] = _inbound_summary
        _inbound_child_session_id = str(payload.get("child_session_id") or "").strip()
        if _inbound_child_session_id:
            inbound_metadata["inbound_child_session_id"] = _inbound_child_session_id
        _inbound_child_task_id = str(payload.get("child_task_id") or payload.get("task_id") or "").strip()
        if _inbound_child_task_id:

            inbound_metadata["inbound_child_task_id"] = _inbound_child_task_id
        _inbound_child_node_id = str(payload.get("child_node_id") or payload.get("node_id") or "").strip()
        if _inbound_child_node_id:

            inbound_metadata["inbound_child_node_id"] = _inbound_child_node_id
        _inbound_caller_node_id = str(payload.get("caller_node_id") or "").strip()
        if _inbound_caller_node_id:
            inbound_metadata["inbound_caller_node_id"] = _inbound_caller_node_id
        if bool(payload.get("attachments_outbound_sent")):

            inbound_metadata["inbound_attachments_outbound_sent"] = True

        # task_created 快照一产生就包含 SDK 路由所需字段。
        if _dispatch_origin:
            if _dispatch_context_mode:
                _dispatch_origin.setdefault("context_mode", _dispatch_context_mode)
            if _dispatch_parent_conv_key:
                _dispatch_origin.setdefault("parent_conversation_key", _dispatch_parent_conv_key)

        task = self._create_task_locked(
            session_id=branch_session_id,
            session_generation=generation,
            kind=TaskKind.node,
            node_id=entry_node,
            input_data={
                "instruction": text,
                "context_ref": last_ctx_ref,
                "switched_from": default_node if session_override else "",
                "resume_data": {},
                "use_context": use_context,
                "_system_task": bool(payload.get("_system_task", False)),
                "schedule_id": str(payload.get("schedule_id") or ""),
                "active_tasks_summary": active_tasks_summary,
                "attachments": attachments or [],
                "parent_session_id": session_id,
                "branch_session_id": branch_session_id,
                "base_count": branch_base_count,
                "base_last_id": str(fork_meta.get("base_last_id") or ""),
                "fork_copied": int(fork_meta.get("copied") or 0),
                "task_context": {
                    "conversation_key": str(payload.get("conversation_key") or ""),
                    "channel": str(payload.get("channel") or ""),
                    "message_id": str(payload.get("message_id") or ""),
                    "entry_node_id": entry_node,
                    "session_id": branch_session_id,
                    "parent_session_id": session_id,
                    "branch_session_id": branch_session_id,
                    "base_count": branch_base_count,
                    "session_generation": generation,
                    "is_system_task": bool(payload.get("_system_task", False)),
                    "switched_from": default_node if session_override else "",
                    "use_context": use_context,
                    "platform_auth": platform_auth,
                    "route_hints": route_hints,
                    # knowledge_inject 从 rctx.task_context 读它，决定这轮要不要
                    # 按人加载某些人的跨会话记忆档案。
                    "memory_hints": memory_hints,
                    # [2026-05-29 方案C第一步] 为什么：SDK 只读取 task_created
                    # 事件的 payload.input.task_context，不能依赖后续对 Task 的补写。
                    # 怎么改：把 dispatch 上下文模式和父频道 route key 写入
                    # task_context。目的：子 session 映射和审批归属判断使用结构化字段。
                    "dispatch_context_mode": _dispatch_context_mode,
                    "parent_conversation_key": _dispatch_parent_conv_key,
                    "route_conversation_key": _dispatch_parent_conv_key,
                },
                "dispatch_context_mode": _dispatch_context_mode,
                "_dispatch_origin": _dispatch_origin,
                **inbound_metadata,
            },
            continuation={},
            source_inbound_seq=inbound_seq,
            caller_task_id=None,
        )
        # [2026-05-28/2026-05-29] dispatch_origin 透传：异步 dispatch 走 inbound 时
        # 将回调元数据写入 task.input。为什么：task 完成后 task_router 需要知道结果
        # 应发回哪个 session，同时 task_created 事件也必须携带 route 元数据。怎么改：
        # _create_task_locked 前已写入 _dispatch_origin，这里只补 caller_node_id 并保持
        # 兼容。目的：回调注入与 SDK 路由读取同一份结构化元数据。
        if isinstance(_dispatch_origin, dict) and _dispatch_origin:
            task.input["_caller_node_id"] = str(_dispatch_origin.get("caller_node_id") or "")
        # Emit transient inbound_accepted so Bot can advance watermark
        self.eventlog.append(
            session_id=session_id,
            component="supervisor",
            type_="inbound_accepted",
            payload={
                "inbound_seq": inbound_seq,
                "task_id": task.task_id,
                "session_id": session_id,
                "branch_session_id": branch_session_id,
                "conversation_key": str(payload.get("conversation_key") or ""),
            },
            transient=True,
        )
        return task

    def _active_tasks_summary_locked(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session 中活跃（pending/running）task 的摘要列表。"""
        result: list[dict[str, Any]] = []
        session_ids = {session_id, *self._entry_branch_ids_for_parent_locked(session_id)}
        for task in self.tasks.values():
            if task.session_id not in session_ids:
                continue
            if self._task_terminal(task):
                continue
            result.append({
                "task_id": task.task_id,
                "kind": task.kind.value,
                "node_id": task.node_id or "",
                "tool_name": task.tool_name or "",
                "status": task.status.value,
                "instruction": str(task.input.get("instruction") or "")[:200],
                "created_at": task.created_at.isoformat() if task.created_at else "",
                # [Fork/Merge 2026-05-12] 摘要暴露分支来源。
                # 原因：主 session 可能同时有多个 branch 任务，入口节点需要区分它们。
                # 做法：优先读取 task.input.branch_session_id，否则用 task.session_id。
                # 目的：保留旧摘要字段的同时支持并发分支可观测性。
                "branch_session_id": str(task.input.get("branch_session_id") or (task.session_id if task.session_id != session_id else "")),
                "parent_session_id": str(task.input.get("parent_session_id") or session_id),
                "source_inbound_seq": task.source_inbound_seq,
            })
        return result

    # [2026-05-28] 新增可选 node_id 参数：只取消指定节点的活跃任务。
    # 为什么：原本 cancel_active_tasks 会取消 session 内所有任务，粒度太粗。
    # 怎么改：传入 node_id 时，跳过不匹配的任务。
    # 目的：允许调用方按节点粒度管理任务生命周期。
    def cancel_active_tasks(self, session_id: str, *, exclude_task_id: str | None = None, node_id: str | None = None) -> dict[str, Any]:
        sid = (session_id or "").strip()
        if not sid:
            return {"ok": False}
        _filter_node = (node_id or "").strip() or None
        with self._lock:
            keep_ids: set[str] = set()
            tid = (exclude_task_id or "").strip()
            while tid:
                keep_ids.add(tid)
                t = self.tasks.get(tid)
                if t is None:
                    break
                tid = (t.caller_task_id or "").strip()

            now = _now()
            count = 0
            session_ids = {sid, *self._entry_branch_ids_for_parent_locked(sid)}
            for task in self.tasks.values():
                if task.session_id not in session_ids or self._task_terminal(task) or task.task_id in keep_ids:
                    continue
                # node_id 过滤：仅当指定了 node_id 且不匹配时跳过
                if _filter_node and task.node_id != _filter_node:
                    continue
                if task.status == TaskStatus.pending:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    # [Fork/Merge 2026-05-12] 本地取消直接置终态时同步收束入口分支。
                    # 原因：pending 任务不会再由 engine 上报完成。做法：调用幂等 finalize。
                    # 目的：让 cancel_active_tasks 与 finish/fail 路径一样清理分支。
                    self._finalize_branch_task_locked(task, merge=True)
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.suspended:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.waiting_for_task_id = None
                    task.updated_at = now
                    self._finalize_branch_task_locked(task, merge=True)
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.running:
                    if task.cancel_requested:
                        # 已标记取消但 worker 未响应；如果 lease 已过期，直接终结
                        if task.lease_expires_at and task.lease_expires_at < now:
                            task.status = TaskStatus.cancelled
                            task.lease_expires_at = None
                            task.updated_at = now
                            self._finalize_branch_task_locked(task, merge=True)
                            self._event_task_snapshot("task_cancelled", task)
                            count += 1
                    else:
                        task.cancel_requested = True
                        task.updated_at = now
                        # 如果 lease 已过期，worker 大概率已死，直接终结
                        if task.lease_expires_at and task.lease_expires_at < now:
                            task.status = TaskStatus.cancelled
                            task.lease_expires_at = None
                            self._finalize_branch_task_locked(task, merge=True)
                            self._event_task_snapshot("task_cancelled", task)
                        else:
                            self._event_task_snapshot("task_cancel_requested", task)
                        count += 1
            return {"ok": True, "cancelled_count": count}

    def cancel_single_task(self, task_id: str) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链。"""
        tid = (task_id or "").strip()
        if not tid:
            return {"ok": False, "error": "task_id required"}
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return {"ok": False, "error": "task not found"}
            if self._task_terminal(task):
                return {"ok": True, "cancelled_count": 0, "already_terminal": True}

            # 收集目标 task 及其所有后代子任务（BFS）
            to_cancel: set[str] = {tid}
            queue = [tid]
            while queue:
                parent_id = queue.pop(0)
                for t in self.tasks.values():
                    if t.caller_task_id == parent_id and not self._task_terminal(t) and t.task_id not in to_cancel:
                        to_cancel.add(t.task_id)
                        queue.append(t.task_id)

            now = _now()
            count = 0
            for cancel_tid in to_cancel:
                t = self.tasks.get(cancel_tid)
                if t is None or self._task_terminal(t):
                    continue
                if t.status == TaskStatus.pending:
                    t.cancel_requested = True
                    t.status = TaskStatus.cancelled
                    t.updated_at = now
                    t.lease_expires_at = None
                    # [Fork/Merge 2026-05-12] 单任务取消可能直接终结入口分支。
                    # 原因：pending 分支不会再进入完成路由。做法：在记录取消事件前 finalize。
                    # 目的：保证显式 task cancel 与自然完成一样回收 branch。
                    self._finalize_branch_task_locked(t, merge=True)
                    self._event_task_snapshot("task_cancelled", t)
                    count += 1
                elif t.status == TaskStatus.suspended:
                    t.cancel_requested = True
                    t.status = TaskStatus.cancelled
                    t.waiting_for_task_id = None
                    t.updated_at = now
                    self._finalize_branch_task_locked(t, merge=True)
                    self._event_task_snapshot("task_cancelled", t)
                    count += 1
                elif t.status == TaskStatus.running:
                    if t.cancel_requested:
                        # 已标记取消但 worker 未响应；如果 lease 已过期或从未获得 lease，直接终结
                        # fix: lease_expires_at 为 None 时也视为 worker 已死，避免永久僵尸
                        if not t.lease_expires_at or t.lease_expires_at < now:
                            t.status = TaskStatus.cancelled
                            t.lease_expires_at = None
                            t.updated_at = now
                            self._finalize_branch_task_locked(t, merge=True)
                            self._event_task_snapshot("task_cancelled", t)
                            count += 1
                    else:
                        t.cancel_requested = True
                        t.updated_at = now
                        # 如果 lease 已过期或从未获得 lease，worker 大概率已死，直接终结
                        # fix: lease_expires_at 为 None 时同样强制终结，防止无 lease 任务成为永久僵尸
                        if not t.lease_expires_at or t.lease_expires_at < now:
                            t.status = TaskStatus.cancelled
                            t.lease_expires_at = None
                            self._finalize_branch_task_locked(t, merge=True)
                            self._event_task_snapshot("task_cancelled", t)
                        else:
                            self._event_task_snapshot("task_cancel_requested", t)
                        count += 1
            return {"ok": True, "cancelled_count": count}

    # ---- orphaned task cleanup ----

    def cancel_orphaned_tasks(self) -> int:
        """Cancel all non-terminal tasks after a full restart.

        After a full restart, engine workers are gone and LLM context is
        not recoverable, so requeue is meaningless.  Call once during
        startup after rebuild_from_events.
        """
        with self._lock:
            now = _now()
            count = 0
            for task in self.tasks.values():
                if self._task_terminal(task):
                    continue
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = now
                task.lease_expires_at = None
                if task.waiting_for_task_id:
                    task.waiting_for_task_id = None
                self._event_task_snapshot("task_cancelled", task)
                count += 1
            return count

    # ---- preempt ----

    def preempt_task(self, task_id: str, message: str = "", attachments: list | None = None) -> bool:
        """标记单个 task 为 preempt_requested。不影响 session 状态。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in (TaskStatus.running, TaskStatus.pending):
                return False
            task.preempt_requested = True
            task.preempt_message = message
            if attachments:
                task.preempt_attachments = attachments
            # [Fork/Merge 2026-05-17] Why: a task can be running inside a
            # temporary entry branch, but session event consumers watch the
            # durable parent session. How: use the task route helper when the
            # supervisor includes it, and keep the runtime branch id in the
            # payload for diagnostics. Purpose: accepted preempt requests remain
            # visible without changing the task-local preempt flag semantics.
            route_for_task = getattr(self, "_route_session_id_for_task_locked", None)
            route_session_id = (
                str(route_for_task(task) or "").strip()
                if callable(route_for_task)
                else ""
            ) or task.session_id
            payload = {"task_id": task_id, "session_id": route_session_id, "has_message": bool(message)}
            if route_session_id != task.session_id:
                payload["runtime_session_id"] = task.session_id
            self.eventlog.append(
                session_id=route_session_id,
                component="supervisor",
                type_="preempt_requested",
                payload=payload,
                transient=True,
            )
            return True

    def consume_preempt_message(self, task_id: str) -> dict:
        """消费 preempt message，返回 {message, attachments} 并清空。同时重置 preempt_requested。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"message": "", "attachments": []}
            msg = task.preempt_message
            atts = list(task.preempt_attachments)
            task.preempt_message = ""
            task.preempt_attachments = []
            task.preempt_requested = False
            return {"message": msg, "attachments": atts}

    def is_task_preempted(self, task_id: str) -> dict:
        """查询 task 的 preempt 状态，包含 message。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"preempted": False, "message": "", "attachments": []}
            return {
                "preempted": task.preempt_requested,
                "message": task.preempt_message,
                "attachments": list(task.preempt_attachments),
            }

    def renew_lease(self, task_id: str, worker_id: str, lease_sec: float = 120.0) -> bool:
        """续租 task 的 lease，延长 lease_expires_at。仅 status=running 且 worker_id 匹配时生效。"""
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        if not tid or not wid:
            return False
        with self._lock:
            task = self.tasks.get(tid)
            if task is None or task.status != TaskStatus.running:
                return False
            if task.worker_id != wid:
                return False
            lease_val = max(10.0, min(lease_sec, 600.0))
            task.lease_expires_at = _now() + timedelta(seconds=lease_val)
            # [AutoC 2026-06-03] Why: during tool execution no stream_delta events
            # are emitted, so updated_at goes stale even though the worker is alive.
            # How: piggyback on the 60s heartbeat to refresh updated_at.
            # Purpose: tool-execution phases are implicitly exempt from the stale
            # reaper while still catching truly dead workers (no heartbeat = crash).
            task.updated_at = _now()
            return True

    # ---- task 队列（公开方法） ----

    def assign_next_task(self, *, worker_id: str, lease_sec: float = 120.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        with self._lock:
            now = _now()
            lease_val = max(10.0, min(lease_sec, 600.0))
            for tid in self._task_order:
                task = self.tasks.get(tid)
                if task is None or task.status != TaskStatus.pending:
                    continue

                current_gen = self._current_session_generation_locked(task.session_id)
                if task.cancel_requested or (current_gen and task.session_generation != current_gen):
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_cancelled", task)
                    continue

                if task.lease_expires_at and task.lease_expires_at > now:
                    continue

                if task.worker_id and task.worker_id != wid:
                    if task.lease_expires_at and task.lease_expires_at > now:
                        continue
                    # lease expired – 重新排队
                    task.status = TaskStatus.pending
                    task.worker_id = None
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_requeued", task)

                task.status = TaskStatus.running
                task.worker_id = wid
                task.updated_at = now
                task.lease_expires_at = now + timedelta(seconds=lease_val)
                self._event_task_snapshot("task_started", task)
                return task.model_dump(mode="json")
            return None

    def complete_task(self, *, task_id: str, worker_id: str, result: dict[str, Any]) -> Task | None:
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        if not tid or not wid:
            return None
        should_route = False
        finalize_only = False
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            if self._task_terminal(task):
                should_route = task.route_status != RouteStatus.routed
                if not should_route:
                    logger.info(
                        "task completion already routed; skipping duplicate route",
                        extra=self._task_route_log_extra_locked(task, event="task_route_skipped"),
                    )
            else:
                if task.worker_id and task.worker_id != wid and task.lease_expires_at and task.lease_expires_at > _now():
                    return None
                current_gen = self._current_session_generation_locked(task.session_id)
                if task.cancel_requested or (current_gen and task.session_generation != current_gen):
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = _now()
                    task.lease_expires_at = None
                    task.result = dict(result or {})
                    task.route_status = RouteStatus.routed
                    task.routed_at = task.updated_at
                    task.route_error = ""
                    self._event_task_snapshot("task_cancelled", task, component="engine")
                    finalize_only = True
                else:
                    task.result = dict(result or {})
                    task.updated_at = _now()
                    task.lease_expires_at = None
                    act = str(task.result.get("action") or "").strip()
                    if act == "cancelled":
                        task.status = TaskStatus.cancelled
                    elif act == "fail":
                        task.status = TaskStatus.failed
                    else:
                        task.status = TaskStatus.completed
                    self._reset_task_route_state_locked(task)
                    self._event_task_snapshot("task_completed", task, component="engine")
                    should_route = True

        if finalize_only:
            self._finalize_branch_task(task, merge=True)
        elif should_route:
            self._route_completed_task(task)
        return task

"""Built-in supervisor hook handler for scheduled memory dream tasks."""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_bool, get_str, load_runtime_config
from engine.builtin.knowledge_inject import _MEMORY_NAMESPACE_RE, _conversation_memory_namespace, memory_dir
from engine.cron import cron_match
from engine.builtin.memory_extract import _format_transcript_for_extract
from engine import memory_subjects


log = logging.getLogger(__name__)


# [AutoC 2026-05-31] Why: Dream now runs as a bounded preprocessing
# pipeline under the scheduler lock. How: keep all limits near the handler so
# session scanning, transcript formatting, and pending polling remain predictable.
# Purpose: avoid reintroducing full memory scans or large conversation tails.
_MAX_DREAM_SESSIONS = 5
_DREAM_TRANSCRIPT_MAX_CHARS = 12000
_DREAM_PENDING_TIMEOUT_MINUTES = 15
_TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "missing"}
# [AutoC 2026-06-01] Why: the final Dream node no longer has shell
# access to update scheduler state itself. How: centralize the lock note
# beside the polling constants. Purpose: keep the generated .dream-lock
# format stable and avoid scattering literal strings across the handler.
_DREAM_LOCK_NOTE = "Dream cycle completed via automated pipeline"

# 提示词里那几条清理/提升/重激活约束的判据，代码侧预筛用同一组数字。
_PRUNE_IDLE_DAYS = 30
_PROMOTE_MIN_AGE_DAYS = 7
_PROMOTE_RECENT_HIT_DAYS = 7
_PROMOTE_MIN_CHARS = 200
_REACTIVATE_IDLE_DAYS = 20
_MAX_MAINTENANCE_PER_KIND = 20


def _days_since(stamp: str, *, now: datetime) -> int | None:
    """ISO 时间戳距今多少天；空值或解析失败返回 None。"""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (now - parsed).days)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "DreamHandler",
    "hook_points": [
        ("on_schedule_tick", "on_tick"),
    ],
    "priority": 100,
    # Why: dream reorganizes memory entries that knowledge_inject caches.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing.
    "requires": ["knowledge_inject"],
}


class DreamHandler:
    """Handle scheduled dream creation through injected supervisor callbacks.

    Why: dream was a supervisor-side handler that imported SupervisorState. How:
    read workspace, session counts, and task creation from ctx callbacks instead.
    Purpose: keep the schedule gate while allowing all built-ins to live under
    engine.builtin without supervisor imports.
    """

    name = "dream"

    def __init__(self) -> None:
        # Why: duplicate suppression belongs to the dream feature. How: keep the
        # last fired minute on the handler. Purpose: remove dream-specific fields
        # from SchedulerThread while preserving behavior.
        self._last_dream_fired: str = ""
        # [AutoC 2026-06-01] Why: Dream now waits for memory_extractor
        # tasks and then for the final organizer tasks. How: store the
        # extractor entries and final dream_task_ids in handler memory only.
        # Purpose: update .dream-lock after success without reintroducing
        # execute_command access to the Dream node.
        self._dream_pending: dict[str, Any] | None = None

    def on_tick(self, ctx: dict[str, Any]) -> None:
        """Advance the scheduled Dream preprocessing state machine."""
        if str(ctx.get("schedule_type") or "").strip() != "dream":
            return

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            return
        workspace_root = Path(workspace_root)
        now_value = ctx.get("now")
        now = now_value if isinstance(now_value, datetime) else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_key = str(ctx.get("now_key") or now.strftime("%Y-%m-%d %H:%M"))

        # [AutoC 2026-06-01] Why: pending completion can happen after the cron
        # minute has passed, and pending now covers both extractor tasks and the
        # final Dream task. How: poll the pipeline before cron checks. Purpose:
        # finish a Dream run and update .dream-lock as soon as the task is ready.
        # 在飞的那轮先轮到终态再谈 enabled/cron：关掉 dream 不该把一个已经派出去的
        # 整理任务留在 pending 里。真正会造成冻结的是 organizer 没有轮询期限，
        # 见 _organizer_expired。
        if self._dream_pending is not None:
            self._poll_and_create_dream(ctx=ctx, workspace_root=workspace_root, now=now)
            return

        runtime_cfg = load_runtime_config(workspace_root)
        if not get_bool(runtime_cfg, "memory.dream.enabled", False):
            return

        cron_expr = get_str(runtime_cfg, "memory.dream.cron", "0 3 * * *").strip()
        if not cron_expr:
            return

        if self._last_dream_fired == now_key:
            return

        if not cron_match(cron_expr, now):
            return

        self._last_dream_fired = now_key
        self._start_dream_run(ctx=ctx, workspace_root=workspace_root, now=now, now_key=now_key)

    def _start_dream_run(
        self,
        *,
        ctx: dict[str, Any],
        workspace_root: Path,
        now: datetime,
        now_key: str,
    ) -> None:
        """Create extractor tasks and compute keyword topology for one Dream run."""
        create_task = ctx.get("create_task")
        if not callable(create_task):
            return

        session_ids = self._recent_active_session_ids(workspace_root)
        targets = self._session_memory_targets(workspace_root, session_ids)
        extractors: list[dict[str, str]] = []
        extractor_node = "system.memory_extractor"
        generation_cb = ctx.get("current_session_generation")

        for sid in session_ids:
            conversation_key, namespace = targets.get(sid, ("", ""))
            if not namespace:
                log.debug("[scheduler] dream skipped session without conversation_key: %s", sid)
                continue
            transcript = self._session_transcript(workspace_root=workspace_root, ctx=ctx, session_id=sid)
            if not transcript.strip():
                continue
            try:
                session_generation = int(generation_cb(sid) or 1) if callable(generation_cb) else 1
            except Exception:
                session_generation = 1
            child_sid = f"child_{uuid.uuid4().hex[:12]}"
            # [AutoC 2026-05-31] Why: Dream extractor prompts need the same dynamic
            # book guidance as automatic extraction. How: pass the book list into
            # each instruction builder. Purpose: avoid stale hard-coded categories.
            # 每个会话读自己 namespace 的 book：提取器建议的 book 名要落在同一目录，
            # 否则模型会照着别的群的 book 名分类。
            instruction = self._build_extractor_instruction(
                transcript, book_list=self._build_book_list(workspace_root, namespace),
            )
            try:
                # [AutoC 2026-05-31] Why: Dream preprocessing must reuse the
                # existing memory_extractor node without changing its system
                # prompt. How: pass stricter one-shot JSON instructions through
                # the task input and isolate the run in a child session. Purpose:
                # extract signals without directly writing memory at this stage.
                task = create_task(
                    session_id=sid,
                    session_generation=max(1, session_generation),
                    kind="node",
                    node_id=extractor_node,
                    input_data={
                        "instruction": instruction,
                        "child_session_id": child_sid,
                        "_system_task": True,
                        # 提取器的 tool_access 仍 allow save_memory，只靠提示词约束它别调。
                        # 不带 conversation_key 的话那一次越权写会落进 data/memory/ 根目录，
                        # 谁都读不到还污染全局。
                        "task_context": {"conversation_key": conversation_key},
                    },
                    continuation={},
                    source_inbound_seq=None,
                    caller_task_id=None,
                )
            except Exception as exc:
                log.warning("[scheduler] dream extractor task creation failed for session=%s: %s", sid, exc)
                continue
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if task_id:
                extractors.append({"task_id": task_id, "session_id": sid, "namespace": namespace})

        # [AutoC 2026-05-31] Why: restart persistence is explicitly unnecessary
        # for this workflow. How: keep only the pending ids in process memory.
        # Purpose: orphaned extractor results can be ignored and the next cron run
        # can rebuild the same inputs.
        self._dream_pending = {
            "now_key": now_key,
            "extractors": extractors,
            "session_ids": session_ids,
        }
        log.info(
            "[scheduler] dream preprocessing started sessions=%d extractors=%d namespaces=%d time=%s",
            len(session_ids),
            len(extractors),
            len({item["namespace"] for item in extractors}),
            now.strftime("%Y-%m-%d %H:%M UTC"),
        )

    def _poll_and_create_dream(
        self,
        *,
        ctx: dict[str, Any],
        workspace_root: Path,
        now: datetime,
    ) -> None:
        """Poll Dream pipeline snapshots and advance the run."""
        pending = self._dream_pending
        if pending is None:
            return

        task_snapshots = ctx.get("task_snapshots")
        if not callable(task_snapshots):
            log.warning("[scheduler] dream pipeline cannot poll tasks: missing task_snapshots callback")
            self._dream_pending = None
            return

        dream_task_ids = [str(tid) for tid in pending.get("dream_task_ids", []) if str(tid).strip()]
        if dream_task_ids:
            # [AutoC 2026-06-01] Why: after the final Dream tasks are created,
            # extractor polling is already complete and only the organizer results
            # matter. How: branch on dream_task_ids and poll those tasks.
            # Purpose: write .dream-lock only after the actual Dream cycle has
            # completed successfully.
            self._poll_final_dream_tasks(
                task_snapshots=task_snapshots,
                workspace_root=workspace_root,
                now=now,
                pending=pending,
                dream_task_ids=dream_task_ids,
            )
            return

        if self._pending_expired(pending, now=now):
            log.warning("[scheduler] dream preprocessing expired from %s; dropping pending run", pending.get("now_key"))
            self._dream_pending = None
            return

        extractors = [item for item in pending.get("extractors", []) if isinstance(item, dict)]
        task_ids = [str(item.get("task_id") or "") for item in extractors]
        task_ids = [tid for tid in task_ids if tid]
        snapshots = task_snapshots(task_ids)
        if not isinstance(snapshots, dict):
            return
        if any(str(snapshots.get(tid, {}).get("status") or "") not in _TERMINAL_TASK_STATUSES for tid in task_ids):
            return

        signals_by_namespace: dict[str, list[dict[str, Any]]] = {}
        for item in extractors:
            tid = str(item.get("task_id") or "")
            namespace = str(item.get("namespace") or "")
            if not tid or not namespace:
                continue
            signals_by_namespace.setdefault(namespace, [])
            snapshot = snapshots.get(tid, {})
            if str(snapshot.get("status") or "") != "completed":
                continue
            text = str(snapshot.get("result_text") or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                log.warning("[scheduler] dream extractor result is not JSON array task=%s error=%s", tid[:8], exc)
                continue
            if not isinstance(parsed, list):
                log.warning("[scheduler] dream extractor result ignored task=%s type=%s", tid[:8], type(parsed).__name__)
                continue
            for entry in parsed:
                if isinstance(entry, dict):
                    signals_by_namespace[namespace].append(entry)

        runtime_cfg = load_runtime_config(workspace_root)
        skill_list = self._load_skill_list(workspace_root)
        created: list[str] = []
        for namespace, signals in sorted(signals_by_namespace.items()):
            task_id = self._create_namespace_dream_task(
                ctx=ctx,
                runtime_cfg=runtime_cfg,
                workspace_root=workspace_root,
                now=now,
                namespace=namespace,
                signals=signals,
                skill_list=skill_list,
            )
            if task_id:
                created.append(task_id)

        created.extend(self._create_profile_dream_tasks(
            ctx=ctx, runtime_cfg=runtime_cfg, workspace_root=workspace_root, now=now,
        ))

        if not created:
            # 没有任何 namespace 需要整理时直接收尾：过去这里无条件建任务，
            # 于是每晚都拿空 signals + 空 topology 烧一次模型调用。
            log.info("[scheduler] dream found nothing to organize; skipping final tasks")
            self._dream_pending = None
            return

        # [AutoC 2026-06-01] Why: clearing pending here loses the only link
        # between this scheduler run and the final Dream tasks. How: retain the
        # pending payload and add the final task ids plus creation time. Purpose:
        # a later tick can detect successful completion and write .dream-lock.
        pending["dream_task_ids"] = created
        pending["dream_created_at"] = now.astimezone().isoformat(timespec="seconds")
        log.info(
            "[scheduler] dream tasks created after preprocessing extractors=%d namespaces=%d tasks=%d",
            len(task_ids),
            len(signals_by_namespace),
            len(created),
        )

    def _create_profile_dream_tasks(
        self,
        *,
        ctx: dict[str, Any],
        runtime_cfg: dict[str, Any],
        workspace_root: Path,
        now: datetime,
    ) -> list[str]:
        """给每份人物档案建一个去重任务。

        档案跨会话共享、又不参与年龄淘汰，同一件事被反复记成好几条只增不减，
        长期会把注入预算吃满。这里用的是不带 create_or_update_skill 的专用节点：
        整理人物记忆不该有能力改变 bot 自己的行为。
        """
        if not get_bool(runtime_cfg, "memory.dream.organize_profiles", True):
            return []
        node_id = get_str(
            runtime_cfg, "memory.dream.profile_node_id", "system.dream_profile",
        ).strip()
        skill_list = ""
        created: list[str] = []
        for subject in sorted(memory_subjects.enrolled_subjects(workspace_root)):
            namespace = memory_subjects.subject_namespace(subject)
            if not namespace:
                continue
            entries = self._load_memory_topology_entries(workspace_root, namespace)
            # 一条记忆无从谈起重复，两条起才有合并的余地。
            if len(entries) < 2:
                continue
            hits = self._hit_stamps(workspace_root, namespace, {entry["id"] for entry in entries})
            instruction = self._build_dream_instruction(
                run_id=str(uuid.uuid4()),
                now=now,
                signals=[],
                topology_json=self._build_keyword_topology_json(workspace_root, namespace),
                hit_cache_json=self._load_hit_cache_json(
                    workspace_root, namespace, {entry["id"] for entry in entries},
                ),
                maintenance_json=json.dumps(
                    self._maintenance_candidates(entries, hits, now=now), ensure_ascii=False,
                ),
                skill_list=skill_list,
                book_list=self._build_book_list(workspace_root, namespace),
            )
            task_id = self._create_final_dream_task(
                ctx=ctx,
                runtime_cfg=runtime_cfg,
                now=now,
                namespace=namespace,
                instruction=f"整理 {subject} 的人物档案。subject 参数一律填 {subject}。\n\n{instruction}",
                node_id=node_id,
                subject=subject,
            )
            if task_id:
                created.append(task_id)
        return created

    def _create_namespace_dream_task(
        self,
        *,
        ctx: dict[str, Any],
        runtime_cfg: dict[str, Any],
        workspace_root: Path,
        now: datetime,
        namespace: str,
        signals: list[dict[str, Any]],
        skill_list: str,
    ) -> str:
        """Create one final Dream task scoped to a single memory namespace."""
        entries = self._load_memory_topology_entries(workspace_root, namespace)
        if not signals and not entries:
            return ""
        # [AutoC 2026-05-31] Why: the final organizer also needs to know the
        # current book landscape. How: rebuild the list at final task creation
        # time so files created during preprocessing are visible. Purpose: guide
        # save/delete decisions with live book names.
        hits = self._hit_stamps(workspace_root, namespace, {entry["id"] for entry in entries})
        instruction = self._build_dream_instruction(
            run_id=str(uuid.uuid4()),
            now=now,
            signals=signals,
            topology_json=self._build_keyword_topology_json(workspace_root, namespace),
            hit_cache_json=self._load_hit_cache_json(
                workspace_root, namespace, {entry["id"] for entry in entries},
            ),
            maintenance_json=json.dumps(
                self._maintenance_candidates(entries, hits, now=now), ensure_ascii=False,
            ),
            skill_list=skill_list,
            book_list=self._build_book_list(workspace_root, namespace),
        )
        return self._create_final_dream_task(
            ctx=ctx,
            runtime_cfg=runtime_cfg,
            now=now,
            namespace=namespace,
            instruction=instruction,
        )

    def _create_final_dream_task(
        self,
        *,
        ctx: dict[str, Any],
        runtime_cfg: dict[str, Any],
        now: datetime,
        namespace: str,
        instruction: str,
        node_id: str = "",
        subject: str = "",
    ) -> str:
        """Create the final system.dream task and return its task id."""
        create_task = ctx.get("create_task")
        if not callable(create_task):
            return ""
        node_id = node_id or get_str(runtime_cfg, "memory.dream.node_id", "system.dream").strip()
        # conversation_key 就是目标 namespace：save_memory/delete_memory 只认
        # ToolContext.conversation_key 推出来的目录，写死 system:dream 的话整理动作
        # 全部落在 Dream 自己的目录里。用 namespace 而不是真实 key，是因为真实 key
        # 形如 qq_group:xxx，会命中 supervisor 的 QQ 非管理员硬限制，data/ 直接写不进去。
        conv_key = namespace
        channel = "system"
        msg_id = f"dream:{uuid.uuid4()}"
        try:
            # [AutoC 2026-06-01] Why: the scheduler must later know which final
            # Dream task finished. How: keep using the generic create_task hook but
            # capture the returned Task object's task_id. Purpose: make lock-file
            # updates depend on actual task completion rather than task creation.
            task = create_task(
                channel=channel,
                conversation_key=conv_key,
                kind="node",
                node_id=node_id,
                input_data={
                    "instruction": instruction,
                    "context_ref": "",
                    "resume_data": {},
                    "use_context": False,
                    "_system_task": True,
                    "task_context": {
                        "conversation_key": conv_key,
                        "channel": channel,
                        "message_id": msg_id,
                        "entry_node_id": node_id,
                        "is_system_task": True,
                        "use_context": False,
                        # 人物档案的 namespace 推不出 conv_ 摘要，记忆工具靠这里定位
                        # 已有条目；漏了它，改写会在会话目录另长一条。
                        **({"memory_hints": {"subjects": [subject]}} if subject else {}),
                    },
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if not task_id:
                log.warning("[scheduler] dream inject returned task without task_id")
                return ""
            return task_id
        except Exception as exc:
            log.warning("[scheduler] dream inject failed: %s", exc)
            return ""

    def _poll_final_dream_tasks(
        self,
        *,
        task_snapshots: Any,
        workspace_root: Path,
        now: datetime,
        pending: dict[str, Any],
        dream_task_ids: list[str],
    ) -> None:
        """Poll the final Dream tasks and write the lock once all of them succeed."""
        snapshots = task_snapshots(dream_task_ids)
        if not isinstance(snapshots, dict):
            return
        statuses = {tid: str(snapshots.get(tid, {}).get("status") or "").strip() for tid in dream_task_ids}
        if any(status not in _TERMINAL_TASK_STATUSES for status in statuses.values()):
            if self._organizer_expired(pending, now=now):
                log.warning(
                    "[scheduler] dream organizer stuck since %s (statuses=%s); dropping pending run",
                    pending.get("dream_created_at"),
                    ",".join(sorted(set(statuses.values()))),
                )
                self._dream_pending = None
            return

        failed = [tid for tid, status in statuses.items() if status != "completed"]
        if not failed:
            # 写锁失败也要照常清 pending：锁文件全仓没有任何读取方，丢一次远比把
            # dream 冻结到下次 supervisor 重启轻。
            self._write_dream_lock(workspace_root=workspace_root, completed_at=now)
            log.info(
                "[scheduler] dream tasks completed; lock updated tasks=%d created_at=%s",
                len(dream_task_ids),
                pending.get("dream_created_at"),
            )
        else:
            # [AutoC 2026-06-01] Why: failed, cancelled, or missing final Dream
            # tasks must not mark the cycle as successful. How: clear the pending
            # run without writing .dream-lock. Purpose: allow a future cron fire to
            # retry while preserving the lock as the last known successful run.
            log.warning(
                "[scheduler] dream ended without lock update failed=%d/%d statuses=%s",
                len(failed),
                len(dream_task_ids),
                ",".join(sorted({statuses[tid] for tid in failed})),
            )
        self._dream_pending = None

    def _write_dream_lock(self, *, workspace_root: Path, completed_at: datetime) -> bool:
        """Write data/memory/.dream-lock for a successful Dream cycle."""
        try:
            # [AutoC 2026-06-01] Why: the final Dream prompt cannot use
            # execute_command, but existing scheduling state expects this lock file.
            # How: write a small JSON document from the supervisor hook after the
            # final task reaches completed. Purpose: keep Dream completion durable
            # without giving the Dream node shell access.
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            mem_dir = workspace_root / "data" / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_run": completed_at.astimezone().isoformat(timespec="seconds"),
                "note": _DREAM_LOCK_NOTE,
            }
            (mem_dir / ".dream-lock").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            log.warning("[scheduler] dream lock update failed: %s", exc)
            return False

    def _recent_active_session_ids(self, workspace_root: Path) -> list[str]:
        """Return up to five recent user-facing sessions by updated activity."""
        conversations_dir = workspace_root / "data" / "conversations"
        if not conversations_dir.exists():
            return []

        allowed_sessions = self._active_session_ids_from_registry(workspace_root)
        registry_updates = self._active_session_updates_from_registry(workspace_root)
        ranked: list[tuple[float, str]] = []
        for path in conversations_dir.glob("*.jsonl"):
            sid = path.stem.strip()
            if not sid or sid.startswith("child_") or sid.startswith("branch_"):
                continue
            if allowed_sessions is not None and sid not in allowed_sessions:
                continue
            try:
                file_mtime = path.stat().st_mtime
            except OSError:
                continue
            # [AutoC 2026-05-31] Why: the Dream design asks for active sessions
            # ordered by updated_at, but older sessions.json rows may only have a
            # created_at field. How: prefer explicit updated_at/last_active_at
            # values when present, and fall back to the conversation JSONL mtime.
            # Purpose: honor newer registry metadata without losing compatibility
            # with existing deployments.
            rank_time = registry_updates.get(sid, file_mtime) if registry_updates is not None else file_mtime
            ranked.append((rank_time, sid))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [sid for _mtime, sid in ranked[:_MAX_DREAM_SESSIONS]]

    def _load_session_registry(self, workspace_root: Path) -> dict[str, dict[str, Any]] | None:
        """Read sessions.json rows keyed by session id, or None when unavailable."""
        path = workspace_root / "data" / "sessions.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        rows: dict[str, dict[str, Any]] = {}
        for sid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("reset") or entry.get("is_child"):
                continue
            rows[str(entry.get("session_id") or sid)] = entry
        return rows

    def _active_session_ids_from_registry(self, workspace_root: Path) -> set[str] | None:
        """Read sessions.json and return active non-child session ids when available."""
        rows = self._load_session_registry(workspace_root)
        if rows is None:
            return None

        active: set[str] = set()
        for sid, entry in rows.items():
            conv_key = str(entry.get("conversation_key") or "")
            channel = str(entry.get("channel") or "")
            # [AutoC 2026-05-31] Why: Dream should preprocess live user-facing
            # conversations, not its own internal system sessions. How: skip
            # system/internal registry rows before ranking JSONL mtimes. Purpose:
            # prevent recursive Dream self-analysis.
            # Dream 自己的整理任务以目标 namespace 为 conversation_key，长得像
            # conv_<hash>，不排掉的话下一轮会把上一轮的整理记录当成活跃会话再提取一次。
            if conv_key.startswith("system:") or channel == "internal":
                continue
            if _MEMORY_NAMESPACE_RE.match(conv_key):
                continue
            active.add(sid)
        return active

    def _active_session_updates_from_registry(self, workspace_root: Path) -> dict[str, float] | None:
        """Read explicit activity timestamps from sessions.json when available."""
        rows = self._load_session_registry(workspace_root)
        if rows is None:
            return None

        updates: dict[str, float] = {}
        for sid, entry in rows.items():
            raw_updated = str(entry.get("updated_at") or entry.get("last_active_at") or "").strip()
            if not raw_updated:
                continue
            try:
                updated_at = datetime.fromisoformat(raw_updated)
            except Exception:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updates[sid] = updated_at.timestamp()
        return updates

    def _session_memory_targets(self, workspace_root: Path, session_ids: list[str]) -> dict[str, tuple[str, str]]:
        """Map session ids to their (conversation_key, memory namespace) pair."""
        rows = self._load_session_registry(workspace_root) or {}
        targets: dict[str, tuple[str, str]] = {}
        for sid in session_ids:
            conv_key = str((rows.get(sid) or {}).get("conversation_key") or "").strip()
            namespace = _conversation_memory_namespace(conv_key)
            if namespace:
                targets[sid] = (conv_key, namespace)
        return targets

    def _session_transcript(self, *, workspace_root: Path, ctx: dict[str, Any], session_id: str) -> str:
        """Format one session transcript with the memory extractor formatter."""
        try:
            messages = self._load_recent_conversation_messages(workspace_root=workspace_root, session_id=session_id)
            transcript = _format_transcript_for_extract(messages, max_chars=_DREAM_TRANSCRIPT_MAX_CHARS)
            if transcript.strip():
                return transcript
        except Exception as exc:
            log.debug("[scheduler] dream conversation tail read failed session=%s error=%s", session_id, exc)

        session_messages = ctx.get("session_messages")
        if not callable(session_messages):
            return ""
        try:
            messages = session_messages(session_id, 200)
        except Exception:
            return ""
        return _format_transcript_for_extract(messages, max_chars=_DREAM_TRANSCRIPT_MAX_CHARS)

    def _load_recent_conversation_messages(self, *, workspace_root: Path, session_id: str) -> list[dict[str, Any]]:
        """Read a bounded recent JSONL window for one conversation session."""
        path = workspace_root / "data" / "conversations" / f"{session_id}.jsonl"
        if not path.exists():
            return []
        # [AutoC 2026-05-31] Why: Dream preprocessing runs while the scheduler
        # holds the supervisor lock, so loading an entire large ConversationStore
        # file would increase lock hold time. How: read only a bounded tail chunk,
        # parse valid JSONL rows, then keep the most recent rows that fit the
        # transcript budget. Purpose: provide the extractor useful recent context
        # without reviving the old full-tail scan behavior.
        max_bytes = max(_DREAM_TRANSCRIPT_MAX_CHARS * 8, 65536)
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read().decode("utf-8", errors="ignore")
        if start > 0 and "\n" in raw:
            raw = raw.split("\n", 1)[1]

        parsed: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                parsed.append(item)

        recent_reversed: list[dict[str, Any]] = []
        total = 0
        for item in reversed(parsed):
            role = str(item.get("role") or "")
            if role == "system":
                continue
            content = item.get("content", "")
            if isinstance(content, list):
                content_len = sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
            else:
                content_len = len(str(content or ""))
            total += content_len + len(role) + 16
            if recent_reversed and total > _DREAM_TRANSCRIPT_MAX_CHARS:
                break
            recent_reversed.append(item)
        return list(reversed(recent_reversed))

    def _build_extractor_instruction(self, transcript: str, book_list: str = "") -> str:
        """Build the one-shot instruction that turns memory_extractor into a signal emitter."""
        # [AutoC 2026-05-31] Why: preprocessing emits candidate memory records
        # with book names but must not rely on static categories. How: include the
        # dynamic book list before the transcript. Purpose: make later Dream
        # organization prefer existing books without blocking new ones.
        return f"""本次调用来自 Dream 预处理流水线。
以下是当前 memory book 列表：
<book_list>
{book_list}
</book_list>
保存时优先使用已有 book，也可以创建新 book。

禁止调用 save_memory、delete_memory、list_memories。
只分析下方 transcript。
最终必须调用 finish，finish 的 text 必须是纯 JSON 数组：
[{{"id": "...", "book": "...", "content": "...", "keywords": ["..."]}}]
无可保存信息时返回 []。

--- 以下是对话记录 ---
{transcript}"""

    def _build_book_list(self, workspace_root: Path, namespace: str) -> str:
        """Return current memory book names with entry counts for one namespace."""
        # 记忆按会话落在 data/memory/conv_<hash>/ 下，扫顶层拿不到任何 book；
        # 空 namespace 只会命中未隔离的历史 book。
        # 带条目数：提示词让模型「合并条目数 ≤ 5 的碎片 book」，只给名字它只能靠猜，
        # 或者去全量 list_memories —— 那正是约束 1 禁止的。
        mem_dir = memory_dir(workspace_root, namespace)
        if not mem_dir.exists():
            return "(no books found)"
        books: list[str] = []
        for path in sorted(mem_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                entries = data.get("entries") if isinstance(data, dict) else None
                count = len(entries) if isinstance(entries, list) else 0
            except Exception as exc:
                log.warning("[scheduler] dream book list skipped %s: %s", path.name, exc)
                continue
            books.append(f"{path.stem}({count})")
        return ", ".join(books) if books else "(no books found)"

    def _build_keyword_topology_json(self, workspace_root: Path, namespace: str) -> str:
        """Build Jaccard keyword clusters from one namespace's memory books."""
        entries = self._load_memory_topology_entries(workspace_root, namespace)
        clusterable = [entry for entry in entries if entry["keyword_set"]]
        parent = list(range(len(clusterable)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        keyword_index: dict[str, list[int]] = {}
        for idx, entry in enumerate(clusterable):
            for keyword in entry["keyword_set"]:
                keyword_index.setdefault(keyword, []).append(idx)

        seen_pairs: set[tuple[int, int]] = set()
        for bucket in keyword_index.values():
            for pos, left in enumerate(bucket):
                left_keywords = clusterable[left]["keyword_set"]
                for right in bucket[pos + 1:]:
                    pair = (left, right) if left < right else (right, left)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    right_keywords = clusterable[right]["keyword_set"]
                    intersection = len(left_keywords & right_keywords)
                    union_size = len(left_keywords | right_keywords)
                    if union_size and intersection / union_size > 0.5:
                        union(left, right)

        grouped: dict[int, list[dict[str, Any]]] = {}
        for idx, entry in enumerate(clusterable):
            grouped.setdefault(find(idx), []).append(entry)

        clusters: list[dict[str, Any]] = []
        for members in grouped.values():
            if len(members) < 2:
                continue
            member_keys = sorted(f"{m['book']}:{m['id']}" for m in members)
            cluster_id = f"cluster_{uuid.uuid5(uuid.NAMESPACE_URL, '|'.join(member_keys)).hex[:12]}"
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "entries": [
                        {
                            "book": m["book"],
                            "id": m["id"],
                            "content_preview": m["content_preview"],
                            "content_chars": m["content_chars"],
                            "keywords": m["keywords"],
                            "constant": m["constant"],
                            "source": m["source"],
                            "created_at": m["created_at"],
                            "priority": m["priority"],
                        }
                        for m in sorted(members, key=lambda item: (item["book"], item["id"]))
                    ],
                    "suggested_action": "merge_candidates",
                }
            )
        clusters.sort(key=lambda cluster: len(cluster.get("entries", [])), reverse=True)
        payload = {
            "clusters": clusters,
            "total_entries": len(entries),
            "total_clusters": len(clusters),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _load_memory_topology_entries(self, workspace_root: Path, namespace: str) -> list[dict[str, Any]]:
        """Read minimal memory fields required for topology clustering."""
        mem_dir = memory_dir(workspace_root, namespace)
        if not mem_dir.exists() or not mem_dir.is_dir():
            return []

        result: list[dict[str, Any]] = []
        for yaml_path in sorted(mem_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("[scheduler] dream topology skipped %s: %s", yaml_path.name, exc)
                continue
            if not isinstance(data, dict):
                continue
            book = str(data.get("book") or yaml_path.stem).strip() or yaml_path.stem
            raw_entries = data.get("entries")
            if not isinstance(raw_entries, list):
                continue
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                raw_keywords = entry.get("keywords")
                if isinstance(raw_keywords, list):
                    keywords = [str(keyword).strip() for keyword in raw_keywords if str(keyword).strip()]
                elif isinstance(raw_keywords, str) and raw_keywords.strip():
                    keywords = [raw_keywords.strip()]
                else:
                    keywords = []
                content = str(entry.get("content") or "").strip()
                # [AutoC 2026-05-31] Why: the final Dream node must not scan full
                # memory files, but it still needs enough metadata to avoid
                # deleting protected entries. How: expose a small preview plus the
                # source/constant protection fields, not the full Task or raw YAML.
                # Purpose: make topology cleanup precise and safe.
                result.append(
                    {
                        "book": book,
                        "id": eid,
                        "content_preview": content[:50],
                        "content_chars": len(content),
                        "keywords": keywords,
                        "keyword_set": set(keywords),
                        "constant": bool(entry.get("constant", False)),
                        "source": str(entry.get("source") or ""),
                        # 提示词按 created_at 判 30 天过期、7 天提升，按 priority 挑重激活候选，
                        # 按 content 长度判该不该提升成 skill —— 不发这几个字段，那几条约束就只能靠猜。
                        "created_at": str(entry.get("created_at") or ""),
                        "priority": int(entry.get("priority") or 0),
                    }
                )
        return result

    def _pending_expired(self, pending: dict[str, Any], *, now: datetime) -> bool:
        """Return whether a pending Dream run has exceeded its polling window."""
        raw_key = str(pending.get("now_key") or "").strip()
        try:
            started = datetime.strptime(raw_key, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo or timezone.utc)
        except Exception:
            return False
        return now - started > timedelta(minutes=_DREAM_PENDING_TIMEOUT_MINUTES)

    def _organizer_expired(self, pending: dict[str, Any], *, now: datetime) -> bool:
        """Return whether the organizer phase has outlived its polling window.

        15 分钟那道超时只覆盖提取阶段。整理任务一旦进入 pending，轮询就再没有任何
        期限：只要状态不落在终态集合里（suspended 也不在）就直接 return，而
        `_dream_pending` 只在内存里 —— 唯一的恢复方式是重启 supervisor。
        """
        raw = str(pending.get("dream_created_at") or "").strip()
        if not raw:
            return False
        try:
            # 这里写的是带时区偏移的 isoformat，不能照抄 _pending_expired 的 strptime
            # ——那会抛异常并被外层吞掉，等于永不超时。
            started = datetime.fromisoformat(raw)
        except Exception:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo or timezone.utc)
        # 上限跟着 supervisor 回收 running 任务的硬上限走，再留一个回收周期的余量，
        # 免得这里比 supervisor 先放手、把一个还在跑的整理任务判成卡死。
        try:
            max_running = max(300.0, float(os.getenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "3600")))
        except (TypeError, ValueError):
            max_running = 3600.0
        return now - started > timedelta(seconds=max_running + 1800)

    def _maintenance_candidates(
        self,
        entries: list[dict[str, Any]],
        hits: dict[str, str],
        *,
        now: datetime,
    ) -> dict[str, list[dict[str, Any]]]:
        """按提示词里的清理/提升/重激活约束预筛候选条目。

        topology 只输出 ≥2 成员的重复簇，一条孤立的过期记忆压根不会出现在里面 ——
        「30 天未命中就清理」「7 天以上且仍被命中就提升成 skill」这几条约束因此常年
        没有操作对象。判龄和命中间隔是纯算术，在代码里筛完只把少量候选交给模型判断。
        """
        prune: list[dict[str, Any]] = []
        promote: list[dict[str, Any]] = []
        reactivate: list[dict[str, Any]] = []

        for entry in entries:
            if entry.get("constant"):
                continue
            stamp = str(hits.get(str(entry.get("id") or "")) or "")
            idle_days = _days_since(stamp, now=now)
            age_days = _days_since(str(entry.get("created_at") or ""), now=now)
            item = {
                "book": entry.get("book", ""),
                "id": entry.get("id", ""),
                "content_preview": entry.get("content_preview", ""),
                "content_chars": entry.get("content_chars", 0),
                "priority": entry.get("priority", 0),
                "created_at": entry.get("created_at", ""),
                "last_hit_at": stamp or "(never)",
                "age_days": age_days,
                "idle_days": idle_days,
            }
            is_auto = str(entry.get("source") or "") == "auto"
            # 从未命中的条目 idle_days 是 None，按「最老」处理：它们才是最该清理的，
            # 而 hit_cache 里根本没有它们的键，模型无从判断。
            stale = idle_days is None or idle_days >= _PRUNE_IDLE_DAYS
            old_enough = age_days is not None and age_days >= _PRUNE_IDLE_DAYS
            if is_auto and stale and old_enough:
                prune.append(item)
            elif (
                is_auto
                and age_days is not None and age_days >= _PROMOTE_MIN_AGE_DAYS
                and idle_days is not None and idle_days <= _PROMOTE_RECENT_HIT_DAYS
                and int(entry.get("content_chars") or 0) > _PROMOTE_MIN_CHARS
            ):
                promote.append(item)
            elif (
                int(entry.get("priority") or 0) > 0
                and idle_days is not None
                and _REACTIVATE_IDLE_DAYS <= idle_days < _PRUNE_IDLE_DAYS
            ):
                reactivate.append(item)

        def _top(items: list[dict[str, Any]], *, key: str, reverse: bool) -> list[dict[str, Any]]:
            ordered = sorted(items, key=lambda i: (i.get(key) is None, i.get(key) or 0), reverse=reverse)
            return ordered[:_MAX_MAINTENANCE_PER_KIND]

        return {
            "prune": _top(prune, key="idle_days", reverse=True),
            "promote": _top(promote, key="content_chars", reverse=True),
            "reactivate": _top(reactivate, key="idle_days", reverse=True),
        }

    def _hit_stamps(self, workspace_root: Path, namespace: str, entry_ids: set[str]) -> dict[str, str]:
        """entry_id → 最后命中时间。命中查找必须走 hit_timestamp。

        升级前的记录是不带 namespace 的扁平 key，只认新 key 会让全部历史命中一夜之间
        变成「从未命中」，整个语料一次性进 Prune 候选。
        """
        if not entry_ids:
            return {}
        try:
            from engine.memory_hit_cache import hit_timestamp, read_hit_cache

            data = read_hit_cache(workspace_root)
        except Exception as exc:
            log.warning("[scheduler] dream: failed to load hit_cache: %s", exc)
            return {}
        if not data:
            return {}
        stamps: dict[str, str] = {}
        for eid in entry_ids:
            stamp = hit_timestamp(data, namespace, eid)
            if stamp:
                stamps[str(eid)] = stamp
        return stamps

    def _load_hit_cache_json(self, workspace_root: Path, namespace: str, entry_ids: set[str]) -> str:
        """Load hit_cache.json for one namespace's entries as a compact JSON string.

        [AutoC 2026-05-31] Why: Dream Prune phase needs hit timestamps to
        determine which auto-source entries are expired (30d no hit).
        How: read the JSON file, truncate to entries with hits in last 60d
        to keep instruction bounded. Purpose: enable lifecycle management.
        """
        # 每个 Dream 任务只能操作自己那个 namespace，传全量会让模型引用它删不掉的 id。
        # 命中查找必须走 hit_timestamp：升级前的记录是不带 namespace 的扁平 key，
        # 只认新 key 会让全部历史命中一夜之间变成「从未命中」，整个语料一次性进 Prune 候选。
        stamps = self._hit_stamps(workspace_root, namespace, entry_ids)
        if not stamps:
            return "{}"
        from datetime import timedelta as _td
        cutoff = datetime.now(timezone.utc) - _td(days=60)
        filtered: dict[str, str] = {}
        for eid, stamp in stamps.items():
            try:
                ts = datetime.fromisoformat(stamp)
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                filtered[eid] = stamp
        return json.dumps(filtered, ensure_ascii=False)

    def _load_skill_list(self, workspace_root: Path) -> str:
        """Load existing skill names and descriptions.

        [AutoC 2026-05-31] Why: Dream Promote phase needs to know existing
        skills to avoid creating duplicates. How: scan skills/*/SKILL.md
        frontmatter for name and description. Purpose: enable L1→L2 promotion.
        """
        skills_dir = workspace_root / "skills"
        if not skills_dir.exists():
            return "(no skills directory)"
        lines: list[str] = []
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            name = skill_dir.name
            # Extract description from frontmatter
            desc = ""
            try:
                content = skill_md.read_text(encoding="utf-8")[:500]
                for line in content.split("\n"):
                    if line.strip().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
            except Exception:
                pass
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines) if lines else "(no skills found)"

    def _build_dream_instruction(
        self,
        *,
        run_id: str,
        now: datetime,
        signals: list[dict[str, Any]],
        topology_json: str,
        hit_cache_json: str = "{}",
        maintenance_json: str = "{}",
        skill_list: str = "",
        book_list: str = "",
    ) -> str:
        """Assemble the final Dream task instruction from preprocessed data."""
        signals_json = json.dumps(signals, ensure_ascii=False, indent=2)
        # [AutoC 2026-05-31] Why: Dream should preserve reusable lessons,
        # related-memory activation, and valuable-but-dormant memories during
        # scheduled cleanup. How: include pattern extraction, association
        # discovery, and reactivation constraints in every generated Dream task.
        # Purpose: prevent memory organization from collapsing recurring
        # incidents into one concrete record or pruning useful rules too early.
        return f"""[auto_dream]
run_id: {run_id}
time: {now.strftime('%Y-%m-%d %H:%M UTC')}

以下是从最近活跃 session 中提取的新信号：
<extracted_signals>
{signals_json}
</extracted_signals>

以下是现有记忆的关键词拓扑聚类分析：
<keyword_topology>
{topology_json}
</keyword_topology>

以下是记忆关键词命中时间记录（entry_id → 最后命中时间 ISO；不在表内 = 从未命中）：
<hit_cache>
{hit_cache_json}
</hit_cache>

以下是已按判龄和命中间隔预筛出的维护候选（idle_days / age_days 已算好，last_hit_at
为 (never) 表示从未命中）：
<maintenance_candidates>
{maintenance_json}
</maintenance_candidates>

以下是现有 skill 列表：
<skill_list>
{skill_list}
</skill_list>

以下是当前 memory book 列表：
<book_list>
{book_list}
</book_list>

约束：
1. 不要调用 list_memories 全量扫描。
2. 不要读取对话文件（tail/cat/read_file）。
3. 只基于上面的 signals、topology、hit_cache 和 skill_list 做操作。
4. 对 signals 中的新信息：判断是否值得保存，调用 save_memory。
5. 对 topology 中的重复簇：判断是否需要合并/删除/更新，调用 save_memory/delete_memory。
6. 过期清理（Prune）：从 maintenance_candidates.prune 里逐条审查，确认无保留价值的用 delete_memory 清理。
7. 提升（Promote）：对 maintenance_candidates.promote 中的条目，检查 skill_list 是否已有同主题 skill，如有则合并，如无则用 create_or_update_skill 提升为 skill，之后 delete_memory 删除原条目。单次最多提升 3 条。
8. 单轮最多操作 20 条（含 save/delete/create_or_update_skill）。
9. constant=true 的记忆和 source 不是 auto 的手工记忆，保持保护不删。
10. 完成后用 finish 报告操作摘要。
11. Book 整理：检查 <book_list> 中条目数 ≤ 5 的碎片 book，将其条目用 save_memory 迁移到语义最近的大 book，然后 delete_memory 删除原条目。
12. 结构抽象（Pattern Extraction）：合并重复簇（约束5）时，不要只保留最完整的一条——如果簇内多条记忆描述的是同一类事件的不同实例（如 5 条都是「在生产环境直接改代码出事」的记录），应从中归纳出一条更抽象的规则（如「所有代码修改必须先在 original 做」），而非简单保留其中一条。
13. 关联链强化（Association Discovery）：处理 topology 簇时，交叉比对 <hit_cache>。如果簇内多条记忆的最后命中时间彼此相差在 48h 内，说明它们在实际使用中经常一起被需要。用 save_memory 给它们互相补充 keywords，使它们更容易一起被激活。
14. 重激活（Reactivation）：对 maintenance_candidates.reactivate 中的条目，审查其 keywords 是否过窄导致命中率低。如果内容仍有价值，用 save_memory 拆分或优化 keywords 使其更容易被触发，而非坐等过期删除。"""

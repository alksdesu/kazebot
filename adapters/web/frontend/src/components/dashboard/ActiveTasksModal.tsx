// [AutoC 2026-06-04] Active task monitor modal.
// Why: operators need details behind the running-task count. How: poll the
// active-task summary endpoint every 5s while the modal is open, using the
// shared Modal shell. Purpose: DRY modal UX, reusable from any page.
import { useEffect, useState } from 'react';

import { cancelTask, fetchActiveTasks, type ActiveTask } from '../../api/supervisorClient';
import { usePageNotice } from '../../hooks/usePageNotice';
import { useRequestGuard } from '../../hooks/useRequestGuard';
import { useSettingsStore } from '../../store/settingsStore';
import { useChatStore, type TaskActivity } from '../../store/chatStore';
import { useViewStore } from '../../store/viewStore';
import { TONE_BOX } from '../../styles/tones';
import { startPolling } from '../../utils/polling';
import { Modal } from '../common';
import { errorMessage } from '../../api/request';

interface ActiveTasksModalProps {
  open: boolean;
  onClose: () => void;
}

function shortId(value: string | null | undefined, length = 8): string {
  return value ? value.slice(0, length) : '';
}

function parseTime(value: string): number {
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function formatRuntime(createdAt: string): string {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - parseTime(createdAt)) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

function formatRelativeMs(timestamp: number): string {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (elapsedSeconds < 60) return `${elapsedSeconds}s ago`;
  const minutes = Math.floor(elapsedSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatRelative(updatedAt: string): string {
  return formatRelativeMs(parseTime(updatedAt));
}

function statusClass(status: ActiveTask['status']): string {
  if (status === 'running') return TONE_BOX.success;
  if (status === 'pending') return TONE_BOX.warning;
  return TONE_BOX.neutral;
}

function activityClass(activity: TaskActivity): string {
  if (activity.phase === 'thinking') return `animate-pulse ${TONE_BOX.meta}`;
  if (activity.phase === 'generating') return `animate-pulse ${TONE_BOX.success}`;
  if (activity.phase === 'tool_call') return TONE_BOX.info;
  if (activity.phase === 'awaiting_approval') return TONE_BOX.warning;
  return '';
}

function activityLabel(activity: TaskActivity): string {
  if (activity.phase === 'thinking') return '思考中';
  if (activity.phase === 'generating') return '生成中';
  if (activity.phase === 'tool_call') return `工具: ${activity.detail || '未知'}`;
  if (activity.phase === 'awaiting_approval') return '等待审批';
  return '';
}

function selectTaskActivity(task: ActiveTask, activities: Readonly<Record<string, TaskActivity>>): TaskActivity | undefined {
  // [AutoC 2026-06-04] Why: the backend now maintains current_phase/current_detail
  // on the live Task object, so the API response already carries the real-time state.
  // How: prefer backend fields; fall back to WS-driven taskActivities for sub-5s
  // freshness between API polls. Purpose: modal shows correct state on first open.
  if (task.current_phase) {
    return { phase: task.current_phase as TaskActivity['phase'], detail: task.current_detail || '', lastEventAt: Date.now() };
  }
  const candidates: TaskActivity[] = [];
  const byTask = activities[task.task_id];
  if (byTask) candidates.push(byTask);
  if (task.node_id) {
    const bySessionNode = activities[`${task.session_id}:${task.node_id}`];
    const byNode = activities[task.node_id];
    if (bySessionNode) candidates.push(bySessionNode);
    if (byNode) candidates.push(byNode);
  }
  const bySession = activities[task.session_id];
  if (bySession) candidates.push(bySession);
  return candidates.sort((a, b) => b.lastEventAt - a.lastEventAt)[0];
}

function taskTitle(task: ActiveTask): string {
  return task.node_id || `task:${shortId(task.task_id)}`;
}

export const ActiveTasksModal = ({ open, onClose }: ActiveTasksModalProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const requests = useRequestGuard();
  const taskActivities = useChatStore(state => state.taskActivities);
  const [tasks, setTasks] = useState<ActiveTask[]>([]);
  const [loading, setLoading] = useState(false);
  const { error, setError, setLoadError, clearNotice } = usePageNotice();
  const [refreshTick, setRefreshTick] = useState(0);
  const [cancellingTaskIds, setCancellingTaskIds] = useState<Record<string, boolean>>({});

  // Reopening starts a fresh look: a cancel failure from the last round should not linger.
  useEffect(() => { if (open) clearNotice(); }, [open, clearNotice]);

  useEffect(() => {
    if (!open) return undefined;

    const load = async () => {
      // A slow poll that returns after a newer one must not put the older list back.
      const request = requests.begin('tasks');
      if (!adminToken) {
        if (request.isCurrent()) {
          setTasks([]);
          setLoadError('需要管理员令牌才能读取活跃任务。');
          setLoading(false);
        }
        return;
      }

      try {
        setLoading(true);
        const nextTasks = await fetchActiveTasks(adminToken);
        if (request.isCurrent()) {
          setTasks(nextTasks);
          setLoadError('');
        }
      } catch (loadError) {
        if (request.isCurrent()) {
          setLoadError(errorMessage(loadError, '活跃任务刷新失败。'));
        }
      } finally {
        if (request.isCurrent()) setLoading(false);
      }
    };

    const stop = startPolling(load, 5000, { first: load });
    return () => {
      requests.drop('tasks');
      stop();
    };
  }, [requests, adminToken, open, refreshTick]);

  const handleCancel = async (task: ActiveTask) => {
    if (!adminToken || task.cancel_requested || cancellingTaskIds[task.task_id]) return;
    // [AutoC 2026-06-04] Why: cancellation is per row and may take a backend worker
    // round-trip to become visible in polling. How: keep a local disabled state,
    // call the precise cancel endpoint, then trigger an immediate list refresh.
    // Purpose: users get fast feedback and cannot double-submit the same task cancel.
    setCancellingTaskIds(current => ({ ...current, [task.task_id]: true }));
    setError('');
    try {
      await cancelTask(adminToken, task.task_id);
      if (!requests.isCurrent()) return;
      setRefreshTick(value => value + 1);
    } catch (cancelError) {
      if (!requests.isCurrent()) return;
      setError(errorMessage(cancelError, '任务取消失败。'));
    } finally {
      setCancellingTaskIds(current => {
        const next = { ...current };
        delete next[task.task_id];
        return next;
      });
    }
  };

  return (
    <Modal
      ariaLabel="活跃任务详情"
      maxWidth="max-w-4xl"
      onClose={onClose}
      open={open}
      subtitle="任务监控"
      title="活跃任务详情"
    >
      <div className="p-3">
        {error && (
          <div className={`mb-3 border px-2.5 py-2 text-[0.65rem] ${TONE_BOX.danger}`} role="alert">
            {error}
          </div>
        )}

        {loading && tasks.length === 0 ? (
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-4 text-center text-[0.75rem] text-[var(--duties-secondary)]">
            正在加载活跃任务…
          </div>
        ) : tasks.length === 0 ? (
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-4 text-center text-[0.75rem] text-[var(--duties-secondary)]">
            当前没有活跃任务
          </div>
        ) : (
          <ul className="space-y-2">
            {tasks.map(task => {
              const activity = selectTaskActivity(task, taskActivities);
              const hasLiveStatus = Boolean(activity && activity.phase !== 'idle');
              const statusLabel = hasLiveStatus && activity ? activityLabel(activity) : task.status;
              const statusStyles = hasLiveStatus && activity ? activityClass(activity) : statusClass(task.status);
              const cancelling = Boolean(cancellingTaskIds[task.task_id]) || task.cancel_requested;
              return (
                <li
                  className="grid grid-cols-[minmax(0,1.5fr)_auto_minmax(5rem,0.65fr)_minmax(5rem,0.65fr)_minmax(4rem,0.55fr)_auto] items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2.5 text-[0.7rem]"
                  key={task.task_id}
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs font-semibold text-[var(--duties-text)]">
                      {taskTitle(task)} <span className="text-[var(--duties-tertiary)]">#{shortId(task.task_id)}</span>
                    </p>
                    <p className="mt-0.5 truncate font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{task.task_id}</p>
                    <p className="mt-1 truncate text-[0.65rem] leading-4 text-[var(--duties-secondary)]" title={task.input_summary || '无输入摘要'}>
                      {task.input_summary || '无输入摘要'}
                    </p>
                  </div>
                  <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[0.6rem] ${statusStyles}`}>
                    {statusLabel}
                  </span>
                  <div className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">运行时长</p>
                    <p>{formatRuntime(task.created_at)}</p>
                  </div>
                  <div className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">最后活动</p>
                    <p>{activity ? formatRelativeMs(activity.lastEventAt) : formatRelative(task.updated_at)}</p>
                  </div>
                  <div className="min-w-0 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">worker</p>
                    <p className="truncate">{shortId(task.worker_id) || '无'}</p>
                  </div>
                  <div className="flex gap-1">
                    <button
                      aria-label={`查看任务 ${task.task_id} 的 session`}
                      className={`rounded-sm border px-2 py-1 font-mono text-[0.6rem] transition-colors ${TONE_BOX.info} hover:bg-[var(--duties-info-hover)]`}
                      onClick={() => {
                        // [AutoC 2026-06-04] Fix: switch back to chat view, then
                        // enter virtual child session. This is a temporary overlay
                        // that does not touch the sidebar conversation list.
                        useViewStore.getState().closeSettings();
                        useChatStore.getState().viewChildSession(task.session_id, task.task_id);
                        onClose();
                      }}
                      type="button"
                    >
                      查看
                    </button>
                    <button
                      aria-label={`取消任务 ${task.task_id}`}
                      className={`rounded-sm border px-2 py-1 font-mono text-[0.6rem] transition-colors ${
                        cancelling
                          ? `cursor-not-allowed ${TONE_BOX.neutral}`
                          : `${TONE_BOX.danger} hover:bg-[var(--duties-danger-hover)]`
                      }`}
                      disabled={cancelling}
                      onClick={() => { void handleCancel(task); }}
                      type="button"
                    >
                      {cancelling ? '取消中...' : '取消'}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Modal>
  );
};

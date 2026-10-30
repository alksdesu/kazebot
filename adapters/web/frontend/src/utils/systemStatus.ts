import type { AdminState, RestartInProgress, RestartResult } from '../api/supervisorClient';
import type { ConnectionStatus } from '../store/chatStore';
import { appendSentence } from './sentence';

// Shared derivations for Supervisor status surfaces (dashboard and settings).
export function formatUptime(seconds: number | undefined): string {
  // A freshly started Supervisor legitimately reports 0, so only undefined/negative is unknown.
  if (seconds === undefined || seconds < 0) return '未知';
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}天 ${hours}小时`;
  if (hours > 0) return `${hours}小时 ${minutes}分钟`;
  if (minutes > 0) return `${minutes}分钟`;
  return `${total}秒`;
}

export function countActiveTasks(tasks: Record<string, number> | undefined): number {
  // Suspended and pending tasks are still non-terminal work, so they count as active.
  return (tasks?.running || 0) + (tasks?.pending || 0) + (tasks?.suspended || 0);
}

// The counter covers every pending approval; the list is the fallback when the counter has no pending key.
export function countPendingApprovals(state: AdminState): number {
  return state.approvals.pending ?? state.pending_approvals.length;
}

export function connectionDotClass(status: ConnectionStatus): string {
  if (status === 'open') return 'bg-[var(--duties-live)]';
  if (status === 'connecting' || status === 'reconnecting') return 'bg-[var(--duties-warning)]';
  if (status === 'closed') return 'bg-[var(--duties-danger)]';
  // Idle only means no socket is needed after a finished turn; a red dot there would be a false alarm.
  return 'bg-[var(--duties-tertiary)] opacity-40';
}

// Keys are the reason strings in supervisor/types.py; an unknown reason still gets a readable line.
const RESTART_REFUSALS: Record<string, string> = {
  'no process manager': '调度器没有在管理引擎进程',
  'restart already in progress': '上一次重启还没收尾，稍后再试',
};

const RESTART_TARGETS: Record<string, string> = {
  engine: '引擎重启',
  all: '全量重启',
};

const CLOCK = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function inProgressRefusal(inProgress: RestartInProgress): string | undefined {
  const since = new Date(inProgress.since);
  if (Number.isNaN(since.getTime())) return undefined;
  const what = RESTART_TARGETS[inProgress.target] ?? '重启';
  return `上一次${what}（${CLOCK.format(since)} 开始）还没收尾，稍后再试`;
}

/** The request reached the supervisor but its receipt was unreadable: whether a restart got queued is unknown. */
export function unreadRestartMessage(receiptProblem: string): string {
  return appendSentence(receiptProblem, '不知道排没排上，去运行页看 worker 有没有重新拉起');
}

export function restartRefusal(result: RestartResult | undefined): string {
  const inProgress = result?.in_progress ? inProgressRefusal(result.in_progress) : undefined;
  return inProgress ?? RESTART_REFUSALS[result?.reason ?? ''] ?? '调度器没有排上这次重启';
}

import type { RestartInProgress, RestartResult } from '../api/supervisorClient';

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

export function restartRefusal(result: RestartResult | undefined): string {
  const inProgress = result?.in_progress ? inProgressRefusal(result.in_progress) : undefined;
  return inProgress ?? RESTART_REFUSALS[result?.reason ?? ''] ?? '调度器没有排上这次重启';
}

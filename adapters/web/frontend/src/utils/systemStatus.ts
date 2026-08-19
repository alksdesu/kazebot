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

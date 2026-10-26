// 提示缓存命中率汇总：/v1/admin/usage/cache 的返回形状与请求。
import { MOUNT } from '../instances/paths';
import { requestJson } from './request';
import type { MemoryOwner } from './supervisorClient';

export type CacheUsageDimension = 'session' | 'node' | 'model' | 'channel';
export type CacheBaselineMode = 'legacy' | 'before';

export interface CacheUsageStats {
  /** 带缓存数据、计入命中率的任务数。 */
  tasks: number;
  prompt_tokens: number;
  cache_read_tokens: number;
  miss_tokens: number;
  /** 一个带缓存数据的任务都没有时是 null：显示「无数据」，不当成 0%。 */
  hit_rate: number | null;
  /** 渠道没报缓存的任务，不进命中率的分子分母。 */
  no_data_tasks: number;
  no_data_prompt_tokens: number;
}

export interface CacheUsageSession {
  conversation_key: string;
  owner: MemoryOwner | null;
  current_account: boolean;
  /** 记忆整理这类内部任务借来的会话，不是真人对话。 */
  internal: boolean;
}

export interface CacheUsageRow {
  /** 会话号、节点 id、模型名或渠道名；老记录没有模型和渠道时为空串。 */
  key: string;
  current: CacheUsageStats;
  baseline: CacheUsageStats;
  /** 只有按会话汇总时才有。 */
  session?: CacheUsageSession;
}

export interface CacheUsageScan {
  files: number;
  reparsed: number;
  bad_lines: number;
  records: number;
  segments: number;
  duplicates: number;
  without_usage: number;
}

export interface CacheUsageReport {
  group_by: CacheUsageDimension;
  window: { since: string | null; until: string | null };
  baseline: { mode: CacheBaselineMode; before: string | null };
  totals: { current: CacheUsageStats; baseline: CacheUsageStats };
  rows: CacheUsageRow[];
  scan: CacheUsageScan;
}

export interface CacheUsageQuery {
  groupBy: CacheUsageDimension;
  /** ISO 8601。只筛带完成时间的新记录。 */
  since?: string;
  until?: string;
  /** 给了就拿这个时间之前的新记录当基线；不给是升级前的旧记录。 */
  baselineBefore?: string;
}

export function cacheUsageUrl(query: CacheUsageQuery): string {
  const params = new URLSearchParams({ group_by: query.groupBy });
  if (query.since) params.set('since', query.since);
  if (query.until) params.set('until', query.until);
  if (query.baselineBefore) params.set('baseline_before', query.baselineBefore);
  return `${MOUNT}/v1/admin/usage/cache?${params.toString()}`;
}

export function getCacheUsage(
  query: CacheUsageQuery,
  options: { token?: string; signal?: AbortSignal } = {},
): Promise<CacheUsageReport> {
  return requestJson<CacheUsageReport>(cacheUsageUrl(query), { token: options.token, signal: options.signal });
}

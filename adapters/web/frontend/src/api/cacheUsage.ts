// 提示缓存命中率汇总：/v1/admin/usage/cache 的返回形状与请求。
import { MOUNT } from '../instances/paths';
import { requestJson } from './request';
import { BOOLEAN, NUMBER, TEXT, at, nullable, oneOf, shaped, shapedRows } from './shape';
import { MEMORY_OWNER, type MemoryOwner } from './supervisorClient';

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

const STATS = {
  tasks: NUMBER,
  prompt_tokens: NUMBER,
  cache_read_tokens: NUMBER,
  miss_tokens: NUMBER,
  hit_rate: nullable(NUMBER),
  no_data_tasks: NUMBER,
  no_data_prompt_tokens: NUMBER,
};
const SESSION = { conversation_key: TEXT, current_account: BOOLEAN, internal: BOOLEAN };
const SCAN = {
  files: NUMBER, reparsed: NUMBER, bad_lines: NUMBER, records: NUMBER, segments: NUMBER, duplicates: NUMBER, without_usage: NUMBER,
};

function statsOf(what: string, value: unknown, path: string): CacheUsageStats {
  const { tasks, prompt_tokens, cache_read_tokens, miss_tokens, hit_rate, no_data_tasks, no_data_prompt_tokens } = shaped(what, value, STATS, path);
  return { tasks, prompt_tokens, cache_read_tokens, miss_tokens, hit_rate, no_data_tasks, no_data_prompt_tokens };
}

function sessionOf(what: string, value: unknown, path: string): CacheUsageSession {
  const session = shaped(what, value, SESSION, path);
  const owner = session.owner === null ? null : shaped(what, session.owner, MEMORY_OWNER, at(path, 'owner')) as unknown as MemoryOwner;
  return { conversation_key: session.conversation_key, owner, current_account: session.current_account, internal: session.internal };
}

// 命中率表读不懂就抛：缺一格补成 0，「无数据」就成了 0% 命中。
function cacheUsageOf(what: string, value: unknown): CacheUsageReport {
  const report = shaped(what, value, { group_by: oneOf('session', 'node', 'model', 'channel') });
  const period = shaped(what, report.window, { since: nullable(TEXT), until: nullable(TEXT) }, 'window');
  const baseline = shaped(what, report.baseline, { mode: oneOf('legacy', 'before'), before: nullable(TEXT) }, 'baseline');
  const totals = shaped(what, report.totals, {}, 'totals');
  return {
    group_by: report.group_by,
    window: { since: period.since, until: period.until },
    baseline: { mode: baseline.mode, before: baseline.before },
    totals: { current: statsOf(what, totals.current, 'totals.current'), baseline: statsOf(what, totals.baseline, 'totals.baseline') },
    rows: shapedRows(what, report.rows, { key: TEXT }, 'rows').map((row, index) => {
      const path = at('rows', index);
      return {
        key: row.key,
        current: statsOf(what, row.current, at(path, 'current')),
        baseline: statsOf(what, row.baseline, at(path, 'baseline')),
        ...(row.session === undefined ? {} : { session: sessionOf(what, row.session, at(path, 'session')) }),
      };
    }),
    scan: shaped(what, report.scan, SCAN, 'scan'),
  };
}

export function getCacheUsage(
  query: CacheUsageQuery,
  options: { token?: string; signal?: AbortSignal } = {},
): Promise<CacheUsageReport> {
  const what = '缓存命中汇总';
  return requestJson(cacheUsageUrl(query), what, (body) => cacheUsageOf(what, body), { token: options.token, signal: options.signal });
}

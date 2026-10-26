// 提示缓存命中率：按会话、节点、模型或渠道看命中 / 输入，基线和当前值并排，验收缓存改动前后的差别。
import { useEffect, useMemo, useState } from 'react';

import {
  getCacheUsage,
  type CacheUsageDimension,
  type CacheUsageReport,
  type CacheUsageRow,
  type CacheUsageStats,
} from '../api/cacheUsage';
import { errorMessage } from '../api/request';
import { scopeIdentity } from '../features/conversationNames';
import { useConversationNames } from '../features/useConversationNames';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Empty, ErrorText, Facts, Footnote, Input, Pager, Segmented } from './components';
import { asPercent } from './format';

const DIMENSIONS: ReadonlyArray<readonly [CacheUsageDimension, string]> = [
  ['session', '会话'],
  ['node', '节点'],
  ['model', '模型'],
  ['channel', '渠道'],
];

export type CacheUsageRange = 'day' | 'week' | 'fortnight' | 'all';

const RANGES: ReadonlyArray<readonly [CacheUsageRange, string]> = [
  ['day', '最近 24 小时'],
  ['week', '最近 7 天'],
  ['fortnight', '最近 14 天'],
  ['all', '全部新记录'],
];

const RANGE_HOURS: Readonly<Record<CacheUsageRange, number | null>> = {
  day: 24,
  week: 24 * 7,
  fortnight: 24 * 14,
  all: null,
};

type BaselineChoice = 'legacy' | 'before';

const BASELINES: ReadonlyArray<readonly [BaselineChoice, string]> = [
  ['legacy', '升级前的旧记录'],
  ['before', '截止时间之前'],
];

const HOUR_MS = 3_600_000;
const PAGE = 20;

const METRICS: ReadonlyArray<{ label: string; cell: (stats: CacheUsageStats) => string }> = [
  { label: '总输入', cell: (stats) => tokenText(stats, stats.prompt_tokens) },
  { label: '命中', cell: (stats) => tokenText(stats, stats.cache_read_tokens) },
  { label: '未命中', cell: (stats) => tokenText(stats, stats.miss_tokens) },
  { label: '命中率', cell: rateText },
  { label: '无数据任务', cell: noDataText },
];

const SIDES = [['baseline', '基线'], ['current', '当前']] as const;

const TOOLBAR = 'flex flex-wrap items-center gap-2';
const TOOL_LABEL = 'w-12 flex-none text-xs text-[var(--duties-secondary)]';
const FRAME = 'mt-4 overflow-x-auto border border-[var(--duties-border)]';
const TABLE = 'w-full border-collapse text-xs tabular-nums';
const HEAD = 'border-b border-[var(--duties-border)] bg-[var(--duties-muted)] px-2 py-1.5 text-right font-medium';
const HEAD_NAME = 'border-b border-[var(--duties-border)] bg-[var(--duties-muted)] px-2 py-1.5 text-left font-medium';
const NAME = 'max-w-xs truncate border-t border-[var(--duties-border)] px-2 py-1.5 text-left';
// 基线压暗一档，并排看时一眼分得出哪一列是改动前
const CELL: Record<(typeof SIDES)[number][0], string> = {
  baseline: 'whitespace-nowrap border-t border-[var(--duties-border)] px-2 py-1.5 text-right text-[var(--duties-secondary)]',
  current: 'whitespace-nowrap border-t border-[var(--duties-border)] px-2 py-1.5 text-right',
};
const TOTAL_ROW = 'bg-[var(--duties-panel)] font-medium';

const tokens = (value: number): string => value.toLocaleString('zh-CN');

function tokenText(stats: CacheUsageStats, value: number): string {
  return stats.tasks > 0 ? tokens(value) : '—';
}

/** 一个带缓存数据的任务都没有时写「无数据」，不写 0%：那是渠道没报，不是没命中。 */
export function rateText(stats: CacheUsageStats): string {
  if (stats.hit_rate !== null) return `${asPercent(stats.hit_rate)}%`;
  return stats.no_data_tasks > 0 ? '无数据' : '—';
}

function noDataText(stats: CacheUsageStats): string {
  return stats.no_data_tasks > 0 ? tokens(stats.no_data_tasks) : '—';
}

/** datetime-local 的值按本地时间读，转成带时区的 ISO；读不出来是 null。 */
export function cutoffIso(value: string): string | null {
  if (!value) return null;
  const moment = new Date(value);
  return Number.isNaN(moment.getTime()) ? null : moment.toISOString();
}

/** 当前值从哪一刻算起。按截止时间取基线时不早于截止时间，基线和当前两段不重叠。 */
export function windowStart(range: CacheUsageRange, cutoff: string | null, now: number): string | undefined {
  const hours = RANGE_HOURS[range];
  const starts = [
    ...(hours === null ? [] : [now - hours * HOUR_MS]),
    ...(cutoff ? [Date.parse(cutoff)] : []),
  ];
  return starts.length ? new Date(Math.max(...starts)).toISOString() : undefined;
}

const localTime = (iso: string): string => new Date(iso).toLocaleString('zh-CN');

function baselineText(report: CacheUsageReport): string {
  if (report.baseline.mode === 'before' && report.baseline.before) {
    return `基线：${localTime(report.baseline.before)} 之前完成的新记录。`;
  }
  return '基线：升级前写的旧记录。它们没有完成时间，时间段筛不到，整体算作改动前。';
}

function windowText(report: CacheUsageReport): string {
  const { since, until } = report.window;
  if (since && until) return `当前：${localTime(since)} 到 ${localTime(until)} 之间完成的新记录。`;
  if (since) return `当前：${localTime(since)} 之后完成的新记录。`;
  if (until) return `当前：${localTime(until)} 之前完成的新记录。`;
  return '当前：全部带完成时间的新记录。';
}

function scanText(report: CacheUsageReport): string {
  const { scan } = report;
  const parts = [
    `读了 ${scan.files} 份转写，其中 ${scan.reparsed} 份有变化重新解析`,
    `重复的副本 ${scan.duplicates} 份，只算一次`,
  ];
  if (scan.without_usage) parts.push(`${scan.without_usage} 条记录没有输入用量，不计入`);
  if (scan.bad_lines) parts.push(`跳过 ${scan.bad_lines} 行读不懂的记录`);
  return `${parts.join('；')}。`;
}

const StatsCells = ({ row }: { row: Pick<CacheUsageRow, 'baseline' | 'current'> }) => (
  <>
    {METRICS.flatMap((metric) => SIDES.map(([side]) => (
      <td className={CELL[side]} key={`${metric.label}-${side}`}>{metric.cell(row[side])}</td>
    )))}
  </>
);

const UsageTable = ({ names, offset, report }: {
  names: (row: CacheUsageRow) => string;
  offset: number;
  report: CacheUsageReport;
}) => {
  const dimension = DIMENSIONS.find(([value]) => value === report.group_by)?.[1] || '名称';
  return (
    <div className={FRAME}>
      <table className={TABLE}>
        <caption className="sr-only">按{dimension}汇总的缓存命中率，每项指标左列是基线、右列是当前</caption>
        <thead>
          <tr>
            <th className={HEAD_NAME} rowSpan={2} scope="col">{dimension}</th>
            {METRICS.map((metric) => (
              <th className={HEAD} colSpan={SIDES.length} key={metric.label} scope="colgroup">{metric.label}</th>
            ))}
          </tr>
          <tr>
            {METRICS.flatMap((metric) => SIDES.map(([side, label]) => (
              <th className={HEAD} key={`${metric.label}-${side}`} scope="col">{label}</th>
            )))}
          </tr>
        </thead>
        <tbody>
          <tr className={TOTAL_ROW}>
            <th className={NAME} scope="row">合计</th>
            <StatsCells row={report.totals} />
          </tr>
          {report.rows.slice(offset, offset + PAGE).map((row) => (
            <tr key={row.key}>
              <th className={NAME} scope="row" title={row.key || undefined}>{names(row)}</th>
              <StatsCells row={row} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export function CacheUsagePage() {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [dimension, setDimension] = useState<CacheUsageDimension>('session');
  const [range, setRange] = useState<CacheUsageRange>('week');
  const [baseline, setBaseline] = useState<BaselineChoice>('legacy');
  const [cutoffInput, setCutoffInput] = useState('');
  const [reloads, setReloads] = useState(0);
  const [report, setReport] = useState<CacheUsageReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [offset, setOffset] = useState(0);

  const cutoff = baseline === 'before' ? cutoffIso(cutoffInput) : null;
  const awaitingCutoff = baseline === 'before' && cutoff === null;
  const requests = useRequestGuard(JSON.stringify([dimension, range, cutoff, awaitingCutoff, reloads]));

  useEffect(() => {
    if (!adminToken || awaitingCutoff) {
      // 上一次在途的请求已随换代作废，它的 finally 不会再回来收起「正在汇总」
      setLoading(false);
      return;
    }
    const ticket = requests.begin('report');
    setLoading(true);
    setError('');
    getCacheUsage(
      { groupBy: dimension, since: windowStart(range, cutoff, Date.now()), baselineBefore: cutoff || undefined },
      { token: adminToken, signal: ticket.signal },
    )
      .then((result) => {
        if (!ticket.isCurrent()) return;
        setReport(result);
        setOffset(0);
      })
      .catch((caught) => { if (ticket.isCurrent()) setError(errorMessage(caught)); })
      .finally(() => { if (ticket.isCurrent()) setLoading(false); });
  }, [requests, adminToken, awaitingCutoff, dimension, range, cutoff]);

  const sessionRows = useMemo(() => (report?.group_by === 'session' ? report.rows : []), [report]);
  const descriptors = useMemo(() => sessionRows.map((row) => ({
    scope: row.session?.conversation_key || row.key,
    owner: row.session?.owner ?? null,
    current_account: row.session?.current_account,
  })), [sessionRows]);
  const conversationNames = useConversationNames(descriptors);

  const rowName = (row: CacheUsageRow): string => {
    if (report?.group_by !== 'session') return row.key || '未知';
    if (row.session?.internal) return `内部任务 · ${row.key}`;
    return conversationNames.get(scopeIdentity(row.session?.conversation_key || row.key)) || row.key;
  };

  return (
    <Block hint="命中 / 总输入。总输入里含缓存读写，渠道没报缓存的任务单独计数，不算进比例。" title="提示缓存命中率">
      <div className="space-y-2">
        <div className={TOOLBAR}>
          <span className={TOOL_LABEL}>维度</span>
          <Segmented choices={DIMENSIONS} label="汇总维度" onPick={setDimension} size="compact" value={dimension} />
        </div>
        <div className={TOOLBAR}>
          <span className={TOOL_LABEL}>时间段</span>
          <Segmented choices={RANGES} label="时间段" onPick={setRange} size="compact" value={range} />
        </div>
        <div className={TOOLBAR}>
          <span className={TOOL_LABEL}>基线</span>
          <Segmented choices={BASELINES} label="基线" onPick={setBaseline} size="compact" value={baseline} />
          {baseline === 'before' && (
            <Input
              aria-label="基线截止时间"
              onChange={(event) => setCutoffInput(event.target.value)}
              type="datetime-local"
              value={cutoffInput}
            />
          )}
          <span className="flex-1" />
          <Button disabled={!adminToken || awaitingCutoff} loading={loading} onClick={() => setReloads((count) => count + 1)} size="sm" tone="quiet">
            刷新
          </Button>
        </div>
      </div>
      {awaitingCutoff && <Facts role="status">选好截止时间后，拿这个时间之前完成的新记录当基线。</Facts>}
      {error && <ErrorText role="alert">{error}</ErrorText>}
      {loading && <Facts role="status">正在汇总…</Facts>}
      {report && (
        <>
          <Facts>{baselineText(report)}{windowText(report)}</Facts>
          {report.rows.length ? (
            <>
              <UsageTable names={rowName} offset={offset} report={report} />
              <Pager offset={offset} onOffset={setOffset} pageSize={PAGE} total={report.rows.length} unit="行" />
            </>
          ) : (
            <Empty>这段时间里没有带用量的任务记录。</Empty>
          )}
          <Facts>{scanText(report)}</Facts>
        </>
      )}
      <Footnote>
        「无数据」是渠道没报缓存字段的任务，不当成 0% 命中。升级前的旧记录没命中时不写这一项：同一会话里有记录报过这一项的（升级后报的 0 也算），缺的按 0 算；从没报过的算无数据。
        按会话汇总时，子代理和入口分支的用量算在它所属的会话名下；只看某个节点用「节点」维度。
      </Footnote>
    </Block>
  );
}

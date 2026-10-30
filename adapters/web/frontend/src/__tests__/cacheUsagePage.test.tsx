// 缓存命中页：基线和当前并排、「无数据」不写成 0%、切维度和时间段会重新汇总。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CacheUsageReport, CacheUsageRow, CacheUsageStats } from '../api/cacheUsage';
import { CacheUsagePage, cutoffIso, rateText, windowStart } from '../console/CacheUsagePage';
import { useSettingsStore } from '../store/settingsStore';

const HOUR = 3_600_000;

const stats = (over: Partial<CacheUsageStats> = {}): CacheUsageStats => ({
  tasks: 0,
  prompt_tokens: 0,
  cache_read_tokens: 0,
  miss_tokens: 0,
  hit_rate: null,
  no_data_tasks: 0,
  no_data_prompt_tokens: 0,
  ...over,
});

const session = (conversationKey: string) => ({
  conversation_key: conversationKey, owner: null, current_account: true, internal: false,
});

const SESSION_ROWS: CacheUsageRow[] = [
  {
    key: 'sess-1',
    current: stats({ tasks: 3, prompt_tokens: 6000, cache_read_tokens: 4500, miss_tokens: 1500, hit_rate: 0.75 }),
    baseline: stats({ tasks: 2, prompt_tokens: 4000, cache_read_tokens: 1000, miss_tokens: 3000, hit_rate: 0.25 }),
    session: session('web:console'),
  },
  {
    key: 'sess-2',
    current: stats(),
    baseline: stats({ no_data_tasks: 1, no_data_prompt_tokens: 900 }),
    session: session(''),
  },
];

const NODE_ROWS: CacheUsageRow[] = [
  {
    key: 'qq.orchestrator',
    current: stats({ tasks: 1, prompt_tokens: 2000, cache_read_tokens: 1000, miss_tokens: 1000, hit_rate: 0.5 }),
    baseline: stats(),
  },
  {
    key: '',
    current: stats(),
    baseline: stats({ tasks: 1, prompt_tokens: 1000, cache_read_tokens: 0, miss_tokens: 1000, hit_rate: 0 }),
  },
];

function reportFor(groupBy: CacheUsageReport['group_by'], params: URLSearchParams): CacheUsageReport {
  const rows = groupBy === 'session' ? SESSION_ROWS : NODE_ROWS;
  return {
    group_by: groupBy,
    window: { since: params.get('since'), until: params.get('until') },
    baseline: params.get('baseline_before')
      ? { mode: 'before', before: params.get('baseline_before') }
      : { mode: 'legacy', before: null },
    totals: {
      current: stats({ tasks: 3, prompt_tokens: 6000, cache_read_tokens: 4500, miss_tokens: 1500, hit_rate: 0.75 }),
      baseline: stats({
        tasks: 2, prompt_tokens: 4000, cache_read_tokens: 1000, miss_tokens: 3000, hit_rate: 0.25,
        no_data_tasks: 1, no_data_prompt_tokens: 900,
      }),
    },
    rows,
    scan: { files: 3, reparsed: 1, bad_lines: 2, records: 5, segments: 4, duplicates: 1, without_usage: 0 },
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

let fetchMock: ReturnType<typeof vi.fn>;

const requested = (index: number): URLSearchParams => {
  const url = new URL(String(fetchMock.mock.calls[index][0]), window.location.href);
  expect(url.pathname).toBe('/v1/admin/usage/cache');
  return url.searchParams;
};

const lastRequest = (): URLSearchParams => requested(fetchMock.mock.calls.length - 1);

const cellsOf = (name: string): string[] => {
  const row = screen.getByRole('rowheader', { name }).closest('tr')!;
  return within(row).getAllByRole('cell').map((cell) => cell.textContent || '');
};

beforeEach(() => {
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const params = new URL(String(input), window.location.href).searchParams;
    return json(reportFor((params.get('group_by') || 'session') as CacheUsageReport['group_by'], params));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('缓存命中页', () => {
  it('合计和每行都是基线、当前两列并排', async () => {
    render(<CacheUsagePage />);

    await screen.findByRole('table');
    expect(cellsOf('合计')).toEqual(['4,000', '6,000', '1,000', '4,500', '3,000', '1,500', '25%', '75%', '1', '—']);
    expect(screen.getAllByRole('columnheader', { name: '基线' })).toHaveLength(5);
    expect(screen.getAllByRole('columnheader', { name: '当前' })).toHaveLength(5);
    expect(screen.getByText(/基线：升级前写的旧记录/)).toBeInTheDocument();
    expect(screen.getByText(/2 行读不懂的记录/)).toBeInTheDocument();
  });

  it('渠道没报缓存的写「无数据」，不写 0%', async () => {
    render(<CacheUsagePage />);

    await screen.findByRole('table');
    const row = SESSION_ROWS[1];
    const name = screen.getByRole('rowheader', { name: /会话（名称暂不可用）/ });
    expect(name).toHaveAttribute('title', row.key);
    const cells = within(name.closest('tr')!).getAllByRole('cell').map((cell) => cell.textContent);
    expect(cells).toEqual(['—', '—', '—', '—', '—', '—', '无数据', '—', '1', '—']);
    expect(cells).not.toContain('0%');
  });

  it('按会话汇总时用会话名，带着管理令牌去取', async () => {
    render(<CacheUsagePage />);

    expect(await screen.findByRole('rowheader', { name: '控制台会话（非全局）' })).toBeInTheDocument();
    expect(requested(0).get('group_by')).toBe('session');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer admin-token');
  });

  it('切到节点维度重新汇总，没有名字的老记录写「未知」', async () => {
    render(<CacheUsagePage />);
    await screen.findByRole('table');

    fireEvent.click(screen.getByRole('button', { name: '节点' }));

    expect(await screen.findByRole('rowheader', { name: 'qq.orchestrator' })).toBeInTheDocument();
    expect(lastRequest().get('group_by')).toBe('node');
    expect(cellsOf('未知')).toEqual(['1,000', '—', '0', '—', '1,000', '—', '0%', '—', '—', '—']);
    expect(screen.getByRole('button', { name: '节点' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('时间段换成 since 参数，全部新记录不带它', async () => {
    render(<CacheUsagePage />);
    await screen.findByRole('table');
    const since = Date.parse(requested(0).get('since') || '');
    expect(Math.abs(since - (Date.now() - 7 * 24 * HOUR))).toBeLessThan(60_000);

    fireEvent.click(screen.getByRole('button', { name: '全部新记录' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(lastRequest().has('since')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '最近 24 小时' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(Math.abs(Date.parse(lastRequest().get('since') || '') - (Date.now() - 24 * HOUR))).toBeLessThan(60_000);
  });

  it('按截止时间取基线：选好时间才去取，当前值从截止时间算起', async () => {
    render(<CacheUsagePage />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '全部新记录' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: '截止时间之前' }));
    expect(screen.getByText(/选好截止时间后/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fireEvent.change(screen.getByLabelText('基线截止时间'), { target: { value: '2026-10-20T08:00' } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const expected = new Date('2026-10-20T08:00').toISOString();
    expect(lastRequest().get('baseline_before')).toBe(expected);
    expect(lastRequest().get('since')).toBe(expected);
    expect(await screen.findByText(/之前完成的新记录/)).toBeInTheDocument();
  });

  it('取不到时把原因报出来', async () => {
    fetchMock.mockImplementationOnce(async () => json({ detail: 'group_by 只能是 session、node、model、channel 之一' }, 400));

    render(<CacheUsagePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('group_by 只能是');
  });

  it('一行都没有时说清楚，不画空表', async () => {
    fetchMock.mockImplementationOnce(async (input: RequestInfo | URL) => json({
      ...reportFor('session', new URL(String(input), window.location.href).searchParams),
      rows: [],
    }));

    render(<CacheUsagePage />);

    expect(await screen.findByText('这段时间里没有带用量的任务记录。')).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });
});

describe('缓存命中页的换算', () => {
  it('rateText 只在真有数据时给百分比', () => {
    expect(rateText(stats({ tasks: 1, prompt_tokens: 3, cache_read_tokens: 1, hit_rate: 1 / 3 }))).toBe('33.33%');
    expect(rateText(stats({ tasks: 1, prompt_tokens: 3, hit_rate: 0 }))).toBe('0%');
    expect(rateText(stats({ no_data_tasks: 2 }))).toBe('无数据');
    expect(rateText(stats())).toBe('—');
  });

  it('cutoffIso 认本地时间，认不出的是 null', () => {
    expect(cutoffIso('2026-10-20T08:00')).toBe(new Date('2026-10-20T08:00').toISOString());
    expect(cutoffIso('')).toBeNull();
    expect(cutoffIso('not a time')).toBeNull();
  });

  it('windowStart 取时间段起点和截止时间里更晚的那个', () => {
    const now = Date.parse('2026-10-27T00:00:00Z');
    expect(windowStart('day', null, now)).toBe('2026-10-26T00:00:00.000Z');
    expect(windowStart('all', null, now)).toBeUndefined();
    expect(windowStart('week', '2026-10-25T00:00:00.000Z', now)).toBe('2026-10-25T00:00:00.000Z');
    expect(windowStart('week', '2026-10-01T00:00:00.000Z', now)).toBe('2026-10-20T00:00:00.000Z');
    expect(windowStart('all', '2026-10-01T00:00:00.000Z', now)).toBe('2026-10-01T00:00:00.000Z');
  });
});

describe('缓存命中页读不懂报告时', () => {
  // 缺一格补成 0 的话，「无数据」就成了 0% 命中。
  it('说没读成，不画表', async () => {
    fetchMock.mockImplementation(async () => {
      const report = reportFor('session', new URLSearchParams());
      return json({ ...report, totals: { ...report.totals, baseline: { ...report.totals.baseline, hit_rate: undefined } } });
    });
    render(<CacheUsagePage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('缓存命中汇总格式不对：缺 totals.baseline.hit_rate');
    expect(screen.queryByRole('table')).toBeNull();
  });
});

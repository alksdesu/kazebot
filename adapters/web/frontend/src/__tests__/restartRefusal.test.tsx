// scheduled=false 报成一句「已请求重启」会让人白等；两个入口都得把原因说出来。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RestartResult } from '../api/supervisorClient';
import { SystemSettingsPage } from '../components/settings/pages/SystemSettingsPage';
import { RestartEngine } from '../console/LinkFacts';
import { useSettingsStore } from '../store/settingsStore';
import { restartRefusal } from '../utils/systemStatus';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const refused = (reason?: string | null): RestartResult => ({ scheduled: false, target: 'engine', reason });

// 期望值跟着同一个格式器走，写死时分秒会随跑测试的机器所在时区变。
const clock = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const at = (since: string) => clock.format(new Date(since));

const SINCE = '2026-09-01T10:11:12+00:00';

const blockedBy = (target: string, since = SINCE): RestartResult => ({
  scheduled: false,
  target: 'engine',
  reason: 'restart already in progress',
  in_progress: { target, since },
});

// 系统页要先拿到这两份状态才渲染得出重启按钮。
const stubFetch = (restart: RestartResult) => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/v1/admin/state')) {
      return jsonResponse({ sessions: 1, approvals: { pending: 0 }, tasks: { running: 0 }, pending_approvals: [] });
    }
    if (url.endsWith('/v1/health')) return jsonResponse({ status: 'ok', uptime_seconds: 60 });
    if (url.endsWith('/v1/admin/restart')) return jsonResponse(restart);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
};

describe('restartRefusal', () => {
  it('把后端的原因常量换成运营者看得懂的话', () => {
    expect(restartRefusal(refused('no process manager'))).toBe('调度器没有在管理引擎进程');
    expect(restartRefusal(refused('restart already in progress'))).toBe('上一次重启还没收尾，稍后再试');
  });

  it('原因缺失或不认识时仍给一句完整的话', () => {
    // 后端新加一个原因常量时前端只会落后一句措辞，不该显示成「未能重启：undefined」。
    expect(restartRefusal(refused('engine is on fire'))).toBe('调度器没有排上这次重启');
    expect(restartRefusal(refused())).toBe('调度器没有排上这次重启');
    expect(restartRefusal(refused(null))).toBe('调度器没有排上这次重启');
    expect(restartRefusal(undefined)).toBe('调度器没有排上这次重启');
  });

  it('说得出挡住这次的是哪种重启、从几点开始', () => {
    // 只说「上一次重启还没收尾」的话，运营者不知道该等一次引擎重启还是一次全量重启。
    expect(restartRefusal(blockedBy('engine'))).toBe(`上一次引擎重启（${at(SINCE)} 开始）还没收尾，稍后再试`);
    expect(restartRefusal(blockedBy('all'))).toBe(`上一次全量重启（${at(SINCE)} 开始）还没收尾，稍后再试`);
  });

  it('没见过的重启目标也说得出时间', () => {
    expect(restartRefusal(blockedBy('shell'))).toBe(`上一次重启（${at(SINCE)} 开始）还没收尾，稍后再试`);
  });

  it('时间读不出来时退回按原因说', () => {
    // 后端换了时间格式的话宁可少一句「几点开始」，也不能把 Invalid Date 显示给人看。
    expect(restartRefusal(blockedBy('engine', 'not a time'))).toBe('上一次重启还没收尾，稍后再试');
    expect(restartRefusal(blockedBy('engine', ''))).toBe('上一次重启还没收尾，稍后再试');
  });

  it('没有正在跑的那次时行为不变', () => {
    expect(restartRefusal({ ...refused('restart already in progress'), in_progress: null }))
      .toBe('上一次重启还没收尾，稍后再试');
    expect(restartRefusal({ ...refused('no process manager'), in_progress: null }))
      .toBe('调度器没有在管理引擎进程');
  });
});

describe('被拒的重启在界面上', () => {
  beforeEach(() => {
    localStorage.clear();
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false });
  });

  it('系统设置页说清是上一次重启还没收尾', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch(refused('restart already in progress'));

    render(<SystemSettingsPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/state', expect.anything()));
    fireEvent.click(screen.getByRole('button', { name: '重启引擎' }));

    expect(await screen.findByRole('status')).toHaveTextContent('未能重启：上一次重启还没收尾，稍后再试');
  });

  it('系统设置页把正在跑的那次和它的开始时间一起说出来', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch(blockedBy('all'));

    render(<SystemSettingsPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/state', expect.anything()));
    fireEvent.click(screen.getByRole('button', { name: '重启引擎' }));

    expect(await screen.findByRole('status'))
      .toHaveTextContent(`未能重启：上一次全量重启（${at(SINCE)} 开始）还没收尾，稍后再试`);
  });

  it('系统设置页碰上缺字段的回复不当成排上了', async () => {
    // 判据是「说排上了才算」，不是「说没排上才算没排」：缺字段的 200 也要照实说。
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch({ target: 'engine' } as RestartResult);

    render(<SystemSettingsPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/state', expect.anything()));
    fireEvent.click(screen.getByRole('button', { name: '重启引擎' }));

    expect(await screen.findByRole('status')).toHaveTextContent('未能重启：调度器没有排上这次重启');
  });

  it('系统设置页排上了就不提原因', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch({ scheduled: true, target: 'engine' });

    render(<SystemSettingsPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/state', expect.anything()));
    fireEvent.click(screen.getByRole('button', { name: '重启引擎' }));

    expect(await screen.findByRole('status')).toHaveTextContent('已请求重启引擎');
  });

  it('控制台说清调度器没在管引擎', async () => {
    stubFetch(refused('no process manager'));

    render(<RestartEngine />);
    fireEvent.click(screen.getByRole('button', { name: '重启' }));
    fireEvent.click(screen.getByRole('button', { name: '确认重启' }));

    expect(await screen.findByText('没能重启：调度器没有在管理引擎进程。')).toBeInTheDocument();
  });

  it('控制台碰上没见过的原因也不说空话', async () => {
    stubFetch(refused('engine is on fire'));

    render(<RestartEngine />);
    fireEvent.click(screen.getByRole('button', { name: '重启' }));
    fireEvent.click(screen.getByRole('button', { name: '确认重启' }));

    expect(await screen.findByText('没能重启：调度器没有排上这次重启。')).toBeInTheDocument();
  });

  it('控制台排上了照旧报成功', async () => {
    stubFetch({ scheduled: true, target: 'engine' });

    render(<RestartEngine />);
    fireEvent.click(screen.getByRole('button', { name: '重启' }));
    fireEvent.click(screen.getByRole('button', { name: '确认重启' }));

    expect(await screen.findByText('已排上重启，几秒后 worker 会重新起来。')).toBeInTheDocument();
  });
});

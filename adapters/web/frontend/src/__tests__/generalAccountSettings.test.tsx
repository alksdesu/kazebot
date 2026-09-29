import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { GeneralSettingsPage } from '../components/settings/pages/GeneralSettingsPage';
import { platformConsoleStore } from '../console/consoleStore';
import { adoptPlatforms, QQ_NAPCAT, resetPlatforms } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  checkHealth: vi.fn(), getProviders: vi.fn(),
  getPlatformState: vi.fn(), getPlatformRaw: vi.fn(),
  getInstances: vi.fn(), getPlatformAccount: vi.fn(), getAccountScope: vi.fn(), getShareGroups: vi.fn(),
}));

const account: api.AccountStatus = {
  configured: true, reachable: true, is_login: true, online: true, account: '1000001', nick: '当前机器人',
};
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
};

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  window.history.replaceState(null, '', '/web/?view=settings&tab=general&section=account');
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'general', activeSettingsSection: 'account' });
  useSettingsStore.setState({ adminToken: 'current-token', isAuthenticated: true, isConnected: true });
  resetPlatforms();
  adoptPlatforms([QQ_NAPCAT], 'qq');
  platformConsoleStore('qq').setState({ live: null, draft: {}, raw: '', loading: false, loadError: '' });
  vi.mocked(api.checkHealth).mockResolvedValue({ status: 'ok' });
  vi.mocked(api.getProviders).mockResolvedValue({
    active_provider: '', providers: {}, fallbacks: [], node_fallbacks: {}, registered: [], reserved_names: [],
  });
  vi.mocked(api.getPlatformState).mockResolvedValue({ published: false, file: { exists: false, mtime_ns: 0, size: 0 } });
  vi.mocked(api.getPlatformRaw).mockResolvedValue({ content: '', hash: '', exists: false });
  vi.mocked(api.getInstances).mockResolvedValue([
    { id: 'main', label: '主实例', path: '', current: true, idx: 0, platform: 'qq', account: '1000001' },
  ]);
  vi.mocked(api.getPlatformAccount).mockResolvedValue(account);
  vi.mocked(api.getAccountScope).mockResolvedValue({
    current_scope: '1000001', live_scope: '', bot_alive: false, preview: null, ambiguous_sources: [],
  });
  vi.mocked(api.getShareGroups).mockResolvedValue({ available: true, reason: '', current: null, groups: [], archives: [] });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  resetPlatforms();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('通用设置中的账号与连接', () => {
  it('深链直接展开管理区，bot 未上报时仍可管理实例、共享配置与账号', async () => {
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('当前机器人')).toBeVisible();
    expect(screen.getByRole('button', { name: '账号与连接' })).toHaveAttribute('aria-expanded', 'true');
    for (const name of ['多开实例', '共享配置', '机器人账号', '快速切换', '记忆归属']) {
      expect(screen.getByRole('heading', { name })).toBeVisible();
    }
    expect(api.getPlatformAccount).toHaveBeenCalledWith('current-token', 'qq');
    expect(api.getAccountScope).toHaveBeenCalledWith('current-token', 'qq', '', undefined);
    expect(api.getInstances).toHaveBeenCalledTimes(1);
    expect(api.getShareGroups).toHaveBeenCalledTimes(1);
  });

  it('尚未确认的平台不会使用默认 QQ 发账号请求，清单到达后继续打开深链', async () => {
    resetPlatforms();
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('正在读取当前实例的平台…')).toBeVisible();
    expect(api.getPlatformAccount).not.toHaveBeenCalled();
    await act(async () => { adoptPlatforms([QQ_NAPCAT], 'qq'); });
    expect(await screen.findByText('当前机器人')).toBeVisible();
    expect(api.getPlatformAccount).toHaveBeenCalledWith('current-token', 'qq');
  });

  it('旧后端只公布一个平台时仍可管理当前实例', async () => {
    adoptPlatforms([QQ_NAPCAT]);
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('当前机器人')).toBeVisible();
    expect(api.getPlatformAccount).toHaveBeenCalledWith('current-token', 'qq');
  });

  it('平台清单有多项但未指定当前实例平台时不猜测账号归属', async () => {
    adoptPlatforms([QQ_NAPCAT, { ...QQ_NAPCAT, id: 'other' }]);
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('无法确定当前实例的平台，请刷新页面后重试。')).toBeVisible();
    expect(api.getPlatformAccount).not.toHaveBeenCalled();
    expect(api.getInstances).not.toHaveBeenCalled();
  });

  it.each(['missing', 'other'])('当前平台为 %s 时不会误操作 QQ 账号', async current => {
    adoptPlatforms([QQ_NAPCAT, { ...QQ_NAPCAT, id: 'other', shortName: '其他平台' }], current);
    render(<GeneralSettingsPage />);
    expect(await screen.findByText(current === 'missing'
      ? '无法确定当前实例的平台，请刷新页面后重试。'
      : '当前实例的平台尚未提供账号管理界面。')).toBeVisible();
    expect(api.getInstances).not.toHaveBeenCalled();
    expect(api.getPlatformAccount).not.toHaveBeenCalled();
    expect(api.getAccountScope).not.toHaveBeenCalled();
  });

  it('令牌尚未通过认证时不挂载管理区或发出账号请求', async () => {
    useSettingsStore.setState({ isAuthenticated: false });
    render(<GeneralSettingsPage />);
    await act(async () => {});
    expect(screen.queryByRole('button', { name: '账号与连接' })).not.toBeInTheDocument();
    expect(api.getInstances).not.toHaveBeenCalled();
    expect(api.getPlatformAccount).not.toHaveBeenCalled();
    expect(api.getShareGroups).not.toHaveBeenCalled();
  });

  it('退出后重新登录，旧令牌迟到的账号响应不能回填到新页面', async () => {
    const old = deferred<api.AccountStatus>();
    vi.mocked(api.getPlatformAccount).mockReturnValueOnce(old.promise);
    render(<GeneralSettingsPage />);
    await act(async () => {});
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    expect(screen.queryByRole('button', { name: '账号与连接' })).not.toBeInTheDocument();
    await act(async () => { useSettingsStore.setState({ adminToken: 'next-token', isAuthenticated: true }); });
    expect(await screen.findByText('当前机器人')).toBeVisible();
    await act(async () => { old.resolve({ ...account, nick: '旧身份账号' }); });
    expect(screen.queryByText('旧身份账号')).not.toBeInTheDocument();
    expect(api.getPlatformAccount).toHaveBeenLastCalledWith('next-token', 'qq');
  });

  it('离开通用设置后停止账号状态轮询', async () => {
    vi.useFakeTimers();
    vi.mocked(api.getPlatformAccount).mockResolvedValue({ configured: true, reachable: false });
    const page = render(<GeneralSettingsPage />);
    await act(async () => {});
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(3000); });
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
    page.unmount();
    await act(async () => { vi.advanceTimersByTime(30000); });
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
  });
});

// 账号页的提示。这一页三秒一次地重读状态，换号和扫码的进展不能被它冲掉。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { AccountWorkspaceSlot } from '../console/AccountWorkspace';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getInstances: vi.fn(), getPlatformAccount: vi.fn(), getPlatformLoginQrcode: vi.fn(),
  getAccountScope: vi.fn(), enterPlatformLoginMode: vi.fn(), switchPlatformAccount: vi.fn(),
  pinPlatformAccount: vi.fn(),
}));
vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn(async () => 'data:image/png;base64,QR') } }));

const SCAN_HINT = '用要登录的那个 QQ 扫码。二维码会过期，过期就点一下「刷新二维码」。';

const instances: api.ConsoleInstance[] = [
  { id: 'main', label: '主实例', path: '', current: true, idx: 0, accounts: { qq: '1000001' } },
];
const online: api.AccountStatus = {
  configured: true, reachable: true, is_login: true, online: true, account: '1000001', nick: '当前机器人',
  quick_login: [{ account: '1000001', nick: '本号' }, { account: '2000002', nick: '备用号' }],
};
const waitingScan: api.AccountStatus = { configured: true, reachable: true, is_login: false, quick_login: [] };

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(api.getInstances).mockResolvedValue(instances);
  vi.mocked(api.getPlatformAccount).mockResolvedValue(online);
  vi.mocked(api.getPlatformLoginQrcode).mockResolvedValue({ qrcode: 'QRSTR', reachable: true });
  vi.mocked(api.getAccountScope).mockResolvedValue({
    current_scope: '1000001', live_scope: '1000001', bot_alive: false, preview: null, ambiguous_sources: [],
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unexpected request')));
  window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account');
  // 轮询在状态读回来之后才挂上，假定时器得先于 render 接管才推得动它。
  vi.useFakeTimers();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const mount = async () => { await act(async () => { render(<AccountWorkspaceSlot />); }); };
const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(3000); });
const click = (name: string) => act(async () => {
  fireEvent.click(screen.getByRole('button', { name }));
});

describe('账号页的提示', () => {
  it('首次读回来之前说的是正在读取', async () => {
    vi.mocked(api.getPlatformAccount).mockReturnValue(new Promise(() => {}));
    await mount();

    expect(screen.getByText('正在读取…')).toBeInTheDocument();
  });

  it('快速切换失败的原因熬得过紧接着那次重读，死号照样置灰', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.switchPlatformAccount).mockRejectedValue(new Error('这个号的登录态已经失效了。'));
    vi.mocked(api.getPlatformAccount)
      .mockResolvedValueOnce(online)
      .mockResolvedValue({
        ...online,
        quick_login: [{ account: '1000001', nick: '本号' }, { account: '2000002', nick: '备用号', available: false }],
      });
    await mount();

    await click('切到这个号');

    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
    expect(screen.getByText('这个号的登录态已经失效了。')).toBeInTheDocument();
    expect(screen.getByText('登录态已失效，只能扫码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '切到这个号' })).toBeDisabled();
  });

  it('手动刷新把换号失败的原因和读取失败的原因一起清掉', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.switchPlatformAccount).mockRejectedValue(new Error('这个号的登录态已经失效了。'));
    vi.mocked(api.getPlatformAccount)
      .mockResolvedValueOnce(online)
      .mockRejectedValueOnce(new Error('读账号状态超时。'))
      .mockResolvedValue(online);
    await mount();

    await click('切到这个号');
    expect(screen.getByText('这个号的登录态已经失效了。读账号状态超时。')).toBeInTheDocument();

    await click('刷新账号状态');
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(3);
    expect(screen.queryByText(/这个号的登录态已经失效了/)).not.toBeInTheDocument();
    expect(screen.queryByText(/读账号状态超时/)).not.toBeInTheDocument();
  });

  it('没配 WebUI token 那一屏的刷新也是手动重读', async () => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue({ configured: false });
    await mount();

    await click('刷新账号状态');

    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
  });

  // 走「换个号登录」进扫码：只有这条路记下了等待起点，超时判据才算得准。
  const intoScanning = async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    await mount();
    await click('换个号登录');
    await tick();
  };

  it('扫码那句熬得过后面几轮轮询', async () => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingScan);
    await intoScanning();
    expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();

    await tick();
    await tick();
    expect(api.getPlatformLoginQrcode).toHaveBeenCalledTimes(1);
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
  });

  it('停止等待只清进展，读取失败的原因还留着', async () => {
    vi.mocked(api.getPlatformAccount)
      .mockResolvedValueOnce(waitingScan)
      .mockResolvedValueOnce(waitingScan)
      .mockRejectedValue(new Error('读账号状态超时。'));
    await intoScanning();

    await tick();
    expect(screen.getByText(`${SCAN_HINT}读账号状态超时。`)).toBeInTheDocument();

    await click('停止等待');

    expect(screen.queryByText(new RegExp(SCAN_HINT.slice(0, 8)))).not.toBeInTheDocument();
    expect(screen.getByText('读账号状态超时。')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '扫码登录' })).not.toBeInTheDocument();
  });
});

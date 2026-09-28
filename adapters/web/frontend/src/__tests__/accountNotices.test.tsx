// 账号页的提示。这一页三秒一次地重读状态，换号和扫码的进展不能被它冲掉。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { AccountWorkspaceSlot } from '../console/AccountWorkspace';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getInstances: vi.fn(), getPlatformAccount: vi.fn(), getPlatformLoginQrcode: vi.fn(),
  getAccountScope: vi.fn(), migrateAccountScope: vi.fn(), enterPlatformLoginMode: vi.fn(),
  switchPlatformAccount: vi.fn(), pinPlatformAccount: vi.fn(),
}));
vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn(async () => 'data:image/png;base64,QR') } }));

const SCAN_HINT = '用要登录的那个 QQ 扫码。二维码会过期，过期就点一下「刷新二维码」。';
const WAITING_HINT = 'NapCat 还没应答，等它起来…';
const TIMEOUT_HINT = '等待超时。NapCat 可能没起来，去服务器上看看容器状态，好了再点「刷新账号状态」。';
const PAUSED_HINT = '已停止等待。要接着扫码，点「刷新账号状态」。';
const READ_FAILED = '读账号状态超时。';
const MIGRATED = '已搬迁 2 份记忆、3 个会话。备份在 /srv/backup/scope-1';
const SCOPE_FAILED = '读归属超时。';
// 重启超时阈值 180 秒，一轮轮询 3 秒，65 轮稳稳跨过去。
const ROUNDS_PAST_TIMEOUT = 65;

const instances: api.ConsoleInstance[] = [
  { id: 'main', label: '主实例', path: '', current: true, idx: 0, accounts: { qq: '1000001' } },
];
const online: api.AccountStatus = {
  configured: true, reachable: true, is_login: true, online: true, account: '1000001', nick: '当前机器人',
  quick_login: [{ account: '1000001', nick: '本号' }, { account: '2000002', nick: '备用号' }],
};
const waitingScan: api.AccountStatus = { configured: true, reachable: true, is_login: false, quick_login: [] };
const waitingWithBackup: api.AccountStatus = {
  configured: true, reachable: true, is_login: false, quick_login: [{ account: '2000002', nick: '备用号' }],
};
const napcatDown: api.AccountStatus = { configured: true, reachable: false, is_login: false, quick_login: [] };
const scopeIdle: api.ScopeStatus = {
  current_scope: '1000001', live_scope: '1000001', bot_alive: false, preview: null, ambiguous_sources: [],
};
const scopePlan: api.ScopeStatus = {
  ...scopeIdle,
  preview: {
    source_scope: '1000001',
    target_scope: '2000002',
    conversations: [{ old_namespace: 'ns-1000001-g', new_namespace: 'ns-2000002-g', has_memory: true, blocked: false }],
    unknown_namespaces: [], unresolved_namespaces: [], other_scopes: [], stores: {},
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(api.getInstances).mockResolvedValue(instances);
  vi.mocked(api.getPlatformAccount).mockResolvedValue(online);
  vi.mocked(api.getPlatformLoginQrcode).mockResolvedValue({ qrcode: 'QRSTR', reachable: true });
  vi.mocked(api.getAccountScope).mockResolvedValue(scopeIdle);
  vi.mocked(api.migrateAccountScope).mockResolvedValue({
    moved_memory_dirs: 2, sessions_changed: 3, backup_dir: '/srv/backup/scope-1',
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unexpected request')));
  window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account');
  // 轮询在状态读回来之后才挂上，假定时器得先于 render 接管才推得动它。
  vi.useFakeTimers();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const mount = async () => { await act(async () => { render(<AccountWorkspaceSlot />); }); };
const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(3000); });
const tickRounds = async (rounds: number) => { for (let round = 0; round < rounds; round += 1) await tick(); };
const tickUntil = async (text: string) => {
  for (let round = 0; round < ROUNDS_PAST_TIMEOUT && !screen.queryByText(text); round += 1) await tick();
};
const click = (name: string) => act(async () => {
  fireEvent.click(screen.getByRole('button', { name }));
});
const fillTarget = (account: string) => act(async () => {
  fireEvent.change(screen.getByLabelText('搬到'), { target: { value: account } });
});

// 号配好了但没登录，watching 会自己盯着，第一轮轮询就把二维码取回来。
const intoScanning = async () => { await mount(); await tick(); };

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
      .mockRejectedValueOnce(new Error(READ_FAILED))
      .mockResolvedValue(online);
    await mount();

    await click('切到这个号');
    expect(screen.getByText(`这个号的登录态已经失效了。${READ_FAILED}`)).toBeInTheDocument();

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

  it('扫码扫过三分钟也不算超时，扫上了就报已登录', async () => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingScan);
    await intoScanning();

    await tickRounds(ROUNDS_PAST_TIMEOUT);
    expect(screen.queryByText(TIMEOUT_HINT)).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
    expect(api.getPlatformLoginQrcode).toHaveBeenCalledTimes(1);

    vi.mocked(api.getPlatformAccount).mockResolvedValue(online);
    await tick();

    expect(screen.getByText('已登录 当前机器人，并设为重启后自动登录。')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
  });

  it('谁也没点，光靠自己盯着进的扫码，每一轮都不会被判超时', async () => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingScan);
    await intoScanning();

    for (let round = 0; round < ROUNDS_PAST_TIMEOUT; round += 1) {
      await tick();
      expect(screen.queryByText(TIMEOUT_HINT)).not.toBeInTheDocument();
      expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
      // 换号键只在 stage 不是 idle 时锁着，锁着就说明没被超时打回去。
      expect(screen.getByRole('button', { name: '换个号登录' })).toBeDisabled();
    }
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
  });

  it('换号后 NapCat 一直不应答，超过三分钟就不再等了', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(napcatDown);
    await mount();

    await click('换个号登录');
    await tick();
    expect(screen.getByText(WAITING_HINT)).toBeInTheDocument();

    await tickUntil(TIMEOUT_HINT);
    const reads = vi.mocked(api.getPlatformAccount).mock.calls.length;
    await tickRounds(3);

    expect(screen.getByText(TIMEOUT_HINT)).toBeInTheDocument();
    expect(screen.queryByText(WAITING_HINT)).not.toBeInTheDocument();
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(reads);
    expect(screen.getByRole('button', { name: '换个号登录' })).toBeEnabled();
    expect(screen.queryByRole('heading', { name: '扫码登录' })).not.toBeInTheDocument();
    expect(api.getPlatformLoginQrcode).not.toHaveBeenCalled();
  });

  it('等 NapCat 起来那句只念一遍', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(napcatDown);
    await mount();

    await click('换个号登录');
    await tick();

    expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
    const spoken = screen.queryAllByText(WAITING_HINT);
    expect(spoken).toHaveLength(1);
    expect(screen.getByRole('heading', { name: '扫码登录' }).closest('section')).toContainElement(spoken[0]);
  });

  it('那次读取挂住了照样超时，挂着的读回来也不落地', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(napcatDown);
    await mount();
    await click('换个号登录');

    // 换号后这次读取一直吊着不回来，超时只能靠时钟自己判，不能等它。
    let land: (next: api.AccountStatus) => void = () => {};
    const hung = new Promise<api.AccountStatus>((resolve) => { land = resolve; });
    vi.mocked(api.getPlatformAccount).mockReturnValue(hung);
    await tickRounds(ROUNDS_PAST_TIMEOUT);

    expect(screen.getByText(TIMEOUT_HINT)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '换个号登录' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();

    land(online);
    await tick();

    expect(screen.getByText(TIMEOUT_HINT)).toBeInTheDocument();
    expect(screen.getByText('NapCat 没应答，容器可能正在重启')).toBeInTheDocument();
    expect(screen.getByText('未登录')).toBeInTheDocument();
    expect(screen.queryByText('当前机器人')).not.toBeInTheDocument();
  });

  it('等待超时之后点刷新账号状态，又自己盯着了', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(napcatDown);
    await mount();
    await click('换个号登录');
    await tickUntil(TIMEOUT_HINT);

    await click('刷新账号状态');
    const reads = vi.mocked(api.getPlatformAccount).mock.calls.length;
    await tick();

    expect(api.getPlatformAccount).toHaveBeenCalledTimes(reads + 1);
    expect(screen.getByText(WAITING_HINT)).toBeInTheDocument();
    expect(screen.queryByText(TIMEOUT_HINT)).not.toBeInTheDocument();
  });

  it('停止等待之后不再轮询，读取失败的原因还留着', async () => {
    vi.mocked(api.getPlatformAccount)
      .mockResolvedValueOnce(waitingScan)
      .mockResolvedValueOnce(waitingScan)
      .mockRejectedValue(new Error(READ_FAILED));
    await intoScanning();

    await tick();
    expect(screen.getByText(`${SCAN_HINT}${READ_FAILED}`)).toBeInTheDocument();

    await click('停止等待');
    const reads = vi.mocked(api.getPlatformAccount).mock.calls.length;
    await tick();
    await tick();

    expect(screen.getByText(`${PAUSED_HINT}${READ_FAILED}`)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '扫码登录' })).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(reads);
    expect(api.getPlatformLoginQrcode).toHaveBeenCalledTimes(1);
  });

  it('停止等待之后点刷新账号状态，又自己盯着了', async () => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingScan);
    await intoScanning();
    await click('停止等待');

    await click('刷新账号状态');
    await tick();

    expect(api.getPlatformAccount).toHaveBeenCalledTimes(4);
    expect(api.getPlatformLoginQrcode).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
  });

  it('停止等待之后换个号登录，又自己盯着了', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.enterPlatformLoginMode).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingScan);
    await intoScanning();
    await click('停止等待');

    await click('换个号登录');
    await tick();

    expect(api.enterPlatformLoginMode).toHaveBeenCalledTimes(1);
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(3);
    expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
  });

  it('停止等待之后快速切换，又自己盯着了', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.switchPlatformAccount).mockResolvedValue(undefined);
    vi.mocked(api.getPlatformAccount).mockResolvedValue(waitingWithBackup);
    await intoScanning();
    await click('停止等待');

    await click('切到这个号');
    await tick();

    expect(api.switchPlatformAccount).toHaveBeenCalledWith('admin-token', 'qq', '2000002');
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(3);
    expect(screen.getByRole('img', { name: 'QQ 登录二维码' })).toBeInTheDocument();
    expect(screen.getByText(SCAN_HINT)).toBeInTheDocument();
  });
});

describe('记忆归属的提示', () => {
  // 搬完立刻重读一次归属，读失败不能把刚搬完的备份路径顶掉。
  const migrateThenFailRead = async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await mount();
    await fillTarget('2000002');
    await click('预览');
    await click('执行搬迁');
  };

  it('搬迁的结果熬得过紧接着那次重读，读失败的原因接在后面', async () => {
    vi.mocked(api.getAccountScope)
      .mockResolvedValueOnce(scopeIdle)
      .mockResolvedValueOnce(scopePlan)
      .mockRejectedValue(new Error(SCOPE_FAILED));

    await migrateThenFailRead();

    expect(api.migrateAccountScope).toHaveBeenCalledWith('admin-token', 'qq', '2000002', '1000001');
    expect(screen.getByText(`${MIGRATED}。${SCOPE_FAILED}`)).toBeInTheDocument();
  });

  it('再点预览把搬迁的结果和读失败的原因一起清掉', async () => {
    vi.mocked(api.getAccountScope)
      .mockResolvedValueOnce(scopeIdle)
      .mockResolvedValueOnce(scopePlan)
      .mockRejectedValueOnce(new Error(SCOPE_FAILED))
      .mockResolvedValue(scopePlan);
    await migrateThenFailRead();
    expect(screen.getByText(`${MIGRATED}。${SCOPE_FAILED}`)).toBeInTheDocument();

    await click('预览');

    expect(screen.queryByText(`${MIGRATED}。${SCOPE_FAILED}`)).not.toBeInTheDocument();
    expect(screen.queryByText(MIGRATED)).not.toBeInTheDocument();
    expect(screen.queryByText(SCOPE_FAILED)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '执行搬迁' })).toBeEnabled();
  });

  it('首次读归属失败，下一次读回来那句就没了', async () => {
    vi.mocked(api.getAccountScope)
      .mockRejectedValueOnce(new Error(SCOPE_FAILED))
      .mockResolvedValue(scopePlan);
    await mount();
    expect(screen.getByText(SCOPE_FAILED)).toBeInTheDocument();
    expect(screen.getByText('还没绑定账号')).toBeInTheDocument();

    await fillTarget('2000002');
    await click('预览');

    expect(screen.queryByText(SCOPE_FAILED)).not.toBeInTheDocument();
    expect(screen.queryByText('还没绑定账号')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '执行搬迁' })).toBeEnabled();
  });
});

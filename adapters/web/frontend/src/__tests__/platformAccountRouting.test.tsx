// 账号页的每个请求都要打到所在平台自己的 /platforms/<id>/account 下，账号字段一律叫 account。
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

import { AccountScope } from '../console/AccountScope';
import { AccountWorkspaceSlot } from '../console/AccountWorkspace';
import { PlatformProvider } from '../console/platformContext';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import type { ConsoleInstance } from '../api/supervisorClient';

const TELEGRAM: PlatformDescriptor = {
  id: 'telegram',
  displayName: 'Telegram',
  shortName: 'TG',
  channels: [{ name: 'tg_group', kind: 'group', hashed: true }],
  hasConfig: true,
  entrypoints: ['telegram_bot.py'],
  connectionPeer: 'Telegram Bot API',
  connectionHint: '检查 bot token 与出网代理',
};

const main: ConsoleInstance = { id: 'main', label: '主实例', path: '', current: true, idx: 0, accounts: { qq: '1000001' } };
const other = (accounts: Record<string, string>): ConsoleInstance => ({
  id: 'xiaoyu', label: '第二实例', path: '/i/xiaoyu', current: false, idx: 1, accounts,
});
let instances: ConsoleInstance[] = [main];
const scope = { current_scope: '90001', live_scope: '', bot_alive: false, preview: null, ambiguous_sources: [] };
const online = {
  configured: true, reachable: true, is_login: true, online: true, account: '1000001', nick: '当前机器人',
  quick_login: [{ account: '1000001', nick: '本号' }, { account: '2000002', nick: '备用号' }],
};

let accountState: Record<string, unknown> = online;

const json = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});

function stubFetch() {
  const mock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/v1/instances')) return json({ instances });
    if (url.includes('/account/scope')) return json(scope);
    if (url.includes('/account/qrcode')) return json({ qrcode: '', reachable: true });
    if (/\/account\/(switch|relogin|pin)$/.test(url)) return json({ ok: true });
    if (url.includes('/account')) return json(accountState);
    return json({});
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

type FetchMock = ReturnType<typeof stubFetch>;
const urls = (mock: FetchMock) => mock.mock.calls.map(([input]) => String(input));
const callTo = (mock: FetchMock, url: string) => mock.mock.calls.find(([input]) => String(input) === url);

const inTelegram = (child: ReactNode) => render(<PlatformProvider platform={TELEGRAM}>{child}</PlatformProvider>);

beforeEach(() => {
  accountState = online;
  instances = [main];
  useSettingsStore.setState({ adminToken: 'current-token', isAuthenticated: true });
  window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account');
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('记忆归属按平台取数', () => {
  it('包在平台里时读那个平台的作用域', async () => {
    const fetchMock = stubFetch();
    inTelegram(<AccountScope />);
    await screen.findByText('90001');
    expect(urls(fetchMock)).toEqual(['/v1/platforms/telegram/account/scope']);
  });

  it('不套 Provider 时落在默认平台上', async () => {
    const fetchMock = stubFetch();
    render(<AccountScope />);
    await screen.findByText('90001');
    expect(urls(fetchMock)).toEqual(['/v1/platforms/qq/account/scope']);
  });

  it('换了平台就重新取一次，不留着上一个平台的归属', async () => {
    const fetchMock = stubFetch();
    const view = inTelegram(<AccountScope />);
    await screen.findByText('90001');
    view.rerender(<PlatformProvider platform={QQ_NAPCAT}><AccountScope /></PlatformProvider>);
    await waitFor(() => expect(urls(fetchMock)).toEqual([
      '/v1/platforms/telegram/account/scope', '/v1/platforms/qq/account/scope',
    ]));
  });
});

describe('账号页的平台归属', () => {
  it('账号状态读的是本平台的接口', async () => {
    const fetchMock = stubFetch();
    inTelegram(<AccountWorkspaceSlot />);
    await screen.findByText('当前机器人');
    expect(urls(fetchMock)).toContain('/v1/platforms/telegram/account');
  });

  it('快速切换按 account 字段列号码并认出当前号', async () => {
    stubFetch();
    inTelegram(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByText('1000001')).toBeInTheDocument();
    expect(within(quick).getByText('2000002')).toBeInTheDocument();
    expect(within(quick).getByRole('button', { name: '当前账号' })).toBeDisabled();
    expect(within(quick).getByRole('button', { name: '切到这个号' })).toBeEnabled();
  });

  it('别的实例在另一个平台上记着同一串号码，不算占用', async () => {
    instances = [main, other({ qq: '2000002' })];
    stubFetch();
    inTelegram(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByRole('button', { name: '切到这个号' })).toBeEnabled();
    expect(within(quick).queryByRole('link', { name: '去它的控制台' })).not.toBeInTheDocument();
  });

  it('本平台上被别的实例占着的号只给控制台入口', async () => {
    instances = [main, other({ telegram: '2000002' })];
    stubFetch();
    inTelegram(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByRole('link', { name: '去它的控制台' })).toHaveAttribute('href', '/i/xiaoyu/web/?view=settings&tab=qq-account');
    expect(within(quick).queryByRole('button', { name: '切到这个号' })).not.toBeInTheDocument();
  });

  it('切号请求带着 account 字段发给本平台', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubFetch();
    inTelegram(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    fireEvent.click(within(quick).getByRole('button', { name: '切到这个号' }));
    await waitFor(() => expect(urls(fetchMock)).toContain('/v1/platforms/telegram/account/switch'));
    const [, init] = callTo(fetchMock, '/v1/platforms/telegram/account/switch')!;
    expect(init?.method).toBe('POST');
    expect(init?.body).toBe('{"account":"2000002"}');
  });

  it('登上之后把这个号钉给本平台自动登录', async () => {
    accountState = { ...online, is_login: false, quick_login: [] };
    const fetchMock = stubFetch();
    vi.useFakeTimers();
    await act(async () => { inTelegram(<AccountWorkspaceSlot />); });
    expect(screen.getByText('未登录')).toBeInTheDocument();

    accountState = { ...online, account: '2000002', nick: '新机器人', quick_login: [] };
    await act(async () => vi.advanceTimersByTimeAsync(3000));

    const [, init] = callTo(fetchMock, '/v1/platforms/telegram/account/pin')!;
    expect(init?.method).toBe('POST');
    expect(screen.getByText('已登录 新机器人，并设为重启后自动登录。')).toBeInTheDocument();
  });
});

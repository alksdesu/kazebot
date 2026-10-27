import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LOADED, UNREAD, useSettingsStore } from '../store/settingsStore';

const mocks = vi.hoisted(() => ({ health: vi.fn(), providers: vi.fn(), verify: vi.fn(), confirm: vi.fn(), disconnect: vi.fn() }));
vi.mock('../api/supervisorClient', () => ({
  MOUNT: '',
  HEALTH_TIMEOUT_MS: 10_000,
  checkHealth: mocks.health,
  getProviders: mocks.providers,
  verifyAdminAuth: mocks.verify,
  getNodes: vi.fn().mockResolvedValue([]),
  activeProviderConfig: (value: unknown) => value,
  disconnectGlobalWS: mocks.disconnect,
}));
vi.mock('../hooks/useUnsavedChanges', () => ({ confirmNavigation: mocks.confirm }));
import { GeneralSettingsPage } from '../components/settings/pages/GeneralSettingsPage';
import { useChatStore } from '../store/chatStore';

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { resolve, promise };
}

const providerTable = (model: string) => ({
  active_provider: 'openai',
  providers: { openai: { model, base_url: '', api_key_present: false } },
  fallbacks: [],
  registered: ['openai'],
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.health.mockReset().mockResolvedValue({ status: 'ok' });
  mocks.providers.mockReset().mockResolvedValue(providerTable('fixture-model'));
  mocks.verify.mockReset();
  mocks.confirm.mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'fixture-token', isAuthenticated: true, isConnected: false, availableNodes: [], modelConfig: null, storageWarning: '', providerTable: null, providerTableRead: UNREAD });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('通用设置', () => {
  it('清楚区分调度器连接与适配进程状态，并可重新检查连接', async () => {
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('调度器已连接')).toBeVisible();
    expect(screen.getByText(/平台适配进程的状态在该平台的「链路」页/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '刷新连接' }));
    await act(async () => {});
    expect(mocks.health).toHaveBeenCalledTimes(2);
  });

  it('连接失败提供重试而不清除已验证身份', async () => {
    mocks.health.mockRejectedValueOnce(new Error('offline'));
    render(<GeneralSettingsPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接调度器');
    expect(useSettingsStore.getState().adminToken).toBe('fixture-token');
    expect(useSettingsStore.getState().isAuthenticated).toBe(true);
  });

  it('退出登录后拒绝迟到的模型目录回填', async () => {
    const data = pending<unknown>();
    mocks.providers.mockReturnValue(data.promise);
    render(<GeneralSettingsPage />);
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    await act(async () => { data.resolve(providerTable('old-model')); });
    const current = useSettingsStore.getState();
    expect(current.adminToken).toBeNull();
    expect(current.modelConfig).toBeNull();
    expect(current.activeNodeId).toBe('');
    expect(current.providerTable).toBeNull();
    expect(current.providerTableRead).toEqual(UNREAD);
  });

  it('退出登录把这次登录读到的东西连同读取状态一起清掉', async () => {
    useSettingsStore.setState({
      allNodes: [{ id: 'old.node', type: 'ai' }],
      availableNodes: [{ id: 'old.node', type: 'ai' }],
      nodeListRead: LOADED,
      globalModel: 'old-global',
      globalConfigRead: LOADED,
      sessionProviderOverride: { model: 'old-session-model' },
      sessionProviderOverrideSessionId: 'old-session',
    });
    render(<GeneralSettingsPage />);
    await waitFor(() => expect(useSettingsStore.getState().providerTableRead).toEqual(LOADED));

    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));

    expect(useSettingsStore.getState()).toMatchObject({
      allNodes: [],
      availableNodes: [],
      nodeListRead: UNREAD,
      globalModel: '',
      globalConfigRead: UNREAD,
      providerTable: null,
      providerTableRead: UNREAD,
      modelConfig: null,
      sessionProviderOverride: null,
      sessionProviderOverrideSessionId: '',
    });
  });

  it('拒绝丢弃草稿时不退出登录', () => {
    mocks.confirm.mockReturnValue(false);
    render(<GeneralSettingsPage />);
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    expect(useSettingsStore.getState().isAuthenticated).toBe(true);
    expect(useSettingsStore.getState().adminToken).toBe('fixture-token');
    expect(mocks.disconnect).not.toHaveBeenCalled();
  });

  // 事件流还开着就会继续收旧账号的事件、拿空令牌去自动审批；内存里的对话也不该留给下一个登录的人。
  it('退出登录先关掉全局事件流、清空内存里的对话，再丢令牌', () => {
    useChatStore.setState({ conversations: [{ id: 'c1', sessionId: 's1', title: '旧对话', updatedAt: '2026-05-31T00:00:00.000Z' }], activeConversationId: 'c1', connectionStatus: 'open' });
    render(<GeneralSettingsPage />);

    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));

    expect(mocks.disconnect).toHaveBeenCalledOnce();
    expect(useChatStore.getState()).toMatchObject({ conversations: [], activeConversationId: null, connectionStatus: 'idle' });
    expect(useSettingsStore.getState()).toMatchObject({ adminToken: null, isAuthenticated: false });
  });

  it('独立未认证页面复用同一个可恢复的登录表单', async () => {
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false });
    mocks.verify.mockResolvedValue(false);
    render(<GeneralSettingsPage />);
    fireEvent.change(screen.getByLabelText('管理员令牌'), { target: { value: 'fixture-invalid-token' } });
    fireEvent.click(screen.getByRole('button', { name: /^登录$/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('令牌无效');
  });

  it('卸载取消连接检查，迟到结果不能覆盖新页面状态', async () => {
    const health = pending<unknown>();
    mocks.health.mockReturnValue(health.promise);
    const page = render(<GeneralSettingsPage />);
    const signal = mocks.health.mock.calls[0][0] as AbortSignal;
    page.unmount();
    expect(signal.aborted).toBe(true);
    useSettingsStore.setState({ isConnected: false });
    await act(async () => { health.resolve({ status: 'ok' }); });
    expect(useSettingsStore.getState().isConnected).toBe(false);
  });
});

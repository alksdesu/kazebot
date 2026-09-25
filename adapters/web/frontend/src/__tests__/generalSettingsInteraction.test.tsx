import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSettingsStore } from '../store/settingsStore';

const mocks = vi.hoisted(() => ({ health: vi.fn(), providers: vi.fn(), verify: vi.fn(), confirm: vi.fn() }));
vi.mock('../api/supervisorClient', () => ({
  MOUNT: '',
  checkHealth: mocks.health,
  getProviders: mocks.providers,
  verifyAdminAuth: mocks.verify,
  getNodes: vi.fn().mockResolvedValue([]),
  activeProviderConfig: (value: unknown) => value,
}));
vi.mock('../hooks/useUnsavedChanges', () => ({ confirmNavigation: mocks.confirm }));
import { GeneralSettingsPage } from '../components/settings/pages/GeneralSettingsPage';

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { resolve, promise };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.health.mockReset().mockResolvedValue({ status: 'ok' });
  mocks.providers.mockReset().mockResolvedValue({ model: 'fixture-model', base_url: '', api_key_present: false });
  mocks.verify.mockReset();
  mocks.confirm.mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'fixture-token', isAuthenticated: true, isConnected: false, availableNodes: [], modelConfig: null, storageWarning: '' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('通用设置', () => {
  it('清楚区分调度器连接与 QQ 状态，并可重新检查连接', async () => {
    render(<GeneralSettingsPage />);
    expect(await screen.findByText('调度器已连接')).toBeVisible();
    expect(screen.getByText(/QQ 适配器和模型工作进程/)).toBeVisible();
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
    await act(async () => { data.resolve({ model: 'old-model', base_url: '', api_key_present: false }); });
    const current = useSettingsStore.getState();
    expect(current.adminToken).toBeNull();
    expect(current.modelConfig).toBeNull();
    expect(current.activeNodeId).toBe('');
  });

  it('拒绝丢弃草稿时不退出登录', () => {
    mocks.confirm.mockReturnValue(false);
    render(<GeneralSettingsPage />);
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    expect(useSettingsStore.getState().isAuthenticated).toBe(true);
    expect(useSettingsStore.getState().adminToken).toBe('fixture-token');
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

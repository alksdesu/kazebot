import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  directory: vi.fn(), verify: vi.fn(), navigate: vi.fn(), platforms: vi.fn(), plugins: vi.fn(), chat: vi.fn(),
}));

vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  getInstanceDirectory: mocks.directory,
  verifyAdminAuth: mocks.verify,
}));
vi.mock('../instances/navigation', async original => ({
  ...await original<typeof import('../instances/navigation')>(),
  navigateToInstance: mocks.navigate,
}));
vi.mock('../platforms/usePlatformRegistry', async original => ({
  ...await original<typeof import('../platforms/usePlatformRegistry')>(),
  usePlatformRegistry: mocks.platforms,
}));
vi.mock('../plugins/runtime', async original => ({
  ...await original<typeof import('../plugins/runtime')>(),
  useFrontendPlugins: mocks.plugins,
}));
vi.mock('../hooks/useChat', () => ({ useChat: mocks.chat }));

import App from '../App';
import { InstanceDirectory } from '../components/instances/InstanceDirectory';
import { InstanceNavigation } from '../components/instances/InstanceNavigation';
import { InstanceDirectoryProvider } from '../components/instances/useInstanceDirectory';
import { LoginPage } from '../components/auth/LoginPage';
import { AppLayout } from '../components/layout/AppLayout';
import { rememberInstance } from '../instances/navigation';
import { InstanceLink, InstanceSwitch } from '../console/InstanceSwitch';
import { useSettingsStore } from '../store/settingsStore';

const directory = {
  current_path: '',
  instances: [{ label: '主实例', path: '' }, { label: 'miaomiao', path: '/i/miaomiao' }],
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, '', '/web/?entry=instances');
  mocks.directory.mockReset().mockResolvedValue(directory);
  mocks.verify.mockReset().mockResolvedValue(false);
  mocks.navigate.mockReset().mockReturnValue(true);
  useSettingsStore.setState({ adminToken: null, isAuthenticated: false, rightPanelOpen: false });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, '', '/web/');
});

describe('独立实例入口', () => {
  it('入口移除地址里的令牌，避免跨实例 Referer 泄露', async () => {
    window.history.replaceState(null, '', '/web/?entry=instances&token=private-token');
    render(<App />);
    await screen.findByRole('button', { name: '进入 miaomiao' });
    expect(window.location.search).toBe('?entry=instances');
    expect(mocks.verify).not.toHaveBeenCalled();
    expect(localStorage.getItem('clonoth_admin_token')).toBeNull();
  });

  it('其他标签页改变最近实例后入口及时刷新', async () => {
    rememberInstance('');
    render(<App />);
    await screen.findByRole('button', { name: '继续进入 主实例' });
    act(() => {
      localStorage.setItem('clonoth_recent_instance', '/i/miaomiao');
      window.dispatchEvent(new StorageEvent('storage', { key: 'clonoth_recent_instance', newValue: '/i/miaomiao' }));
    });
    expect(screen.getByRole('button', { name: '继续进入 miaomiao' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续进入 主实例' })).not.toBeInTheDocument();
  });

  it('旧管理清单中的非法地址不会使其他管理内容崩溃', () => {
    render(<><InstanceLink path="/i/../broken">控制台</InstanceLink><InstanceSwitch rows={[
      { id: 'main', label: '主实例', path: '', current: true },
      { id: 'miaomiao', label: 'miaomiao', path: '/i/miaomiao', current: false },
      { id: 'bad', label: '坏地址', path: '/i/../broken', current: false },
    ]} /><button>其他管理操作</button></>);
    expect(screen.getByText('控制台')).toHaveAttribute('aria-disabled', 'true');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: '坏地址' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '其他管理操作' })).toBeInTheDocument();
  });

  it.each([false, true])('认证状态为 %s 时都先选实例，不启动控制台', async authenticated => {
    useSettingsStore.setState({ isAuthenticated: authenticated, adminToken: 'root-token' });
    const fetch = vi.fn(); const socket = vi.fn();
    vi.stubGlobal('fetch', fetch); vi.stubGlobal('WebSocket', socket);
    render(<App />);

    expect(await screen.findByRole('button', { name: '进入 miaomiao' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '进入 主实例' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: '管理员登录' })).not.toBeInTheDocument();
    expect(mocks.verify).not.toHaveBeenCalled();
    expect(mocks.platforms).not.toHaveBeenCalled();
    expect(mocks.plugins).not.toHaveBeenCalled();
    expect(mocks.chat).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(socket).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(mocks.directory).toHaveBeenCalledTimes(1);
  });

  it('选择实例不验证或复制当前令牌', async () => {
    useSettingsStore.setState({ adminToken: 'root-token' });
    localStorage.setItem('clonoth_admin_token', 'root-token');
    localStorage.setItem('clonoth_admin_token/i/miaomiao', 'miaomiao-token');
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '进入 miaomiao' }));

    expect(mocks.navigate).toHaveBeenCalledWith('/i/miaomiao', false);
    expect(mocks.verify).not.toHaveBeenCalled();
    expect(useSettingsStore.getState().adminToken).toBe('root-token');
    expect(localStorage.getItem('clonoth_admin_token')).toBe('root-token');
    expect(localStorage.getItem('clonoth_admin_token/i/miaomiao')).toBe('miaomiao-token');
  });

  it.each([['', '主实例'], ['/i/miaomiao', 'miaomiao']])('最近使用 %s 可继续进入，所有实例仍可选', async (path, label) => {
    rememberInstance(path);
    render(<InstanceDirectory />);
    const resume = await screen.findByRole('button', { name: `继续进入 ${label}` });
    expect(within(screen.getByRole('list', { name: '可选实例' })).getAllByRole('button')).toHaveLength(2);
    fireEvent.click(resume);
    expect(mocks.navigate).toHaveBeenCalledWith(path, false);
  });

  it('忽略已删除的最近实例，不自动跳转', async () => {
    rememberInstance('/i/deleted');
    render(<InstanceDirectory />);
    await screen.findByRole('button', { name: '进入 主实例' });
    expect(screen.queryByRole('button', { name: /继续进入/ })).not.toBeInTheDocument();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it('存储不可用仍可显示目录并选择实例', async () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('denied'); });
    render(<InstanceDirectory />);
    fireEvent.click(await screen.findByRole('button', { name: '进入 miaomiao' }));
    expect(mocks.navigate).toHaveBeenCalledWith('/i/miaomiao', false);
    expect(screen.queryByRole('button', { name: /继续进入/ })).not.toBeInTheDocument();
  });

  it('加载失败可手动进入当前实例，也可重试目录', async () => {
    mocks.directory.mockRejectedValueOnce(new Error('offline'));
    render(<InstanceDirectory />);
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读取实例目录');
    fireEvent.click(screen.getByRole('button', { name: '进入当前实例' }));
    expect(mocks.navigate).toHaveBeenCalledWith('', false);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByRole('button', { name: '进入 miaomiao' })).toBeInTheDocument();
    expect(mocks.directory).toHaveBeenCalledTimes(2);
  });

  it('卸载取消目录请求并忽略迟到的结果', async () => {
    let resolve!: (value: typeof directory) => void;
    mocks.directory.mockReturnValueOnce(new Promise<typeof directory>(done => { resolve = done; }));
    const view = render(<InstanceDirectory />);
    expect(screen.getByRole('status')).toHaveTextContent('正在读取实例目录');
    const signal = mocks.directory.mock.calls[0][0] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { resolve(directory); });
    expect(screen.queryByRole('button', { name: '进入 miaomiao' })).not.toBeInTheDocument();
  });
});

describe('登录与侧栏的实例导航', () => {
  it('直接访问控制台保持登录入口且提供实例选择', async () => {
    window.history.replaceState(null, '', '/web/');
    render(<App />);
    expect(screen.getByRole('form', { name: '管理员登录' })).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toHaveValue('');
    expect(mocks.platforms).toHaveBeenCalled();
    expect(mocks.plugins).toHaveBeenCalled();
    expect(mocks.chat).not.toHaveBeenCalled();
    expect(mocks.directory).toHaveBeenCalledTimes(1);
  });

  it('内嵌登录不重复呈现实例导航', () => {
    render(<LoginPage embedded />);
    expect(screen.queryByRole('navigation', { name: '实例导航' })).not.toBeInTheDocument();
    expect(mocks.directory).not.toHaveBeenCalled();
  });

  it('多个消费者共用目录请求，取消导航后选择仍指向当前实例', async () => {
    mocks.navigate.mockReturnValue(false);
    render(<InstanceDirectoryProvider><InstanceNavigation /><InstanceNavigation /></InstanceDirectoryProvider>);
    const selectors = await screen.findAllByRole('combobox', { name: '切换实例' });
    expect(mocks.directory).toHaveBeenCalledTimes(1);
    fireEvent.change(selectors[0], { target: { value: '/i/miaomiao' } });
    expect(mocks.navigate).toHaveBeenCalledWith('/i/miaomiao');
    expect(selectors[0]).toHaveValue('');
  });

  it('单实例显示当前名称但不显示切换控件', async () => {
    mocks.directory.mockResolvedValueOnce({ current_path: '', instances: directory.instances.slice(0, 1) });
    render(<InstanceNavigation />);
    expect(await screen.findByText('主实例')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('目录错误时保留选择入口和重试', async () => {
    mocks.directory.mockRejectedValueOnce(new Error('offline'));
    render(<InstanceNavigation />);
    expect(await screen.findByRole('link', { name: '选择实例' })).toHaveAttribute('href', '/web/?entry=instances');
    fireEvent.click(screen.getByRole('button', { name: '重试实例目录' }));
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
  });

  it('移动端展开导航后可直接切换实例', async () => {
    const width = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    try {
      render(<AppLayout sidebar={<button>页面导航</button>} header="页面"><p>内容</p></AppLayout>);
      fireEvent.click(screen.getByRole('button', { name: '打开导航' }));
      const sidebar = screen.getByRole('dialog', { name: '主导航' });
      const select = await within(sidebar).findByRole('combobox', { name: '切换实例' });
      fireEvent.change(select, { target: { value: '/i/miaomiao' } });
      expect(mocks.navigate).toHaveBeenCalledWith('/i/miaomiao');
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
    }
  });
});

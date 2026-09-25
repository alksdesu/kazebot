import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => {
  const state = { adminToken: null as string | null, isAuthenticated: false, entryNodeId: '' };
  return {
    state,
    verify: vi.fn(),
    loadNodes: vi.fn().mockResolvedValue(undefined),
    getNodes: vi.fn(),
    setAvailableNodes: vi.fn(),
    setEntryNodeId: vi.fn(),
    setToken: vi.fn((token: string | null) => { state.adminToken = token; }),
    setAuthenticated: vi.fn((value: boolean) => { state.isAuthenticated = value; }),
  };
});

vi.mock('../api/supervisorClient', () => ({ checkAdminAuth: mocks.verify, verifyAdminAuth: mocks.verify, getNodes: mocks.getNodes }));
vi.mock('../components/auth/loadEntryNodes', () => ({ loadEntryNodes: mocks.loadNodes, TOKEN_SOURCE_HINT: '从服务启动信息中获取管理员令牌。' }));
vi.mock('../store/settingsStore', () => {
  const getState = () => ({ ...mocks.state, setAdminToken: mocks.setToken, setAuthenticated: mocks.setAuthenticated, setAvailableNodes: mocks.setAvailableNodes, setEntryNodeId: mocks.setEntryNodeId });
  return { LS_KEY_NODE: 'fixture-node', useSettingsStore: Object.assign(() => getState(), { getState }) };
});

import { LoginPage } from '../components/auth/LoginPage';

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.state.adminToken = null;
  mocks.state.isAuthenticated = false;
  mocks.state.entryNodeId = '';
  mocks.verify.mockReset();
  mocks.getNodes.mockReset();
  window.history.replaceState(null, '', '/web/');
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('登录交互', () => {
  it('读取直达令牌后立即清理地址，同时保留工作区和锚点', () => {
    window.history.replaceState(null, '', '/web/?view=materials&token=fixture-link-token#preview');
    mocks.verify.mockReturnValue(new Promise(() => {}));
    render(<LoginPage />);
    expect(window.location.search).toBe('?view=materials');
    expect(window.location.hash).toBe('#preview');
    expect(mocks.verify.mock.calls[0][0]).toBe('fixture-link-token');
  });

  it('连续回车只发送一次验证请求', () => {
    mocks.verify.mockReturnValue(new Promise(() => {}));
    render(<LoginPage />);
    const input = screen.getByPlaceholderText('管理员令牌');
    fireEvent.change(input, { target: { value: 'fixture-manual-token' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(mocks.verify).toHaveBeenCalledTimes(1);
  });

  it('首个令牌为空时仍清理全部重复的令牌参数', () => {
    window.history.replaceState(null, '', '/web/?view=materials&token=&token=fixture-hidden-token');
    render(<LoginPage />);
    expect(window.location.search).toBe('?view=materials');
    expect(mocks.verify).not.toHaveBeenCalled();
  });

  it('令牌框具有持久标签，拒绝后保留输入供修正', async () => {
    mocks.verify.mockResolvedValue(false);
    render(<LoginPage />);
    const input = screen.getByLabelText('管理员令牌');
    fireEvent.change(input, { target: { value: 'fixture-invalid-token' } });
    fireEvent.click(screen.getByRole('button', { name: /^登录$/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('令牌无效');
    expect(input).toHaveValue('fixture-invalid-token');
    expect(mocks.setAuthenticated).not.toHaveBeenCalled();
  });

  it('登录成功后保存令牌并加载入口节点', async () => {
    mocks.verify.mockResolvedValue(true);
    render(<LoginPage />);
    fireEvent.change(screen.getByPlaceholderText('管理员令牌'), { target: { value: ' fixture-manual-token ' } });
    fireEvent.click(screen.getByRole('button', { name: /^登录$/ }));
    await act(async () => {});
    expect(mocks.setToken).toHaveBeenCalledWith('fixture-manual-token');
    expect(mocks.setAuthenticated).toHaveBeenCalledWith(true);
    expect(mocks.loadNodes.mock.calls[0][0]).toBe('fixture-manual-token');
  });

  it('切换到手动登录后，旧的自动验证结果不能覆盖新身份', async () => {
    mocks.state.adminToken = 'fixture-saved-token';
    const old = pending<boolean>();
    mocks.verify.mockReturnValueOnce(old.promise).mockResolvedValueOnce(true);
    render(<LoginPage />);
    fireEvent.click(screen.getByRole('button', { name: '使用其他令牌' }));
    fireEvent.change(screen.getByLabelText('管理员令牌'), { target: { value: 'fixture-new-token' } });
    fireEvent.click(screen.getByRole('button', { name: /^登录$/ }));
    await act(async () => {});
    await act(async () => { old.resolve(true); });
    expect(mocks.setToken).toHaveBeenCalledTimes(1);
    expect(mocks.state.adminToken).toBe('fixture-new-token');
  });

  it('服务不可达时不删除已保存的令牌，也不误报令牌无效', async () => {
    mocks.state.adminToken = 'fixture-saved-token';
    mocks.verify.mockRejectedValue(new TypeError('Failed to fetch'));
    render(<LoginPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('请检查服务和网络');
    expect(mocks.state.adminToken).toBe('fixture-saved-token');
    expect(mocks.setToken).not.toHaveBeenCalled();
  });

  it('明确拒绝旧令牌时清理失效登录状态', async () => {
    mocks.state.adminToken = 'fixture-expired-token';
    mocks.verify.mockResolvedValue(false);
    render(<LoginPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('令牌无效');
    expect(mocks.state.adminToken).toBeNull();
  });

  it('网络恢复后可直接重试已保存登录，不必重新输入令牌', async () => {
    mocks.state.adminToken = 'fixture-saved-token';
    mocks.verify.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValueOnce(true);
    render(<LoginPage />);
    fireEvent.click(await screen.findByRole('button', { name: '重试已保存登录' }));
    await act(async () => {});
    expect(mocks.verify).toHaveBeenCalledTimes(2);
    expect(mocks.verify.mock.calls[1][0]).toBe('fixture-saved-token');
    expect(mocks.setAuthenticated).toHaveBeenCalledWith(true);
  });

  it('超时后恢复输入且忽略迟到成功', async () => {
    vi.useFakeTimers();
    mocks.state.adminToken = 'fixture-saved-token';
    const response = pending<boolean>();
    mocks.verify.mockReturnValue(response.promise);
    render(<LoginPage />);
    await act(async () => { vi.advanceTimersByTime(10000); });
    expect(screen.getByRole('alert')).toHaveTextContent('连接超时');
    expect(screen.getByLabelText('管理员令牌')).toBeEnabled();
    expect(mocks.verify.mock.calls[0][1].aborted).toBe(true);
    await act(async () => { response.resolve(true); });
    expect(mocks.setAuthenticated).not.toHaveBeenCalled();
    expect(mocks.state.adminToken).toBe('fixture-saved-token');
  });

  it('卸载会取消尚未结束的验证', async () => {
    mocks.state.adminToken = 'fixture-saved-token';
    const response = pending<boolean>();
    mocks.verify.mockReturnValue(response.promise);
    const view = render(<LoginPage />);
    view.unmount();
    expect(mocks.verify.mock.calls[0][1].aborted).toBe(true);
    await act(async () => { response.resolve(true); });
    expect(mocks.setAuthenticated).not.toHaveBeenCalled();
  });
});

describe('认证请求分类', () => {
  it.each([401, 403])('仅将 %s 归为令牌被拒绝', async status => {
    const api = await vi.importActual<typeof import('../api/supervisorClient')>('../api/supervisorClient');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status }));
    await expect(api.verifyAdminAuth('fixture-token')).resolves.toBe(false);
  });

  it('传递取消信号并区分服务器故障', async () => {
    const api = await vi.importActual<typeof import('../api/supervisorClient')>('../api/supervisorClient');
    const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    vi.stubGlobal('fetch', fetcher);
    const controller = new AbortController();
    await expect(api.verifyAdminAuth('fixture-token', controller.signal)).rejects.toThrow('503');
    expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal);
    await expect(api.checkAdminAuth('fixture-token')).resolves.toBe(false);
  });
});

describe('入口节点加载归属', () => {
  it('身份改变后不应用旧身份的节点列表', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-old-token';
    const nodes = pending<Array<{ id: string; type: string }>>();
    mocks.getNodes.mockReturnValue(nodes.promise);
    const request = module.loadEntryNodes('fixture-old-token');
    mocks.state.adminToken = 'fixture-new-token';
    nodes.resolve([{ id: 'old.node', type: 'ai' }]);
    await request;
    expect(mocks.setAvailableNodes).not.toHaveBeenCalled();
    expect(mocks.setEntryNodeId).not.toHaveBeenCalled();
  });

  it('保留正常节点加载并过滤系统节点', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.getNodes.mockResolvedValue([{ id: 'entry.node', type: 'ai' }, { id: 'system.internal', type: 'ai' }]);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setAvailableNodes).toHaveBeenCalledWith([{ id: 'entry.node', type: 'ai' }]);
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('entry.node');
  });

  it('成功空列表清理旧选择，网络失败不抹掉选择', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = 'old.identity.node';
    mocks.getNodes.mockResolvedValueOnce([{ id: 'system.internal', type: 'ai' }]);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('');
    mocks.setEntryNodeId.mockClear();
    mocks.getNodes.mockRejectedValueOnce(new Error('offline'));
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setEntryNodeId).not.toHaveBeenCalled();
  });
});

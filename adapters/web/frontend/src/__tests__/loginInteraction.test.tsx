import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => {
  const unread = () => ({ loaded: false, error: '' });
  const state = {
    adminToken: null as string | null, isAuthenticated: false, entryNodeId: '', availableNodes: [] as unknown[], defaultNodeId: '',
    nodeListRead: unread(), globalConfigRead: unread(),
  };
  return {
    state,
    verify: vi.fn(),
    loadNodes: vi.fn().mockResolvedValue(undefined),
    getNodes: vi.fn(),
    getAppConfig: vi.fn(),
    setNodeList: vi.fn(),
    failNodeList: vi.fn(),
    setGlobalConfig: vi.fn(),
    failGlobalConfig: vi.fn(),
    setEntryNodeId: vi.fn(),
    setToken: vi.fn((token: string | null) => { state.adminToken = token; }),
    setAuthenticated: vi.fn((value: boolean) => { state.isAuthenticated = value; }),
  };
});

vi.mock('../api/supervisorClient', () => ({
  checkAdminAuth: mocks.verify, verifyAdminAuth: mocks.verify, getNodes: mocks.getNodes, getAppConfig: mocks.getAppConfig,
  MOUNT: '', getInstanceDirectory: vi.fn().mockResolvedValue({ current_path: '', instances: [{ label: '主实例', path: '' }] }),
}));
vi.mock('../components/auth/loadEntryNodes', () => ({ loadEntryNodes: mocks.loadNodes, TOKEN_SOURCE_HINT: '从服务启动信息中获取管理员令牌。' }));
vi.mock('../store/settingsStore', () => {
  const getState = () => ({
    ...mocks.state,
    setAdminToken: mocks.setToken,
    setAuthenticated: mocks.setAuthenticated,
    setNodeList: mocks.setNodeList,
    failNodeList: mocks.failNodeList,
    setGlobalConfig: mocks.setGlobalConfig,
    failGlobalConfig: mocks.failGlobalConfig,
    setEntryNodeId: mocks.setEntryNodeId,
  });
  return { LS_KEY_NODE: 'fixture-node', useSettingsStore: Object.assign(() => getState(), { getState, subscribe: () => () => {} }) };
});

import { LoginPage } from '../components/auth/LoginPage';
import { filterEntryNodes } from '../utils/entryNodes';

const appConfig = (defaultEntry: string) => ({ provider: 'openai', openai: { model: '', base_url: '' }, default_entry_node_id: defaultEntry });

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
  mocks.state.availableNodes = [];
  mocks.state.defaultNodeId = '';
  mocks.state.nodeListRead = { loaded: false, error: '' };
  mocks.state.globalConfigRead = { loaded: false, error: '' };
  mocks.verify.mockReset();
  mocks.getNodes.mockReset();
  mocks.getAppConfig.mockReset().mockResolvedValue(appConfig(''));
  // 和真 store 一样：读到的节点表筛出入口清单，读到的全局配置记下默认入口；读成过以后没读成不再记
  mocks.setNodeList.mockImplementation((nodes: Parameters<typeof filterEntryNodes>[0]) => {
    mocks.state.availableNodes = filterEntryNodes(nodes);
    mocks.state.nodeListRead = { loaded: true, error: '' };
  });
  mocks.failNodeList.mockImplementation((reason: string) => {
    if (!mocks.state.nodeListRead.loaded) mocks.state.nodeListRead = { loaded: false, error: reason };
  });
  mocks.setGlobalConfig.mockImplementation((_model: string, _baseUrl: string, defaultEntry = '') => {
    mocks.state.defaultNodeId = defaultEntry;
    mocks.state.globalConfigRead = { loaded: true, error: '' };
  });
  mocks.failGlobalConfig.mockImplementation((reason: string) => {
    if (!mocks.state.globalConfigRead.loaded) mocks.state.globalConfigRead = { loaded: false, error: reason };
  });
  mocks.setEntryNodeId.mockImplementation((id: string) => { mocks.state.entryNodeId = id; });
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    await expect(api.verifyAdminAuth('fixture-token')).rejects.toThrow('503');
    await expect(api.checkAdminAuth('fixture-token')).resolves.toBe(false);

    // 读请求自带时限，fetch 拿到的是时限的内部信号，外部取消得转发进去才中止得掉。
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })));
    const controller = new AbortController();
    const verifying = api.verifyAdminAuth('fixture-token', controller.signal);
    controller.abort();
    await expect(verifying).rejects.toThrow('Aborted');
  });
});

describe('入口节点加载归属', () => {
  // 命令审核是 shell 委派下去的子节点，根入口只有 shell 和 coder
  const ROOTS_AND_CHILD = [
    { id: 'bootstrap.cmd_reviewer', type: 'ai' },
    { id: 'bootstrap.shell_orchestrator', type: 'ai', delegate_targets: ['bootstrap.cmd_reviewer'] },
    { id: 'bootstrap.coder', type: 'ai' },
  ];

  it('身份改变后不应用旧身份的节点列表', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-old-token';
    const nodes = pending<Array<{ id: string; type: string }>>();
    mocks.getNodes.mockReturnValue(nodes.promise);
    const request = module.loadEntryNodes('fixture-old-token');
    mocks.state.adminToken = 'fixture-new-token';
    nodes.resolve([{ id: 'old.node', type: 'ai' }]);
    await request;
    expect(mocks.setNodeList).not.toHaveBeenCalled();
    expect(mocks.failNodeList).not.toHaveBeenCalled();
    expect(mocks.setGlobalConfig).not.toHaveBeenCalled();
    expect(mocks.setEntryNodeId).not.toHaveBeenCalled();
  });

  it('保留正常节点加载并过滤系统节点', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    const nodes = [{ id: 'entry.node', type: 'ai' }, { id: 'system.internal', type: 'ai' }];
    mocks.getNodes.mockResolvedValue(nodes);
    await module.loadEntryNodes('fixture-token');
    // 整张表交给 store：入口清单由它筛，查模型要用的全部节点也留在它那里
    expect(mocks.setNodeList).toHaveBeenCalledWith(nodes);
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('entry.node');
  });

  it('别的入口委派下去的子节点不当默认入口，存着的这种入口换成根入口', async () => {
    // 新装实例的节点表里命令审核排在最前，它是 shell 委派下去的子节点，收到用户消息不该由它接。
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = 'bootstrap.cmd_reviewer';
    localStorage.removeItem('fixture-node');
    mocks.getNodes.mockResolvedValue([
      { id: 'bootstrap.cmd_reviewer', type: 'ai' },
      { id: 'bootstrap.shell_orchestrator', type: 'ai', delegate_targets: ['bootstrap.cmd_reviewer'] },
    ]);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('bootstrap.shell_orchestrator');
  });

  it('存着的根入口原样留着，哪怕后端配的默认入口是别的', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = 'bootstrap.coder';
    localStorage.removeItem('fixture-node');
    mocks.getAppConfig.mockResolvedValue(appConfig('bootstrap.shell_orchestrator'));
    mocks.getNodes.mockResolvedValue([
      { id: 'bootstrap.shell_orchestrator', type: 'ai' },
      { id: 'bootstrap.coder', type: 'ai', delegate_targets: ['bootstrap.coder'] },
    ]);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setNodeList).toHaveBeenCalled();
    expect(mocks.setEntryNodeId).not.toHaveBeenCalled();
  });

  it('替用户挑入口时，后端配的默认入口在根入口里就挑它', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    localStorage.removeItem('fixture-node');
    mocks.getAppConfig.mockResolvedValue(appConfig('bootstrap.coder'));
    mocks.getNodes.mockResolvedValue(ROOTS_AND_CHILD);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('bootstrap.coder');
  });

  it('后端配的默认入口不在根入口里时挑第一个根入口', async () => {
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    localStorage.removeItem('fixture-node');
    mocks.getAppConfig.mockResolvedValue(appConfig('bootstrap.cmd_reviewer'));
    mocks.getNodes.mockResolvedValue(ROOTS_AND_CHILD);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('bootstrap.shell_orchestrator');
  });

  it('全局配置没读成时不替用户挑，也不写入口', async () => {
    // 入口留空时新对话本来就走后端默认入口，没读到它就挑，挑中的可能不是它
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = 'gone.node';
    localStorage.removeItem('fixture-node');
    mocks.getAppConfig.mockRejectedValue(new Error('config 500'));
    mocks.getNodes.mockResolvedValue(ROOTS_AND_CHILD);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setNodeList).toHaveBeenCalledWith(ROOTS_AND_CHILD);
    expect(mocks.failGlobalConfig).toHaveBeenCalledWith('config 500');
    expect(mocks.setEntryNodeId).not.toHaveBeenCalled();
  });

  it('模板、示例和 internal 节点不进入口列表，存着的模板入口换成第一个能用的', async () => {
    // 新装实例的节点表里模板排在最前：不滤掉的话首次登录的入口就是它，发出的消息全会失败。
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = '_persistent_agent.template';
    localStorage.removeItem('fixture-node');
    const entry = { id: 'qq.orchestrator', type: 'ai', active: true, internal: false };
    const nodes = [
      { id: '_persistent_agent.template', type: 'ai', active: false },
      { id: 'qq.orchestrator.example', type: 'ai', active: false },
      { id: 'chat.intent', type: 'ai', active: true, internal: true },
      entry,
    ];
    mocks.getNodes.mockResolvedValue(nodes);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setNodeList).toHaveBeenCalledWith(nodes);
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('qq.orchestrator');
  });

  it('节点文件用不了的不进入口列表，存着的这种入口换成第一个能用的', async () => {
    // engine 加载不了它，发出的消息全会失败。节点表里的 type 是生效的值：没写 type 的报成 ai，
    // 和 engine 的 entry_node_problem 判法一致，所以省略 type 的节点照样能当入口。
    const module = await vi.importActual<typeof import('../components/auth/loadEntryNodes')>('../components/auth/loadEntryNodes');
    mocks.state.adminToken = 'fixture-token';
    mocks.state.entryNodeId = 'qq.skill';
    localStorage.removeItem('fixture-node');
    const untyped = { id: 'qq.untyped', type: 'ai', active: true, internal: false, problem: '' };
    const nodes = [
      { id: 'qq.broken', type: '', active: true, internal: false, problem: 'YAML 读不出来：第 2 行第 24 列：mapping values are not allowed here' },
      { id: 'qq.skill', type: 'ai', active: true, internal: false, problem: 'kind 写的是 skill，节点只认 node' },
      untyped,
    ];
    mocks.getNodes.mockResolvedValue(nodes);
    await module.loadEntryNodes('fixture-token');
    expect(mocks.setNodeList).toHaveBeenCalledWith(nodes);
    expect(mocks.setEntryNodeId).toHaveBeenCalledWith('qq.untyped');
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
    // 没读成的原因记进 store，顶栏拿它说话
    expect(mocks.failNodeList).toHaveBeenCalledWith('offline');
  });
});

// 审批 / 系统 / 节点文件三页的提示：操作结果熬得过紧接着的重读和后台轮询，
// 轮询失败把原因接在后面，下一次读成功只去掉原因，手动刷新才两样都清；
// 换人登录整页重来，上一个会话留下的提示一句都不带进新会话。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { AdminState, NodeFileInfo } from '../api/supervisorClient';
import { ApprovalsSettingsPage } from '../components/settings/pages/ApprovalsSettingsPage';
import { NodeFilesSettingsPage } from '../components/settings/pages/NodeFilesSettingsPage';
import { SystemSettingsPage } from '../components/settings/pages/SystemSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

// 系统页同屏还挂着活跃任务弹窗，它自己另有一套提示和 5 秒轮询。
vi.mock('../components/dashboard/ActiveTasksModal', () => ({ ActiveTasksModal: () => null }));

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  checkHealth: vi.fn(),
  decideApproval: vi.fn(),
  getAdminState: vi.fn(),
  getNodeFileRaw: vi.fn(),
  getNodeFiles: vi.fn(),
  makeNodeFileExample: vi.fn(),
  reloadConfig: vi.fn(),
  restartEngine: vi.fn(),
  updateNodeFileRaw: vi.fn(),
}));

const APPROVALS_POLL_MS = 10_000;
const SYSTEM_POLL_MS = 15_000;

/** 推进假时钟，顺带让这一轮该落地的 promise 都跑完。 */
const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

/** 没有提示时整个 StatusText 不渲染。 */
const statusText = (): string => screen.getByRole('status').textContent || '';

const expectNoStatus = () => expect(screen.queryByRole('status')).toBeNull();

const OTHER_TOKEN = 'another-admin-token';

/** 换人登录：写接口返回前改掉 store 里的 token。 */
const swapToken = () => { useSettingsStore.setState({ adminToken: OTHER_TOKEN }); };

/** getAdminState 每一次用的 token，按调用先后排。 */
const stateTokens = (): (string | undefined)[] => vi.mocked(api.getAdminState).mock.calls.map(([token]) => token);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const adminState = (over: Partial<AdminState> = {}): AdminState => ({
  sessions: 1,
  approvals: { pending: 1 },
  tasks: { running: 0 },
  pending_approvals: [],
  engine_runtime: { worker_id: 'worker-a' },
  ...over,
});

const withApproval = (): AdminState => adminState({
  pending_approvals: [{
    approval_id: 'approval-1',
    operation: '执行命令',
    details: { tool_name: 'execute_command', path: '/tmp/demo', reason: '需要执行命令' },
  }],
});

const nodeFile = (name: string, over: Partial<NodeFileInfo> = {}): NodeFileInfo => ({
  name,
  path: `config/nodes/${name}`,
  kind: 'node',
  suffix: '.yaml',
  is_example: false,
  base_name: name,
  size: 1,
  updated_at: 0,
  ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
  vi.useFakeTimers();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedApproval: null, systemLogs: [] });
  vi.mocked(api.checkHealth).mockResolvedValue({ status: 'ok', uptime_seconds: 60 });
  vi.mocked(api.decideApproval).mockResolvedValue({ ok: true });
  vi.mocked(api.reloadConfig).mockResolvedValue({ ok: true });
  vi.mocked(api.restartEngine).mockResolvedValue({ scheduled: true, target: 'engine' });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  useSettingsStore.setState({ adminToken: null, isAuthenticated: false });
  useSettingsSelectionStore.setState({ selectedApproval: null, systemLogs: [] });
});

describe('审批页审批之后的提示', () => {
  it('「已允许审批」熬过紧接着的重读和 10 秒轮询', async () => {
    vi.mocked(api.getAdminState).mockResolvedValueOnce(withApproval()).mockResolvedValue(adminState());
    render(<ApprovalsSettingsPage />);
    await tick();
    expect(api.getAdminState).toHaveBeenCalledTimes(1);
    expectNoStatus();

    click('允许');
    await tick();

    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'allow', 'settings allow');
    expect(api.getAdminState).toHaveBeenCalledTimes(2);
    expect(screen.getByText('暂无待审批项')).toBeInTheDocument();
    expect(statusText()).toBe('已允许审批');

    await tick(APPROVALS_POLL_MS - 1);
    expect(api.getAdminState).toHaveBeenCalledTimes(2);

    await tick(1);
    expect(api.getAdminState).toHaveBeenCalledTimes(3);
    expect(statusText()).toBe('已允许审批');
  });

  it('轮询失败把原因接在「已拒绝审批」后面，下一次读成功只去掉原因', async () => {
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(withApproval())
      .mockResolvedValueOnce(withApproval())
      .mockRejectedValueOnce(new Error('503 待审批列表拉不到'))
      .mockResolvedValue(adminState());
    render(<ApprovalsSettingsPage />);
    await tick();

    click('拒绝');
    await tick();
    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'deny', 'settings deny');
    expect(statusText()).toBe('已拒绝审批');

    await tick(APPROVALS_POLL_MS);
    expect(statusText()).toBe('已拒绝审批。503 待审批列表拉不到');

    await tick(APPROVALS_POLL_MS);
    expect(statusText()).toBe('已拒绝审批');
  });

  it('点「刷新列表」把结果和原因一起清掉', async () => {
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(withApproval())
      .mockResolvedValueOnce(withApproval())
      .mockRejectedValueOnce(new Error('503 待审批列表拉不到'))
      .mockResolvedValue(withApproval());
    render(<ApprovalsSettingsPage />);
    await tick();

    click('允许');
    await tick();
    await tick(APPROVALS_POLL_MS);
    expect(statusText()).toBe('已允许审批。503 待审批列表拉不到');

    click('刷新列表');
    await tick();

    expect(api.getAdminState).toHaveBeenCalledTimes(4);
    expectNoStatus();
  });
});

describe('审批页两次读取撞车', () => {
  it('手动刷新被轮询顶掉，慢回来的那份列表不落地，转圈照样收掉', async () => {
    const slow = deferred<AdminState>();
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(withApproval())
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue(adminState());
    render(<ApprovalsSettingsPage />);
    await tick();

    click('刷新列表');
    await tick();
    expect(screen.getByRole('button', { name: '正在刷新…' })).toBeDisabled();

    await tick(APPROVALS_POLL_MS);
    expect(api.getAdminState).toHaveBeenCalledTimes(3);
    expect(screen.getByText('暂无待审批项')).toBeInTheDocument();

    await act(async () => { slow.resolve(withApproval()); });

    expect(screen.getByText('暂无待审批项')).toBeInTheDocument();
    expect(screen.queryByText('execute_command')).toBeNull();
    expect(screen.getByRole('button', { name: '刷新列表' })).not.toBeDisabled();
  });

  it('被顶掉的那次刷新失败也不报原因', async () => {
    const slow = deferred<AdminState>();
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(withApproval())
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue(adminState());
    render(<ApprovalsSettingsPage />);
    await tick();

    click('刷新列表');
    await tick(APPROVALS_POLL_MS);
    expect(screen.getByText('暂无待审批项')).toBeInTheDocument();

    await act(async () => { slow.reject(new Error('503 待审批列表拉不到')); });

    expectNoStatus();
    expect(screen.getByRole('button', { name: '刷新列表' })).not.toBeDisabled();
  });
});

describe('审批页写完发现换了人', () => {
  it('允许之后既不写提示，也不拿旧 token 重读', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(withApproval());
    vi.mocked(api.decideApproval).mockImplementation(async () => { swapToken(); return { ok: true }; });
    render(<ApprovalsSettingsPage />);
    await tick();

    click('允许');
    await tick();

    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'allow', 'settings allow');
    expectNoStatus();
    // 第二次是换人之后重挂轮询自己读的，不是收尾那次。
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('拒绝之后既不写提示，也不拿旧 token 重读', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(withApproval());
    vi.mocked(api.decideApproval).mockImplementation(async () => { swapToken(); return { ok: true }; });
    render(<ApprovalsSettingsPage />);
    await tick();

    click('拒绝');
    await tick();

    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'deny', 'settings deny');
    expectNoStatus();
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('允许失败也不把原因写到新会话的页面上', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(withApproval());
    vi.mocked(api.decideApproval).mockImplementation(async () => { swapToken(); throw new Error('审批已过期：410'); });
    render(<ApprovalsSettingsPage />);
    await tick();

    click('允许');
    await tick();

    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'allow', 'settings allow');
    expect(screen.queryByText('审批已过期：410')).toBeNull();
    expectNoStatus();
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('拒绝失败也不把原因写到新会话的页面上', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(withApproval());
    vi.mocked(api.decideApproval).mockImplementation(async () => { swapToken(); throw new Error('审批已过期：410'); });
    render(<ApprovalsSettingsPage />);
    await tick();

    click('拒绝');
    await tick();

    expect(api.decideApproval).toHaveBeenCalledWith('admin-token', 'approval-1', 'deny', 'settings deny');
    expect(screen.queryByText('审批已过期：410')).toBeNull();
    expectNoStatus();
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });
});

describe('系统页重载之后的提示', () => {
  it('「配置已重载」熬过紧接着的重读和 15 秒轮询', async () => {
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(adminState())
      .mockResolvedValue(adminState({ engine_runtime: { worker_id: 'worker-b' } }));
    render(<SystemSettingsPage />);
    await tick();
    expect(api.getAdminState).toHaveBeenCalledTimes(1);
    expect(screen.getByText('worker-a')).toBeInTheDocument();
    expectNoStatus();

    click('重载配置');
    await tick();

    expect(api.reloadConfig).toHaveBeenCalledWith('admin-token');
    expect(api.getAdminState).toHaveBeenCalledTimes(2);
    expect(screen.getByText('worker-b')).toBeInTheDocument();
    expect(statusText()).toBe('配置已重载');

    await tick(SYSTEM_POLL_MS - 1);
    expect(api.getAdminState).toHaveBeenCalledTimes(2);

    await tick(1);
    expect(api.getAdminState).toHaveBeenCalledTimes(3);
    expect(statusText()).toBe('配置已重载');
  });

  it('轮询失败只接在提示后面，不进操作日志', async () => {
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(adminState())
      .mockResolvedValueOnce(adminState())
      .mockRejectedValueOnce(new Error('503 系统状态拉不到'))
      .mockResolvedValue(adminState());
    render(<SystemSettingsPage />);
    await tick();

    click('重载配置');
    await tick();
    expect(statusText()).toBe('配置已重载');

    await tick(SYSTEM_POLL_MS);
    expect(statusText()).toBe('配置已重载。503 系统状态拉不到');

    const logs = useSettingsSelectionStore.getState().systemLogs;
    expect(logs).toHaveLength(1);
    expect(logs[0]).toMatch(/ 配置已重载$/);

    await tick(SYSTEM_POLL_MS);
    expect(statusText()).toBe('配置已重载');
    expect(useSettingsSelectionStore.getState().systemLogs).toHaveLength(1);
  });

  it('点「刷新状态」把结果和原因一起清掉', async () => {
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(adminState())
      .mockResolvedValueOnce(adminState())
      .mockRejectedValueOnce(new Error('503 系统状态拉不到'))
      .mockResolvedValue(adminState());
    render(<SystemSettingsPage />);
    await tick();

    click('重载配置');
    await tick();
    await tick(SYSTEM_POLL_MS);
    expect(statusText()).toBe('配置已重载。503 系统状态拉不到');

    click('刷新状态');
    await tick();

    expect(api.getAdminState).toHaveBeenCalledTimes(4);
    expectNoStatus();
  });
});

describe('系统页两次读取撞车', () => {
  it('手动刷新被轮询顶掉，慢回来的那份状态不落地，转圈照样收掉', async () => {
    const slow = deferred<AdminState>();
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(adminState())
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue(adminState({ engine_runtime: { worker_id: 'worker-b' } }));
    render(<SystemSettingsPage />);
    await tick();
    expect(screen.getByText('worker-a')).toBeInTheDocument();

    click('刷新状态');
    await tick();
    expect(screen.getByRole('button', { name: '正在刷新…' })).toBeDisabled();

    await tick(SYSTEM_POLL_MS);
    expect(api.getAdminState).toHaveBeenCalledTimes(3);
    expect(screen.getByText('worker-b')).toBeInTheDocument();

    await act(async () => { slow.resolve(adminState({ engine_runtime: { worker_id: 'worker-c' } })); });

    expect(screen.getByText('worker-b')).toBeInTheDocument();
    expect(screen.queryByText('worker-c')).toBeNull();
    expect(screen.getByRole('button', { name: '刷新状态' })).not.toBeDisabled();
  });

  it('被顶掉的那次刷新失败也不报原因', async () => {
    const slow = deferred<AdminState>();
    vi.mocked(api.getAdminState)
      .mockResolvedValueOnce(adminState())
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue(adminState({ engine_runtime: { worker_id: 'worker-b' } }));
    render(<SystemSettingsPage />);
    await tick();

    click('刷新状态');
    await tick(SYSTEM_POLL_MS);
    expect(screen.getByText('worker-b')).toBeInTheDocument();

    await act(async () => { slow.reject(new Error('503 系统状态拉不到')); });

    expectNoStatus();
    expect(screen.getByRole('button', { name: '刷新状态' })).not.toBeDisabled();
  });
});

describe('系统页写完发现换了人', () => {
  it('重载配置之后不写提示、不进操作日志，也不拿旧 token 重读', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    vi.mocked(api.reloadConfig).mockImplementation(async () => { swapToken(); return { ok: true }; });
    render(<SystemSettingsPage />);
    await tick();

    click('重载配置');
    await tick();

    expect(api.reloadConfig).toHaveBeenCalledWith('admin-token');
    expectNoStatus();
    expect(useSettingsSelectionStore.getState().systemLogs).toEqual([]);
    // 第二次是换人之后重挂轮询自己读的，不是收尾那次。
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('重启引擎之后不写提示，也不进操作日志', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    vi.mocked(api.restartEngine).mockImplementation(async () => {
      swapToken();
      return { scheduled: true, target: 'engine' };
    });
    render(<SystemSettingsPage />);
    await tick();

    click('重启引擎');
    await tick();

    expect(api.restartEngine).toHaveBeenCalledWith('admin-token');
    expectNoStatus();
    expect(useSettingsSelectionStore.getState().systemLogs).toEqual([]);
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('重载失败之后不写原因，也不进操作日志', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    vi.mocked(api.reloadConfig).mockImplementation(async () => { swapToken(); throw new Error('503 配置重载失败'); });
    render(<SystemSettingsPage />);
    await tick();

    click('重载配置');
    await tick();

    expect(api.reloadConfig).toHaveBeenCalledWith('admin-token');
    expect(screen.queryByText('503 配置重载失败')).toBeNull();
    expectNoStatus();
    expect(useSettingsSelectionStore.getState().systemLogs).toEqual([]);
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('重启失败之后不写原因，也不进操作日志', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    vi.mocked(api.restartEngine).mockImplementation(async () => { swapToken(); throw new Error('503 重启接口不通'); });
    render(<SystemSettingsPage />);
    await tick();

    click('重启引擎');
    await tick();

    expect(api.restartEngine).toHaveBeenCalledWith('admin-token');
    expect(screen.queryByText('503 重启接口不通')).toBeNull();
    expectNoStatus();
    expect(useSettingsSelectionStore.getState().systemLogs).toEqual([]);
    expect(stateTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });
});

describe('换人登录之后', () => {
  it('审批页丢掉上一个会话的「已允许审批」', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(withApproval());
    render(<ApprovalsSettingsPage />);
    await tick();

    click('允许');
    await tick();
    expect(statusText()).toBe('已允许审批');

    await act(async () => { swapToken(); });
    await tick();

    expectNoStatus();
    expect(stateTokens()).toEqual(['admin-token', 'admin-token', OTHER_TOKEN]);
  });

  it('审批页丢掉上一个会话读取失败的原因，不等新会话读完', async () => {
    const slow = deferred<AdminState>();
    vi.mocked(api.getAdminState).mockImplementation(async (token) => {
      if (token === 'admin-token') throw new Error('503 待审批列表拉不到');
      return slow.promise;
    });
    render(<ApprovalsSettingsPage />);
    await tick();
    expect(statusText()).toBe('503 待审批列表拉不到');

    await act(async () => { swapToken(); });
    await tick();

    expectNoStatus();
    await act(async () => { slow.resolve(adminState()); });
  });

  it('系统页丢掉上一个会话的「配置已重载」，操作日志照留', async () => {
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    render(<SystemSettingsPage />);
    await tick();

    click('重载配置');
    await tick();
    expect(statusText()).toBe('配置已重载');

    await act(async () => { swapToken(); });
    await tick();

    expectNoStatus();
    // 操作日志是跨页面的流水，不跟着页面重挂载清掉。
    expect(useSettingsSelectionStore.getState().systemLogs).toHaveLength(1);
    expect(stateTokens()).toEqual(['admin-token', 'admin-token', OTHER_TOKEN]);
  });

  it('系统页丢掉上一个会话的重启拒绝原因', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getAdminState).mockResolvedValue(adminState());
    vi.mocked(api.restartEngine).mockResolvedValue({ scheduled: false, target: 'engine', reason: 'no process manager' });
    render(<SystemSettingsPage />);
    await tick();

    click('重启引擎');
    await tick();
    expect(statusText()).toBe('未能重启：调度器没有在管理引擎进程');

    await act(async () => { swapToken(); });
    await tick();

    expectNoStatus();
  });
});

describe('节点文件页的提示', () => {
  beforeEach(() => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getNodeFiles).mockResolvedValue([nodeFile('qq.orchestrator.yaml')]);
    vi.mocked(api.getNodeFileRaw).mockResolvedValue('id: qq');
    vi.mocked(api.updateNodeFileRaw).mockResolvedValue({ ok: true });
    vi.mocked(api.makeNodeFileExample).mockResolvedValue({
      ok: true, created: true, path: 'config/nodes/qq.orchestrator.example.yaml',
    });
  });

  it('创建 Example 的结果熬过紧接着那次重读', async () => {
    vi.mocked(api.getNodeFiles)
      .mockResolvedValueOnce([nodeFile('qq.orchestrator.yaml')])
      .mockResolvedValue([nodeFile('qq.orchestrator.yaml'), nodeFile('qq.orchestrator.example.yaml', { is_example: true })]);
    render(<NodeFilesSettingsPage />);
    await tick();
    expect(api.getNodeFiles).toHaveBeenCalledTimes(1);

    click('从已保存文件创建 Example');
    await tick();

    expect(api.makeNodeFileExample).toHaveBeenCalledWith('admin-token', 'qq.orchestrator.yaml');
    expect(api.getNodeFiles).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('option', { name: 'qq.orchestrator.example.yaml · example' })).toBeInTheDocument();
    expect(statusText()).toBe('已创建 config/nodes/qq.orchestrator.example.yaml');
    // 没有未保存修改就一句都不问：读列表本身不再弹确认。
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('创建 Example 后重读失败把原因接在后面，点「刷新列表」两样都清', async () => {
    vi.mocked(api.getNodeFiles)
      .mockResolvedValueOnce([nodeFile('qq.orchestrator.yaml')])
      .mockRejectedValueOnce(new Error('503 节点文件列表拉不到'))
      .mockResolvedValue([nodeFile('qq.orchestrator.yaml')]);
    render(<NodeFilesSettingsPage />);
    await tick();

    click('从已保存文件创建 Example');
    await tick();
    expect(statusText()).toBe('已创建 config/nodes/qq.orchestrator.example.yaml。503 节点文件列表拉不到');

    click('刷新列表');
    await tick();

    expect(api.getNodeFiles).toHaveBeenCalledTimes(3);
    expectNoStatus();
  });

  it('有未保存修改时刷新被取消就什么都不动，提示也留着', async () => {
    vi.mocked(api.updateNodeFileRaw).mockRejectedValue(new Error('写不进去：403'));
    render(<NodeFilesSettingsPage />);
    await tick();

    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: 'id: qq\nname: 改过' } });
    click('保存当前文件');
    await tick();
    expect(statusText()).toBe('写不进去：403');

    vi.mocked(window.confirm).mockReturnValue(false);
    click('刷新列表');
    await tick();

    expect(window.confirm).toHaveBeenCalledWith('当前文件还有未保存修改，确定离开或重新加载吗？');
    expect(api.getNodeFiles).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('写不进去：403');
    expect(screen.getByLabelText('文件内容')).toHaveValue('id: qq\nname: 改过');

    vi.mocked(window.confirm).mockReturnValue(true);
    click('刷新列表');
    await tick();

    expect(api.getNodeFiles).toHaveBeenCalledTimes(2);
    expectNoStatus();
    expect(screen.getByLabelText('文件内容')).toHaveValue('id: qq');
  });

  it('换文件立刻清掉上一份的提示，读失败再显示原因', async () => {
    const second = deferred<string>();
    vi.mocked(api.getNodeFiles).mockResolvedValue([nodeFile('a.yaml'), nodeFile('b.yaml')]);
    vi.mocked(api.getNodeFileRaw).mockImplementation(async (_token, name) => (
      name === 'a.yaml' ? 'id: a' : second.promise
    ));
    render(<NodeFilesSettingsPage />);
    await tick();

    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: 'id: a\nname: 改过' } });
    click('保存当前文件');
    await tick();
    expect(statusText()).toBe('已保存 a.yaml');

    fireEvent.change(screen.getByLabelText('当前文件'), { target: { value: 'b.yaml' } });
    expectNoStatus();

    await act(async () => { second.reject(new Error('读不到 b.yaml：404')); });
    expect(statusText()).toBe('读不到 b.yaml：404');
  });
});

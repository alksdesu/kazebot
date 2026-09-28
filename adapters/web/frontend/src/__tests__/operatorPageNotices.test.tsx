// 审批 / 系统 / 节点文件三页的提示：操作结果熬得过紧接着的重读和后台轮询，
// 轮询失败把原因接在后面，下一次读成功只去掉原因，手动刷新才两样都清。
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

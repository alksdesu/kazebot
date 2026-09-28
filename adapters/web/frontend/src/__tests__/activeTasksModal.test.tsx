// 活跃任务弹窗的提示。五秒一次的轮询会不停重读，取消的结果不能被它顺手冲掉。
import { act, cleanup, fireEvent, render, screen, type RenderResult } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { cancelTask, fetchActiveTasks, type ActiveTask } from '../api/supervisorClient';
import { ActiveTasksModal } from '../components/dashboard/ActiveTasksModal';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  cancelTask: vi.fn(),
  fetchActiveTasks: vi.fn(),
}));

const TASK_ID = 'task-aaaaaaaa1111';
const CANCEL_LABEL = `取消任务 ${TASK_ID}`;

const task = (over: Partial<ActiveTask> = {}): ActiveTask => ({
  task_id: TASK_ID,
  session_id: 'sess-1',
  node_id: 'qq.chat',
  status: 'running',
  kind: 'node',
  created_at: '2026-06-04T10:00:00.000Z',
  updated_at: '2026-06-04T10:00:30.000Z',
  worker_id: 'worker-a',
  caller_task_id: null,
  input_summary: '在查日志',
  cancel_requested: false,
  current_phase: '',
  current_detail: '',
  ...over,
});

const noop = () => {};

const OTHER_TOKEN = 'another-admin-token';

/** fetchActiveTasks 每一次用的 token，按调用先后排。 */
const taskTokens = (): (string | undefined)[] => vi.mocked(fetchActiveTasks).mock.calls.map(([token]) => token);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  useChatStore.setState({ taskActivities: {} });
  vi.mocked(fetchActiveTasks).mockResolvedValue([task()]);
  vi.mocked(cancelTask).mockResolvedValue(undefined);
  // 轮询在弹窗打开时就挂上了，假定时器得先于 render 接管才推得动它。
  vi.useFakeTimers();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const open = async () => {
  const view = render(<ActiveTasksModal onClose={noop} open />);
  await act(async () => {});
  return view;
};
const tick = (ms = 5000) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const clickCancel = () => act(async () => {
  fireEvent.click(screen.getByRole('button', { name: CANCEL_LABEL }));
});

describe('活跃任务弹窗的提示', () => {
  it('取消失败的原因熬得过一轮轮询', async () => {
    vi.mocked(cancelTask).mockRejectedValue(new Error('这个任务已经结束了，取消不了。'));
    await open();

    await clickCancel();
    expect(screen.getByText('这个任务已经结束了，取消不了。')).toBeInTheDocument();

    await tick();
    expect(fetchActiveTasks).toHaveBeenCalledTimes(2);
    expect(screen.getByText('这个任务已经结束了，取消不了。')).toBeInTheDocument();
  });

  it('再点一次取消，开头就把上一次的结果抹掉', async () => {
    let unblock!: () => void;
    const pending = new Promise<void>((resolve) => { unblock = resolve; });
    vi.mocked(cancelTask)
      .mockRejectedValueOnce(new Error('这个任务已经结束了，取消不了。'))
      .mockReturnValueOnce(pending);
    await open();

    await clickCancel();
    expect(screen.getByText('这个任务已经结束了，取消不了。')).toBeInTheDocument();

    await clickCancel();
    expect(screen.queryByText('这个任务已经结束了，取消不了。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: CANCEL_LABEL })).toBeDisabled();

    await act(async () => { unblock(); });
  });

  it('关掉再打开，取消的结果和读取失败的原因都清掉', async () => {
    vi.mocked(fetchActiveTasks)
      .mockResolvedValueOnce([task()])
      .mockRejectedValueOnce(new Error('活跃任务刷新失败。'))
      .mockResolvedValue([task()]);
    vi.mocked(cancelTask).mockRejectedValue(new Error('这个任务已经结束了，取消不了。'));
    const view = await open();

    await clickCancel();
    await tick();
    expect(screen.getByText('这个任务已经结束了，取消不了。活跃任务刷新失败。')).toBeInTheDocument();

    await act(async () => { view.rerender(<ActiveTasksModal onClose={noop} open={false} />); });
    await act(async () => { view.rerender(<ActiveTasksModal onClose={noop} open />); });

    expect(screen.queryByText(/这个任务已经结束了/)).not.toBeInTheDocument();
    expect(screen.queryByText(/活跃任务刷新失败/)).not.toBeInTheDocument();
    expect(screen.getByText('qq.chat', { exact: false })).toBeInTheDocument();
  });

  it('读取失败的原因在下一次轮询成功后消失', async () => {
    vi.mocked(fetchActiveTasks)
      .mockRejectedValueOnce(new Error('活跃任务刷新失败。'))
      .mockResolvedValue([task()]);
    await open();
    expect(screen.getByText('活跃任务刷新失败。')).toBeInTheDocument();

    await tick();
    expect(screen.queryByText('活跃任务刷新失败。')).not.toBeInTheDocument();
  });
});

describe('取消完发现换了人', () => {
  /** 换人登录：取消还没回来就改掉 store 里的 token。 */
  const swapToken = () => act(async () => { useSettingsStore.setState({ adminToken: OTHER_TOKEN }); });

  it('取消成功也不拿旧 token 再刷一次列表', async () => {
    const slow = deferred<void>();
    vi.mocked(cancelTask).mockReturnValueOnce(slow.promise);
    await open();

    await clickCancel();
    await swapToken();
    // 这一次是换人之后重挂轮询自己读的。
    expect(taskTokens()).toEqual(['admin-token', OTHER_TOKEN]);

    await act(async () => { slow.resolve(); });

    expect(cancelTask).toHaveBeenCalledWith('admin-token', TASK_ID);
    expect(taskTokens()).toEqual(['admin-token', OTHER_TOKEN]);
  });

  it('取消失败也不把原因写到新会话的弹窗上', async () => {
    const slow = deferred<void>();
    vi.mocked(cancelTask).mockReturnValueOnce(slow.promise);
    await open();

    await clickCancel();
    await swapToken();

    await act(async () => { slow.reject(new Error('这个任务已经结束了，取消不了。')); });

    expect(screen.queryByText('这个任务已经结束了，取消不了。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: CANCEL_LABEL })).not.toBeDisabled();
  });
});

describe('上一轮没回来', () => {
  it('挂住时推几个间隔都不再发，放行后接着轮询', async () => {
    const slow = deferred<ActiveTask[]>();
    vi.mocked(fetchActiveTasks)
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue([task()]);
    await open();
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);

    await tick();
    await tick();
    await tick();
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);
    expect(screen.getByText('正在加载活跃任务…')).toBeInTheDocument();

    await act(async () => { slow.resolve([]); });
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);
    expect(screen.getByText('当前没有活跃任务')).toBeInTheDocument();

    await tick();
    expect(fetchActiveTasks).toHaveBeenCalledTimes(2);
    expect(screen.getByText('在查日志')).toBeInTheDocument();
  });

  it('头一次读挂住时第一个间隔就不发，放行后下一个间隔正好补一次', async () => {
    const slow = deferred<ActiveTask[]>();
    vi.mocked(fetchActiveTasks)
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue([task({ input_summary: '换了一件事做' })]);
    await open();
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);

    await tick(4999);
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);

    await tick(1);
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);
    expect(screen.getByText('正在加载活跃任务…')).toBeInTheDocument();

    await act(async () => { slow.resolve([task()]); });
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);
    expect(screen.getByText('在查日志')).toBeInTheDocument();

    await tick(4999);
    expect(fetchActiveTasks).toHaveBeenCalledTimes(1);

    await tick(1);
    expect(fetchActiveTasks).toHaveBeenCalledTimes(2);
    expect(screen.getByText('换了一件事做')).toBeInTheDocument();
  });
});

describe('在途的读取撞上新一轮', () => {
  // 轮询自己撞不了车（上一轮没回来就跳过这一轮），只有关掉再打开才让在途那次和新一轮并排。
  const reopen = async (view: RenderResult) => {
    await act(async () => { view.rerender(<ActiveTasksModal onClose={noop} open={false} />); });
    await act(async () => { view.rerender(<ActiveTasksModal onClose={noop} open />); });
  };

  it('慢的那次回来不把旧列表放回去，转圈由最新那次收掉', async () => {
    const slow = deferred<ActiveTask[]>();
    vi.mocked(fetchActiveTasks)
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue([]);
    const view = await open();
    expect(screen.getByText('正在加载活跃任务…')).toBeInTheDocument();

    await reopen(view);
    expect(screen.getByText('当前没有活跃任务')).toBeInTheDocument();

    await act(async () => { slow.resolve([task({ input_summary: '旧的那一轮' })]); });

    expect(screen.getByText('当前没有活跃任务')).toBeInTheDocument();
    expect(screen.queryByText('旧的那一轮')).not.toBeInTheDocument();
  });

  it('慢的那次失败不报原因', async () => {
    const slow = deferred<ActiveTask[]>();
    vi.mocked(fetchActiveTasks)
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValue([task()]);
    const view = await open();

    await reopen(view);
    expect(screen.getByText('在查日志')).toBeInTheDocument();

    await act(async () => { slow.reject(new Error('503 活跃任务读不到')); });

    expect(screen.queryByText('503 活跃任务读不到')).not.toBeInTheDocument();
  });
});

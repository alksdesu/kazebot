// 活跃任务弹窗的提示。五秒一次的轮询会不停重读，取消的结果不能被它顺手冲掉。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
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
const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(5000); });
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

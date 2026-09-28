// 运行诊断页。engine 崩了 supervisor 照样活着，这一页是唯一看得出来的地方，
// 所以最要紧的是「挂了要看得出来是挂了」，不能显示成空白或正常。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  checkHealth,
  getRuntimeStatus,
  listLogFiles,
  readLogTail,
  retryEngine,
} from '../api/supervisorClient';
import { RuntimeSettingsPage } from '../components/settings/pages/RuntimeSettingsPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  checkHealth: vi.fn(),
  getRuntimeStatus: vi.fn(),
  listLogFiles: vi.fn(),
  readLogTail: vi.fn(),
  retryEngine: vi.fn(),
}));

const worker = (over: Record<string, unknown> = {}) => ({
  alive: true, pid: 9001, uptime_sec: 120, failures: 0, respawns: 0,
  given_up: false, last_exit_code: null, last_log: '', retry_in_sec: 0,
  generation: 'abcd1234ef', ...over,
});

const status = (over: Record<string, unknown> = {}) => ({
  supervised: true,
  workers: { 'engine-1': worker(), 'engine-2': worker({ pid: 9002 }) },
  tasks: { queued: 0, running: 0 },
  started_at: '2026-08-22T19:40:09+00:00',
  uptime_sec: 3600,
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(checkHealth).mockResolvedValue({ status: 'ok', uptime_seconds: 3600 } as any);
  vi.mocked(getRuntimeStatus).mockResolvedValue(status() as any);
  vi.mocked(listLogFiles).mockResolvedValue([
    { name: 'supervisor.log', size: 28990, modified: 1000 },
    { name: 'engine-1-20260822.log', size: 220, modified: 900 },
  ]);
  vi.mocked(readLogTail).mockResolvedValue({ text: '[engine] worker ready', truncated: false });
  vi.mocked(retryEngine).mockResolvedValue(undefined);
});

afterEach(() => { vi.useRealTimers(); });

const rowFor = (name: string) =>
  screen.getAllByText(name).map((node) => node.parentElement!).find(Boolean)!;

describe('worker 状态', () => {
  it('两个 worker 都摆出来，带运行时长', async () => {
    render(<RuntimeSettingsPage />);
    await screen.findByText('engine-1');

    expect(screen.getByText('engine-2')).toBeTruthy();
    expect(within(rowFor('engine-1')).getByText(/运行中/)).toBeTruthy();
  });

  it('挂掉的 worker 说的是挂了，不是一片空白', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(status({
      workers: { 'engine-1': worker({ alive: false, pid: null, retry_in_sec: 4 }) },
    }) as any);
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText(/4 秒后重拉/)).toBeTruthy());
  });

  it('状态不只靠颜色表达', async () => {
    // 状态灯是 aria-hidden 的，读屏只能拿到这句文字。
    render(<RuntimeSettingsPage />);
    await screen.findByText('engine-1');

    expect(within(rowFor('engine-1')).getByText(/运行中/)).toBeTruthy();
  });

  it('不由 supervisor 管的部署说的是测不到，不是都挂了', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(status({ supervised: false, workers: {} }) as any);
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText(/测不到/)).toBeTruthy());
  });

  it('队列深度看得见', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(status({ tasks: { queued: 3, running: 2 } }) as any);
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText(/等待 3 个，执行中 2 个/)).toBeTruthy());
  });
});

const givenUp = () => status({
  workers: {
    'engine-1': worker({
      alive: false, pid: null, given_up: true, failures: 5, last_exit_code: 13,
      last_log: 'PermissionError: /opt/kazebot/tools/__init__.py',
    }),
  },
});

describe('停止重试之后', () => {
  it('置顶一条，把最后那次的原因摆出来', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(givenUp() as any);
    render(<RuntimeSettingsPage />);

    await screen.findByText(/engine-1 起不来，已停止重试/);
    expect(screen.getByText(/连续失败 5 次.*退出码 13/)).toBeTruthy();
    // 排障要的就是这一句，不摆出来还得自己去翻服务器。
    expect(screen.getByText(/PermissionError/)).toBeTruthy();
  });

  it('手动重试能点，点完重新拉状态', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(givenUp() as any);
    render(<RuntimeSettingsPage />);

    fireEvent.click(await screen.findByRole('button', { name: '手动重试' }));

    await waitFor(() => expect(retryEngine).toHaveBeenCalledWith('admin-token'));
    await waitFor(() => expect(getRuntimeStatus).toHaveBeenCalledTimes(2));
  });

  it('一切正常时没有这条红字', async () => {
    render(<RuntimeSettingsPage />);
    await screen.findByText('engine-1');

    expect(screen.queryByRole('button', { name: '手动重试' })).toBeNull();
  });
});

describe('提示的去留', () => {
  const mount = async () => {
    // 轮询在 mount 时就挂上了，假定时器得先于 render 接管才推得动它。
    vi.useFakeTimers();
    await act(async () => { render(<RuntimeSettingsPage />); });
  };
  const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(5000); });

  it('重试失败的原因熬得过一轮轮询', async () => {
    vi.mocked(getRuntimeStatus).mockResolvedValue(givenUp() as any);
    vi.mocked(retryEngine).mockRejectedValue(new Error('拉不起来：端口 8770 被占用'));
    await mount();

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '手动重试' })); });
    expect(screen.getByText('拉不起来：端口 8770 被占用')).toBeInTheDocument();

    await tick();
    expect(getRuntimeStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByText('拉不起来：端口 8770 被占用')).toBeInTheDocument();
  });

  it('再点一次重试，开头就把上一次的原因抹掉', async () => {
    let unblock!: () => void;
    const pending = new Promise<void>((resolve) => { unblock = resolve; });
    vi.mocked(getRuntimeStatus).mockResolvedValue(givenUp() as any);
    vi.mocked(retryEngine)
      .mockRejectedValueOnce(new Error('拉不起来：端口 8770 被占用'))
      .mockReturnValueOnce(pending);
    await mount();

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '手动重试' })); });
    expect(screen.getByText('拉不起来：端口 8770 被占用')).toBeInTheDocument();

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '手动重试' })); });
    expect(screen.queryByText('拉不起来：端口 8770 被占用')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '正在重试…' })).toBeDisabled();

    await act(async () => { unblock(); });
  });

  it('轮询失败的原因在下一次轮询成功后消失', async () => {
    vi.mocked(getRuntimeStatus)
      .mockResolvedValueOnce(status() as any)
      .mockRejectedValueOnce(new Error('502 Bad Gateway'))
      .mockResolvedValue(status() as any);
    await mount();
    expect(screen.queryByText('502 Bad Gateway')).not.toBeInTheDocument();

    await tick();
    expect(screen.getByText('502 Bad Gateway')).toBeInTheDocument();

    await tick();
    expect(screen.queryByText('502 Bad Gateway')).not.toBeInTheDocument();
  });

  it('重试失败后轮询也失败，两句都在', async () => {
    vi.mocked(getRuntimeStatus)
      .mockResolvedValueOnce(givenUp() as any)
      .mockRejectedValue(new Error('502 Bad Gateway'));
    vi.mocked(retryEngine).mockRejectedValue(new Error('拉不起来：端口 8770 被占用'));
    await mount();

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '手动重试' })); });
    await tick();

    expect(screen.getByText('拉不起来：端口 8770 被占用。502 Bad Gateway')).toBeInTheDocument();
  });
});

describe('日志', () => {
  it('列出文件并默认打开最新那个', async () => {
    render(<RuntimeSettingsPage />);
    // 选中的那个在列表和标题里各出现一次。
    await screen.findAllByText('supervisor.log');

    expect(screen.getByText('engine-1-20260822.log')).toBeTruthy();
    await waitFor(() => expect(readLogTail).toHaveBeenCalledWith('admin-token', 'supervisor.log', 500));
  });

  it('换一个文件就读那个', async () => {
    render(<RuntimeSettingsPage />);
    fireEvent.click(await screen.findByText('engine-1-20260822.log'));

    await waitFor(() =>
      expect(readLogTail).toHaveBeenCalledWith('admin-token', 'engine-1-20260822.log', 500));
  });

  it('截断了要说一声，不然会以为日志就这么点', async () => {
    vi.mocked(readLogTail).mockResolvedValue({ text: 'tail only', truncated: true });
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText(/只显示了尾部/)).toBeTruthy());
  });

  it('读失败时把原因说出来', async () => {
    vi.mocked(readLogTail).mockRejectedValue(new Error('404 没有这个日志文件'));
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText('404 没有这个日志文件')).toBeTruthy());
  });

  it('一个日志都没有时不报错', async () => {
    vi.mocked(listLogFiles).mockResolvedValue([]);
    render(<RuntimeSettingsPage />);

    await waitFor(() => expect(screen.getByText(/还没有日志文件/)).toBeTruthy());
    expect(readLogTail).not.toHaveBeenCalled();
  });
});

describe('没登录', () => {
  it('提示去登录，不去打接口', async () => {
    useSettingsStore.setState({ adminToken: '', isAuthenticated: false });
    render(<RuntimeSettingsPage />);

    expect(screen.getByText(/请先在通用页面登录/)).toBeTruthy();
    expect(getRuntimeStatus).not.toHaveBeenCalled();
  });
});

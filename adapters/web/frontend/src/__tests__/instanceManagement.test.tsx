import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountPage } from '../console/AccountPage';
import * as api from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getInstances: vi.fn(), getQqAccount: vi.fn(), getQqLoginQrcode: vi.fn(),
  createInstance: vi.fn(), deleteInstance: vi.fn(), getInstanceProgress: vi.fn(),
  qqEnterLoginMode: vi.fn(), qqQuickLogin: vi.fn(), qqPinAccount: vi.fn(),
}));

const rows: api.ConsoleInstance[] = [
  { uin: '1000001', label: '主实例', path: '', current: true, idx: 0 },
  { uin: '1000002', label: '第二实例', path: '/i/1000002', current: false, idx: 1 },
];
const account: api.QqAccount = { configured: true, reachable: true, is_login: true, online: true, uin: '1000001', nick: '当前机器人' };
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({ adminToken: 'current-token', isAuthenticated: true });
  vi.mocked(api.getInstances).mockResolvedValue(rows);
  vi.mocked(api.getQqAccount).mockResolvedValue(account);
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unexpected request')));
  window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account');
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('实例管理入口', () => {
  it.each([
    ['NapCat 未配置', { configured: false }],
    ['NapCat 离线', { configured: true, reachable: false }],
  ])('%s 时仍可进入其他实例，不再显示模板', async (_label, state) => {
    vi.mocked(api.getQqAccount).mockResolvedValue(state);
    render(<AccountPage />);
    expect(await screen.findByRole('heading', { name: '多开实例' })).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '去它的控制台' })).toHaveAttribute('href', '/i/1000002/web/?view=settings&tab=qq-account');
    expect(screen.queryByText('多实例总览与模板')).not.toBeInTheDocument();
    expect(screen.queryByText('已保存的模板')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(api.getInstances).toHaveBeenCalledTimes(1);
  });

  it('账号读取失败不阻塞实例列表，错误保留并可重试', async () => {
    vi.mocked(api.getQqAccount).mockRejectedValueOnce(new Error('账号状态读取失败')).mockResolvedValue(account);
    render(<AccountPage />);
    expect(await screen.findByText('账号状态读取失败')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expect(await screen.findByText('当前机器人')).toBeInTheDocument();
  });

  it('列表读取失败不冒充未启用多开，重试后恢复', async () => {
    vi.mocked(api.getInstances).mockRejectedValueOnce(new Error('实例读取失败')).mockResolvedValue(rows);
    render(<AccountPage />);
    expect(await screen.findByText('实例读取失败')).toBeInTheDocument();
    expect(screen.queryByText(/还没启用多开/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新实例列表' }));
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    expect(api.getInstances).toHaveBeenCalledTimes(2);
  });

  it('令牌变更时清空旧视图，拒绝旧账号和实例列表的迟到结果', async () => {
    const oldAccount = deferred<api.QqAccount>();
    const oldRows = deferred<api.ConsoleInstance[]>();
    vi.mocked(api.getQqAccount).mockReturnValueOnce(oldAccount.promise).mockResolvedValue({ ...account, nick: '新凭据机器人' });
    vi.mocked(api.getInstances).mockReturnValueOnce(oldRows.promise).mockResolvedValue([{ ...rows[0], label: '新凭据实例' }]);
    render(<AccountPage />);
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    expect(await screen.findByText('新凭据机器人')).toBeInTheDocument();
    await act(async () => { oldAccount.resolve({ ...account, nick: '旧凭据机器人' }); oldRows.resolve(rows); });
    expect(screen.queryByText('旧凭据机器人')).not.toBeInTheDocument();
    expect(screen.queryByText('第二实例')).not.toBeInTheDocument();
    expect(screen.getByText('新凭据实例')).toBeInTheDocument();
  });

  it('凭据切换 A → B → A 后，第一次 A 的响应仍然失效', async () => {
    const oldAccount = deferred<api.QqAccount>();
    const oldRows = deferred<api.ConsoleInstance[]>();
    vi.mocked(api.getQqAccount).mockReturnValueOnce(oldAccount.promise).mockResolvedValue({ ...account, nick: '最新机器人' });
    vi.mocked(api.getInstances).mockReturnValueOnce(oldRows.promise).mockResolvedValue([{ ...rows[0], label: '最新实例' }]);
    render(<AccountPage />);
    act(() => useSettingsStore.setState({ adminToken: 'other-token' }));
    await screen.findByText('最新机器人');
    act(() => useSettingsStore.setState({ adminToken: 'current-token' }));
    await screen.findByText('最新实例');
    await act(async () => { oldAccount.resolve({ ...account, nick: '第一轮旧账号' }); oldRows.reject(new Error('第一轮旧错误')); });
    expect(screen.queryByText('第一轮旧账号')).not.toBeInTheDocument();
    expect(screen.queryByText('第一轮旧错误')).not.toBeInTheDocument();
    expect(screen.getByText('最新实例')).toBeInTheDocument();
  });

  it('共享一份实例清单，归属别的实例的 QQ 只能进入其控制台', async () => {
    vi.mocked(api.getQqAccount).mockResolvedValue({ ...account, quick_login: [{ uin: '1000002', nick: '另一个QQ' }] });
    render(<AccountPage />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByRole('link', { name: '去它的控制台' })).toHaveAttribute('href', '/i/1000002/web/?view=settings&tab=qq-account');
    expect(within(quick).queryByRole('button', { name: '切到这个号' })).not.toBeInTheDocument();
    expect(api.getInstances).toHaveBeenCalledTimes(1);
  });

  it('实例清单读取失败时不能绕过账号归属检查进行快速登录', async () => {
    vi.mocked(api.getInstances).mockRejectedValue(new Error('归属未知'));
    vi.mocked(api.getQqAccount).mockResolvedValue({ ...account, quick_login: [{ uin: '1000002', nick: '另一个QQ' }] });
    render(<AccountPage />);
    await screen.findByText('归属未知');
    expect(screen.getByRole('button', { name: '切到这个号' })).toBeDisabled();
    expect(screen.getByText(/实例归属尚未确认/)).toBeInTheDocument();
    expect(api.qqQuickLogin).not.toHaveBeenCalled();
  });

  it('实例整页跳转沿用原生离开保护，未放行时保留实例与草稿', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<AccountPage />);
    const select = await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例 QQ 号' }), { target: { value: '1000003' } });
    const link = screen.getByRole('link', { name: '去它的控制台' });
    expect(link).toHaveAttribute('href', '/i/1000002/web/?view=settings&tab=qq-account');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    expect(select).toHaveValue('');
    expect(screen.getByRole('textbox', { name: '新实例 QQ 号' })).toHaveValue('1000003');
  });

  it('实例链接保留另开标签能力，不提示丢弃当前页草稿', async () => {
    function Draft() { useUnsavedChanges(true); return null; }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<><Draft /><AccountPage /></>);
    const link = await screen.findByRole('link', { name: '去它的控制台' });
    const click = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true, button: 0 });
    let preventedByComponent = true;
    document.addEventListener('click', event => {
      preventedByComponent = event.defaultPrevented;
      event.preventDefault();
    }, { once: true });
    fireEvent(link, click);
    expect(preventedByComponent).toBe(false);
    expect(confirm).not.toHaveBeenCalled();
  });
});

describe('实例创建与归档', () => {
  it('创建只发给当前实例，提交期间冻结输入且不重复发起，失败保留草稿', async () => {
    const pending = deferred<api.InstancePlan>();
    vi.mocked(api.createInstance).mockReturnValue(pending.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountPage />);
    await screen.findByRole('combobox', { name: '切换实例' });
    const number = screen.getByRole('textbox', { name: '新实例 QQ 号' });
    const label = screen.getByRole('textbox', { name: '实例备注' });
    fireEvent.change(number, { target: { value: '1000003' } });
    fireEvent.change(label, { target: { value: '新实例备注' } });
    const create = screen.getByRole('button', { name: '建实例' });
    fireEvent.click(create); fireEvent.click(create);
    expect(number).toBeDisabled(); expect(label).toBeDisabled();
    expect(api.createInstance).toHaveBeenCalledExactlyOnceWith('current-token', '1000003', '新实例备注');
    await act(async () => pending.reject(new Error('创建失败')));
    expect(screen.getByRole('alert')).toHaveTextContent('创建失败');
    expect(number).toHaveValue('1000003'); expect(label).toHaveValue('新实例备注');
    expect(create).toBeEnabled();
  });

  it.each(['0', '100', '123456789012', 'not-a-number', '1000002'])('拒绝无效或已存在的 QQ 号 %s', async value => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountPage />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例 QQ 号' }), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    expect(api.createInstance).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('创建确认取消后没有写请求或清空草稿', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<AccountPage />);
    await screen.findByRole('combobox', { name: '切换实例' });
    const number = screen.getByRole('textbox', { name: '新实例 QQ 号' });
    fireEvent.change(number, { target: { value: '1000003' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    expect(api.createInstance).not.toHaveBeenCalled();
    expect(number).toHaveValue('1000003');
  });

  it('归档要求准确输入号码，当前实例和主实例不能被删除', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('1000099');
    render(<AccountPage />);
    const remove = await screen.findByRole('button', { name: '停用并归档' });
    expect(screen.getAllByRole('button', { name: '停用并归档' })).toHaveLength(1);
    fireEvent.click(remove);
    expect(api.deleteInstance).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('输入的号对不上');
    prompt.mockReturnValue(null); fireEvent.click(remove);
    expect(api.deleteInstance).not.toHaveBeenCalled();
  });

  it('归档完成刷新共享实例清单，并显示归档而非新号登录提示', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('1000002');
    vi.mocked(api.deleteInstance).mockResolvedValue();
    vi.mocked(api.getInstanceProgress).mockResolvedValue({ uin: '1000002', lines: ['已归档'], finished: true, ok: true, detail: '' });
    render(<AccountPage />);
    const remove = await screen.findByRole('button', { name: '停用并归档' });
    vi.useFakeTimers();
    await act(async () => fireEvent.click(remove));
    vi.mocked(api.getInstances).mockResolvedValue([rows[0]]);
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(api.deleteInstance).toHaveBeenCalledExactlyOnceWith('current-token', '1000002');
    expect(api.getInstanceProgress).toHaveBeenCalledExactlyOnceWith('current-token', '1000002');
    expect(screen.getByText('实例已停用，工作区和聊天记录已归档。')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '切换实例' })).not.toBeInTheDocument();
    expect(screen.queryByText('第二实例')).not.toBeInTheDocument();
  });

  it('凭据变更后迟到的创建结果不会启动旧凭据轮询或覆盖新界面', async () => {
    const pending = deferred<api.InstancePlan>();
    vi.mocked(api.createInstance).mockReturnValue(pending.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountPage />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例 QQ 号' }), { target: { value: '1000003' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    await waitFor(() => expect(api.getInstances).toHaveBeenCalledWith('new-token'));
    vi.useFakeTimers();
    await act(async () => pending.resolve({ uin: '1000003', label: '旧请求', idx: 2, prefix: '/i/1000003', ports: {}, workspace: '/synthetic' }));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(api.getInstanceProgress).not.toHaveBeenCalled();
    expect(screen.queryByText('已提交，等 root 侧接手…')).not.toBeInTheDocument();
  });

  it('归档进度读取失败保留任务并自动重试，不重复提交归档', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('1000002');
    vi.mocked(api.deleteInstance).mockResolvedValue();
    vi.mocked(api.getInstanceProgress).mockRejectedValueOnce(new Error('暂时断线')).mockResolvedValue({ uin: '1000002', lines: ['完成'], finished: true, ok: true, detail: '' });
    render(<AccountPage />);
    const remove = await screen.findByRole('button', { name: '停用并归档' });
    vi.useFakeTimers();
    await act(async () => fireEvent.click(remove));
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(screen.getByRole('alert')).toHaveTextContent('读取进度失败，将自动重试：暂时断线');
    expect(remove).toBeDisabled();
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(screen.getByText('实例已停用，工作区和聊天记录已归档。')).toBeInTheDocument();
    expect(api.deleteInstance).toHaveBeenCalledTimes(1);
    expect(api.getInstanceProgress).toHaveBeenCalledTimes(2);
  });
});

describe('账号操作请求归属', () => {
  it('旧登录轮询迟到时不能替新凭据固定自动登录账号', async () => {
    const pending = deferred<api.QqAccount>();
    let oldReads = 0;
    vi.mocked(api.getQqAccount).mockImplementation(token => token === 'new-token'
      ? Promise.resolve({ ...account, nick: '新机器人' })
      : oldReads++ ? pending.promise : Promise.resolve({ ...account, is_login: false }));
    vi.useFakeTimers();
    await act(async () => render(<AccountPage />));
    expect(screen.getByText('未登录')).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(api.getQqAccount).toHaveBeenCalledTimes(2);
    await act(async () => useSettingsStore.setState({ adminToken: 'new-token' }));
    await act(async () => pending.resolve({ ...account, nick: '旧轮询机器人' }));
    expect(api.qqPinAccount).not.toHaveBeenCalled();
    expect(screen.getByText('新机器人')).toBeInTheDocument();
    expect(screen.queryByText('旧轮询机器人')).not.toBeInTheDocument();
  });

  it('旧凭据重启请求完成后不应对新账号启动登录轮询', async () => {
    const pending = deferred<void>();
    vi.mocked(api.qqEnterLoginMode).mockReturnValue(pending.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountPage />);
    await screen.findByText('当前机器人');
    fireEvent.click(screen.getByRole('button', { name: '换个号登录' }));
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    await waitFor(() => expect(api.getQqAccount).toHaveBeenCalledWith('new-token'));
    vi.useFakeTimers();
    await act(async () => pending.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(api.getQqAccount).toHaveBeenCalledTimes(2);
    expect(api.qqPinAccount).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: '扫码登录' })).not.toBeInTheDocument();
  });
});

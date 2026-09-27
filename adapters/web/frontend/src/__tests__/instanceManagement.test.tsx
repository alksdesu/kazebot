import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountWorkspaceSlot } from '../console/AccountWorkspace';
import { PlatformProvider } from '../console/platformContext';
import * as api from '../api/supervisorClient';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getInstances: vi.fn(), getPlatformAccount: vi.fn(), getPlatformLoginQrcode: vi.fn(),
  createInstance: vi.fn(), deleteInstance: vi.fn(), getInstanceProgress: vi.fn(),
  enterPlatformLoginMode: vi.fn(), switchPlatformAccount: vi.fn(), pinPlatformAccount: vi.fn(), getAccountScope: vi.fn(),
}));

const rows: api.ConsoleInstance[] = [
  { id: 'main', label: '主实例', path: '', current: true, idx: 0, accounts: { qq: '1000001' } },
  { id: 'xiaoyu', label: '第二实例', path: '/i/xiaoyu', current: false, idx: 1, accounts: { qq: '1000002' } },
];
const account: api.AccountStatus = { configured: true, reachable: true, is_login: true, online: true, account: '1000001', nick: '当前机器人' };
const plan = (id: string): api.InstancePlan => ({
  id, label: '', idx: 2, prefix: `/i/${id}`, ports: {}, workspace: '/synthetic', accounts: {},
});
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
  vi.mocked(api.getPlatformAccount).mockResolvedValue(account);
  vi.mocked(api.getAccountScope).mockResolvedValue({
    current_scope: '1000001', live_scope: '1000001', bot_alive: false, preview: null, ambiguous_sources: [],
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unexpected request')));
  window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account');
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('实例管理入口', () => {
  it.each([
    ['NapCat 未配置', { configured: false }],
    ['NapCat 离线', { configured: true, reachable: false }],
  ])('%s 时仍可进入其他实例，不再显示模板', async (_label, state) => {
    vi.mocked(api.getPlatformAccount).mockResolvedValue(state);
    render(<AccountWorkspaceSlot />);
    expect(await screen.findByRole('heading', { name: '多开实例' })).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '去它的控制台' })).toHaveAttribute('href', '/i/xiaoyu/web/?view=settings&tab=qq-account');
    expect(screen.queryByText('多实例总览与模板')).not.toBeInTheDocument();
    expect(screen.queryByText('已保存的模板')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(api.getInstances).toHaveBeenCalledTimes(1);
  });

  it('每行标出实例名和它登记的账号', async () => {
    vi.mocked(api.getInstances).mockResolvedValue([
      rows[0], { ...rows[1], accounts: { qq: '1000002', telegram: '777' } },
    ]);
    render(<AccountWorkspaceSlot />);
    const list = (await screen.findByRole('heading', { name: '多开实例' })).closest('section')!;
    expect(within(list).getByText('main')).toBeInTheDocument();
    expect(within(list).getByText('QQ 1000001')).toBeInTheDocument();
    expect(within(list).getByText('xiaoyu')).toBeInTheDocument();
    // 认不出的平台退回平台 id，不能把账号行整行吞掉。
    expect(within(list).getByText('QQ 1000002，telegram 777')).toBeInTheDocument();
  });

  it('还没建过实例时给出装机命令，也不给建实例的入口', async () => {
    vi.mocked(api.getInstances).mockResolvedValue([]);
    render(<AccountWorkspaceSlot />);
    expect(await screen.findByText('sudo deploy/install_provision.sh <主实例名> <当前QQ号>')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '建实例' })).not.toBeInTheDocument();
  });

  it('账号读取失败不阻塞实例列表，错误保留并可重试', async () => {
    vi.mocked(api.getPlatformAccount).mockRejectedValueOnce(new Error('账号状态读取失败')).mockResolvedValue(account);
    render(<AccountWorkspaceSlot />);
    expect(await screen.findByText('账号状态读取失败')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expect(await screen.findByText('当前机器人')).toBeInTheDocument();
  });

  it('列表读取失败不冒充未启用多开，重试后恢复', async () => {
    vi.mocked(api.getInstances).mockRejectedValueOnce(new Error('实例读取失败')).mockResolvedValue(rows);
    render(<AccountWorkspaceSlot />);
    expect(await screen.findByText('实例读取失败')).toBeInTheDocument();
    expect(screen.queryByText(/还没启用多开/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新实例列表' }));
    expect(await screen.findByRole('combobox', { name: '切换实例' })).toBeInTheDocument();
    expect(api.getInstances).toHaveBeenCalledTimes(2);
  });

  it('令牌变更时清空旧视图，拒绝旧账号和实例列表的迟到结果', async () => {
    const oldAccount = deferred<api.AccountStatus>();
    const oldRows = deferred<api.ConsoleInstance[]>();
    vi.mocked(api.getPlatformAccount).mockReturnValueOnce(oldAccount.promise).mockResolvedValue({ ...account, nick: '新凭据机器人' });
    vi.mocked(api.getInstances).mockReturnValueOnce(oldRows.promise).mockResolvedValue([{ ...rows[0], label: '新凭据实例' }]);
    render(<AccountWorkspaceSlot />);
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    expect(await screen.findByText('新凭据机器人')).toBeInTheDocument();
    await act(async () => { oldAccount.resolve({ ...account, nick: '旧凭据机器人' }); oldRows.resolve(rows); });
    expect(screen.queryByText('旧凭据机器人')).not.toBeInTheDocument();
    expect(screen.queryByText('第二实例')).not.toBeInTheDocument();
    expect(screen.getByText('新凭据实例')).toBeInTheDocument();
  });

  it('凭据切换 A → B → A 后，第一次 A 的响应仍然失效', async () => {
    const oldAccount = deferred<api.AccountStatus>();
    const oldRows = deferred<api.ConsoleInstance[]>();
    vi.mocked(api.getPlatformAccount).mockReturnValueOnce(oldAccount.promise).mockResolvedValue({ ...account, nick: '最新机器人' });
    vi.mocked(api.getInstances).mockReturnValueOnce(oldRows.promise).mockResolvedValue([{ ...rows[0], label: '最新实例' }]);
    render(<AccountWorkspaceSlot />);
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
    vi.mocked(api.getPlatformAccount).mockResolvedValue({ ...account, quick_login: [{ account: '1000002', nick: '另一个QQ' }] });
    render(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByRole('link', { name: '去它的控制台' })).toHaveAttribute('href', '/i/xiaoyu/web/?view=settings&tab=qq-account');
    expect(within(quick).queryByRole('button', { name: '切到这个号' })).not.toBeInTheDocument();
    expect(api.getInstances).toHaveBeenCalledTimes(1);
  });

  it('快速切换只认账号归属，实例名和号码撞上不算占用', async () => {
    vi.mocked(api.getInstances).mockResolvedValue([
      rows[0], { id: '1000002', label: '数字名实例', path: '/i/1000002', current: false, idx: 1, accounts: { qq: '2000002' } },
    ]);
    vi.mocked(api.getPlatformAccount).mockResolvedValue({ ...account, quick_login: [{ account: '1000002', nick: '另一个QQ' }] });
    render(<AccountWorkspaceSlot />);
    const quick = (await screen.findByRole('heading', { name: '快速切换' })).closest('section')!;
    expect(within(quick).getByRole('button', { name: '切到这个号' })).toBeEnabled();
    expect(within(quick).queryByRole('link', { name: '去它的控制台' })).not.toBeInTheDocument();
  });

  it('实例清单读取失败时不能绕过账号归属检查进行快速登录', async () => {
    vi.mocked(api.getInstances).mockRejectedValue(new Error('归属未知'));
    vi.mocked(api.getPlatformAccount).mockResolvedValue({ ...account, quick_login: [{ account: '1000002', nick: '另一个QQ' }] });
    render(<AccountWorkspaceSlot />);
    await screen.findByText('归属未知');
    expect(screen.getByRole('button', { name: '切到这个号' })).toBeDisabled();
    expect(screen.getByText(/实例归属尚未确认/)).toBeInTheDocument();
    expect(api.switchPlatformAccount).not.toHaveBeenCalled();
  });

  it('实例整页跳转沿用原生离开保护，未放行时保留实例与草稿', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<AccountWorkspaceSlot />);
    const select = await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: 'xiaoyu2' } });
    const link = screen.getByRole('link', { name: '去它的控制台' });
    expect(link).toHaveAttribute('href', '/i/xiaoyu/web/?view=settings&tab=qq-account');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    expect(select).toHaveValue('');
    expect(screen.getByRole('textbox', { name: '新实例名' })).toHaveValue('xiaoyu2');
  });

  it.each(['新实例名', '实例备注', '新实例要登录的 QQ 号'])('%s 填了东西就算未保存', async name => {
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name }), { target: { value: '1' } });
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });

  it('实例链接保留另开标签能力，不提示丢弃当前页草稿', async () => {
    function Draft() { useUnsavedChanges(true); return null; }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<><Draft /><AccountWorkspaceSlot /></>);
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
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    const id = screen.getByRole('textbox', { name: '新实例名' });
    const label = screen.getByRole('textbox', { name: '实例备注' });
    const number = screen.getByRole('textbox', { name: '新实例要登录的 QQ 号' });
    const create = screen.getByRole('button', { name: '建实例' });
    expect(create).toBeDisabled();
    fireEvent.change(id, { target: { value: 'xiaoyu2' } });
    fireEvent.change(label, { target: { value: '新实例备注' } });
    fireEvent.change(number, { target: { value: '1000003' } });
    fireEvent.click(create); fireEvent.click(create);
    expect(id).toBeDisabled(); expect(label).toBeDisabled(); expect(number).toBeDisabled();
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/^建一个新 bot「xiaoyu2」？/));
    expect(api.createInstance).toHaveBeenCalledExactlyOnceWith('current-token', 'xiaoyu2', '新实例备注', { qq: '1000003' });
    await act(async () => pending.reject(new Error('创建失败')));
    expect(screen.getByRole('alert')).toHaveTextContent('创建失败');
    expect(id).toHaveValue('xiaoyu2'); expect(label).toHaveValue('新实例备注'); expect(number).toHaveValue('1000003');
    expect(create).toBeEnabled();
  });

  it('留空 QQ 号时不替新实例登记任何账号', async () => {
    vi.mocked(api.createInstance).mockReturnValue(deferred<api.InstancePlan>().promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: 'xiaoyu2' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    expect(api.createInstance).toHaveBeenCalledExactlyOnceWith('current-token', 'xiaoyu2', '', {});
  });

  it.each([
    ['Xiaoyu', '', '实例名要以小写字母开头，只用小写字母、数字和下划线，2-32 位'],
    ['bot-2', '', '实例名要以小写字母开头，只用小写字母、数字和下划线，2-32 位'],
    ['stickers', '', '实例名要以小写字母开头，只用小写字母、数字和下划线，2-32 位'],
    ['xiaoyu', '', '已经有叫这个名字的实例了，换一个。'],
    ['xiaoyu2', '1000002', '这个 QQ 号已经归实例「第二实例」管了，请直接切换到它的控制台。'],
    ['xiaoyu2', '1000001', '这个 QQ 号已经归实例「主实例」管了，请直接切换到它的控制台。'],
  ])('拦住 %s / %s 并说清原因', async (id, number, note) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: id } });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例要登录的 QQ 号' }), { target: { value: number } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    expect(api.createInstance).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(note);
  });

  it('创建确认取消后没有写请求或清空草稿', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    const id = screen.getByRole('textbox', { name: '新实例名' });
    fireEvent.change(id, { target: { value: 'xiaoyu2' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    expect(api.createInstance).not.toHaveBeenCalled();
    expect(id).toHaveValue('xiaoyu2');
  });

  it('按后端定的实例名轮询进度，不用输入框里那个', async () => {
    vi.mocked(api.createInstance).mockResolvedValue(plan('xiaoyu2_1'));
    vi.mocked(api.getInstanceProgress).mockResolvedValue({ id: 'xiaoyu2_1', lines: ['建好了'], finished: true, ok: true, detail: '' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: 'xiaoyu2' } });
    vi.useFakeTimers();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '建实例' })));
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(api.getInstanceProgress).toHaveBeenCalledExactlyOnceWith('current-token', 'xiaoyu2_1');
    expect(screen.getByText('实例已建立，请去它自己的控制台扫码登录。')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '新实例名' })).toHaveValue('');
  });

  it('归档要求准确输入实例名，当前实例和主实例不能被删除', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('xiaoyu9');
    render(<AccountWorkspaceSlot />);
    const remove = await screen.findByRole('button', { name: '停用并归档' });
    expect(screen.getAllByRole('button', { name: '停用并归档' })).toHaveLength(1);
    fireEvent.click(remove);
    expect(prompt).toHaveBeenCalledWith(expect.stringMatching(/^归档实例 第二实例（xiaoyu）？/));
    expect(prompt).toHaveBeenCalledWith(expect.stringContaining('输入这个实例名确认：'));
    expect(api.deleteInstance).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('输入的实例名对不上，没有执行');
    prompt.mockReturnValue(null); fireEvent.click(remove);
    expect(api.deleteInstance).not.toHaveBeenCalled();
  });

  it('归档完成刷新共享实例清单，并显示归档而非新号登录提示', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('xiaoyu');
    vi.mocked(api.deleteInstance).mockResolvedValue();
    vi.mocked(api.getInstanceProgress).mockResolvedValue({ id: 'xiaoyu', lines: ['已归档'], finished: true, ok: true, detail: '' });
    render(<AccountWorkspaceSlot />);
    const remove = await screen.findByRole('button', { name: '停用并归档' });
    vi.useFakeTimers();
    await act(async () => fireEvent.click(remove));
    vi.mocked(api.getInstances).mockResolvedValue([rows[0]]);
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(api.deleteInstance).toHaveBeenCalledExactlyOnceWith('current-token', 'xiaoyu');
    expect(api.getInstanceProgress).toHaveBeenCalledExactlyOnceWith('current-token', 'xiaoyu');
    expect(screen.getByText('实例已停用，工作区和聊天记录已归档。')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '切换实例' })).not.toBeInTheDocument();
    expect(screen.queryByText('第二实例')).not.toBeInTheDocument();
  });

  it('凭据变更后迟到的创建结果不会启动旧凭据轮询或覆盖新界面', async () => {
    const pending = deferred<api.InstancePlan>();
    vi.mocked(api.createInstance).mockReturnValue(pending.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByRole('combobox', { name: '切换实例' });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: 'xiaoyu2' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    await waitFor(() => expect(api.getInstances).toHaveBeenCalledWith('new-token'));
    vi.useFakeTimers();
    await act(async () => pending.resolve({ ...plan('xiaoyu2'), label: '旧请求' }));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(api.getInstanceProgress).not.toHaveBeenCalled();
    expect(screen.queryByText('已提交，等 root 侧接手…')).not.toBeInTheDocument();
  });

  it('归档进度读取失败保留任务并自动重试，不重复提交归档', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('xiaoyu');
    vi.mocked(api.deleteInstance).mockResolvedValue();
    vi.mocked(api.getInstanceProgress).mockRejectedValueOnce(new Error('暂时断线')).mockResolvedValue({ id: 'xiaoyu', lines: ['完成'], finished: true, ok: true, detail: '' });
    render(<AccountWorkspaceSlot />);
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
    const pending = deferred<api.AccountStatus>();
    let oldReads = 0;
    vi.mocked(api.getPlatformAccount).mockImplementation(token => token === 'new-token'
      ? Promise.resolve({ ...account, nick: '新机器人' })
      : oldReads++ ? pending.promise : Promise.resolve({ ...account, is_login: false }));
    vi.useFakeTimers();
    await act(async () => render(<AccountWorkspaceSlot />));
    expect(screen.getByText('未登录')).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
    await act(async () => useSettingsStore.setState({ adminToken: 'new-token' }));
    await act(async () => pending.resolve({ ...account, nick: '旧轮询机器人' }));
    expect(api.pinPlatformAccount).not.toHaveBeenCalled();
    expect(screen.getByText('新机器人')).toBeInTheDocument();
    expect(screen.queryByText('旧轮询机器人')).not.toBeInTheDocument();
  });

  it('旧凭据重启请求完成后不应对新账号启动登录轮询', async () => {
    const pending = deferred<void>();
    vi.mocked(api.enterPlatformLoginMode).mockReturnValue(pending.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AccountWorkspaceSlot />);
    await screen.findByText('当前机器人');
    fireEvent.click(screen.getByRole('button', { name: '换个号登录' }));
    act(() => useSettingsStore.setState({ adminToken: 'new-token' }));
    await waitFor(() => expect(api.getPlatformAccount).toHaveBeenCalledWith('new-token', 'qq'));
    vi.useFakeTimers();
    await act(async () => pending.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(api.getPlatformAccount).toHaveBeenCalledTimes(2);
    expect(api.pinPlatformAccount).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: '扫码登录' })).not.toBeInTheDocument();
  });
});

// 平台名接中文：「这个 QQ 号」要空格，「这个微信号」不要。
describe('汉字名平台的账号文案', () => {
  const WECHAT: PlatformDescriptor = {
    id: 'wechat',
    displayName: '微信',
    shortName: '微信',
    channels: [{ name: 'wechat_group', kind: 'group', hashed: true }],
    hasConfig: true,
    entrypoints: ['wechat_bot.py'],
    connectionPeer: '微信网关',
    connectionHint: '检查登录状态与出网代理',
  };
  const renderFor = (platform: PlatformDescriptor) => render(
    <PlatformProvider platform={platform}><AccountWorkspaceSlot /></PlatformProvider>,
  );

  it('要登录的号那一栏两处名字都不多空格', async () => {
    renderFor(WECHAT);
    await screen.findByRole('combobox', { name: '切换实例' });

    expect(screen.getByText('要登录的微信号')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '新实例要登录的微信号' })).toBeInTheDocument();
  });

  it('QQ 那两处照旧带空格', async () => {
    renderFor(QQ_NAPCAT);
    await screen.findByRole('combobox', { name: '切换实例' });

    expect(screen.getByText('要登录的 QQ 号')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '新实例要登录的 QQ 号' })).toBeInTheDocument();
  });

  it('账号已经归别的实例时那句也不多空格', async () => {
    vi.mocked(api.getInstances).mockResolvedValue([
      { ...rows[0], accounts: { wechat: '1000001' } }, { ...rows[1], accounts: { wechat: '1000002' } },
    ]);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderFor(WECHAT);
    await screen.findByRole('combobox', { name: '切换实例' });

    fireEvent.change(screen.getByRole('textbox', { name: '新实例名' }), { target: { value: 'xiaoyu2' } });
    fireEvent.change(screen.getByRole('textbox', { name: '新实例要登录的微信号' }), { target: { value: '1000002' } });
    fireEvent.click(screen.getByRole('button', { name: '建实例' }));

    expect(api.createInstance).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('这个微信号已经归实例「第二实例」管了，请直接切换到它的控制台。');
  });
});

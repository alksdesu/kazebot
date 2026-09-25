import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { featureRequest } from '../features/client';
import { CommunityPage } from '../features/community/CommunityPage';
import { ConversationSettings } from '../features/community/ConversationSettings';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({ ...await original<typeof import('../api/supervisorClient')>(), getConversations: vi.fn() }));
vi.mock('../features/client', () => ({ featureRequest: vi.fn() }));
const policy = { response_policy_enabled: true, topic_enabled: true, merge_window_sec: 1, merge_max_wait_sec: 4, reply_budget_per_minute: 6 };
const group = 'qq_group:1234567890abcdef12345678';
const privateScope = 'qq_private:abcdef1234567890abcdef12';
const row = (scope: string, label: string, current = true): api.ConversationRow => ({ session_id: scope, conversation_key: scope, channel: 'qq', bytes: 1, updated_at: 0, current_account: current, owner: { kind: scope.startsWith('qq_group:') ? 'group' : 'private', label, name_available: true } });
let defaults: { settings: typeof policy; values: Partial<typeof policy>; revision: number };
let overrides: Partial<typeof policy>;
let revision: number;
const state = (scope = group) => ({ scope, revision, settings: { ...defaults.settings, ...overrides, welcome_enabled: true }, defaults: defaults.settings, overrides, inherited_fields: Object.keys(policy).filter(key => !(key in overrides)), defaults_revision: defaults.revision, guide: {}, quiet: {} });
beforeEach(() => {
  vi.clearAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'fixture-auth', isAuthenticated: true });
  defaults = { settings: { ...policy }, values: {}, revision: 2 }; overrides = {}; revision = 3;
  vi.mocked(api.getConversations).mockResolvedValue([row(group, '读书群'), row(privateScope, '小林'), row('web:console', '旧控制台'), row('qq_group:old', '历史群', false)]);
  vi.mocked(featureRequest).mockImplementation(async (path, options = {}) => {
    if (path.endsWith('/settings/scopes')) return { items: [] } as never;
    if (path.endsWith('/settings/defaults')) {
      if (options.method === 'PATCH') { const body = options.body as { values: Partial<typeof policy> }; defaults = { ...defaults, settings: { ...defaults.settings, ...body.values }, values: { ...defaults.values, ...body.values }, revision: defaults.revision + 1 }; }
      return structuredClone(defaults) as never;
    }
    if (path.endsWith('/settings/overrides')) {
      const body = options.body as { values: Partial<typeof policy>; reset_fields: (keyof typeof policy)[] };
      overrides = { ...overrides, ...body.values }; for (const key of body.reset_fields) delete overrides[key]; revision++;
      return structuredClone(state(options.scope)) as never;
    }
    if (path.endsWith('/activities') || path.endsWith('/decisions')) return { items: [] } as never;
    return structuredClone(state(options.scope)) as never;
  });
});
afterEach(() => vi.restoreAllMocks());

describe('策略默认值与受控目标', () => {
  it('首次进入直接读取独立实例默认，不把控制台当全局且不请求安静或判定', async () => {
    render(<ConversationSettings />);
    await waitFor(() => expect(featureRequest).toHaveBeenCalledWith('/v1/community/settings/defaults', expect.anything()));
    const calls = vi.mocked(featureRequest).mock.calls;
    expect(calls.every(([path, options]) => (path.endsWith('/settings/defaults') || path.endsWith('/settings/scopes')) && !options?.scope)).toBe(true);
    expect(screen.queryByText('临时安静')).not.toBeInTheDocument();
  });
  it('群协作未选择时仅列当前真实群，既不默认首群也不请求业务接口', async () => {
    render(<CommunityPage />); await screen.findByRole('option', { name: '读书群' });
    expect(screen.queryByRole('option', { name: /控制台|小林|历史群|实例默认/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText('所属会话')).toHaveValue(''); expect(featureRequest).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '创建' })).not.toBeInTheDocument();
  });
  it.each(['web:console', privateScope, 'qq_group:unknown', 'qq_group:old'])('无效群初值%s不请求也不提供提交', async scope => {
    render(<CommunityPage scope={scope} />); await screen.findByRole('option', { name: '读书群' });
    expect(featureRequest).not.toHaveBeenCalled(); expect(screen.queryByRole('button', { name: '创建' })).not.toBeInTheDocument();
  });
});

const responseLabel = '统一选择忽略、表态、短答和办事';
const windowLabel = '同人短句等待秒数';
const maxLabel = '连续追加最长等待秒数';
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; }); return { resolve, reject, promise }; }
const mutations = () => vi.mocked(featureRequest).mock.calls.filter(([, options]) => options?.method === 'PATCH');
function custom(label: string) { fireEvent.change(screen.getByLabelText(`${label}来源`), { target: { value: 'custom' } }); }

describe('实例默认与逐项继承的读写合同', () => {
  it('实例保存只提交改动的五项策略，不带scope/欢迎词；false与0均保留', async () => {
    render(<ConversationSettings />);
    fireEvent.click(await screen.findByLabelText(responseLabel));
    fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: '保存实例默认配置' }));
    await screen.findByText('实例默认配置已保存；已有会话覆盖保持不变。');
    expect(mutations()).toEqual([['/v1/community/settings/defaults', { method: 'PATCH', body: { values: { response_policy_enabled: false, merge_window_sec: 0 }, reset_fields: [], expected_revision: 2 } }]]);
  });
  it('局部默认禁用继承字段，显式false/0是自定义而不是inherit', async () => {
    render(<ConversationSettings scope={group} />);
    expect(await screen.findByLabelText(responseLabel)).toBeDisabled();
    expect(screen.getByLabelText(windowLabel)).toBeDisabled();
    custom(responseLabel); fireEvent.click(screen.getByLabelText(responseLabel));
    custom(windowLabel); fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText('会话覆盖已保存。');
    const body = mutations()[0][1]?.body as Record<string, unknown>;
    expect(body).toEqual({ values: { response_policy_enabled: false, merge_window_sec: 0 }, reset_fields: ['topic_enabled', 'merge_max_wait_sec', 'reply_budget_per_minute'], expected_revision: 3, expected_defaults_revision: 2 });
    expect(screen.getByLabelText(`${responseLabel}来源`)).toHaveValue('custom');
    expect(screen.getByLabelText(responseLabel)).not.toBeChecked();
  });
  it('全部恢复继承仅修改草稿；保存删除覆盖而不把有效值重新写成覆盖', async () => {
    overrides = { response_policy_enabled: false, merge_window_sec: 0 };
    render(<ConversationSettings scope={group} />);
    fireEvent.click(await screen.findByRole('button', { name: '全部恢复继承' }));
    expect(mutations()).toHaveLength(0);
    expect(screen.getByLabelText(responseLabel)).toBeChecked();
    expect(screen.getByLabelText(responseLabel)).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText('会话覆盖已保存。');
    expect(mutations()[0][1]?.body).toEqual({ values: {}, reset_fields: Object.keys(policy), expected_revision: 3, expected_defaults_revision: 2 });
    expect(overrides).toEqual({});
  });
  it('自定义与默认数值相同仍是需要保存的来源变更，单项切回默认只删除该来源', async () => {
    overrides = { topic_enabled: false };
    render(<ConversationSettings scope={group} />); await screen.findByLabelText(responseLabel);
    custom(windowLabel);
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('按引用和参与者接续话题来源'), { target: { value: 'inherit' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText('会话覆盖已保存。');
    expect(overrides).toEqual({ merge_window_sec: 1 });
  });
  it('继承与自定义共同验证等待对，保留小数输入中间态', async () => {
    render(<ConversationSettings scope={group} />); await screen.findByLabelText(windowLabel); custom(windowLabel);
    const input = screen.getByLabelText(windowLabel);
    for (const value of ['', '1', '1.', '1.2']) { fireEvent.change(input, { target: { value } }); expect(input).toHaveValue(value); }
    fireEvent.change(input, { target: { value: '5' } });
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('最长等待不能短于');
    custom(maxLabel); fireEvent.change(screen.getByLabelText(maxLabel), { target: { value: '6' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' })); await screen.findByText('会话覆盖已保存。');
    expect(overrides).toEqual({ merge_window_sec: 5, merge_max_wait_sec: 6 });
  });
  it('409保留草稿与旧版本，重载可取消，确认后才获取新快照', async () => {
    const original = vi.mocked(featureRequest).getMockImplementation()!;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => { if (options?.method === 'PATCH') throw Error('409 设置版本已变化'); return original(path, options); });
    render(<ConversationSettings scope={group} />); await screen.findByLabelText(windowLabel); custom(windowLabel);
    fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '2.5' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText(/409 设置版本已变化/);
    expect(screen.getByLabelText(windowLabel)).toHaveValue('2.5');
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeDisabled();
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(screen.getByRole('button', { name: '重新载入设置' }));
    expect(screen.getByLabelText(windowLabel)).toHaveValue('2.5');
    defaults.revision = 9; defaults.settings.merge_window_sec = 0.5;
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: '重新载入设置' }));
    await waitFor(() => expect(screen.getByLabelText(windowLabel)).toHaveValue('0.5'));
    expect(screen.getByLabelText(windowLabel)).toBeDisabled();
    vi.mocked(featureRequest).mockImplementation(original);
    custom(windowLabel); fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' })); await screen.findByText('会话覆盖已保存。');
    expect(mutations().at(-1)?.[1]?.body).toMatchObject({ expected_defaults_revision: 9 });
  });
  it('刷新判定发现默认版本变化时保留草稿并禁保存，不悄悄rebase', async () => {
    render(<ConversationSettings scope={group} />); await screen.findByLabelText(windowLabel); custom(windowLabel);
    fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '2' } });
    defaults.revision++;
    fireEvent.click(screen.getByRole('button', { name: '刷新判定' }));
    await screen.findByText(/服务端设置已变化/);
    expect(screen.getByLabelText(windowLabel)).toHaveValue('2');
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeDisabled();
    expect(mutations()).toHaveLength(0);
  });
  it.each(['network', 'metadata'])('实例默认读取%s失败不伪造可保存表单，可重试', async failure => {
    const original = vi.mocked(featureRequest).getMockImplementation()!; let first = true;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (first && path.endsWith('/settings/defaults')) { first = false; if (failure === 'network') throw Error('404 not found'); return { settings: policy } as never; }
      return original(path, options);
    });
    render(<ConversationSettings />); await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: '保存实例默认配置' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
    expect(await screen.findByRole('button', { name: '保存实例默认配置' })).toBeDisabled();
    expect(mutations()).toHaveLength(0);
  });
  it('旧服务local缺继承metadata不能把effective当override保存', async () => {
    const current = state();
    vi.mocked(featureRequest).mockImplementation(async path => path.endsWith('/decisions') || path.endsWith('/settings/scopes') ? { items: [] } as never : { scope: group, settings: current.settings, revision: 3, guide: {}, quiet: {} } as never);
    render(<ConversationSettings scope={group} />); await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: '保存会话覆盖' })).not.toBeInTheDocument();
  });
  it('名册失败不阻止独立全局配置，不能用失败名册提交群协作', async () => {
    vi.mocked(api.getConversations).mockRejectedValue(Error('offline'));
    const first = render(<ConversationSettings />); await screen.findByRole('button', { name: '保存实例默认配置' });
    expect(featureRequest).toHaveBeenCalledWith('/v1/community/settings/defaults', expect.anything());
    first.unmount(); vi.mocked(featureRequest).mockClear();
    render(<CommunityPage scope={group} />); await screen.findByText(/会话列表读取失败/);
    expect(featureRequest).not.toHaveBeenCalled(); expect(screen.queryByRole('button', { name: '创建' })).not.toBeInTheDocument();
  });
  it('历史QQ会话可管理它自己的覆盖，但控制台/Web都不成为策略目标', async () => {
    render(<ConversationSettings />);
    const option = await screen.findByRole('option', { name: /历史群.*历史账号/ });
    expect(option).toHaveValue('qq_group:old');
    expect(screen.queryByRole('option', { name: /控制台/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('配置作用域'), { target: { value: 'qq_group:old' } });
    await screen.findByRole('button', { name: '保存会话覆盖' }); custom(windowLabel);
    fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' })); await screen.findByText('会话覆盖已保存。');
    expect(mutations()[0][1]?.scope).toBe('qq_group:old');
  });
  it('取消脏目标切换后作用域、草稿、请求均不变', async () => {
    render(<ConversationSettings />); fireEvent.click(await screen.findByLabelText(responseLabel));
    await screen.findByRole('option', { name: '小林' });
    const before = vi.mocked(featureRequest).mock.calls.length;
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.change(screen.getByLabelText('配置作用域'), { target: { value: privateScope } });
    expect(screen.getByLabelText('配置作用域')).toHaveValue('@instance-defaults');
    expect(screen.getByLabelText(responseLabel)).not.toBeChecked();
    expect(featureRequest).toHaveBeenCalledTimes(before);
  });
  it('被保留的旧控制台初值不自动升级为全局也不请求业务设置', async () => {
    render(<ConversationSettings scope="web:console" />); await screen.findByRole('option', { name: '读书群' });
    expect(screen.getByLabelText('配置作用域')).toHaveValue('');
    expect(vi.mocked(featureRequest).mock.calls.map(([path]) => path)).toEqual(['/v1/community/settings/scopes']);
  });
});

describe('身份与多阶段异步边界', () => {
  it('实例A→B→A迟到defaults响应不能替换新快照', async () => {
    const old = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!;
    let first = true;
    vi.mocked(featureRequest).mockImplementation((path, options) => { if (first && path.endsWith('/settings/defaults')) { first = false; return old.promise as never; } return original(path, options); });
    render(<ConversationSettings />);
    act(() => useSettingsStore.setState({ adminToken: 'fixture-b' }));
    await screen.findByLabelText(windowLabel);
    defaults.settings.merge_window_sec = 2;
    act(() => useSettingsStore.setState({ adminToken: 'fixture-auth' }));
    await waitFor(() => expect(screen.getByLabelText(windowLabel)).toHaveValue('2'));
    await act(async () => old.resolve({ settings: { ...policy, merge_window_sec: 9 }, values: {}, revision: 99 }));
    expect(screen.getByLabelText(windowLabel)).toHaveValue('2');
  });
  it('迟到保存不能回填新目标或清除新目标草稿', async () => {
    const pending = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!;
    vi.mocked(featureRequest).mockImplementation((path, options) => options?.method === 'PATCH' ? pending.promise as never : original(path, options));
    render(<ConversationSettings />); fireEvent.click(await screen.findByLabelText(responseLabel));
    fireEvent.click(screen.getByRole('button', { name: '保存实例默认配置' }));
    fireEvent.change(screen.getByLabelText('配置作用域'), { target: { value: group } });
    await screen.findByRole('button', { name: '保存会话覆盖' }); custom(windowLabel);
    fireEvent.change(screen.getByLabelText(windowLabel), { target: { value: '3' } });
    await act(async () => pending.resolve(defaults));
    expect(screen.getByLabelText(windowLabel)).toHaveValue('3');
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeEnabled();
    expect(screen.queryByText(/实例默认配置已保存/)).not.toBeInTheDocument();
  });
  it.each(['token', 'unmount', 'aba', 'scope'])('群指引PUT迟到后%s变化阻断第二个welcome PATCH', async transition => {
    const pending = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!;
    vi.mocked(api.getConversations).mockResolvedValue([row(group, '读书群'), row('qq_group:second', '第二群')]);
    vi.mocked(featureRequest).mockImplementation((path, options) => path.endsWith('/guide') && options?.method === 'PUT' ? pending.promise as never : original(path, options));
    const view = render(<CommunityPage scope={group} />);
    fireEvent.change(await screen.findByLabelText('群规'), { target: { value: '旧身份规则' } });
    fireEvent.click(screen.getByRole('button', { name: '发布群指引' }));
    await waitFor(() => expect(featureRequest).toHaveBeenCalledWith('/v1/community/guide', expect.objectContaining({ method: 'PUT' })));
    if (transition === 'unmount') view.unmount();
    else if (transition === 'scope') { fireEvent.change(screen.getByLabelText('所属会话'), { target: { value: 'qq_group:second' } }); await screen.findByLabelText('群规'); }
    else {
      act(() => useSettingsStore.setState({ adminToken: 'fixture-b' })); await screen.findByLabelText('群规');
      if (transition === 'aba') { act(() => useSettingsStore.setState({ adminToken: 'fixture-auth' })); await screen.findByLabelText('群规'); }
    }
    await act(async () => pending.resolve(state()));
    expect(mutations()).toHaveLength(0);
  });
  it('身份变更清空创建和群指引草稿，普通刷新仍保留', async () => {
    render(<CommunityPage scope={group} />);
    fireEvent.change(await screen.findByLabelText('活动标题'), { target: { value: '私有活动草稿' } });
    fireEvent.change(screen.getByLabelText('群规'), { target: { value: '私有群规' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新' })); await screen.findByText('列表已更新。');
    expect(screen.getByLabelText('活动标题')).toHaveValue('私有活动草稿');
    expect(screen.getByLabelText('群规')).toHaveValue('私有群规');
    act(() => useSettingsStore.setState({ adminToken: 'fixture-b' }));
    expect(await screen.findByLabelText('活动标题')).toHaveValue(''); expect(screen.getByLabelText('群规')).toHaveValue('');
  });
});

it.each(['token', 'aba'])('同步%s变更与PUT resolve处于同一批更新时也不发送第二个写请求', async transition => {
  const pending = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!;
  vi.mocked(featureRequest).mockImplementation((path, options) => path.endsWith('/guide') && options?.method === 'PUT' ? pending.promise as never : original(path, options));
  render(<CommunityPage scope={group} />);
  fireEvent.change(await screen.findByLabelText('群规'), { target: { value: '旧身份规则' } });
  fireEvent.click(screen.getByRole('button', { name: '发布群指引' }));
  await act(async () => {
    useSettingsStore.setState({ adminToken: 'fixture-b' });
    if (transition === 'aba') useSettingsStore.setState({ adminToken: 'fixture-auth' });
    pending.resolve(state());
    await Promise.resolve();
  });
  expect(mutations()).toHaveLength(0);
  expect(await screen.findByLabelText('群规')).toHaveValue('');
});

it('未render中间身份的ABA仍拒绝旧defaults回包并读取新快照', async () => {
  const pending = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!;
  let first = true;
  vi.mocked(featureRequest).mockImplementation((path, options) => { if (first && path.endsWith('/settings/defaults')) { first = false; return pending.promise as never; } return original(path, options); });
  render(<ConversationSettings />);
  await act(async () => {
    useSettingsStore.setState({ adminToken: 'fixture-b' });
    useSettingsStore.setState({ adminToken: 'fixture-auth' });
    pending.resolve({ settings: { ...policy, merge_window_sec: 9 }, values: {}, revision: 99 });
    await Promise.resolve();
  });
  expect(await screen.findByLabelText(windowLabel)).toHaveValue('1');
  expect(vi.mocked(featureRequest).mock.calls.filter(([path]) => path.endsWith('/settings/defaults'))).toHaveLength(2);
});

describe('保留覆盖的恢复目录', () => {
  const orphan = 'qq_group:orphan-cleared-history';
  it('清空聊天历史后仍可按原始scope恢复孤儿覆盖，群协作却不扩充资格', async () => {
    const original = vi.mocked(featureRequest).getMockImplementation()!;
    overrides = { merge_max_wait_sec: 2 };
    vi.mocked(api.getConversations).mockResolvedValue([]);
    vi.mocked(featureRequest).mockImplementation((path, options) => path.endsWith('/settings/scopes') ? Promise.resolve({ items: Object.keys(overrides).length ? [{ scope: orphan, owner: null, current_account: false }] : [] }) as never : original(path, options));
    const page = render(<ConversationSettings />);
    const option = await screen.findByRole('option', { name: /群聊（名称暂不可用）.*历史账号/ });
    expect(option).toHaveValue(orphan); expect(document.body.textContent).not.toContain(orphan);
    fireEvent.change(screen.getByLabelText('配置作用域'), { target: { value: orphan } });
    fireEvent.click(await screen.findByRole('button', { name: '全部恢复继承' }));
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' })); await screen.findByText(/会话覆盖已清除/);
    expect(mutations()[0][1]?.scope).toBe(orphan); expect(overrides).toEqual({});
    expect(screen.queryByRole('option', { name: /仅保存设置/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存会话覆盖' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('配置作用域'), { target: { value: '@instance-defaults' } });
    await screen.findByRole('button', { name: '保存实例默认配置' });
    page.unmount(); vi.mocked(featureRequest).mockClear();
    render(<CommunityPage scope={orphan} />); await screen.findByText(/当前账号暂无可选群聊/);
    expect(featureRequest).not.toHaveBeenCalled(); expect(screen.queryByRole('button', { name: '创建' })).not.toBeInTheDocument();
  });
  it('两目录相同scope只列一项，并优先真实会话的已知名称', async () => {
    const original = vi.mocked(featureRequest).getMockImplementation()!;
    vi.mocked(featureRequest).mockImplementation((path, options) => path.endsWith('/settings/scopes') ? Promise.resolve({ items: [{ scope: group, owner: { kind: 'group', label: '旧别名', name_available: false }, current_account: true }] }) as never : original(path, options));
    render(<ConversationSettings />);
    expect(await screen.findAllByRole('option', { name: '读书群' })).toHaveLength(1);
    expect(screen.queryByRole('option', { name: /旧别名/ })).not.toBeInTheDocument();
  });
  it.each(['conversations', 'policies'])('%s目录失败不把半份名册当完整目标，但保留独立global编辑', async failed => {
    const original = vi.mocked(featureRequest).getMockImplementation()!;
    if (failed === 'conversations') vi.mocked(api.getConversations).mockRejectedValueOnce(Error('offline'));
    let first = true;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/settings/scopes')) {
        if (failed === 'policies' && first) { first = false; throw Error('404 unknown endpoint'); }
        return { items: [{ scope: orphan, owner: null, current_account: true }] } as never;
      }
      return original(path, options);
    });
    render(<ConversationSettings />); await screen.findByRole('button', { name: '重试会话列表' });
    expect(screen.queryByRole('option', { name: '读书群' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /名称暂不可用/ })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByLabelText(responseLabel));
    expect(screen.getByRole('button', { name: '保存实例默认配置' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '重试会话列表' }));
    await screen.findByRole('option', { name: '读书群' });
    expect(screen.getByLabelText(responseLabel)).not.toBeChecked();
  });
  it('新身份与批量ABA拒绝旧恢复目录，名称不串号', async () => {
    const pending = deferred<unknown>(); const original = vi.mocked(featureRequest).getMockImplementation()!; let first = true;
    vi.mocked(featureRequest).mockImplementation((path, options) => {
      if (first && path.endsWith('/settings/scopes')) { first = false; return pending.promise as never; }
      return original(path, options);
    });
    render(<ConversationSettings />); await screen.findByLabelText(responseLabel);
    await act(async () => {
      useSettingsStore.setState({ adminToken: 'fixture-b' }); useSettingsStore.setState({ adminToken: 'fixture-auth' });
      pending.resolve({ items: [{ scope: orphan, owner: { kind: 'group', label: '旧身份私密名称', name_available: true }, current_account: true }] });
    });
    await screen.findByRole('option', { name: '读书群' });
    expect(screen.queryByText('旧身份私密名称')).not.toBeInTheDocument();
  });
  it('重新读取失败保留现有草稿但暂停保存，重试成功后恢复', async () => {
    const original = vi.mocked(featureRequest).getMockImplementation()!;
    render(<ConversationSettings />); fireEvent.click(await screen.findByLabelText(responseLabel));
    vi.mocked(featureRequest).mockImplementation(async (path, options) => path.endsWith('/settings/defaults') ? { settings: policy } as never : original(path, options));
    fireEvent.click(screen.getByRole('button', { name: '重新载入设置' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText(responseLabel)).not.toBeChecked();
    expect(screen.getByRole('button', { name: '保存实例默认配置' })).toBeDisabled();
    vi.mocked(featureRequest).mockImplementation(original);
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
    await waitFor(() => expect(screen.getByLabelText(responseLabel)).toBeChecked());
  });
});

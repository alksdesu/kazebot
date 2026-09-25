import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { conversationNames, scopeIdentity } from '../features/conversationNames';
import { ScopeSelect } from '../features/execution/ScopeSelect';
import { RemindersPage } from '../features/execution/RemindersPage';
import { ExecutionPage } from '../features/execution/ExecutionPage';
import { CommunityPage } from '../features/community/CommunityPage';
import { ConversationSettings } from '../features/community/ConversationSettings';
import { featureRequest } from '../features/client';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({ ...await original<typeof import('../api/supervisorClient')>(), getConversations: vi.fn() }));
vi.mock('../features/client', () => ({ featureRequest: vi.fn() }));
const group = 'qq_group:1234567890abcdef12345678';
const person = 'qq_private:abcdef1234567890abcdef12';
const row = (scope: string, label = '', other: Partial<api.ConversationRow> = {}): api.ConversationRow => ({ session_id: `sid-${scope}`, conversation_key: scope, channel: 'qq', bytes: 1, updated_at: 1, owner: label ? { kind: scope.startsWith('qq_group:') ? 'group' : 'private', label } : null, current_account: true, ...other });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { resolve, promise }; }
beforeEach(() => {
  vi.clearAllMocks(); useSettingsStore.setState({ adminToken: 'fixture-a', isAuthenticated: true });
  useChatStore.setState({ conversations: [] }); vi.mocked(api.getConversations).mockResolvedValue([]);
  vi.mocked(featureRequest).mockResolvedValue({ reminders: [] });
});
afterEach(() => vi.restoreAllMocks());

describe('共享会话名称', () => {
  it.each(['GroupA', 'qq_group:真实群名'])('后端明确标记真实名称时不根据外观降级：%s', async label => {
    vi.mocked(api.getConversations).mockResolvedValue([row(group, label, { owner: { kind: 'group', label, alias: 'GroupA', name_available: true } })]);
    render(<ScopeSelect value={group} onChange={() => {}} />);
    expect(await screen.findByRole('option', { name: label })).toHaveValue(group);
    expect(screen.queryByText(/匿名别名/)).not.toBeInTheDocument();
  });
  it('群名与人名用作可见标签，但选中的值仍是原始scope', async () => {
    vi.mocked(api.getConversations).mockResolvedValue([row(group, '周末读书群'), row(person, '小林')]);
    const change = vi.fn(); render(<ScopeSelect value={group} onChange={change} />);
    expect(await screen.findByRole('option', { name: '周末读书群' })).toHaveValue(group);
    expect(screen.getByText('当前范围：周末读书群')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '小林' })).toHaveValue(person);
    fireEvent.change(screen.getByLabelText('所属会话'), { target: { value: person } }); expect(change).toHaveBeenCalledWith(person);
    expect(document.body.textContent).not.toContain(group); expect(document.body.textContent).not.toContain(person);
  });
  it('同名保留独立选择、匿名与历史账号明确标记、缺名不暴露完整哈希', async () => {
    const other = 'qq_group:9876543210abcdef12345678';
    vi.mocked(api.getConversations).mockResolvedValue([row(group, '同名群'), row(other, '同名群', { current_account: false }), row(person, 'PersonA', { owner: { kind: 'private', label: 'PersonA', alias: 'PersonA' } }), row('qq_group:ffffffffffffffffffffffff')]);
    render(<ScopeSelect value={group} onChange={() => {}} />);
    const duplicates = await screen.findAllByRole('option', { name: /^同名群/ });
    expect(duplicates).toHaveLength(2); expect(duplicates[0].textContent).not.toBe(duplicates[1].textContent);
    expect(duplicates.some(item => item.textContent?.includes('历史账号'))).toBe(true);
    expect(screen.getByRole('option', { name: /私聊（匿名别名：PersonA）/ })).toHaveValue(person);
    expect(screen.getByRole('option', { name: /群聊（名称暂不可用）/ })).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('ffffffffffffffffffffffff');
  });
  it('身份切换请求失败时立即清掉旧身份名称，并可重试', async () => {
    vi.mocked(api.getConversations).mockResolvedValueOnce([row(group, '旧账号私有群')]).mockRejectedValueOnce(Error('offline')).mockResolvedValueOnce([row(group, '新账号群')]);
    render(<ScopeSelect value={group} onChange={() => {}} />); await screen.findByRole('option', { name: '旧账号私有群' });
    act(() => useSettingsStore.setState({ adminToken: 'fixture-b' }));
    expect(screen.queryByText('旧账号私有群')).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '重试会话列表' }));
    expect(await screen.findByRole('option', { name: '新账号群' })).toBeInTheDocument();
  });
  it('A到B再回A时迟到回应不能覆盖当前名册，退出认证也不保留名称', async () => {
    const old = deferred<api.ConversationRow[]>();
    vi.mocked(api.getConversations).mockReturnValueOnce(old.promise).mockResolvedValueOnce([row(person, '账号B名称')]).mockResolvedValueOnce([row(group, '账号A新名称')]);
    render(<ScopeSelect value={group} onChange={() => {}} />);
    act(() => useSettingsStore.setState({ adminToken: 'fixture-b' })); await screen.findByRole('option', { name: '账号B名称' });
    act(() => useSettingsStore.setState({ adminToken: 'fixture-a' })); await screen.findByRole('option', { name: '账号A新名称' });
    await act(async () => old.resolve([row(group, '账号A旧回应')]));
    expect(screen.queryByText('账号A旧回应')).not.toBeInTheDocument();
    act(() => useSettingsStore.setState({ adminToken: null })); expect(screen.queryByText('账号A新名称')).not.toBeInTheDocument();
    expect(api.getConversations).toHaveBeenCalledTimes(3);
  });
  it('网页名称只取同实例store，不能给QQ或历史账号借用网页标题', async () => {
    useChatStore.setState({ conversations: [{ id: 'chat-1', sessionId: 'web-sid', title: '旅行计划', updatedAt: '' }] });
    vi.mocked(api.getConversations).mockResolvedValue([row('web:chat-1'), row(group)]);
    render(<ScopeSelect value="web:chat-1" onChange={() => {}} />);
    expect(await screen.findByRole('option', { name: '旅行计划' })).toHaveValue('web:chat-1');
    expect(screen.getByRole('option', { name: /群聊（名称暂不可用）/ })).toHaveValue(group);
  });
  it.each([
    ['提醒', RemindersPage, '/v1/reminders'], ['任务', ExecutionPage, '/v1/execution/plans'],
    ['群协作', CommunityPage, '/v1/community/activities'], ['会话设置', ConversationSettings, '/v1/community/state'],
  ] as const)('真实%s页选名称后读取请求仍携带原key', async (_name, Page, endpoint) => {
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/state')) return { scope: options?.scope, revision: 0, guide: {}, quiet: {}, settings: { response_policy_enabled: false, topic_enabled: false, merge_window_sec: 0, merge_max_wait_sec: 4, reply_budget_per_minute: 6, welcome_enabled: false } } as never;
      if (path.endsWith('/plans')) return { plans: [] } as never;
      if (path.endsWith('/tools')) return { tools: [] } as never;
      if (path.endsWith('/activities') || path.endsWith('/decisions')) return { items: [] } as never;
      return { reminders: [] } as never;
    });
    vi.mocked(api.getConversations).mockResolvedValue([row(person, '小周')]); render(<Page />);
    await screen.findByRole('option', { name: '小周' }); fireEvent.change(screen.getByLabelText('所属会话'), { target: { value: person } });
    await waitFor(() => expect(featureRequest).toHaveBeenCalledWith(endpoint, expect.objectContaining({ scope: person })));
    expect(screen.getByText('当前范围：小周')).toBeInTheDocument();
  });
  it('联合identity区分同scope不同机器人，名称视为文本并移除控制字符', () => {
    const names = conversationNames([{ scope: group, bot_scope: 'one', owner: { kind: 'group', label: '<img>\n相同' } }, { scope: group, bot_scope: 'two', owner: { kind: 'group', label: '<img>\n相同' } }]);
    expect(names.size).toBe(2); expect(new Set(names.values()).size).toBe(2);
    expect(names.get(scopeIdentity(group, 'one'))).toContain('<img>相同');
    expect(scopeIdentity('a|b', 'c')).not.toBe(scopeIdentity('a', 'b|c'));
  });
});

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { featureRequest } from '../../client';
import { CommunityPage } from '../CommunityPage';
import { ConversationSettings } from '../ConversationSettings';
import type { CommunityState } from '../types';

vi.mock('../../client', () => ({ featureRequest: vi.fn() }));
vi.mock('../../../api/supervisorClient', async original => ({ ...await original<typeof import('../../../api/supervisorClient')>(), getConversations: vi.fn() }));

const request = vi.mocked(featureRequest);
let state: CommunityState;
let activities: unknown[];

beforeEach(() => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'community-fixture', isAuthenticated: true });
  vi.mocked(api.getConversations).mockResolvedValue(['a', 'b'].map(id => ({ conversation_key: `qq_group:${id}`, session_id: id, channel: 'qq', bytes: 0, updated_at: 0, current_account: true, owner: { kind: 'group', label: `群 ${id}`, name_available: true } })));
  activities = [];
  state = { scope: 'qq_group:a', revision: 0, guide: {}, quiet: {}, settings: {
    response_policy_enabled: false, topic_enabled: false, merge_window_sec: 0,
    merge_max_wait_sec: 4, reply_budget_per_minute: 6, welcome_enabled: false,
  } };
  const { welcome_enabled: _welcome, ...policy } = state.settings;
  state.defaults = { ...policy }; state.overrides = { ...policy }; state.inherited_fields = []; state.defaults_revision = 0;
  request.mockReset();
  request.mockImplementation(async (path, options = {}) => {
    if (path.endsWith('/settings/scopes')) return { items: [] };
    if (path.endsWith('/activities') && options.method === 'POST') {
      activities = [{ id: 'A123', ...(options.body as object), status: 'open', participants: [], remaining: 8 }];
      return activities[0];
    }
    if (path.endsWith('/activities')) return { items: activities };
    if (path.endsWith('/guide')) { state.guide = options.body as CommunityState['guide']; return state; }
    if (path.endsWith('/settings')) { Object.assign(state.settings, options.body); return structuredClone(state); }
    if (path.endsWith('/settings/overrides')) {
      const body = options.body as { values: object; reset_fields: (keyof typeof policy)[] };
      Object.assign(state.overrides!, body.values);
      for (const key of body.reset_fields) delete state.overrides![key];
      state.settings = { ...state.defaults!, ...state.overrides, welcome_enabled: state.settings.welcome_enabled }; state.revision++;
      state.inherited_fields = (Object.keys(policy) as (keyof typeof policy)[]).filter(key => !(key in state.overrides!));
      return structuredClone(state);
    }
    if (path.endsWith('/decisions')) return { items: [] };
    if (path.endsWith('/quiet')) {
      const body = options.body as { mode: string; duration_sec: number };
      state.quiet = body.mode === 'off' ? {} : { mode: body.mode, expires_at: Date.now() / 1000 + body.duration_sec };
      return state;
    }
    return structuredClone(state);
  });
});
afterEach(() => vi.restoreAllMocks());

describe('群协作真实请求流程', () => {
  it('创建活动后读取服务端的编号和名额', async () => {
    render(<CommunityPage scope="qq_group:a" />);
    fireEvent.change(await screen.findByLabelText('活动标题'), { target: { value: '周六聚餐' } });
    fireEvent.change(screen.getByLabelText('活动名额'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(await screen.findByText(/A123/)).toBeTruthy();
    expect(request).toHaveBeenCalledWith('/v1/community/activities', expect.objectContaining({ method: 'POST', scope: 'qq_group:a', body: expect.objectContaining({ title: '周六聚餐', capacity: 8 }) }));
  });

  it('切换欢迎开关不会抹掉未发布的群规', async () => {
    render(<CommunityPage scope="qq_group:a" />);
    const rules = await screen.findByLabelText('群规');
    fireEvent.change(rules, { target: { value: '友善交流' } });
    fireEvent.click(screen.getByRole('checkbox'));
    expect((rules as HTMLTextAreaElement).value).toBe('友善交流');
    fireEvent.click(screen.getByRole('button', { name: '发布群指引' }));
    await waitFor(() => expect(state.guide.rules).toBe('友善交流'));
    await waitFor(() => expect(state.settings.welcome_enabled).toBe(true));
  });

  it('保存策略和手动恢复都作用于选定群', async () => {
    render(<ConversationSettings scope="qq_group:a" />);
    const checkbox = await screen.findByRole('checkbox', { name: '统一选择忽略、表态、短答和办事' });
    fireEvent.click(checkbox);
    fireEvent.change(screen.getByLabelText('同人短句等待秒数'), { target: { value: '1.2' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await waitFor(() => expect(state.settings.merge_window_sec).toBe(1.2));
    fireEvent.click(screen.getByRole('button', { name: '完全安静' }));
    await waitFor(() => expect(state.quiet.mode).toBe('silent'));
    await waitFor(() => expect(screen.getByRole('button', { name: '恢复' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: '恢复' }));
    await waitFor(() => expect(state.quiet).toEqual({}));
    expect(request).toHaveBeenCalledWith('/v1/community/quiet', expect.objectContaining({ scope: 'qq_group:a', body: expect.objectContaining({ mode: 'off' }) }));
  });

  it('后端拒绝操作时显示原因并保留原状态', async () => {
    request.mockImplementation(async path => {
      if (path.endsWith('/settings/scopes')) return { items: [] };
      if (path.endsWith('/settings/overrides')) throw new Error('403 需要本群管理权限');
      if (path.endsWith('/decisions')) return { items: [] };
      return state;
    });
    render(<ConversationSettings scope="qq_group:a" />);
    fireEvent.click(await screen.findByLabelText('统一选择忽略、表态、短答和办事'));
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('需要本群管理权限');
  });
});

it.each([
  ['协作', CommunityPage, '发布群指引', '/guide'],
  ['设置', ConversationSettings, '保存会话覆盖', '/settings/overrides'],
] as const)('切换群后丢弃旧群%s操作的迟到刷新', async (_name, Page, button, endpoint) => {
  let finish!: (value: unknown) => void;
  request.mockImplementation(async (path, options = {}) => {
    if (path.endsWith('/settings/scopes')) return { items: [] };
    if (path.endsWith(endpoint) && options.scope === 'qq_group:a') return new Promise(resolve => { finish = resolve; });
    if (path.endsWith('/activities') || path.endsWith('/decisions')) return { items: [] };
    return { ...structuredClone(state), scope: options.scope, guide: { rules: options.scope === 'qq_group:a' ? '甲群规' : '乙群规' }, settings: { ...state.settings, merge_window_sec: options.scope === 'qq_group:a' ? 1 : 2 }, overrides: { ...state.overrides, merge_window_sec: options.scope === 'qq_group:a' ? 1 : 2 } };
  });
  render(<Page scope="qq_group:a" />);
  if (_name === '设置') fireEvent.click(await screen.findByLabelText('统一选择忽略、表态、短答和办事'));
  fireEvent.click(await screen.findByRole('button', { name: button }));
  await waitFor(() => expect(finish).toBeTypeOf('function'));
  fireEvent.change(screen.getByLabelText(_name === '设置' ? '配置作用域' : '所属会话'), { target: { value: 'qq_group:b' } });
  await screen.findByRole('button', { name: button });
  const before = request.mock.calls.length;
  finish({});
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(request.mock.calls.slice(before).filter(([, options]) => options?.scope === 'qq_group:a' && !options.method)).toEqual([]);
  if (_name === '协作') expect(screen.getByLabelText('群规')).toHaveValue('乙群规');
  else expect(screen.getByLabelText('同人短句等待秒数')).toHaveValue('2');
});


it('keeps unpublished guide and welcome drafts when refreshing activities', async () => {
  render(<CommunityPage scope="qq_group:a" />);
  fireEvent.change(await screen.findByLabelText('群规'), { target: { value: '未发布群规' } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText('列表已更新。');
  expect(screen.getByLabelText('群规')).toHaveValue('未发布群规');
  expect(screen.getByRole('checkbox')).toBeChecked();
});

it('keeps unsaved conversation settings when refreshing decisions or applying quiet mode', async () => {
  render(<ConversationSettings scope="qq_group:a" />);
  fireEvent.change(await screen.findByLabelText('同人短句等待秒数'), { target: { value: '1.5' } });
  fireEvent.click(screen.getByRole('button', { name: '刷新判定' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '完全安静' })).toBeEnabled());
  expect(screen.getByLabelText('同人短句等待秒数')).toHaveValue('1.5');
  fireEvent.click(screen.getByRole('button', { name: '完全安静' }));
  await screen.findByText(/完全安静，到/);
  expect(screen.getByLabelText('同人短句等待秒数')).toHaveValue('1.5');
});

it('keeps a dirty guide in its current scope when discarding is declined', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<CommunityPage scope="qq_group:a" />);
  fireEvent.change(await screen.findByLabelText('群规'), { target: { value: '不能丢失' } });
  fireEvent.change(screen.getByLabelText('所属会话'), { target: { value: 'qq_group:b' } });
  expect(screen.getByLabelText('所属会话')).toHaveValue('qq_group:a');
  expect(screen.getByLabelText('群规')).toHaveValue('不能丢失');
  confirm.mockRestore();
});


it('preserves decimal input while editing and validates the numeric range before saving', async () => {
  render(<ConversationSettings scope="qq_group:a" />);
  const input = await screen.findByLabelText('同人短句等待秒数');
  fireEvent.change(input, { target: { value: '' } });
  expect(input).toHaveValue(''); expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeDisabled();
  for (const value of ['1', '1.', '1.2']) { fireEvent.change(input, { target: { value } }); expect(input).toHaveValue(value); }
  fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
  await waitFor(() => expect(state.settings.merge_window_sec).toBe(1.2));
});

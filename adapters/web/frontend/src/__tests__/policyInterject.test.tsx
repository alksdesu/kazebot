// 会话设置里的两项插话覆盖：null 是跟随平台配置，实例默认可以留空，会话自定义必须写值；概率按百分数读写。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { featureRequest } from '../features/client';
import { ConversationSettings } from '../features/community/ConversationSettings';
import { draftValue, effectivePolicy, policyDraft, policyPatch, readPolicySnapshot } from '../features/community/policyForm';
import type { ConversationPolicy } from '../features/community/types';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({ ...await original<typeof import('../api/supervisorClient')>(), getConversations: vi.fn() }));
vi.mock('../features/client', () => ({ featureRequest: vi.fn() }));

interface Policy {
  response_policy_enabled: boolean;
  merge_window_sec: number;
  merge_max_wait_sec: number;
  reply_budget_per_minute: number;
  interject_presence: boolean | null;
  interject_absent_probability: number | null;
}
type PolicyKey = keyof Policy;
interface PatchBody { values: Partial<Policy>; reset_fields: PolicyKey[] }

const factory: Policy = {
  response_policy_enabled: true,
  merge_window_sec: 1,
  merge_max_wait_sec: 4,
  reply_budget_per_minute: 6,
  interject_presence: null,
  interject_absent_probability: null,
};
const legacy = { response_policy_enabled: true, merge_window_sec: 1, merge_max_wait_sec: 4, reply_budget_per_minute: 6 };
const allKeys = Object.keys(factory) as PolicyKey[];
const group = 'qq_group:1234567890abcdef12345678';
const presenceLabel = '在场 / 离场';
const probabilityLabel = '离场时去问模型的概率（%）';
const row = (scope: string, label: string): api.ConversationRow => ({ session_id: scope, conversation_key: scope, channel: 'qq', bytes: 1, updated_at: 0, current_account: true, owner: { kind: 'group', label, name_available: true } });

let defaults: { settings: Policy; values: Partial<Policy>; revision: number };
let overrides: Partial<Policy>;
let revision: number;
const state = (scope = group) => ({
  scope,
  revision,
  settings: { ...defaults.settings, ...overrides, welcome_enabled: true },
  defaults: defaults.settings,
  overrides,
  inherited_fields: allKeys.filter(key => !(key in overrides)),
  defaults_revision: defaults.revision,
  guide: {},
  quiet: {},
});
const instanceDefaults = (values: Partial<Policy>) => {
  defaults = { settings: { ...factory, ...values } as Policy, values, revision: 2 };
};
const mutations = () => vi.mocked(featureRequest).mock.calls.filter(([, options]) => options?.method === 'PATCH');

beforeEach(() => {
  vi.clearAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'fixture-auth', isAuthenticated: true });
  instanceDefaults({}); overrides = {}; revision = 3;
  vi.mocked(api.getConversations).mockResolvedValue([row(group, '读书群')]);
  vi.mocked(featureRequest).mockImplementation(async (path, options = {}) => {
    if (path.endsWith('/settings/scopes')) return { items: [] } as never;
    if (path.endsWith('/settings/defaults')) {
      if (options.method === 'PATCH') {
        // 和服务端一样：reset_fields 撤掉写下的值，生效值回到出厂的「跟随」。
        const body = options.body as PatchBody;
        const values: Partial<Policy> = { ...defaults.values, ...body.values };
        for (const key of body.reset_fields) delete values[key];
        defaults = { settings: { ...factory, ...values } as Policy, values, revision: defaults.revision + 1 };
      }
      return structuredClone(defaults) as never;
    }
    if (path.endsWith('/settings/overrides')) {
      const body = options.body as PatchBody;
      overrides = { ...overrides, ...body.values };
      for (const key of body.reset_fields) delete overrides[key];
      revision++;
      return structuredClone(state(options.scope)) as never;
    }
    if (path.endsWith('/activities') || path.endsWith('/decisions')) return { items: [] } as never;
    return structuredClone(state(options.scope)) as never;
  });
});
afterEach(() => vi.restoreAllMocks());

describe('实例默认里的插话覆盖', () => {
  it('没写时显示跟随平台配置，概率框留空', async () => {
    render(<ConversationSettings />);

    expect(await screen.findByLabelText(presenceLabel)).toHaveValue('');
    expect(screen.getByRole('option', { name: '跟随平台配置' })).toBeEnabled();
    expect(screen.getByLabelText(probabilityLabel)).toHaveValue('');
    expect(screen.getByLabelText(probabilityLabel)).toHaveAttribute('placeholder', '跟随平台配置');
    expect(screen.getAllByText(/当前生效：跟随平台配置/)).toHaveLength(2);
    expect(screen.getByText(/在场 \/ 离场与离场概率只对群聊起作用/)).toBeInTheDocument();
  });

  it('写值时概率按百分数填，存成 0～1 的比例', async () => {
    render(<ConversationSettings />);
    fireEvent.change(await screen.findByLabelText(presenceLabel), { target: { value: 'on' } });
    fireEvent.change(screen.getByLabelText(probabilityLabel), { target: { value: '25' } });
    fireEvent.click(screen.getByRole('button', { name: '保存实例默认配置' }));
    await screen.findByText('实例默认配置已保存；已有会话覆盖保持不变。');

    expect(mutations()).toEqual([['/v1/community/settings/defaults', {
      method: 'PATCH',
      body: { values: { interject_presence: true, interject_absent_probability: 0.25 }, reset_fields: [], expected_revision: 2 },
    }]]);
    expect(screen.getByLabelText(probabilityLabel)).toHaveValue('25');
    expect(screen.getByText(/当前生效：25%/)).toBeInTheDocument();
  });

  it('改回跟随用 reset_fields 撤掉写下的值，不发 null', async () => {
    instanceDefaults({ interject_presence: true, interject_absent_probability: 0.25 });
    render(<ConversationSettings />);
    const presence = await screen.findByLabelText(presenceLabel);
    expect(presence).toHaveValue('on');
    expect(screen.getByLabelText(probabilityLabel)).toHaveValue('25');

    fireEvent.change(presence, { target: { value: '' } });
    fireEvent.change(screen.getByLabelText(probabilityLabel), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存实例默认配置' }));
    await screen.findByText('实例默认配置已保存；已有会话覆盖保持不变。');

    expect(mutations()[0][1]?.body).toEqual({
      values: {}, reset_fields: ['interject_presence', 'interject_absent_probability'], expected_revision: 2,
    });
    expect(defaults.values).toEqual({});
  });

  it('百分数越界不让存', async () => {
    render(<ConversationSettings />);
    fireEvent.change(await screen.findByLabelText(probabilityLabel), { target: { value: '150' } });

    expect(screen.getByRole('alert')).toHaveTextContent('离场时去问模型的概率须为 0～100 的百分数。');
    expect(screen.getByRole('button', { name: '保存实例默认配置' })).toBeDisabled();
  });
});

describe('会话覆盖里的插话覆盖', () => {
  it('继承时照实例默认的说法显示，概率按百分数显示', async () => {
    instanceDefaults({ interject_absent_probability: 0.1 });
    render(<ConversationSettings scope={group} />);

    expect(await screen.findByLabelText(presenceLabel)).toBeDisabled();
    expect(screen.getByRole('option', { name: '使用默认（跟随平台配置）' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '使用默认（10%）' })).toBeInTheDocument();
    expect(screen.getByLabelText(probabilityLabel)).toHaveValue('10');
  });

  it('自定义必须选一个值，选好后只写这一项', async () => {
    render(<ConversationSettings scope={group} />);
    await screen.findByLabelText(presenceLabel);
    fireEvent.change(screen.getByLabelText(`${presenceLabel}来源`), { target: { value: 'custom' } });

    // 会话自定义里「跟随」不是一个值：想跟随就把来源切回使用默认。
    expect(screen.getByRole('alert')).toHaveTextContent('自定义的插话设置要选一个值');
    expect(screen.getByRole('option', { name: '请选择' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存会话覆盖' })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(presenceLabel), { target: { value: 'off' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText('会话覆盖已保存。');

    expect(mutations()[0][1]?.body).toEqual({
      values: { interject_presence: false },
      reset_fields: ['response_policy_enabled', 'merge_window_sec', 'merge_max_wait_sec', 'reply_budget_per_minute', 'interject_absent_probability'],
      expected_revision: 3,
      expected_defaults_revision: 2,
    });
    expect(overrides).toEqual({ interject_presence: false });
  });

  it('自定义概率按百分数写', async () => {
    render(<ConversationSettings scope={group} />);
    await screen.findByLabelText(probabilityLabel);
    fireEvent.change(screen.getByLabelText(`${probabilityLabel}来源`), { target: { value: 'custom' } });
    fireEvent.change(screen.getByLabelText(probabilityLabel), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: '保存会话覆盖' }));
    await screen.findByText('会话覆盖已保存。');

    expect(overrides).toEqual({ interject_absent_probability: 0.05 });
  });
});

describe('旧服务端', () => {
  it('没返回插话覆盖项时表单不显示，保存也不提', async () => {
    vi.mocked(featureRequest).mockImplementation(async path => {
      if (path.endsWith('/settings/defaults')) return { settings: legacy, values: {}, revision: 2 } as never;
      return { items: [] } as never;
    });
    render(<ConversationSettings />);
    fireEvent.change(await screen.findByLabelText('同人短句等待秒数'), { target: { value: '2' } });

    expect(screen.queryByLabelText(presenceLabel)).not.toBeInTheDocument();
    expect(screen.queryByText(/在场 \/ 离场与离场概率/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存实例默认配置' }));
    await waitFor(() => expect(mutations()).toHaveLength(1));
    expect(mutations()[0][1]?.body).toEqual({ values: { merge_window_sec: 2 }, reset_fields: [], expected_revision: 2 });
  });
});

describe('policyForm 的插话覆盖', () => {
  const base: ConversationPolicy = { ...factory };

  it('只在服务端返回时把插话覆盖算进策略项', () => {
    expect(readPolicySnapshot({ settings: factory, values: {}, revision: 1 }, true).keys).toEqual(allKeys);
    expect(readPolicySnapshot({ settings: legacy, values: {}, revision: 1 }, true).keys).toEqual(Object.keys(legacy));
  });

  it('写下的值不能是 null，生效值可以', () => {
    expect(() => readPolicySnapshot({ settings: factory, values: { interject_presence: null }, revision: 1 }, true)).toThrow('服务端策略数据不完整');
    expect(readPolicySnapshot({ settings: factory, values: {}, revision: 1 }, true).settings.interject_presence).toBeNull();
  });

  it('百分数和比例来回换', () => {
    expect(draftValue('interject_absent_probability', 0.25)).toBe('25');
    expect(draftValue('interject_absent_probability', null)).toBe('');
    expect(draftValue('interject_presence', false)).toBe(false);
    expect(draftValue('interject_presence', null)).toBe('');
    expect(draftValue('merge_window_sec', 0.5)).toBe('0.5');
  });

  it('实例默认留空是跟随，会话自定义留空报错', () => {
    expect(effectivePolicy(base, { interject_presence: '', interject_absent_probability: '' }, true)).toMatchObject({
      interject_presence: null, interject_absent_probability: null,
    });
    expect(() => effectivePolicy(base, { interject_presence: '' }, false)).toThrow('自定义的插话设置要选一个值');
    expect(effectivePolicy(base, { interject_absent_probability: '12.5' }, false).interject_absent_probability).toBeCloseTo(0.125);
    expect(() => effectivePolicy(base, { interject_absent_probability: '101' }, true)).toThrow('0～100 的百分数');
  });

  it('实例默认改回跟随走 reset_fields', () => {
    const baseline = readPolicySnapshot({ settings: { ...factory, interject_presence: true }, values: { interject_presence: true }, revision: 4 }, true);
    const draft = { ...policyDraft(baseline.settings), interject_presence: '' };
    const effective = effectivePolicy(baseline.defaults, draft, true);

    expect(policyPatch(baseline, draft, effective, true)).toEqual({ values: {}, reset_fields: ['interject_presence'] });
  });
});

import { CONVERSATION_POLICY_KEYS, type CommunityState, type ConversationPolicy, type ConversationPolicyKey } from './types';

export type PolicyDraft = Partial<Record<ConversationPolicyKey, string | boolean>>;
export interface PolicySnapshot {
  revision: number;
  defaultsRevision: number;
  settings: ConversationPolicy;
  defaults: ConversationPolicy;
  overrides: Partial<ConversationPolicy>;
  community?: CommunityState;
}
export const booleanPolicyKeys = new Set<ConversationPolicyKey>(['response_policy_enabled', 'topic_enabled']);

function readPolicy(value: unknown, partial = false): Partial<ConversationPolicy> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('服务端未返回完整策略元数据，请更新服务后重试。');
  const source = value as Record<string, unknown>;
  const result: Record<string, boolean | number> = {};
  for (const key of CONVERSATION_POLICY_KEYS) {
    if (partial && !Object.prototype.hasOwnProperty.call(source, key)) continue;
    const field = source[key];
    if (booleanPolicyKeys.has(key) ? typeof field !== 'boolean' : typeof field !== 'number' || !Number.isFinite(field)) throw new Error('服务端策略数据不完整，未启用保存。');
    result[key] = field as boolean | number;
  }
  return result;
}

export function readPolicySnapshot(value: unknown, global: boolean): PolicySnapshot {
  if (!value || typeof value !== 'object') throw new Error('服务端未返回策略设置。');
  const data = value as Record<string, unknown>;
  if (!Number.isInteger(data.revision) || Number(data.revision) < 0) throw new Error('缺少设置版本，请更新服务后重试。');
  const settings = readPolicy(data.settings) as ConversationPolicy;
  if (global) return { revision: Number(data.revision), defaultsRevision: Number(data.revision), settings, defaults: settings, overrides: readPolicy(data.values, true) };
  if (!Number.isInteger(data.defaults_revision) || Number(data.defaults_revision) < 0 || !Array.isArray(data.inherited_fields)) throw new Error('服务端未返回继承关系，请更新服务后重试。');
  const defaults = readPolicy(data.defaults) as ConversationPolicy;
  const overrides = readPolicy(data.overrides, true);
  const expectedInherited = CONVERSATION_POLICY_KEYS.filter(key => !Object.prototype.hasOwnProperty.call(overrides, key));
  if (data.inherited_fields.length !== expectedInherited.length || expectedInherited.some(key => !(data.inherited_fields as unknown[]).includes(key))) throw new Error('服务端继承关系不完整，未启用保存。');
  return { revision: Number(data.revision), defaultsRevision: Number(data.defaults_revision), settings, defaults, overrides, community: value as CommunityState };
}

export function policyDraft(values: Partial<ConversationPolicy>): PolicyDraft {
  return Object.fromEntries(CONVERSATION_POLICY_KEYS.filter(key => Object.prototype.hasOwnProperty.call(values, key)).map(key => [key, typeof values[key] === 'boolean' ? values[key] : String(values[key])]));
}

export function draftIdentity(draft: PolicyDraft) {
  return JSON.stringify(CONVERSATION_POLICY_KEYS.map(key => [key, draft[key] ?? null]));
}

export function effectivePolicy(defaults: ConversationPolicy, draft: PolicyDraft): ConversationPolicy {
  const result = { ...defaults };
  for (const key of CONVERSATION_POLICY_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(draft, key)) continue;
    const raw = draft[key];
    let value: boolean | number;
    if (booleanPolicyKeys.has(key)) {
      if (typeof raw !== 'boolean') throw new Error('请选择开关状态。');
      value = raw;
    } else {
      value = Number(raw);
      const budget = key === 'reply_budget_per_minute';
      if (typeof raw !== 'string' || !raw.trim() || !Number.isFinite(value) || value < (budget ? 1 : 0) || value > (budget ? 60 : 10) || (budget && !Number.isInteger(value))) throw new Error('回应上限须为 1～60 的整数，等待时间须为 0～10 秒。');
    }
    (result as unknown as Record<string, boolean | number>)[key] = value;
  }
  if (result.merge_max_wait_sec < result.merge_window_sec) throw new Error('最长等待不能短于短句等待窗口，请同时调整这两项。');
  return result;
}

export function samePolicy(left: PolicySnapshot, right: PolicySnapshot, global: boolean) {
  return global
    ? draftIdentity(policyDraft(left.settings)) === draftIdentity(policyDraft(right.settings))
    : left.defaultsRevision === right.defaultsRevision && draftIdentity(policyDraft(left.overrides)) === draftIdentity(policyDraft(right.overrides));
}

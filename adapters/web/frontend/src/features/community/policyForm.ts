import { asPercent } from '../../console/format';
import { ALL_POLICY_KEYS, CONVERSATION_POLICY_KEYS, INTERJECT_POLICY_KEYS, type CommunityState, type ConversationPolicy, type ConversationPolicyKey } from './types';

export type PolicyDraft = Partial<Record<ConversationPolicyKey, string | boolean>>;
export type PolicyValue = boolean | number | null;
export interface PolicySnapshot {
  revision: number;
  defaultsRevision: number;
  // 这台服务端管的策略项：固定四项，加上它返回了的插话覆盖项。旧服务端没有后者，表单不显示、保存也不提。
  keys: readonly ConversationPolicyKey[];
  settings: ConversationPolicy;
  defaults: ConversationPolicy;
  overrides: Partial<ConversationPolicy>;
  community?: CommunityState;
}
export const booleanPolicyKeys = new Set<ConversationPolicyKey>(['response_policy_enabled', 'interject_presence']);
// null 是跟随平台配置：实例默认可以留空；会话自定义必须写值，想跟随就选「使用默认」。
export const nullablePolicyKeys = new Set<ConversationPolicyKey>(INTERJECT_POLICY_KEYS);
// 存的是 0～1 的比例，表单按百分数读写，和控制台功能页一致。
export const percentPolicyKeys = new Set<ConversationPolicyKey>(['interject_absent_probability']);

const owns = (source: object, key: string) => Object.prototype.hasOwnProperty.call(source, key);
const isRecord = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);

function policyKeys(settings: unknown): ConversationPolicyKey[] {
  const source = isRecord(settings) ? settings : {};
  return [...CONVERSATION_POLICY_KEYS, ...INTERJECT_POLICY_KEYS.filter(key => owns(source, key))];
}

function validField(key: ConversationPolicyKey, field: unknown, allowUnset: boolean) {
  if (field === null) return allowUnset && nullablePolicyKeys.has(key);
  return booleanPolicyKeys.has(key) ? typeof field === 'boolean' : typeof field === 'number' && Number.isFinite(field);
}

function readPolicy(value: unknown, keys: readonly ConversationPolicyKey[], partial = false): Partial<ConversationPolicy> {
  if (!isRecord(value)) throw new Error('服务端未返回完整策略元数据，请更新服务后重试。');
  const result: Partial<Record<ConversationPolicyKey, PolicyValue>> = {};
  for (const key of keys) {
    if (partial && !owns(value, key)) continue;
    // 生效值里的 null 是跟随；写下来的值不会是 null，服务端把跟随存成没写。
    if (!validField(key, value[key], !partial)) throw new Error('服务端策略数据不完整，未启用保存。');
    result[key] = value[key] as PolicyValue;
  }
  return result as Partial<ConversationPolicy>;
}

export function readPolicySnapshot(value: unknown, global: boolean): PolicySnapshot {
  if (!value || typeof value !== 'object') throw new Error('服务端未返回策略设置。');
  const data = value as Record<string, unknown>;
  if (!Number.isInteger(data.revision) || Number(data.revision) < 0) throw new Error('缺少设置版本，请更新服务后重试。');
  const keys = policyKeys(data.settings);
  const settings = readPolicy(data.settings, keys) as ConversationPolicy;
  if (global) return { revision: Number(data.revision), defaultsRevision: Number(data.revision), keys, settings, defaults: settings, overrides: readPolicy(data.values, keys, true) };
  if (!Number.isInteger(data.defaults_revision) || Number(data.defaults_revision) < 0 || !Array.isArray(data.inherited_fields)) throw new Error('服务端未返回继承关系，请更新服务后重试。');
  const defaults = readPolicy(data.defaults, keys) as ConversationPolicy;
  const overrides = readPolicy(data.overrides, keys, true);
  const expectedInherited = keys.filter(key => !owns(overrides, key));
  const inherited = data.inherited_fields.filter(key => key !== 'topic_enabled');
  if (inherited.length !== expectedInherited.length || expectedInherited.some(key => !inherited.includes(key))) throw new Error('服务端继承关系不完整，未启用保存。');
  return { revision: Number(data.revision), defaultsRevision: Number(data.defaults_revision), keys, settings, defaults, overrides, community: value as CommunityState };
}

export function draftValue(key: ConversationPolicyKey, value: PolicyValue | undefined): string | boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value !== 'number') return '';
  return String(percentPolicyKeys.has(key) ? asPercent(value) : value);
}

export function policyDraft(values: Partial<ConversationPolicy>): PolicyDraft {
  return Object.fromEntries(ALL_POLICY_KEYS.filter(key => owns(values, key)).map(key => [key, draftValue(key, values[key])]));
}

export function draftIdentity(draft: PolicyDraft) {
  return JSON.stringify(ALL_POLICY_KEYS.map(key => [key, draft[key] ?? null]));
}

function draftField(key: ConversationPolicyKey, raw: string | boolean | undefined, global: boolean): PolicyValue {
  if (nullablePolicyKeys.has(key) && raw === '') {
    if (global) return null;
    throw new Error('自定义的插话设置要选一个值；想跟随实例默认，把来源切回「使用默认」。');
  }
  if (booleanPolicyKeys.has(key)) {
    if (typeof raw !== 'boolean') throw new Error('请选择开关状态。');
    return raw;
  }
  const value = Number(raw);
  const filled = typeof raw === 'string' && Boolean(raw.trim()) && Number.isFinite(value);
  if (percentPolicyKeys.has(key)) {
    if (!filled || value < 0 || value > 100) throw new Error('离场时去问模型的概率须为 0～100 的百分数。');
    return value / 100;
  }
  const budget = key === 'reply_budget_per_minute';
  if (!filled || value < (budget ? 1 : 0) || value > (budget ? 60 : 10) || (budget && !Number.isInteger(value))) throw new Error('回应上限须为 1～60 的整数，等待时间须为 0～10 秒。');
  return value;
}

export function effectivePolicy(defaults: ConversationPolicy, draft: PolicyDraft, global: boolean): ConversationPolicy {
  const result: Partial<Record<ConversationPolicyKey, PolicyValue>> = { ...defaults };
  for (const key of ALL_POLICY_KEYS) {
    if (owns(draft, key)) result[key] = draftField(key, draft[key], global);
  }
  const policy = result as ConversationPolicy;
  if (policy.merge_max_wait_sec < policy.merge_window_sec) throw new Error('最长等待不能短于短句等待窗口，请同时调整这两项。');
  return policy;
}

/** 保存请求的 values 与 reset_fields：实例默认只提交改了的项；会话覆盖逐项要么写值、要么恢复继承。 */
export function policyPatch(baseline: PolicySnapshot, draft: PolicyDraft, effective: ConversationPolicy, global: boolean) {
  const values: Partial<Record<ConversationPolicyKey, boolean | number>> = {};
  const resetFields: ConversationPolicyKey[] = [];
  for (const key of baseline.keys) {
    const next = effective[key] ?? null;
    const changed = global ? next !== (baseline.settings[key] ?? null) : owns(draft, key);
    if (global && !changed) continue;
    // 服务端不收 null：实例默认改回跟随，要用 reset_fields 撤掉写下的值。
    if (changed && next !== null) values[key] = next;
    else resetFields.push(key);
  }
  return { values, reset_fields: resetFields };
}

export function samePolicy(left: PolicySnapshot, right: PolicySnapshot, global: boolean) {
  return global
    ? draftIdentity(policyDraft(left.settings)) === draftIdentity(policyDraft(right.settings))
    : left.defaultsRevision === right.defaultsRevision && draftIdentity(policyDraft(left.overrides)) === draftIdentity(policyDraft(right.overrides));
}

import { BOOLEAN, NON_EMPTY_TEXT, NUMBER, TEXT, TEXT_LIST, at, listOf, oneOf, optional, shaped, shapedRows } from '../../api/shape';
import type { MemoryOwner } from '../../api/supervisorClient';

export interface ConversationPolicy {
  response_policy_enabled: boolean;
  merge_window_sec: number;
  merge_max_wait_sec: number;
  reply_budget_per_minute: number;
  // 群里主动插话的覆盖，null 是跟随平台配置（trigger.presence.*）。旧服务端不返回这两项。
  interject_presence?: boolean | null;
  interject_absent_probability?: number | null;
}
export const CONVERSATION_POLICY_KEYS = ['response_policy_enabled', 'merge_window_sec', 'merge_max_wait_sec', 'reply_budget_per_minute'] as const;
export const INTERJECT_POLICY_KEYS = ['interject_presence', 'interject_absent_probability'] as const;
export const ALL_POLICY_KEYS = [...CONVERSATION_POLICY_KEYS, ...INTERJECT_POLICY_KEYS] as const;
export type ConversationPolicyKey = typeof ALL_POLICY_KEYS[number];
export interface PolicyDefaults { settings: ConversationPolicy; values: Partial<ConversationPolicy>; revision: number }
export interface PolicyScope { scope: string; owner: MemoryOwner | null; current_account: boolean }
export interface CommunityState {
  scope: string;
  revision: number;
  settings: ConversationPolicy & { welcome_enabled: boolean };
  defaults?: ConversationPolicy;
  overrides?: Partial<ConversationPolicy>;
  inherited_fields?: ConversationPolicyKey[];
  defaults_revision?: number;
  guide: { rules?: string; resources?: string; faq?: string; welcome?: string; updated_at?: number };
  quiet: { mode?: string; expires_at?: number };
}
export interface Activity {
  id: string;
  kind: 'poll' | 'event';
  title: string;
  status: string;
  options: string[];
  counts: number[];
  capacity: number;
  remaining: number;
  participants: { name: string; status: string }[];
  deadline: number;
}
export interface Decision { message_id: string; action: string; reason: string }

const ACTIVITY = {
  id: NON_EMPTY_TEXT,
  kind: oneOf('poll', 'event'),
  title: TEXT,
  status: TEXT,
  options: TEXT_LIST,
  counts: listOf(NUMBER),
  capacity: NUMBER,
  remaining: NUMBER,
  deadline: NUMBER,
};
const PARTICIPANT = { name: TEXT, status: TEXT };

/** 活动列表。票数、报名的人读不懂就抛：补成空的，页面会说成零票、没人报名。 */
export function readActivities(what: string, value: unknown): Activity[] {
  return shapedRows(what, shaped(what, value, {}).items, ACTIVITY, 'items').map((row, index) => ({
    ...row,
    participants: shapedRows(what, row.participants, PARTICIPANT, at(at('items', index), 'participants')),
  }));
}

const GUIDE = { rules: optional(TEXT), resources: optional(TEXT), faq: optional(TEXT), welcome: optional(TEXT), updated_at: optional(NUMBER) };
const QUIET = { mode: optional(TEXT), expires_at: optional(NUMBER) };

/** 一个会话的社区状态。群指引读不懂就抛：当成没写，再发布一次就把群里原有的指引清掉了。 */
export function readCommunityState(what: string, value: unknown): CommunityState {
  const state = shaped(what, value, { scope: TEXT, revision: NUMBER });
  shaped(what, state.settings, { welcome_enabled: BOOLEAN }, 'settings');
  shaped(what, state.guide, GUIDE, 'guide');
  shaped(what, state.quiet, QUIET, 'quiet');
  return state as unknown as CommunityState;
}

const DECISION = { message_id: TEXT, action: TEXT, reason: TEXT };

export function readDecisions(what: string, value: unknown): Decision[] {
  return shapedRows(what, shaped(what, value, {}).items, DECISION, 'items');
}

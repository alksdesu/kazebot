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

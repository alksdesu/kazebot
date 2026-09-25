import type { MemoryOwner } from '../../api/supervisorClient';

export interface ConversationPolicy {
  response_policy_enabled: boolean;
  topic_enabled: boolean;
  merge_window_sec: number;
  merge_max_wait_sec: number;
  reply_budget_per_minute: number;
}
export const CONVERSATION_POLICY_KEYS = ['response_policy_enabled', 'topic_enabled', 'merge_window_sec', 'merge_max_wait_sec', 'reply_budget_per_minute'] as const;
export type ConversationPolicyKey = typeof CONVERSATION_POLICY_KEYS[number];
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

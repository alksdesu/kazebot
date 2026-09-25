export interface ConversationPolicy {
  response_policy_enabled: boolean;
  topic_enabled: boolean;
  merge_window_sec: number;
  merge_max_wait_sec: number;
  reply_budget_per_minute: number;
  welcome_enabled: boolean;
}
export interface CommunityState {
  scope: string;
  revision: number;
  settings: ConversationPolicy;
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

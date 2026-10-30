// 系统接口的回包桩，字段照 supervisor 的模型给齐：客户端按形状读，桩缺一项就会被当成读不懂。
import type { AdminApproval, AdminState, AppConfigPublic, RestartResult } from '../../api/supervisorClient';

export function appConfig(overrides: Partial<AppConfigPublic> = {}): AppConfigPublic {
  return {
    version: 1, provider: 'openai', openai: { model: 'gpt-4o-mini', base_url: '', api_key_present: false },
    default_entry_node_id: '', ...overrides,
  };
}

export function pendingApproval(approvalId: string, overrides: Partial<AdminApproval> = {}): AdminApproval {
  return {
    approval_id: approvalId, session_id: 'sess-fixture', operation: 'execute_command', details: {}, status: 'pending',
    fingerprint: `fp-${approvalId}`, requested_at: '2026-06-01T13:59:00.000Z', decided_at: null, decision: null, comment: null,
    tool_call_id: null, node_id: null, task_id: null, ...overrides,
  };
}

/** 计数按后端的口径给齐每个状态，没有的就是 0。 */
export function adminState(overrides: Partial<AdminState> = {}): AdminState {
  return {
    sessions: 0,
    approvals: { pending: 0, allowed: 0, denied: 0 },
    tasks: { pending: 0, running: 0, completed: 0, failed: 0, suspended: 0, cancelled: 0 },
    pending_approvals: [],
    engine_runtime: {},
    ...overrides,
  };
}

export function restartReply(overrides: Partial<RestartResult> = {}): RestartResult {
  return { scheduled: true, target: 'engine', reason: null, in_progress: null, ...overrides };
}

export const configReloadReply = () => ({ ok: true, config: appConfig() });

export const toolsReloadReply = (seq = 1) => ({ ok: true, run_id: 'run-fixture', seq, mcp_config_seq: 0, mcp_reload_seq: 0 });

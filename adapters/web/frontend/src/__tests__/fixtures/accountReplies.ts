// 账号与多开接口的回包桩，字段照 supervisor 的回包给齐：客户端按形状读，桩缺一项就会被当成读不懂。
import type { InstancePlan, InstanceProgress, ScopeMigration, ScopeStatus } from '../../api/supervisorClient';

export function instancePlanReply(id: string, overrides: Partial<InstancePlan> = {}): InstancePlan {
  return {
    id, label: '', idx: 2, prefix: `/i/${id}`, ports: { napcat: 6103, onebot: 6104 },
    workspace: `/srv/${id}`, platform: 'qq', account: '', ...overrides,
  };
}

/** 默认是还在跑的那份；后端只在没跑完时带 state、retryable、action。 */
export function instanceProgressReply(id: string, overrides: Partial<InstanceProgress> = {}): InstanceProgress {
  return {
    id, lines: ['开始建实例'], finished: false, ok: false, detail: '', state: 'running', retryable: true, action: 'create',
    ...overrides,
  };
}

export function scopeStatusReply(overrides: Partial<ScopeStatus> = {}): ScopeStatus {
  return { current_scope: '1000001', live_scope: '1000001', bot_alive: false, preview: null, ambiguous_sources: [], ...overrides };
}

/** 搬迁回执的全貌。客户端的类型只列页面念的几项，其余字段照后端带上。 */
export function scopeMigrationReply(overrides: Partial<ScopeMigration> & { hit_keys_rewritten?: number } = {}): ScopeMigration {
  const reply = {
    ok: true, source_scope: '1000001', target_scope: '2000002', moved_memory_dirs: 2, hit_keys_rewritten: 2,
    hit_cache_problem: '', route_keys_changed: 3, sessions_changed: 3, sessions_reloaded: 3, stores: {}, skipped: [],
    unknown_namespaces: [], unresolved_namespaces: [], backup_dir: '/srv/backup/scope-1', ...overrides,
  };
  return reply;
}

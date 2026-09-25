import { useCallback, useEffect, useRef, useState } from 'react';
import { featureRequest } from '../client';
import { ScopeSelect } from '../execution/ScopeSelect';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useSettingsStore } from '../../store/settingsStore';
import { useActorRequestScope } from '../useActorRequestScope';
import { INSTANCE_DEFAULTS_TARGET, useScopePicker } from '../useScopePicker';
import { actionClass, controlClass, EmptyState, FeatureFeedback, FeaturePage, primaryClass, sectionClass } from '../ui';
import { CONVERSATION_POLICY_KEYS, type ConversationPolicy, type ConversationPolicyKey } from './types';
import { booleanPolicyKeys, draftIdentity, effectivePolicy, policyDraft, readPolicySnapshot, samePolicy, type PolicyDraft, type PolicySnapshot } from './policyForm';

const labels: Record<ConversationPolicyKey, string> = {
  response_policy_enabled: '统一选择忽略、表态、短答和办事',
  topic_enabled: '按引用和参与者接续话题',
  reply_budget_per_minute: '每分钟自主回应上限',
  merge_window_sec: '同人短句等待秒数',
  merge_max_wait_sec: '连续追加最长等待秒数',
};
interface Decision { message_id: string; topic_id: string; action: string; reason: string }
const displayValue = (value: boolean | number) => typeof value === 'boolean' ? value ? '开启' : '关闭' : String(value);

export function ConversationSettings({ scope: initialScope }: { scope?: string }) {
  const [scope, setScope] = useState(initialScope === undefined ? INSTANCE_DEFAULTS_TARGET : initialScope);
  const token = useSettingsStore(state => state.adminToken);
  const picker = useScopePicker('policy', scope);
  const global = scope === INSTANCE_DEFAULTS_TARGET;
  const identity = useActorRequestScope(JSON.stringify([scope, token]));
  const viewKey = identity.key;
  const isCurrent = () => identity.isCurrent() && useSettingsStore.getState().adminToken === token;
  const [snapshot, setSnapshot] = useState<{ key: string; value: PolicySnapshot } | null>(null);
  const baseline = snapshot?.key === viewKey ? snapshot.value : null;
  const baselineRef = useRef(baseline); baselineRef.current = baseline;
  const [latest, setLatest] = useState<PolicySnapshot | null>(null);
  const [draft, setDraft] = useState<PolicyDraft>({});
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [minutes, setMinutes] = useState('30');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [readBlocked, setReadBlocked] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const requestSequence = useRef(0);
  const mutationSequence = useRef(0);
  const mutating = useRef(false);
  const dirty = Boolean(baseline && draftIdentity(draft) !== draftIdentity(policyDraft(global ? baseline.settings : baseline.overrides)));
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const confirmDiscard = useUnsavedChanges(dirty);
  let validation = '';
  let effective: ConversationPolicy | null = null;
  if (baseline) { try { effective = effectivePolicy(baseline.defaults, draft); } catch (reason) { validation = (reason as Error).message; } }
  const validMinutes = /^\d+$/.test(minutes) && Number(minutes) >= 1 && Number(minutes) <= 10080;

  const accept = (next: PolicySnapshot, resetDraft: boolean) => {
    setReadBlocked(false);
    setLatest(next);
    const previous = baselineRef.current;
    if (resetDraft || !dirtyRef.current || !previous) {
      setSnapshot({ key: viewKey, value: next });
      baselineRef.current = next;
      setDraft(policyDraft(global ? next.settings : next.overrides));
      dirtyRef.current = false;
      setConflict(false);
    } else if (samePolicy(previous, next, global)) {
      setSnapshot({ key: viewKey, value: next });
      baselineRef.current = next;
    } else {
      setConflict(true);
      setError('服务端设置已变化，未覆盖你的草稿。请核对后重新载入设置。');
    }
  };
  const refresh = useCallback(async (signal?: AbortSignal, resetDraft = false) => {
    if (!picker.valid || !isCurrent()) return;
    const request = ++requestSequence.current;
    setReadBlocked(true);
    try {
      const [current, logs] = global
        ? [await featureRequest<unknown>('/v1/community/settings/defaults', { signal }), null]
        : await Promise.all([
          featureRequest<unknown>('/v1/community/state', { scope, signal }),
          featureRequest<{ items: Decision[] }>('/v1/community/decisions', { scope, signal }),
        ]);
      if (signal?.aborted || !isCurrent() || request !== requestSequence.current) return;
      const next = readPolicySnapshot(current, global);
      if (!global && next.community?.scope !== scope) throw new Error('服务端返回的会话不匹配，未启用保存。');
      accept(next, resetDraft);
      setDecisions(logs?.items || []);
      setLoading(false);
    } catch (reason) {
      if (signal?.aborted || !isCurrent() || request !== requestSequence.current) return;
      throw reason;
    }
  }, [viewKey, picker.valid]);
  useEffect(() => {
    mutationSequence.current++; mutating.current = false; baselineRef.current = null; dirtyRef.current = false;
    setSnapshot(null); setLatest(null); setDraft({}); setDecisions([]); setBusy(false); setError(''); setNotice(''); setConflict(false); setReadBlocked(true);
    return () => { requestSequence.current++; };
  }, [viewKey]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(picker.valid);
    if (picker.valid) void refresh(controller.signal).catch(reason => { if (!controller.signal.aborted && isCurrent()) { setError(String(reason)); setLoading(false); } });
    return () => { controller.abort(); requestSequence.current++; };
  }, [refresh]);

  const run = async (action: () => Promise<unknown>, message = '') => {
    if (!picker.valid || !isCurrent() || mutating.current) return;
    mutating.current = true; const mutation = ++mutationSequence.current; requestSequence.current++;
    setBusy(true); setError(''); setNotice('');
    try { await action(); if (isCurrent() && message) setNotice(message); }
    catch (reason) { if (isCurrent()) { setError(String(reason)); if (/409|版本|冲突/.test(String(reason))) setConflict(true); } }
    finally { if (mutation === mutationSequence.current && isCurrent()) { mutating.current = false; setBusy(false); setLoading(false); } }
  };
  const reload = () => {
    if (confirmDiscard()) void run(() => refresh(undefined, true));
  };
  const save = () => {
    if (!baseline || !effective || !dirty || conflict || readBlocked) return;
    const clearingOrphan = !global && picker.policyOnlyScopes.includes(scope) && !Object.keys(draft).length;
    const values = Object.fromEntries(CONVERSATION_POLICY_KEYS.filter(key => global ? effective[key] !== baseline.settings[key] : Object.prototype.hasOwnProperty.call(draft, key)).map(key => [key, effective[key]]));
    const body = { values, reset_fields: global ? [] : CONVERSATION_POLICY_KEYS.filter(key => !Object.prototype.hasOwnProperty.call(draft, key)), expected_revision: baseline.revision, ...(global ? {} : { expected_defaults_revision: baseline.defaultsRevision }) };
    void run(async () => {
      const response = await featureRequest<unknown>(global ? '/v1/community/settings/defaults' : '/v1/community/settings/overrides', { ...(global ? {} : { scope }), method: 'PATCH', body });
      if (!isCurrent()) return;
      setReadBlocked(true);
      const next = readPolicySnapshot(response, global);
      if (!global && next.community?.scope !== scope) throw new Error('服务端返回的会话不匹配，请重新载入确认保存结果。');
      accept(next, true);
      if (clearingOrphan) await picker.reload();
    }, global ? '实例默认配置已保存；已有会话覆盖保持不变。' : clearingOrphan ? '会话覆盖已清除；此会话已无聊天记录及单独覆盖，请选择当前实例默认配置继续。' : '会话覆盖已保存。');
  };

  return <FeaturePage title="会话与接话" description="当前实例默认配置适用于本实例的 QQ 群聊和私聊。每个会话可逐项使用默认值或自定义；已有覆盖不会被全局保存清除。">
    <ScopeSelect picker={picker} value={scope} onChange={value => { if (value !== scope && confirmDiscard()) setScope(value); }} />
    <FeatureFeedback error={error} notice={notice} retry={picker.valid ? reload : undefined} />
    {loading && <p role="status">正在读取接话设置…</p>}
    {picker.valid && baseline && <>
      <form className="min-w-0 space-y-4 border border-[var(--duties-border)] p-4" onSubmit={event => { event.preventDefault(); save(); }}>
        <fieldset disabled={busy} className="min-w-0 space-y-4">
          <legend className="font-semibold">{global ? '实例默认接话策略' : '会话接话策略'}{dirty ? ' · 有未保存修改' : ''}</legend>
          <p className="text-[var(--duties-secondary)]">{global ? '这里仅设置五项默认策略，不会让所有会话进入安静，也不会修改群欢迎词。' : '使用默认的字段会跟随实例配置；自定义字段只影响当前会话。切换来源后请保存。'}</p>
          <div className="divide-y divide-[var(--duties-border)]">
            {CONVERSATION_POLICY_KEYS.map(key => {
              const custom = global || Object.prototype.hasOwnProperty.call(draft, key);
              const value = custom ? draft[key] : baseline.defaults[key];
              return <div key={key} className="grid min-w-0 gap-3 py-4 sm:grid-cols-2 sm:items-center">
                <div className="space-y-1"><label htmlFor={`policy-${key}`} className="font-medium">{labels[key]}</label><p className="text-xs text-[var(--duties-secondary)]">当前生效：{displayValue((latest || baseline).settings[key])} · {global ? '实例默认' : Object.prototype.hasOwnProperty.call((latest || baseline).overrides, key) ? '本会话自定义' : '继承实例默认'}</p></div>
                <div className="min-w-0 space-y-2">
                  {!global && <select aria-label={`${labels[key]}来源`} className={controlClass} value={custom ? 'custom' : 'inherit'} onChange={event => setDraft(current => { const next = { ...current }; if (event.target.value === 'inherit') delete next[key]; else next[key] = typeof baseline.defaults[key] === 'boolean' ? baseline.defaults[key] as boolean : String(baseline.defaults[key]); return next; })}><option value="inherit">使用默认（{displayValue(baseline.defaults[key])}）</option><option value="custom">自定义</option></select>}
                  {booleanPolicyKeys.has(key)
                    ? <label className="flex min-h-10 items-center gap-2"><input id={`policy-${key}`} aria-label={labels[key]} type="checkbox" disabled={!custom} checked={Boolean(value)} onChange={event => setDraft(current => ({ ...current, [key]: event.target.checked }))} />{value ? '开启' : '关闭'}</label>
                    : <input id={`policy-${key}`} aria-label={labels[key]} disabled={!custom} inputMode={key === 'reply_budget_per_minute' ? 'numeric' : 'decimal'} className={controlClass} value={String(value ?? '')} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} />}
                </div>
              </div>;
            })}
          </div>
          <p className="text-xs text-[var(--duties-secondary)]">回应上限为 1～60 的整数；等待时间为 0～10 秒，0 表示关闭短句等待，最长等待不能短于短句窗口。</p>
          {validation && <p role="alert" className="text-[var(--duties-danger)]">{validation}</p>}
          <div className="flex flex-wrap gap-3">
            <button className={primaryClass} disabled={busy || !dirty || Boolean(validation) || conflict || readBlocked}>{busy ? '正在保存…' : global ? '保存实例默认配置' : '保存会话覆盖'}</button>
            {!global && <button className={actionClass} type="button" disabled={busy || !Object.keys(draft).length} onClick={() => { setDraft({}); setNotice('五项策略已在草稿中恢复继承，保存后生效。'); }}>全部恢复继承</button>}
            <button className={actionClass} type="button" disabled={busy} onClick={reload}>重新载入设置</button>
          </div>
        </fieldset>
      </form>
      {!global && latest?.community && <>
        <section className={sectionClass}><h2 className="font-semibold">临时安静</h2>
          <p>{latest.community.quiet.mode ? `${latest.community.quiet.mode === 'listen' ? '旁听' : '完全安静'}，到 ${new Date((latest.community.quiet.expires_at || 0) * 1000).toLocaleString()} 自动恢复` : '正常回应'}</p>
          <p>仅作用于当前会话。旁听仍接明确 @、引用和命令；完全安静暂停普通对话与通知，恢复、状态和取消等控制命令仍可使用。</p>
          <label className="block max-w-sm">分钟数<input aria-label="安静分钟数" type="number" min={1} max={10080} className={controlClass} value={minutes} onChange={event => setMinutes(event.target.value)} /></label>
          <div className="flex flex-wrap gap-3">{(['listen', 'silent', 'off'] as const).map(mode => <button className={actionClass} disabled={busy || (mode !== 'off' && !validMinutes)} key={mode} onClick={() => void run(async () => { await featureRequest('/v1/community/quiet', { scope, method: 'POST', body: { mode, duration_sec: mode === 'off' ? 0 : Number(minutes) * 60 } }); if (isCurrent()) await refresh(); })}>{mode === 'listen' ? '旁听' : mode === 'silent' ? '完全安静' : '恢复'}</button>)}</div>
        </section>
        <section className={sectionClass}><h2 className="font-semibold">最近接话判定</h2><button className={actionClass} disabled={busy} onClick={() => void run(() => refresh())}>刷新判定</button>
          {!decisions.length && <EmptyState>启用策略并收到消息后，这里会显示当前会话的实际判定。</EmptyState>}
          {decisions.map(decision => <article key={decision.message_id} className="border border-[var(--duties-border)] p-3"><p>{{ ignore: '忽略', reaction: '表态', react: '表态', reply: '回复', short_reply: '短答', task: '执行任务', execute: '执行任务' }[decision.action] || decision.action} · {decision.reason}</p><p className="text-xs">消息 {decision.message_id} · 话题 {decision.topic_id || '未分配'}</p></article>)}
        </section>
      </>}
    </>}
  </FeaturePage>;
}

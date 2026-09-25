import { useCallback, useEffect, useRef, useState } from 'react';
import { featureRequest } from '../client';
import { ScopeSelect } from '../execution/ScopeSelect';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useRequestScope } from '../asyncState';
import { actionClass, controlClass, EmptyState, FeatureFeedback, FeaturePage, primaryClass, sectionClass } from '../ui';
import type { CommunityState, ConversationPolicy } from './types';

const INPUT = controlClass;
const numericLabels = { reply_budget_per_minute: '每分钟自主回应上限', merge_window_sec: '同人短句等待秒数', merge_max_wait_sec: '连续追加最长等待秒数' };
type NumericKey = keyof typeof numericLabels;
interface Decision { message_id: string; topic_id: string; action: string; reason: string }

export function ConversationSettings({ scope: initialScope = 'web:console' }: { scope?: string }) {
  const [scope, setScope] = useState(initialScope);
  const [state, setState] = useState<CommunityState | null>(null);
  const [settings, setSettings] = useState<ConversationPolicy | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [minutes, setMinutes] = useState('30');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [numbers, setNumbers] = useState<Partial<Record<NumericKey, string>>>({});
  const requestSequence = useRef(0);
  const mutationSequence = useRef(0);
  const mutating = useRef(false);
  const [loading, setLoading] = useState(true);
  const identity = useRequestScope(scope);
  const dirty = Boolean(state && settings && (JSON.stringify(settings) !== JSON.stringify(state.settings) || (Object.keys(numericLabels) as NumericKey[]).some(key => numbers[key] !== String(state.settings[key]))));
  const validNumbers = (Object.keys(numericLabels) as NumericKey[]).every(key => { const raw = numbers[key]; const value = Number(raw); return raw?.trim() && Number.isFinite(value) && value >= (key === 'reply_budget_per_minute' ? 1 : 0) && value <= (key === 'reply_budget_per_minute' ? 60 : 10) && (key !== 'reply_budget_per_minute' || Number.isInteger(value)); }) && Number(numbers.merge_max_wait_sec) >= Number(numbers.merge_window_sec);
  const validMinutes = /^\d+$/.test(minutes) && Number(minutes) >= 1 && Number(minutes) <= 10080;
  const dirtyRef = useRef(false); dirtyRef.current = dirty;
  const confirmDiscard = useUnsavedChanges(dirty);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const refresh = useCallback(async (signal?: AbortSignal, resetDraft = false) => {
    const request = ++requestSequence.current;
    const [current, logs] = await Promise.all([
      featureRequest<CommunityState>('/v1/community/state', { scope, signal }),
      featureRequest<{ items: Decision[] }>('/v1/community/decisions', { scope, signal }),
    ]);
    if (signal?.aborted || !identity.isCurrent() || request !== requestSequence.current || activeScope.current !== scope) return;
    setState(current); if (resetDraft || !dirtyRef.current) { setSettings(current.settings); setNumbers(Object.fromEntries((Object.keys(numericLabels) as NumericKey[]).map(key => [key, String(current.settings[key])]))); } setDecisions(logs.items); setLoading(false);
  }, [scope]);
  useEffect(() => { const controller = new AbortController(); mutationSequence.current++; mutating.current = false; setState(null); setSettings(null); dirtyRef.current = false; setLoading(true); setBusy(false); setError(''); setNotice(''); void refresh(controller.signal, true).catch(reason => { if (!controller.signal.aborted) { setError(String(reason)); setLoading(false); } }); return () => controller.abort(); }, [refresh]);
  const run = async (action: () => Promise<unknown>, resetDraft = false, message = '操作已完成。') => {
    if (mutating.current) return; mutating.current = true; const mutation = ++mutationSequence.current;
    setBusy(true); setError(''); setNotice(''); try { await action(); if (identity.isCurrent()) { await refresh(undefined, resetDraft); if (identity.isCurrent()) setNotice(message); } } catch (reason) { if (identity.isCurrent()) setError(String(reason)); } finally { if (mutation === mutationSequence.current) { mutating.current = false; if (identity.isCurrent()) setBusy(false); } }
  };
  return <FeaturePage title="会话与接话" description="设置仅作用于选中的会话。新策略默认关闭；保存后由 QQ 适配器在数秒内读取。">
    <ScopeSelect value={scope} onChange={value => { if (confirmDiscard()) setScope(value); }} />
    <FeatureFeedback error={error} notice={notice} retry={() => void run(() => refresh())} />
    {loading && <p role="status">正在读取会话设置…</p>}
    {state && settings && <>
      <form className="space-y-4 border border-[var(--duties-border)] p-4" onSubmit={event => { event.preventDefault(); void run(() => featureRequest('/v1/community/settings', { scope, method: 'PATCH', body: { ...settings, ...Object.fromEntries(Object.entries(numbers).map(([key, value]) => [key, Number(value)])) } }), true, '对话设置已保存。'); }}>
        <fieldset disabled={busy} className="min-w-0 space-y-4"><legend className="font-semibold">接话策略{dirty ? ' · 有未保存修改' : ''}</legend><label className="flex gap-2"><input type="checkbox" checked={settings.response_policy_enabled} onChange={event => setSettings({ ...settings, response_policy_enabled: event.target.checked })} />统一选择忽略、表态、短答和办事</label>
        <label className="flex gap-2"><input type="checkbox" checked={settings.topic_enabled} onChange={event => setSettings({ ...settings, topic_enabled: event.target.checked })} />按引用和参与者接续话题（保留最多 6 小时话题缓存）</label>
        {(Object.keys(numericLabels) as NumericKey[]).map(key => <label key={key} className="grid min-w-0 gap-2 sm:grid-cols-2 sm:items-center">{numericLabels[key]}<input aria-label={numericLabels[key]} inputMode={key === 'reply_budget_per_minute' ? 'numeric' : 'decimal'} className={INPUT} value={numbers[key] ?? ''} onChange={event => setNumbers(current => ({ ...current, [key]: event.target.value }))} /></label>)}
        <p className="text-xs text-[var(--duties-secondary)]">回应上限为 1～60 的整数；等待时间为 0～10 秒，0 表示关闭短句等待，最长等待不能短于短句窗口。</p>
        {!validNumbers && <p role="alert" className="text-[var(--duties-danger)]">请检查数值范围及两项等待时间。</p>}
        <button className={primaryClass} disabled={busy || !validNumbers}>保存对话设置</button></fieldset>
      </form>
      <section className={sectionClass}><h2 className="font-semibold">临时安静</h2>
        <p>{state.quiet.mode ? `${state.quiet.mode === 'listen' ? '旁听' : '完全安静'}，到 ${new Date((state.quiet.expires_at || 0) * 1000).toLocaleString()} 自动恢复` : '正常回应'}</p>
        <p className="text-sm">旁听仍接明确 @、引用和命令；完全安静暂停普通对话与通知，恢复、状态和取消等控制命令仍可使用。</p>
        <label className="block max-w-sm">分钟数<input aria-label="安静分钟数" type="number" min={1} max={10080} className={INPUT} value={minutes} onChange={event => setMinutes(event.target.value)} /></label>
        <div className="flex flex-wrap gap-3">{(['listen', 'silent', 'off'] as const).map(mode => <button className={actionClass} disabled={busy || (mode !== 'off' && !validMinutes)} key={mode} onClick={() => void run(() => featureRequest('/v1/community/quiet', { scope, method: 'POST', body: { mode, duration_sec: mode === 'off' ? 0 : Number(minutes) * 60 } }))}>{mode === 'listen' ? '旁听' : mode === 'silent' ? '完全安静' : '恢复'}</button>)}</div>
      </section>
      <section className={sectionClass}><h2 className="font-semibold">最近接话判定</h2><button className={actionClass} disabled={busy} onClick={() => void run(() => refresh())}>刷新判定</button>
        {!decisions.length && <EmptyState>启用策略并收到群消息后，这里会显示实际判定。</EmptyState>}
        {decisions.map(decision => <article key={decision.message_id} className="border border-[var(--duties-border)] p-3"><p>{{ ignore: '忽略', reaction: '表态', react: '表态', reply: '回复', short_reply: '短答', task: '执行任务', execute: '执行任务' }[decision.action] || decision.action} · {decision.reason}</p><p className="text-sm">消息 {decision.message_id} · 话题 {decision.topic_id || '未分配'}</p></article>)}
      </section>
    </>}
  </FeaturePage>;
}

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { featureRequest } from '../client';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useRequestScope } from '../asyncState';
import { actionClass, controlClass, EmptyState, FeatureFeedback, FeaturePage, primaryClass, sectionClass } from '../ui';
import { ScopeSelect } from './ScopeSelect';

interface Reminder { id: string; text: string; due_at: string; timezone: string; revision: number; status: string; delivery_status: string; owner_label?: string }
const deliveries: Record<string, string> = { scheduled: '等待到期', sending: '准备发送', queued: '等待送达', delivered: '已送达', failed: '发送失败', outcome_unknown: '送达结果待核对' };

function readReminders(value: unknown): Reminder[] {
  const data = value as { reminders?: unknown } | null;
  if (!data || !Array.isArray(data.reminders) || data.reminders.some(item => {
    if (!item || typeof item !== 'object') return true;
    if (['id', 'text', 'due_at', 'timezone', 'status', 'delivery_status'].some(key => typeof item[key] !== 'string')) return true;
    if (!Number.isInteger(item.revision) || item.revision < 1 || !Number.isFinite(Date.parse(item.due_at))) return true;
    if (item.owner_label !== undefined && typeof item.owner_label !== 'string') return true;
    try { new Intl.DateTimeFormat('zh-CN', { timeZone: item.timezone }); } catch { return true; }
    return false;
  })) throw new Error('提醒列表响应格式无效，请刷新或检查服务版本。');
  return data.reminders as Reminder[];
}

export function RemindersPage({ scope: initialScope = 'web:console', embedded = false }: { scope?: string; embedded?: boolean }) {
  const dueHelpId = useId();
  const ListHeading = embedded ? 'h3' : 'h2';
  const [scope, setScope] = useState(initialScope);
  const [items, setItems] = useState<Reminder[]>([]);
  const [text, setText] = useState('');
  const [due, setDue] = useState('');
  const [zone, setZone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai');
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [minutes, setMinutes] = useState('10');
  const [filter, setFilter] = useState('open');
  const [query, setQuery] = useState('');
  const sequence = useRef(0);
  const mutating = useRef(false);
  const identity = useRequestScope(scope);
  const confirmDiscard = useUnsavedChanges(Boolean(text || due));
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    try {
      const result = await featureRequest<unknown>('/v1/reminders', { scope });
      if (!identity.isCurrent() || request !== sequence.current) return;
      setItems(readReminders(result)); setReadError('');
    } catch (reason) { if (identity.isCurrent() && request === sequence.current) setReadError(String(reason)); }
    finally { if (identity.isCurrent() && request === sequence.current) setLoading(false); }
  }, [scope]);
  useEffect(() => {
    setItems([]); setLoading(true); setError(''); setReadError(''); setNotice('');
    void refresh(); const timer = setInterval(() => { if (!mutating.current) void refresh(); }, 5000);
    return () => { clearInterval(timer); sequence.current++; };
  }, [refresh]);
  const run = async (action: () => Promise<unknown>, message: string) => {
    if (mutating.current) return;
    mutating.current = true; sequence.current++; setBusy(true); setError(''); setNotice('');
    try { await action(); if (identity.isCurrent()) { setNotice(message); await refresh(); } }
    catch (reason) { if (identity.isCurrent()) setError(String(reason)); }
    finally { mutating.current = false; if (identity.isCurrent()) setBusy(false); }
  };
  const validMinutes = /^\d+$/.test(minutes) && Number(minutes) >= 1 && Number(minutes) <= 525600;
  const act = (item: Reminder, action: string) => run(() => featureRequest(`/v1/reminders/${item.id}/${action}`, {
    scope, method: 'POST', body: { expected_revision: item.revision, ...(action === 'snooze' ? { minutes: Number(minutes) } : {}), action_id: crypto.randomUUID() },
  }), action === 'complete' ? '提醒已完成。' : action === 'cancel' ? '提醒已取消。' : `已延后 ${minutes} 分钟。`);
  const visible = items.filter(item => (filter === 'all' || item.status === filter) && `${item.text} ${item.owner_label || ''} ${item.id}`.toLowerCase().includes(query.toLowerCase()));
  return <FeaturePage title="提醒与待办" embedded={embedded} description="提醒送达后仍保持未完成。核对时间和时区后创建；完成、取消与延后可以分别操作。">
    <ScopeSelect value={scope} disabled={busy} onChange={value => { if (confirmDiscard()) { setText(''); setDue(''); setScope(value); } }} />
    <FeatureFeedback error={error || readError} notice={notice} retry={() => void refresh()} />
    <form className={sectionClass} onSubmit={event => { event.preventDefault(); void run(async () => {
      await featureRequest('/v1/reminders', { scope, method: 'POST', body: { text, due_at: due, timezone: zone } });
      if (identity.isCurrent()) { setText(''); setDue(''); }
    }, '提醒已创建。'); }}>
      <fieldset disabled={busy} className="min-w-0 space-y-4"><legend className="font-semibold">新提醒</legend>
        <label className="block space-y-1">提醒内容<textarea aria-label="提醒内容" required maxLength={4000} rows={3} className={controlClass} value={text} onChange={event => setText(event.target.value)} /></label>
        <div className="grid min-w-0 gap-4 sm:grid-cols-2"><label className="min-w-0 space-y-1">当地时间<input aria-label="当地时间" aria-describedby={dueHelpId} type="datetime-local" required className={controlClass} value={due} onChange={event => setDue(event.target.value)} /><span id={dueHelpId} className="block text-xs text-[var(--duties-secondary)]">时间需在未来五年内，按填写的时区解释。</span></label>
        <label className="min-w-0 space-y-1">时区<input aria-label="时区" required className={controlClass} value={zone} onChange={event => setZone(event.target.value)} placeholder="Asia/Shanghai" /></label></div>
        <button disabled={busy} className={primaryClass}>{busy ? '正在保存…' : '创建提醒'}</button>
      </fieldset>
    </form>
    <section className={sectionClass} aria-label="提醒列表">
      <div className="flex flex-wrap items-center justify-between gap-3"><ListHeading className="font-semibold">提醒列表</ListHeading><button className={actionClass} disabled={busy || loading} onClick={() => void refresh()}>刷新提醒</button></div>
      <div className="grid min-w-0 gap-4 sm:grid-cols-2"><label className="space-y-1">搜索提醒<input className={controlClass} value={query} onChange={event => setQuery(event.target.value)} placeholder="内容、创建者或编号" /></label><label className="space-y-1">提醒状态<select className={controlClass} value={filter} onChange={event => setFilter(event.target.value)}><option value="open">未完成</option><option value="completed">已完成</option><option value="cancelled">已取消</option><option value="all">全部状态</option></select></label></div>
      <label className="block max-w-sm space-y-1">延后分钟数<input aria-label="延后分钟数" inputMode="numeric" className={controlClass} value={minutes} onChange={event => setMinutes(event.target.value)} /></label>
      {!validMinutes && <p className="text-[var(--duties-danger)]">延后时间须为 1～525600 的整数；仍可完成或取消提醒。</p>}
      {loading ? <p role="status">正在读取提醒…</p> : !readError && !visible.length && <EmptyState>{items.length ? '没有符合筛选条件的提醒。' : '这个会话暂无提醒，可在上方创建。'}</EmptyState>}
      {items.length >= 500 && <p>当前展示最近 500 项提醒，未完成事项优先。</p>}
      <ul className="space-y-3">{visible.map(item => <li key={item.id} className="space-y-3 border-t border-[var(--duties-border)] py-4">
        <h3 className="whitespace-pre-wrap font-medium">{item.text}</h3><p>{new Date(item.due_at).toLocaleString('zh-CN', { timeZone: item.timezone })} · {item.timezone}</p>
        <p className="text-xs text-[var(--duties-secondary)]">{item.id} · {item.owner_label || '未标记创建者'} · {item.status === 'completed' ? '已完成' : item.status === 'cancelled' ? '已取消' : '未完成'} · {deliveries[item.delivery_status] || item.delivery_status}</p>
        {item.status === 'open' && <div className="flex flex-wrap gap-3"><button disabled={busy} className={primaryClass} onClick={() => void act(item, 'complete')}>完成</button><button disabled={busy || !validMinutes} className={actionClass} onClick={() => void act(item, 'snooze')}>延后 {minutes || '—'} 分钟</button><button disabled={busy} className={`${actionClass} text-[var(--duties-danger)]`} onClick={() => void act(item, 'cancel')}>取消提醒</button></div>}
      </li>)}</ul>
    </section>
  </FeaturePage>;
}

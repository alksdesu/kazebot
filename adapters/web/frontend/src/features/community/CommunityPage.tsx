import { useCallback, useEffect, useRef, useState } from 'react';
import { featureRequest } from '../client';
import { ScopeSelect } from '../execution/ScopeSelect';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useRequestScope } from '../asyncState';
import { actionClass, controlClass, EmptyState, FeatureFeedback, FeaturePage, primaryClass } from '../ui';
import type { Activity, CommunityState } from './types';

const INPUT = controlClass;
const BUTTON = actionClass;
const states: Record<string, string> = { open: '进行中', closed: '已结束', cancelled: '已取消', joined: '待确认', confirmed: '已确认', waiting: '候补', left: '已退出' };

export function CommunityPage({ scope: initialScope = 'web:console' }: { scope?: string }) {
  const [scope, setScope] = useState(initialScope);
  const [items, setItems] = useState<Activity[]>([]);
  const [state, setState] = useState<CommunityState | null>(null);
  const [guide, setGuide] = useState<CommunityState['guide']>({});
  const [kind, setKind] = useState<'poll' | 'event'>('event');
  const [title, setTitle] = useState('');
  const [options, setOptions] = useState('');
  const [capacity, setCapacity] = useState('10');
  const [deadline, setDeadline] = useState('');
  const [reminder, setReminder] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [welcome, setWelcome] = useState(false);
  const identity = useRequestScope(scope);
  const guideDirty = Boolean(state && (JSON.stringify(guide) !== JSON.stringify(state.guide) || welcome !== state.settings.welcome_enabled));
  const dirtyRef = useRef(false); dirtyRef.current = guideDirty;
  const confirmDiscard = useUnsavedChanges(guideDirty || Boolean(title || options || deadline || reminder));
  const generation = useRef(0);
  const mutationSequence = useRef(0);
  const mutating = useRef(false);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const refresh = useCallback(async (resetDraft = false) => {
    const request = ++generation.current;
    const [activityResult, current] = await Promise.all([
      featureRequest<{ items: Activity[] }>('/v1/community/activities', { scope }),
      featureRequest<CommunityState>('/v1/community/state', { scope }),
    ]);
    if (request !== generation.current || activeScope.current !== scope) return;
    setItems(activityResult.items); setState(current);
    if (resetDraft || !dirtyRef.current) { setGuide(current.guide); setWelcome(current.settings.welcome_enabled); }
    setLoading(false);
  }, [scope]);
  useEffect(() => { mutationSequence.current++; mutating.current = false; setState(null); setItems([]); setGuide({}); setWelcome(false); dirtyRef.current = false; setBusy(false); setLoading(true); setError(''); setNotice(''); void refresh(true).catch(reason => { if (identity.isCurrent()) { setError(String(reason)); setLoading(false); } }); return () => { generation.current++; }; }, [refresh, scope]);
  const run = async (action: () => Promise<unknown>, message = '操作已完成。', resetDraft = false) => {
    if (mutating.current) return; mutating.current = true; const mutation = ++mutationSequence.current;
    setBusy(true); setError(''); setNotice('');
    try { await action(); if (identity.isCurrent()) { await refresh(resetDraft); if (identity.isCurrent()) setNotice(message); } } catch (reason) { if (identity.isCurrent()) setError(String(reason)); } finally { if (mutation === mutationSequence.current) { mutating.current = false; if (identity.isCurrent()) setBusy(false); } }
  };
  const visible = items.filter(item => (filter === 'all' || item.status === filter) && `${item.title} ${item.id}`.toLowerCase().includes(query.toLowerCase()));
  return <FeaturePage title="群协作" description="管理当前会话的活动、投票和群指引。发布前可以保留草稿；活动操作不会覆盖未发布的指引。">
    <ScopeSelect value={scope} onChange={value => { if (confirmDiscard()) { setTitle(''); setOptions(''); setDeadline(''); setReminder(''); setScope(value); } }} />
    <FeatureFeedback error={error} notice={notice} retry={() => void run(() => refresh(), '列表已更新。')} />
    {loading && <p role="status">正在读取活动和群指引…</p>}
    <form className="space-y-3 border border-[var(--duties-border)] p-4" onSubmit={event => { event.preventDefault(); void run(async () => {
      await featureRequest('/v1/community/activities', { scope, method: 'POST', body: {
        kind, title, options: options.split('\n').map(value => value.trim()).filter(Boolean), capacity: Number(capacity),
        deadline: deadline ? new Date(deadline).getTime() / 1000 : 0,
        reminder_at: reminder ? new Date(reminder).getTime() / 1000 : 0,
      } }); if (identity.isCurrent()) { setTitle(''); setOptions(''); setDeadline(''); setReminder(''); }
    }); }}>
      <fieldset disabled={busy} className="min-w-0 space-y-3"><legend className="font-semibold">创建投票或活动</legend>
      <label className="block">类型<select aria-label="活动类型" className={INPUT} value={kind} onChange={event => setKind(event.target.value as 'poll' | 'event')}><option value="event">活动报名</option><option value="poll">投票</option></select></label>
      <label className="block">标题<input aria-label="活动标题" required maxLength={200} className={INPUT} value={title} onChange={event => setTitle(event.target.value)} /></label>
      {kind === 'poll' ? <label className="block">选项，每行一项<textarea aria-label="投票选项" required className={INPUT} value={options} onChange={event => setOptions(event.target.value)} /></label> : <label className="block">名额<input aria-label="活动名额" type="number" required min={1} max={10000} className={INPUT} value={capacity} onChange={event => setCapacity(event.target.value)} /></label>}
      <label className="block">截止时间（本地时间，可留空）<input aria-label="截止时间" type="datetime-local" className={INPUT} value={deadline} onChange={event => setDeadline(event.target.value)} /></label>
      {kind === 'event' && <label className="block">未确认提醒时间（可留空）<input aria-label="确认提醒时间" type="datetime-local" className={INPUT} value={reminder} onChange={event => setReminder(event.target.value)} /></label>}
      <button className={primaryClass} disabled={busy}>{busy ? '正在提交…' : '创建'}</button></fieldset>
    </form>
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">活动与投票</h2><button className={BUTTON} onClick={() => void run(() => refresh(), '列表已更新。')} disabled={busy}>刷新</button></div>
    <div className="grid gap-3 sm:grid-cols-2"><label>搜索活动<input className={INPUT} value={query} onChange={event => setQuery(event.target.value)} placeholder="标题或活动编号" /></label><label>活动状态<select className={INPUT} value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option><option value="open">进行中</option><option value="closed">已结束</option><option value="cancelled">已取消</option></select></label></div>
    {!loading && !error && !visible.length && <EmptyState>{items.length ? '没有符合筛选条件的活动。' : '这个会话暂无活动，可在上方创建。'}</EmptyState>}
    {items.length >= 100 && <p>当前展示最近 100 项活动，可用标题或编号筛选。</p>}
    {visible.map(item => <article key={item.id} className="space-y-2 border border-[var(--duties-border)] p-4">
      <h3 className="font-semibold">{item.title} · {states[item.status] || item.status}</h3><p className="text-xs text-[var(--duties-secondary)]">{item.id}{item.deadline ? ` · 截止 ${new Date(item.deadline * 1000).toLocaleString()}` : ' · 无截止时间'}</p>
      {item.kind === 'poll' ? <ol>{item.options.map((option, index) => <li key={option}>{index + 1}. {option}：{item.counts[index]} 票</li>)}</ol> : <><p>名额 {item.capacity} · 剩余 {item.remaining}</p><ul>{item.participants.map((member, index) => <li key={`${member.name}-${index}`}>{member.name} · {states[member.status]}</li>)}</ul></>}
      {item.status === 'open' && <div className="flex flex-wrap gap-2">{(['remind', 'close', 'cancel'] as const).filter(action => item.kind === 'event' || action !== 'remind').map(action => <button key={action} className={BUTTON} disabled={busy} onClick={() => {
        if (action === 'cancel' && !window.confirm(`取消「${item.title}」并停止其后续提醒？`)) return;
        void run(() => featureRequest(`/v1/community/activities/${item.id}/${action}`, { scope, method: 'POST', body: {} }));
      }}>{action === 'remind' ? '提醒未确认者' : action === 'close' ? '结束' : '取消'}</button>)}</div>}
    </article>)}
    {state && <form className="space-y-3 border border-[var(--duties-border)] p-4" onSubmit={event => { event.preventDefault(); void run(async () => {
      await featureRequest('/v1/community/guide', { scope, method: 'PUT', body: guide });
      await featureRequest('/v1/community/settings', { scope, method: 'PATCH', body: { welcome_enabled: welcome } });
    }, '群指引已发布。', true); }}>
      <fieldset disabled={busy} className="min-w-0 space-y-3"><legend className="font-semibold">群指引{guideDirty ? ' · 有未发布修改' : ''}</legend><p className="text-sm">发布后，群成员发送 /群规、/群资料、/常见问题 即可读取。未填写的内容不会由模型补造。</p>
      {(['rules', 'resources', 'faq', 'welcome'] as const).map(key => <label className="block" key={key}>{{ rules: '群规', resources: '资料和链接', faq: '常见问题', welcome: '欢迎词' }[key]}<textarea aria-label={{ rules: '群规', resources: '资料和链接', faq: '常见问题', welcome: '欢迎词' }[key]} className={INPUT} value={guide[key] || ''} onChange={event => setGuide(current => ({ ...current, [key]: event.target.value }))} /></label>)}
      <label className="flex gap-2"><input type="checkbox" checked={welcome} onChange={event => setWelcome(event.target.checked)} />新成员欢迎（随群指引一起发布，默认关闭）</label>
      <button className={primaryClass} disabled={busy}>发布群指引</button></fieldset>
    </form>}
  </FeaturePage>;
}

import { useEffect, useRef, useState } from 'react';
import { downloadFeature, featureRequest } from '../client';
import { useRequestScope } from '../asyncState';
import { actionClass, FeatureFeedback, primaryClass } from '../ui';
import { readExecutionPlan, statusLabel, type ExecutionPlan } from './types';

export function ExecutionProgressCard({ planId, scope }: { planId: string; scope?: string }) {
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState('');
  const [retry, setRetry] = useState(0);
  const identity = useRequestScope(`${planId}|${scope || ''}`);
  const sequence = useRef(0);
  const mutating = useRef(false);
  useEffect(() => {
    let alive = true;
    setPlan(null); setError(''); setReadError(''); setBusy(false); mutating.current = false;
    const refresh = async () => {
      if (mutating.current) return;
      const request = ++sequence.current;
      try { const value = await featureRequest<unknown>(`/v1/execution/plans/${planId}`, { scope }); if (alive && request === sequence.current) { setPlan(readExecutionPlan(value)); setReadError(''); } }
      catch (reason) { if (alive && request === sequence.current) setReadError(String(reason)); }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => { alive = false; sequence.current++; clearInterval(timer); };
  }, [planId, scope, retry]);
  if (!plan || plan.id !== planId || (scope && plan.scope !== scope)) return readError ? <FeatureFeedback error={readError} retry={() => setRetry(current => current + 1)} /> : <p role="status">正在读取计划进度…</p>;
  const action = async (name: string) => {
    if (mutating.current) return;
    mutating.current = true; sequence.current++; setBusy(true);
    try { setError(''); const next = readExecutionPlan(await featureRequest<unknown>(`/v1/execution/plans/${planId}/${name}`, { scope, method: 'POST', body: { expected_revision: plan.revision, step_ids: [] } })); if (identity.isCurrent()) setPlan(next); } catch (reason) { if (identity.isCurrent()) setError(String(reason)); } finally { if (identity.isCurrent()) { mutating.current = false; setBusy(false); } }
  };
  return <section className="min-w-0 space-y-3 border border-[var(--duties-border)] p-3 text-sm [overflow-wrap:anywhere]" aria-label="计划步骤进度">
    <h3 className="font-semibold">{plan.goal} · {statusLabel[plan.status]}</h3><p className="text-sm">{plan.id} · 版本 {plan.revision} · {plan.work_scope}</p>
    <div className="space-y-1"><p className="font-semibold">输入清单</p>{plan.inputs.map((input, index) => <details key={index}><summary>{input.label || `资料 ${index + 1}`} · {input.kind}</summary><pre className="max-h-48 overflow-auto whitespace-pre-wrap text-sm">{input.value}</pre></details>)}</div>
    <progress className="w-full" max={plan.steps.length || 1} value={plan.steps.filter(step => step.status === 'succeeded').length} />
    <ol className="space-y-1">{plan.steps.map(step => <li key={step.id}>{step.title}：{statusLabel[step.status]}
      {step.kind === 'tool' && <div className="text-sm"><p>工具：{step.operation}</p><dl>{Object.entries(step.arguments).map(([key, value]) => <div key={key}><dt className="font-semibold">{key}</dt><dd className="whitespace-pre-wrap">{typeof value === 'string' ? value : JSON.stringify(value)}</dd></div>)}</dl></div>}
      {step.instruction && <p className="text-sm">{step.instruction}</p>}
      {step.dependencies.length > 0 && <p className="text-sm">依赖：{step.dependencies.map(id => plan.steps.find(candidate => candidate.id === id)?.title || id).join('、')}</p>}
      {step.error && <p className="text-sm">{step.error}</p>}</li>)}</ol>
    <FeatureFeedback error={error || readError} retry={() => setRetry(current => current + 1)} />
    <div className="flex flex-wrap gap-2">{plan.status === 'draft' && <button disabled={busy} className={primaryClass} onClick={() => void action('confirm')}>确认此版本执行</button>}
      {!['completed', 'cancelled'].includes(plan.status) && <button disabled={busy} className={actionClass} onClick={() => void action('cancel')}>停止</button>}
      {['partial', 'failed'].includes(plan.status) && <button disabled={busy} className={actionClass} onClick={() => void action('retry')}>只重试失败步骤</button>}
      {plan.artifacts.map(artifact => <button disabled={busy} className={actionClass} key={artifact.id} onClick={() => void downloadFeature(`/v1/execution/artifacts/${artifact.id}`, artifact.name, scope).catch(reason => setError(String(reason)))}>下载 {artifact.name}</button>)}</div>
  </section>;
}

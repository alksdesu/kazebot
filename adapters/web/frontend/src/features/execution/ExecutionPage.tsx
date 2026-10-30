import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/request';
import { ReceiptUnreadable, readReceipt, receipted } from '../../api/shape';
import { uploadAttachment } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { featureRequest, downloadFeature } from '../client';
import { useRequestGuard } from '../../hooks/useRequestGuard';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { startPolling } from '../../utils/polling';
import { actionClass, controlClass, EmptyState, FeatureFeedback, FeaturePage, primaryClass, sectionClass } from '../ui';
import { ScopeSelect } from './ScopeSelect';
import { StepEditor } from './StepEditor';
import { PLAN_ACTION_RECEIPTS, readExecutionPlan, readExecutionPlans, readExecutionTools, statusLabel, type ExecutionPlan, type PlanAction, type PlanInput, type PlanStep, type ToolOption } from './types';
import { appendSentence } from '../../utils/sentence';

// 传上了却读不懂回执：文件已经在服务器上，只是拿不到路径，加不进清单。
const uploadSource = (file: File, scope: string, token: string) => uploadAttachment(file, scope, token).catch((reason: unknown) => {
  throw reason instanceof ReceiptUnreadable ? new Error(appendSentence(reason.message, '没有加进输入清单，重新选择会再传一份')) : reason;
});

const PLANS = '执行计划列表';
const TOOLS = '可用工具列表';

export function ExecutionPage({ scope: initialScope = 'web:console' }: { scope?: string }) {
  const [scope, setScope] = useState(initialScope);
  const [plans, setPlans] = useState<ExecutionPlan[]>([]);
  const [selected, setSelected] = useState<ExecutionPlan | null>(null);
  const [editRevision, setEditRevision] = useState<number | null>(null);
  const [goal, setGoal] = useState('');
  const [workScope, setWorkScope] = useState('');
  const [sources, setSources] = useState<PlanInput[]>([]);
  const [steps, setSteps] = useState<PlanStep[]>([]);
  const [tools, setTools] = useState<ToolOption[]>([]);
  const [sourceText, setSourceText] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [failedIds, setFailedIds] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [readError, setReadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [stepsValid, setStepsValid] = useState(true);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const mutating = useRef(false);
  const requests = useRequestGuard(scope);
  const token = useSettingsStore(state => state.adminToken);
  const refresh = useCallback(async () => {
    const request = requests.begin('plans');
    try {
      const response = await featureRequest('/v1/execution/plans', { what: PLANS, scope, signal: request.signal });
      if (!request.isCurrent()) return;
      const plans = readExecutionPlans(response);
      setPlans(plans); setReadError('');
      setSelected(current => current ? plans.find(plan => plan.id === current.id) || current : null);
    } catch (reason) { if (request.isCurrent()) setReadError(errorMessage(reason)); }
    finally { if (request.isCurrent()) setLoading(false); }
  }, [requests, scope]);
  useEffect(() => {
    setLoading(true); setReadError(''); setTools([]);
    const stop = startPolling(async () => { if (!mutating.current) await refresh(); }, 3000, { first: refresh });
    const request = requests.begin('tools');
    void featureRequest('/v1/execution/tools', { what: TOOLS, scope, signal: request.signal }).then(result => { if (request.isCurrent()) setTools(readExecutionTools(TOOLS, result)); }).catch(reason => { if (request.isCurrent()) setError(`工具列表读取失败：${errorMessage(reason)}`); });
    return stop;
  }, [requests, refresh, scope]);
  const choose = (plan: ExecutionPlan | null) => {
    setSelected(plan); setEditRevision(plan?.revision ?? null); setGoal(plan?.goal || ''); setWorkScope(plan?.work_scope || ''); setSources(plan?.inputs || []); setSteps(plan?.steps || []); setFailedIds([]); setSourceText(''); setError(''); setNotice(''); setStepsValid(true);
  };
  // action 交回回执读不懂的原因：写已经做完了，照常收尾，原因另起一行报。
  const run = async (action: () => Promise<string | void>) => {
    if (mutating.current) return;
    mutating.current = true; requests.drop('plans'); setBusy(true); setError(''); setNotice('');
    try {
      const receiptProblem = await action();
      if (requests.isCurrent()) { await refresh(); setNotice('操作已完成。'); if (receiptProblem) setError(receiptProblem); }
    } catch (reason) { if (requests.isCurrent()) setError(errorMessage(reason)); } finally { mutating.current = false; if (requests.isCurrent()) setBusy(false); }
  };
  const preview = () => run(async () => {
    const body = { goal, work_scope: workScope, inputs: sources, steps };
    const receipt = selected ? '计划已经改好，可回执' : '计划已经建好，可回执';
    const { result: plan, receiptProblem } = await receipted(featureRequest(selected ? `/v1/execution/plans/${selected.id}` : '/v1/execution/plans', {
      scope, method: selected ? 'PATCH' : 'POST', receipt, body: selected ? { ...body, expected_revision: editRevision } : body,
    }).then(value => readReceipt(receipt, value, readExecutionPlan)));
    if (requests.isCurrent() && plan) choose(plan);
    // 手上的版本号没换成新的：不重新选中，再存会被当成别处改过。
    return receiptProblem && appendSentence(receiptProblem, '在列表里重新选中这份计划再接着改');
  });
  const action = (name: PlanAction, body: unknown = {}) => run(async () => {
    if (!selected) return;
    const receipt = PLAN_ACTION_RECEIPTS[name];
    const { result: updated, receiptProblem } = await receipted(featureRequest(`/v1/execution/plans/${selected.id}/${name}`, { scope, method: 'POST', receipt, body })
      .then(value => readReceipt(receipt, value, readExecutionPlan)));
    if (requests.isCurrent() && updated) setSelected(updated);
    return receiptProblem;
  });
  const editable = !selected || selected.status === 'draft';
  const staleDraft = selected?.status === 'draft' && editRevision !== selected.revision;
  const draftDirty = selected?.status === 'draft' && (staleDraft || goal !== selected.goal || workScope !== selected.work_scope || JSON.stringify(sources) !== JSON.stringify(selected.inputs) || JSON.stringify(steps) !== JSON.stringify(selected.steps));
  const dirty = Boolean(sourceText || draftDirty || (!selected && (goal || workScope || sources.length || steps.length)));
  const confirmDiscard = useUnsavedChanges(dirty);
  const visiblePlans = plans.filter(plan => (filter === 'all' || (filter === 'active' ? !['completed', 'cancelled'].includes(plan.status) : plan.status === filter)) && `${plan.goal} ${plan.id}`.toLowerCase().includes(query.toLowerCase()));
  return <FeaturePage title="执行计划与批处理" description="核对目标、资料和每一步操作，确认当前版本后执行。已完成步骤不会因重试而再次执行。">
    <ScopeSelect value={scope} disabled={busy} onChange={value => { if (confirmDiscard()) { setScope(value); setPlans([]); choose(null); } }} />
    <FeatureFeedback error={error || readError} notice={notice} retry={() => void refresh()} />
    <div className="grid min-w-0 gap-3 sm:grid-cols-2"><label>搜索计划<input className={controlClass} value={query} onChange={event => setQuery(event.target.value)} placeholder="目标或计划编号" /></label><label>计划状态<select className={controlClass} value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option><option value="active">待处理</option><option value="completed">已完成</option><option value="cancelled">已取消</option></select></label></div>
    {loading ? <p role="status">正在读取计划…</p> : !readError && !visiblePlans.length && <EmptyState>{plans.length ? '没有符合筛选条件的计划。' : '暂无计划，可在下方填写目标并生成预览。'}</EmptyState>}
    {plans.length >= 500 && <p>当前展示最近 500 项计划。</p>}
    <nav className="flex flex-wrap gap-2"><button disabled={busy} className={actionClass} onClick={() => { if (confirmDiscard()) choose(null); }}>新计划</button>{visiblePlans.map(plan => <button disabled={busy} className={actionClass} key={plan.id} aria-pressed={selected?.id === plan.id} onClick={() => { if (confirmDiscard()) choose(plan); }}>{plan.goal.slice(0, 30)} · {statusLabel[plan.status] || plan.status}</button>)}</nav>
    {editable && <fieldset disabled={busy} className={sectionClass}><legend className="px-1 font-semibold">{selected ? '编辑草稿' : '新计划'}</legend>
      <label className="block">目标<textarea aria-label="目标" className={controlClass} value={goal} onChange={event => setGoal(event.target.value)} /></label>
      <label className="block">处理范围与要求<textarea aria-label="处理范围与要求" className={controlClass} value={workScope} onChange={event => setWorkScope(event.target.value)} /></label>
      <label className="block">资料或链接<textarea aria-label="资料或链接" className={controlClass} value={sourceText} onChange={event => setSourceText(event.target.value)} /></label>
      <button className={actionClass} type="button" onClick={() => { if (sourceText.trim()) { setSources([...sources, { kind: /^https?:\/\//.test(sourceText.trim()) ? 'url' : 'text', value: sourceText.trim(), label: `资料 ${sources.length + 1}` }]); setSourceText(''); setSteps([]); } }}>加入输入清单</button>
      <label className="block min-w-0">选择文件<input aria-label="选择文件" className="block max-w-full" type="file" multiple onChange={event => {
        const files = Array.from(event.target.files || []);
        void run(async () => { const uploaded = await Promise.all(files.map(file => uploadSource(file, scope, token || ''))); requests.check(); setSources(current => [...current, ...uploaded.map((file, index): PlanInput => ({ kind: 'file', value: file.path, label: files[index].name }))]); setSteps([]); });
      }} /></label>
      <ul>{sources.map((source, index) => <li className="flex min-w-0 flex-wrap justify-between gap-3 border-b py-2 [overflow-wrap:anywhere]" key={`${index}-${source.value}`}>{source.label}: {source.kind === 'text' ? source.value.slice(0, 100) : source.value}<button type="button" onClick={() => { setSources(sources.filter((_, at) => at !== index)); setSteps([]); }}>移除</button></li>)}</ul>
      <StepEditor steps={steps} tools={tools} onChange={setSteps} onValidityChange={setStepsValid} />
      {staleDraft && <p role="alert">计划已在其他位置修改，请先重新加载草稿再编辑。<button className="ml-2 border p-2" onClick={() => { if (confirmDiscard()) choose(selected); }}>重新加载草稿</button></p>}
      <button disabled={busy || staleDraft || !stepsValid || !goal.trim()} className={primaryClass} onClick={() => void preview()}>{selected ? '保存修改并更新预览' : '生成计划预览'}</button>
    </fieldset>}
    {selected && <section className={sectionClass} aria-label="执行进度">
      <h2 className="font-semibold">{selected.id} · 版本 {selected.revision} · {statusLabel[selected.status]}</h2>
      <p>{selected.goal}</p><p className="text-sm">{selected.work_scope}</p>
      <progress className="w-full" max={selected.steps.length || 1} value={selected.steps.filter(step => step.status === 'succeeded').length} />
      <p>{selected.steps.filter(step => step.status === 'succeeded').length} / {selected.steps.length} 步完成</p>
      {draftDirty && <p>有修改尚未保存，请先更新计划预览。</p>}
      {selected.status === 'draft' && <button disabled={busy || draftDirty || !stepsValid} className={primaryClass} onClick={() => void action('confirm', { expected_revision: selected.revision })}>确认版本 {selected.revision} 并执行</button>}
      {!['completed', 'cancelled'].includes(selected.status) && <button disabled={busy} className={`${actionClass} text-[var(--duties-danger)]`} onClick={() => void action('cancel')}>停止计划</button>}
      {selected.steps.map(step => <div key={step.id} className="space-y-2 border-t py-3">
        <p>{step.status === 'failed' && <input aria-label={`重试 ${step.title}`} type="checkbox" checked={failedIds.includes(step.id)} onChange={event => setFailedIds(event.target.checked ? [...failedIds, step.id] : failedIds.filter(id => id !== step.id))} />} {step.title} · {statusLabel[step.status]} · 第 {step.attempt} 次</p>
        {step.dependencies.length > 0 && <p className="text-sm">依赖：{step.dependencies.map(id => selected.steps.find(item => item.id === id)?.title || id).join('、')}</p>}
        {step.error && <p>{step.error}</p>}
        {step.result && <details><summary>查看结果</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap text-sm">{step.result}</pre></details>}
        {step.status === 'outcome_unknown' && <Reconcile disabled={busy} onSubmit={(resolution, note) => void action('resolve-step', { step_id: step.id, resolution, note })} />}
      </div>)}
      {['failed', 'partial'].includes(selected.status) && <button disabled={busy} className={primaryClass} onClick={() => void action('retry', { step_ids: failedIds })}>{failedIds.length ? '只重试勾选失败步骤' : '只重试失败步骤'}</button>}
      <h3 className="font-semibold">逐项结果</h3><ul className="space-y-3">{selected.items.map(item => <li key={item.index} className="space-y-2 border-b pb-3"><p>{item.label}：{statusLabel[item.status]}</p>
        {item.errors?.map((message, index) => <p className="text-sm" key={index}>{message}</p>)}
        {item.artifacts?.map(artifact => <button className="mr-2 border p-2" key={artifact.id} onClick={() => void run(() => downloadFeature(`/v1/execution/artifacts/${artifact.id}`, artifact.name, scope))}>下载 {item.label} 的结果</button>)}
        {['failed', 'partial'].includes(selected.status) && selected.steps.some(step => item.step_ids.includes(step.id) && step.status === 'failed') && <button disabled={busy} className={actionClass} onClick={() => void action('retry', { step_ids: selected.steps.filter(step => item.step_ids.includes(step.id) && step.status === 'failed').map(step => step.id) })}>只重试 {item.label} 的失败步骤</button>}
      </li>)}</ul>
      <div className="flex flex-wrap gap-3">{selected.artifacts.map(artifact => <button disabled={busy} className={actionClass} key={artifact.id} onClick={() => void run(() => downloadFeature(`/v1/execution/artifacts/${artifact.id}`, artifact.name, scope))}>下载 {artifact.name}</button>)}
        <button disabled={busy} className={actionClass} onClick={() => void run(() => downloadFeature(`/v1/execution/plans/${selected.id}/bundle`, `${selected.id}.zip`, scope))}>下载结果与逐项清单</button></div>
    </section>}
  </FeaturePage>;
}

function Reconcile({ disabled, onSubmit }: { disabled: boolean; onSubmit: (resolution: string, note: string) => void }) {
  const [note, setNote] = useState('');
  return <div><label>核对说明<input className={controlClass} value={note} onChange={event => setNote(event.target.value)} /></label>
    <button disabled={disabled || !note.trim()} className="ml-2 border p-2" onClick={() => onSubmit('succeeded', note)}>已核实执行成功</button>
    <button disabled={disabled || !note.trim()} className="ml-2 border p-2" onClick={() => onSubmit('not_applied', note)}>已核实没有执行</button></div>;
}

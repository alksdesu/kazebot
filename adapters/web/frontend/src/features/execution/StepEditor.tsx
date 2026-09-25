import { useEffect, useRef, useState } from 'react';
import { actionClass, controlClass } from '../ui';
import type { ParameterSchema, PlanStep, ToolOption } from './types';

function ParameterInput({ value, spec, label, onChange }: { value: unknown; spec: ParameterSchema; label: string; onChange: (value: unknown) => void }) {
  const display = (item: unknown) => item === undefined ? '' : typeof item === 'object' ? JSON.stringify(item) : String(item);
  const [raw, setRaw] = useState(() => display(value));
  const sent = useRef(value);
  useEffect(() => { if (value !== sent.current) setRaw(display(value)); sent.current = value; }, [value]);
  if (spec.type === 'boolean' || spec.enum) return <select className={controlClass} aria-label={label} value={value === undefined ? '' : JSON.stringify(value)} onChange={event => onChange(event.target.value === '' ? undefined : JSON.parse(event.target.value))}>
    <option value="">未设置</option>{(spec.enum || [true, false]).map(option => <option key={JSON.stringify(option)} value={JSON.stringify(option)}>{typeof option === 'boolean' ? option ? '开启' : '关闭' : String(option)}</option>)}
  </select>;
  const edit = (text: string) => {
    setRaw(text);
    let next: unknown = text;
    if (['number', 'integer'].includes(spec.type || '') && text.trim() && Number.isFinite(Number(text))) next = Number(text);
    if (spec.type === 'object' || spec.type === 'array') { try { next = JSON.parse(text); } catch { next = text; } }
    sent.current = next; onChange(next);
  };
  return spec.type === 'object' || spec.type === 'array'
    ? <textarea className={controlClass} aria-label={label} rows={3} value={raw} onChange={event => edit(event.target.value)} />
    : <input className={controlClass} aria-label={label} inputMode={['number', 'integer'].includes(spec.type || '') ? 'decimal' : undefined} value={raw} onChange={event => edit(event.target.value)} />;
}

export function parameterError(value: unknown, spec: ParameterSchema, required: boolean): string {
  if (value === undefined) return required ? '此参数为必填项' : '';
  if (spec.enum && !spec.enum.some(option => JSON.stringify(option) === JSON.stringify(value))) return '请选择有效选项';
  if (spec.type === 'boolean' && typeof value !== 'boolean') return '请选择开启或关闭';
  if (spec.type === 'number' || spec.type === 'integer') {
    if (typeof value !== 'number' || !Number.isFinite(value) || (spec.type === 'integer' && !Number.isInteger(value))) return spec.type === 'integer' ? '请输入整数' : '请输入有效数字';
    if (spec.minimum !== undefined && value < spec.minimum) return `不能小于 ${spec.minimum}`;
    if (spec.maximum !== undefined && value > spec.maximum) return `不能大于 ${spec.maximum}`;
  }
  if (spec.type === 'array' && !Array.isArray(value)) return '请输入 JSON 数组，例如 ["内容"]';
  if (spec.type === 'object' && (!value || typeof value !== 'object' || Array.isArray(value))) return '请输入 JSON 对象，例如 {"字段":"内容"}';
  return '';
}

export function StepEditor({ steps, tools, onChange, onValidityChange }: { steps: PlanStep[]; tools: ToolOption[]; onChange: (steps: PlanStep[]) => void; onValidityChange?: (valid: boolean) => void }) {
  const update = (id: string, patch: Partial<PlanStep>) => onChange(steps.map(step => step.id === id ? { ...step, ...patch } : step));
  const errors = (step: PlanStep) => {
    const schema = tools.find(tool => tool.name === step.operation)?.input_schema;
    return Object.fromEntries(Object.entries(schema?.properties || {}).map(([key, spec]) => [key, parameterError(step.arguments[key], spec, Boolean(schema?.required?.includes(key)))]));
  };
  const valid = steps.every(step => step.kind !== 'tool' || Boolean(step.operation) && !Object.values(errors(step)).some(Boolean));
  useEffect(() => onValidityChange?.(valid), [valid, onValidityChange]);
  const add = () => onChange([...steps, {
    id: `custom_${crypto.randomUUID()}`, title: '新步骤', kind: 'model', dependencies: steps.length ? [steps[steps.length - 1].id] : [],
    input_index: null, operation: '', arguments: {}, instruction: '', status: 'pending', error: '', result: '', attempt: 0, task_id: '',
  }]);
  return <section aria-label="步骤编辑" className="min-w-0 space-y-4"><h3 className="font-semibold">执行步骤</h3>
    {steps.map((step, index) => <fieldset key={step.id} className="min-w-0 space-y-3 border border-[var(--duties-border)] p-3">
      <legend className="px-1 font-medium">{index + 1}. {step.title}</legend>
      <div className="grid min-w-0 gap-3 sm:grid-cols-2"><label className="min-w-0 space-y-1">步骤名称<input aria-label={`步骤 ${index + 1} 名称`} className={controlClass} value={step.title} onChange={event => update(step.id, { title: event.target.value })} /></label>
      <label className="min-w-0 space-y-1">执行方式<select className={controlClass} value={step.kind} onChange={event => update(step.id, { kind: event.target.value as PlanStep['kind'] })}>
        <option value="read_input">读取已选资料</option><option value="model">按要求处理资料</option><option value="tool">使用已授权工具</option><option value="artifact">保存前面步骤的结果</option>
      </select></label></div>
      {step.kind === 'tool' && <>
        <label className="block min-w-0 space-y-1">工具<select className={controlClass} value={step.operation} onChange={event => update(step.id, { operation: event.target.value, arguments: {} })}>
          <option value="">请选择</option>{!tools.some(tool => tool.name === step.operation) && step.operation && <option value={step.operation}>{step.operation}</option>}{tools.map(tool => <option value={tool.name} key={tool.name}>{tool.name}</option>)}
        </select></label>
        <p className="text-xs text-[var(--duties-secondary)]">{tools.find(tool => tool.name === step.operation)?.description || '选择工具后填写本步参数。'}</p>
        {tools.find(tool => tool.name === step.operation)?.effect === 'external' && <p>执行前仍会检查操作权限；结果无法确认时暂停重试，保留人工核对入口。</p>}
        {Object.entries(tools.find(tool => tool.name === step.operation)?.input_schema.properties || {}).map(([key, spec]) => <label className="block min-w-0 space-y-1" key={`${step.operation}:${key}`}>{spec.description || key}{tools.find(tool => tool.name === step.operation)?.input_schema.required?.includes(key) ? '（必填）' : ''}<ParameterInput label={spec.description || key} value={step.arguments[key]} spec={spec} onChange={value => {
          const args = { ...step.arguments }; if (value === undefined) delete args[key]; else args[key] = value; update(step.id, { arguments: args });
        }} />{errors(step)[key] && <span role="alert" className="block text-[var(--duties-danger)]">{errors(step)[key]}</span>}</label>)}
      </>}
      {step.kind === 'model' && <label className="block space-y-1">本步要求<textarea className={controlClass} value={step.instruction} onChange={event => update(step.id, { instruction: event.target.value })} /></label>}
      {step.kind === 'read_input' && <label className="block space-y-1">资料序号<input type="number" min="1" className={controlClass} value={(step.input_index ?? 0) + 1} onChange={event => update(step.id, { input_index: Number(event.target.value) - 1 })} /></label>}
      <div className="space-y-2"><p>等待哪些步骤完成</p><div className="flex flex-wrap gap-3">{steps.filter(other => other.id !== step.id).map(other => <label className="flex items-start gap-2" key={other.id}><input type="checkbox" checked={step.dependencies.includes(other.id)} onChange={event => update(step.id, { dependencies: event.target.checked ? [...step.dependencies, other.id] : step.dependencies.filter(id => id !== other.id) })} />{other.title}</label>)}</div></div>
      <button className={`${actionClass} text-[var(--duties-danger)]`} type="button" onClick={() => onChange(steps.filter(item => item.id !== step.id).map(item => ({ ...item, dependencies: item.dependencies.filter(id => id !== step.id) })))}>删除此步骤</button>
    </fieldset>)}
    <button type="button" className={actionClass} onClick={add}>添加步骤</button>
  </section>;
}

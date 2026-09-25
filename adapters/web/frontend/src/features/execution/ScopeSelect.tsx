import { controlClass } from '../ui';
import { useScopePicker, type ScopePicker } from '../useScopePicker';

interface ScopeSelectProps { value: string; onChange: (value: string) => void; disabled?: boolean; picker?: ScopePicker }

function ScopeField({ onChange, disabled = false, picker }: ScopeSelectProps & { picker: ScopePicker }) {
  const label = picker.mode === 'policy' ? '配置作用域' : '所属会话';
  return <div className="min-w-0 space-y-2"><label className="block min-w-0 space-y-2 text-sm">{label}<select aria-label={label} disabled={disabled} className={controlClass} value={picker.value} onChange={event => onChange(event.target.value)}>
    {picker.mode !== 'all' && <option value="">{picker.placeholder}</option>}
    {[...picker.options].map(([key, text]) => <option key={key} value={key}>{text}</option>)}
  </select></label><p className="break-words text-xs text-[var(--duties-secondary)]">当前范围：{picker.options.get(picker.value) || picker.placeholder}</p>
    {picker.loading && <p role="status">正在读取会话名称…</p>}
    {picker.message && <p role="status">{picker.message}</p>}
    {picker.error && <p role="status">{picker.mode === 'all' ? picker.error : '会话列表读取失败；不能选择或操作未经确认的会话。'} <button type="button" className="underline" disabled={disabled} onClick={() => void picker.reload()}>重试会话列表</button></p>}
  </div>;
}

function AutomaticScopeSelect(props: ScopeSelectProps) { const picker = useScopePicker('all', props.value); return <ScopeField {...props} picker={picker} />; }
export function ScopeSelect(props: ScopeSelectProps) { return props.picker ? <ScopeField {...props} picker={props.picker} /> : <AutomaticScopeSelect {...props} />; }

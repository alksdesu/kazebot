import { controlClass } from '../ui';
import { scopeIdentity } from '../conversationNames';
import { useConversationDirectory } from '../useConversationDirectory';
import { useConversationNames } from '../useConversationNames';

export function ScopeSelect({ value, onChange, disabled = false }: { value: string; onChange: (value: string) => void; disabled?: boolean }) {
  const { rows, error, loading, reload } = useConversationDirectory();
  const descriptions = new Map(rows.filter(row => row.conversation_key).map(row => [row.conversation_key, { scope: row.conversation_key, owner: row.owner, current_account: row.current_account }]));
  descriptions.set('web:console', { scope: 'web:console', owner: null, current_account: true });
  if (value && !descriptions.has(value)) descriptions.set(value, { scope: value, owner: null, current_account: undefined });
  const names = useConversationNames([...descriptions.values()], !loading && !error, rows.map(row => row.conversation_key));
  const options = new Map([...descriptions].map(([key]) => [key, names.get(scopeIdentity(key))!]));
  return <div className="min-w-0 space-y-2"><label className="block min-w-0 space-y-2 text-sm">所属会话<select aria-label="所属会话" disabled={disabled} className={controlClass} value={value} onChange={event => onChange(event.target.value)}>
    {[...options].map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </select></label><p className="break-words text-xs text-[var(--duties-secondary)]">当前范围：{options.get(value) || '请选择会话'}</p>
    {loading && <p role="status">正在读取会话名称…</p>}
    {error && <p role="status">{error} <button type="button" className="underline" disabled={disabled} onClick={() => void reload()}>重试会话列表</button></p>}
  </div>;
}

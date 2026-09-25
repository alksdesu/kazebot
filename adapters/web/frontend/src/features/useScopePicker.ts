import { useSettingsStore } from '../store/settingsStore';
import { scopeIdentity, type ConversationDescriptor } from './conversationNames';
import { useConversationDirectory } from './useConversationDirectory';
import { useConversationNames } from './useConversationNames';

export const INSTANCE_DEFAULTS_TARGET = '@instance-defaults';
export type ScopePickerMode = 'all' | 'policy' | 'groups';

export function useScopePicker(mode: ScopePickerMode, value: string) {
  const token = useSettingsStore(state => state.adminToken);
  const directory = useConversationDirectory(mode === 'policy');
  const eligible = directory.rows.filter(row => {
    if (!row.conversation_key) return false;
    if (mode === 'all') return true;
    if ((mode === 'groups' && row.current_account === false) || row.owner?.kind === 'agent') return false;
    return /^qq_group:.+/.test(row.conversation_key) || (mode === 'policy' && /^qq_private:.+/.test(row.conversation_key));
  });
  const descriptions = new Map<string, ConversationDescriptor>(eligible.map(row => [row.conversation_key, { scope: row.conversation_key, owner: row.owner, current_account: row.current_account }]));
  if (mode === 'all') {
    descriptions.set('web:console', { scope: 'web:console' });
    if (value && !descriptions.has(value)) descriptions.set(value, { scope: value });
  }
  const names = useConversationNames([...descriptions.values()], !directory.loading && !directory.error, eligible.map(row => row.conversation_key));
  const options = new Map([...descriptions].map(([key]) => [key, `${names.get(scopeIdentity(key))!}${directory.policyOnlyScopes.includes(key) ? '（仅保存设置）' : ''}`]));
  if (mode === 'policy') options.set(INSTANCE_DEFAULTS_TARGET, '当前实例默认配置');
  const valid = Boolean(token && options.has(value) && (mode === 'all' || (mode === 'policy' && value === INSTANCE_DEFAULTS_TARGET) || (!directory.loading && !directory.error)));
  const placeholder = mode === 'groups' ? '请选择群聊' : '请选择配置目标';
  let message = '';
  if (mode !== 'all' && value && !valid && !directory.loading && !directory.error) message = directory.rows.some(row => row.conversation_key === value && row.current_account === false)
    ? '这个会话属于历史账号，不能用于当前实例的设置或群操作。'
    : '当前目标不在可用会话中，请重新选择；不会自动切到其它群。';
  return { mode, options, valid, value: options.has(value) ? value : '', placeholder, message, ...directory };
}

export type ScopePicker = ReturnType<typeof useScopePicker>;

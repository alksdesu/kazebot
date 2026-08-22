// 号码表：群白名单、私聊白名单、管理员名单共用。备注只存本地，见 aliases.ts。
import { useState } from 'react';

import { readAliases, writeAlias, type AliasMap } from './aliases';
import { ErrorText, Input } from './components';
import { addId, idIsValid, useConsoleStore, useIdList, useInputRule } from './consoleStore';
import { LIST_SPACING, type ListSpacing } from './WordList';

interface IdListProps {
  configKey: string;
  empty: string;
  label: string;
  placeholder: string;
  spacing?: ListSpacing;
}

export const IdList = ({ configKey, empty, label, placeholder, spacing = 'option' }: IdListProps) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const ids = useIdList(configKey);
  const rule = useInputRule(configKey);
  const [pending, setPending] = useState('');
  const [rejected, setRejected] = useState('');
  const [aliases, setAliases] = useState<AliasMap>(readAliases);
  const [editing, setEditing] = useState<number | null>(null);

  const commit = () => {
    const next = addId(ids, pending, rule);
    if (next === ids && pending.trim()) {
      setRejected(idIsValid(pending, rule) ? '这个号码已经在名单里了' : '只能填数字号码');
      return;
    }
    setRejected('');
    setPending('');
    if (next !== ids) setDraft(configKey, next);
  };

  return (
    <div className={LIST_SPACING[spacing]}>
      <span className="mb-1.5 block text-xs text-[var(--duties-secondary)]">{label}</span>
      <div className="mb-2 flex flex-col gap-1">
        {ids.length === 0 && <span className="text-xs text-[var(--duties-secondary)]">{empty}</span>}
        {ids.map((id) => (
          <span className="flex items-center gap-2" key={id}>
            <code className="min-w-[10ch] text-xs">{id}</code>
            {editing === id ? (
              <Input
                autoFocus
                onBlur={() => setEditing(null)}
                onChange={(event) => setAliases(writeAlias(id, event.target.value))}
                onKeyDown={(event) => event.key === 'Enter' && setEditing(null)}
                placeholder="备注（只存在本机）"
                value={aliases[String(id)] || ''}
                width="alias"
              />
            ) : (
              <button
                className="border border-dashed border-[var(--duties-border)] px-1.5 py-0.5 text-[0.65rem] text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-secondary)] hover:text-[var(--duties-text)]"
                onClick={() => setEditing(id)}
                type="button"
              >
                {aliases[String(id)] || '加个备注'}
              </button>
            )}
            <button
              aria-label={`移出名单 ${id}`}
              className="w-6 flex-none border border-[var(--duties-border)] bg-[var(--duties-panel)] py-1 text-xs leading-tight text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-danger)] hover:text-[var(--duties-danger)]"
              onClick={() => setDraft(configKey, ids.filter((item) => item !== id))}
              type="button"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <Input
        disabled={!rule}
        inputMode="numeric"
        onChange={(event) => { setPending(event.target.value); setRejected(''); }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          commit();
        }}
        placeholder={rule ? placeholder : 'bot 没公布号码规则，先升级 bot 再改名单'}
        value={pending}
        width="wide"
      />
      {rejected && <ErrorText>{rejected}</ErrorText>}
    </div>
  );
};

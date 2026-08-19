// 号码表：群白名单、私聊白名单、管理员名单共用。备注只存本地，见 aliases.ts。
import { useState } from 'react';

import { readAliases, writeAlias, type AliasMap } from './aliases';
import { addId, idIsValid, useConsoleStore, useIdList, useInputRule } from './consoleStore';

interface IdListProps {
  configKey: string;
  empty: string;
  label: string;
  placeholder: string;
}

export const IdList = ({ configKey, empty, label, placeholder }: IdListProps) => {
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
    <div className="qc-words">
      <span className="qc-words-label">{label}</span>
      <div className="qc-idrows">
        {ids.length === 0 && <span className="qc-chip-empty">{empty}</span>}
        {ids.map((id) => (
          <span className="qc-idrow" key={id}>
            <code className="qc-idnum">{id}</code>
            {editing === id ? (
              <input
                autoFocus
                className="qc-inp qc-inp-alias"
                onBlur={() => setEditing(null)}
                onChange={(event) => setAliases(writeAlias(id, event.target.value))}
                onKeyDown={(event) => event.key === 'Enter' && setEditing(null)}
                placeholder="备注（只存在本机）"
                value={aliases[String(id)] || ''}
              />
            ) : (
              <button className="qc-idalias" onClick={() => setEditing(id)} type="button">
                {aliases[String(id)] || '加个备注'}
              </button>
            )}
            <button
              aria-label={`移出名单 ${id}`}
              className="qc-flag qc-flag-x"
              onClick={() => setDraft(configKey, ids.filter((item) => item !== id))}
              type="button"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <input
        className="qc-inp qc-inp-wide"
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
      />
      {rejected && <p className="qc-login-error">{rejected}</p>}
    </div>
  );
};

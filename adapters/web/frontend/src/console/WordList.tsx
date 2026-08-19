// 词表编辑：名字、关键词、触发前缀共用。空格与全角符号都算词的一部分，所以只按回车提交。
import { useState } from 'react';

import { addWord, useConsoleStore, useInputRule, useWordList } from './consoleStore';

interface WordListProps {
  configKey: string;
  label: string;
  placeholder: string;
  empty: string;
}

export const WordList = ({ configKey, label, placeholder, empty }: WordListProps) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const words = useWordList(configKey);
  const rule = useInputRule(configKey);
  const [pending, setPending] = useState('');

  const commit = () => {
    const next = addWord(words, pending, rule);
    setPending('');
    if (next !== words) setDraft(configKey, next);
  };

  return (
    <div className="qc-words">
      <span className="qc-words-label">{label}</span>
      <div className="qc-chips">
        {words.length === 0 && <span className="qc-chip-empty">{empty}</span>}
        {words.map((word) => (
          <span className="qc-chip" key={word}>
            <span className="qc-chip-text">{word}</span>
            <button
              aria-label={`删除 ${word}`}
              className="qc-chip-x"
              onClick={() => setDraft(configKey, words.filter((item) => item !== word))}
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
        onChange={(event) => setPending(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          commit();
        }}
        placeholder={rule ? placeholder : 'bot 没公布词表规则，先升级 bot 再改'}
        value={pending}
      />
    </div>
  );
};

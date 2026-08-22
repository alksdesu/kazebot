// 词表编辑：名字、关键词、触发前缀共用。空格与全角符号都算词的一部分，所以只按回车提交。
import { useState } from 'react';

import { Chip, Chips, Input } from './components';
import { addWord, useConsoleStore, useInputRule, useWordList } from './consoleStore';

// 三种落位：开关格内跟着复选框缩进，Loose 里只留上边距，Panel 里贴边。
export type ListSpacing = 'option' | 'loose' | 'panel';

export const LIST_SPACING: Record<ListSpacing, string> = {
  option: 'ml-[25px] mt-3',
  loose: 'mt-3',
  panel: '',
};

interface WordListProps {
  configKey: string;
  label: string;
  placeholder: string;
  empty: string;
  spacing?: ListSpacing;
}

export const WordList = ({ configKey, label, placeholder, empty, spacing = 'option' }: WordListProps) => {
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
    <div className={LIST_SPACING[spacing]}>
      <span className="mb-1.5 block text-xs text-[var(--duties-secondary)]">{label}</span>
      <Chips>
        {words.length === 0 && <span className="text-xs text-[var(--duties-secondary)]">{empty}</span>}
        {words.map((word) => (
          <Chip
            key={word}
            onRemove={() => setDraft(configKey, words.filter((item) => item !== word))}
            removeLabel={`删除 ${word}`}
          >
            {word}
          </Chip>
        ))}
      </Chips>
      <Input
        disabled={!rule}
        onChange={(event) => setPending(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          commit();
        }}
        placeholder={rule ? placeholder : 'bot 没公布词表规则，先升级 bot 再改'}
        value={pending}
        width="wide"
      />
    </div>
  );
};

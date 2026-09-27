// 词表编辑：名字、关键词、触发前缀共用。空格与全角符号都算词的一部分，所以只按回车确认。
import { useState, type ReactNode } from 'react';

import { Chip, Chips, Input } from './components';
import { addWord, useConsoleStore, useInputRule, useWordList } from './consoleStore';

// 三种落位：开关格内跟着复选框缩进，Loose 里只留上边距，Panel 里贴边。
export type ListSpacing = 'option' | 'loose' | 'panel';

export const LIST_SPACING: Record<ListSpacing, string> = {
  option: 'ml-[var(--duties-option-indent)] mt-3',
  loose: 'mt-3',
  panel: '',
};

/** 列表控件的标题行。aside 跟在标题右边，比如来源切换。 */
export const ListLabel = ({ aside, label }: { aside?: ReactNode; label: string }) => (
  <div className="mb-1.5 flex flex-wrap items-center gap-2">
    <span className="text-xs text-[var(--duties-secondary)]">{label}</span>
    {aside}
  </div>
);

interface WordListProps {
  aside?: ReactNode;
  configKey: string;
  label: string;
  placeholder: string;
  empty: string;
  spacing?: ListSpacing;
}

export const WordList = ({ aside, configKey, label, placeholder, empty, spacing = 'option' }: WordListProps) => {
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
      <ListLabel aside={aside} label={label} />
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

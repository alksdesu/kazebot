// 后端给的说明文字里，反引号标出的是代码片段，渲染成等宽字。
import { Fragment } from 'react';

const CODE = 'bg-[var(--duties-muted)] px-1 py-px font-mono text-mini';

export interface TextRun {
  code: boolean;
  text: string;
}

/** 按反引号切段。落单的反引号原样当文字，不吞掉后半句。 */
export function textRuns(text: string): TextRun[] {
  const parts = text.split('`');
  const paired = parts.length % 2 === 1;
  return parts.flatMap<TextRun>((part, index) => {
    if (index % 2 === 0) return part ? [{ code: false, text: part }] : [];
    if (!paired && index === parts.length - 1) return [{ code: false, text: `\`${part}` }];
    return [{ code: true, text: part }];
  });
}

export const RichText = ({ text }: { text: string }) => (
  <>
    {textRuns(text).map((run, index) => (
      run.code
        ? <code className={CODE} key={index}>{run.text}</code>
        : <Fragment key={index}>{run.text}</Fragment>
    ))}
  </>
);

// 双编辑器的原文那一半：折叠起来的原文编辑器，和表单与原文对不上时的交代。
import type { ReactNode } from 'react';

import type { RawForm } from '../../hooks/useRawForm';
import { YamlEditor } from '../common';

type RawSource = Pick<RawForm<unknown>, 'raw' | 'setRaw' | 'parseError' | 'editError' | 'note'>;

export const RawFormStatus = ({ source }: { source: RawSource }) => (
  <>
    {source.parseError && (
      <p className="text-xs leading-5 text-[var(--duties-danger)]" role="alert">原文解析不了，表单先停用，在原文里修好再改：{source.parseError}</p>
    )}
    {source.editError && (
      <p className="text-xs leading-5 text-[var(--duties-danger)]" role="alert">表单里的改动还没写进原文：{source.editError}</p>
    )}
    {source.note && <p className="text-xs leading-5 text-[var(--duties-tertiary)]" role="status">{source.note}。</p>}
  </>
);

export const RawSourceDetails = ({ children, height, label, loading = false, readOnly = false, source, summary }: {
  children?: ReactNode;
  height: string;
  label: string;
  /** 原文还没读回来：先不摆编辑框，免得空白的框被当成文件内容。 */
  loading?: boolean;
  readOnly?: boolean;
  source: RawSource;
  summary: string;
}) => (
  <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
    <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-tertiary)]">{summary}</summary>
    <div className="mt-3 space-y-2">
      <p className="text-xs leading-5 text-[var(--duties-tertiary)]">和上面的表单是同一份内容，改哪边另一边都跟着变。</p>
      {loading ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">正在读取原文。</p>
      ) : (
        <YamlEditor aria-label={label} height={height} onChange={source.setRaw} readOnly={readOnly} value={source.raw} />
      )}
      {children}
    </div>
  </details>
);

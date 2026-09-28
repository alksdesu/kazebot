// 折叠分区：收起时一行摘要，展开才渲染正文。平台页和全局默认页共用。
import { useId, type ReactNode } from 'react';

import { Icon } from '../components/common';
import { ErrorText } from './components';

interface SectionProps {
  children: ReactNode;
  /** 待写回的改动叫什么：平台页攒着一起「应用」，全局设置按区块「保存」。 */
  commitVerb?: '应用' | '保存';
  /** 深链锚点，同时是记住展开状态的键。 */
  id: string;
  error?: string;
  icon?: string;
  onToggle: () => void;
  open: boolean;
  /** 有几项改动落在这个分区里。0 不显示，省得每个头上都挂一个「0 项」。 */
  pending?: number;
  /** 收起时那一行。来自 bot 公布的生效状态，不是浏览器里的草稿。 */
  summary: ReactNode;
  title: string;
}

export const Section = ({
  children, commitVerb = '应用', error, icon, id, onToggle, open, pending = 0, summary, title,
}: SectionProps) => {
  const baseId = useId();
  const ids = {
    body: `${baseId}-body`,
    title: `${baseId}-title`,
    summary: `${baseId}-summary`,
    error: `${baseId}-error`,
    pending: `${baseId}-pending`,
  };
  // 按钮的名字只念标题和要紧的标记，摘要作为描述另念：整行连成一个名字，读屏每次聚焦都得听完一长串。
  const labelledBy = [ids.title, error ? ids.error : '', pending > 0 ? ids.pending : ''].filter(Boolean).join(' ');

  return (
    <section className="border-b border-[var(--duties-border)] last:border-b-0" id={`section-${id}`}>
      <h2 className="m-0">
        <button
          aria-controls={ids.body}
          aria-describedby={summary ? ids.summary : undefined}
          aria-expanded={open}
          aria-labelledby={labelledBy}
          className="app-button flex w-full min-w-0 flex-wrap items-center gap-2.5 px-1 py-3 text-left"
          onClick={onToggle}
          type="button"
        >
          <Icon className="flex-none text-[var(--duties-secondary)]" name={open ? 'keyboard_arrow_down' : 'chevron_right'} size={18} />
          {icon && <Icon className="flex-none text-[var(--duties-secondary)]" name={icon} size={18} />}
          <span className="flex-none text-base font-semibold" id={ids.title}>{title}</span>
          <span className="min-w-0 flex-1 truncate text-xs text-[var(--duties-secondary)]" id={ids.summary}>{summary}</span>
          {error && <span className="flex-none text-xs text-[var(--duties-danger)]" id={ids.error}>配置有误</span>}
          {pending > 0 && (
            <span className="flex-none text-xs font-medium text-[var(--duties-danger)]" id={ids.pending}>
              {pending} 项待{commitVerb}
            </span>
          )}
        </button>
      </h2>
      {/* 收起时容器也留着：aria-controls 指向的元素不在，读屏就不知道这个按钮管的是哪一块。 */}
      <div className="settings-section-body pb-5" hidden={!open} id={ids.body}>
        {open && (
          <>
            {error && <ErrorText>{error}</ErrorText>}
            {children}
          </>
        )}
      </div>
    </section>
  );
};

/** 分区展开后第一段：这一块管什么。后端 schema 里分区的 note。 */
export const SectionNote = ({ children }: { children: ReactNode }) => (
  <p className="mb-4 max-w-[var(--duties-prose-max-width)] text-xs leading-5 text-[var(--duties-secondary)]">
    {children}
  </p>
);

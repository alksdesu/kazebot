// [2026-05-31] NoticeBlockView renders reducer notices inside MessageCard v2.
// Why: reducer events such as retries, warnings, and errors need a compact place in
// the same message stream as text and tools. How: map NoticeBlock levels to left-border
// colors and simple icons while preserving optional titles. Purpose: avoid reintroducing
// a separate live-only preview channel for operational messages.
import type { NoticeBlock } from '../../../types/message';
import { Icon } from '../../common';

interface NoticeBlockViewProps {
  block: NoticeBlock;
}

const LEVEL_STYLES: Record<NoticeBlock['level'], { icon: string; className: string; titleClassName: string }> = {
  // [2026-06-01] Why: notice icons used Unicode information, warning, and error
  // glyphs. How: keep icon data as Material Symbol names. Purpose: notice blocks
  // share the same icon primitive as message and tool status UI.
  info: {
    icon: 'info',
    className: 'border-blue-400 bg-blue-50 text-blue-800',
    titleClassName: 'text-blue-900',
  },
  warning: {
    icon: 'warning',
    className: 'border-orange-400 bg-orange-50 text-orange-800',
    titleClassName: 'text-orange-900',
  },
  error: {
    icon: 'error',
    className: 'border-red-400 bg-red-50 text-red-800',
    titleClassName: 'text-red-900',
  },
};

export const NoticeBlockView = ({ block }: NoticeBlockViewProps) => {
  const style = LEVEL_STYLES[block.level];

  return (
    <div className={`border-l-2 px-3 py-2 text-xs ${style.className}`}>
      <div className="flex items-start gap-2">
        <span className="mt-0.5 font-mono">
          <Icon name={style.icon} size={14} />
        </span>
        <div className="min-w-0 flex-1">
          {block.title && <div className={`mb-0.5 font-mono font-semibold ${style.titleClassName}`}>{block.title}</div>}
          <div className="whitespace-pre-wrap break-words">{block.text}</div>
        </div>
      </div>
    </div>
  );
};

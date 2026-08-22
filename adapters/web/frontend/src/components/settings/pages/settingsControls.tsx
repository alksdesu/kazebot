// 带草稿态的设置页共用件：分节、开关格、存盘栏。
// 与 settingsPagePrimitives 的分工：那边是即时生效的静态版式，这边是「改了要存」的那套。
import type { ReactNode } from 'react';

const MUTED = 'text-[var(--duties-secondary)]';

export const Block = ({ children, hint, title }: {
  children: ReactNode;
  hint?: string;
  title: string;
}) => (
  <section className="mb-8">
    <div className="mb-4 flex items-baseline gap-2.5 border-b border-[var(--duties-border)] pb-2">
      <span className="font-mono text-sm font-semibold tracking-[-0.02em]">{title}</span>
      {hint && <span className={`text-xs ${MUTED}`}>{hint}</span>}
    </div>
    {children}
  </section>
);

/** 开关格。1px 间隙靠容器底色透出来当分隔线，省掉每格各画一条边。 */
export const Grid = ({ children }: { children: ReactNode }) => (
  <div className="grid gap-px overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-border)] [grid-template-columns:repeat(auto-fit,minmax(238px,1fr))]">
    {children}
  </div>
);

export const Empty = ({ children }: { children: ReactNode }) => (
  <p className={`px-4 py-6 text-center text-[0.8rem] ${MUTED}`}>{children}</p>
);

export const Footnote = ({ children }: { children: ReactNode }) => (
  <p className={`mt-3 max-w-[62ch] text-xs leading-5 ${MUTED}`}>{children}</p>
);

export const Facts = ({ children }: { children: ReactNode }) => (
  <p className={`mt-2.5 text-xs leading-5 ${MUTED}`}>{children}</p>
);

const CHECKBOX = 'h-[15px] w-[15px] flex-none accent-[var(--duties-text)]';

interface OptionProps {
  checked: boolean;
  children?: ReactNode;
  desc?: string;
  disabled?: boolean;
  name: string;
  onChange: (checked: boolean) => void;
}

// children 刻意留在 label 外面：词表和数字框都在这一层，包进 label 之后点它们旁边的
// 空白会连带翻转开关。
export const Option = ({ checked, children, desc, disabled, name, onChange }: OptionProps) => (
  <div className={`bg-[var(--duties-panel)] px-4 py-3.5${disabled ? ' opacity-50' : ''}`}>
    <label className={disabled ? 'block' : 'block cursor-pointer'}>
      <span className="flex items-center gap-2.5">
        <input
          checked={checked}
          className={CHECKBOX}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          type="checkbox"
        />
        <span className="font-medium">{name}</span>
      </span>
      {desc && <p className={`ml-[25px] mt-1.5 text-xs leading-5 ${MUTED}`}>{desc}</p>}
    </label>
    {children}
  </div>
);

/** 附属于某个开关的次级选项。层级靠缩进表达。 */
export const Sub = ({ checked, label, onChange }: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) => (
  <label className={`ml-[25px] mt-2.5 flex cursor-pointer items-center gap-2.5 text-xs ${MUTED}`}>
    <input
      checked={checked}
      className={CHECKBOX}
      onChange={(event) => onChange(event.target.checked)}
      type="checkbox"
    />
    <span>{label}</span>
  </label>
);

export const Field = ({ children, label }: { children: ReactNode; label: string }) => (
  <span className="ml-[25px] mt-2.5 flex items-center gap-2.5 text-xs">
    <label className={MUTED}>{label}</label>
    {children}
  </span>
);

export type ButtonTone = 'solid' | 'quiet' | 'halt';

const TONE: Record<ButtonTone, string> = {
  solid: 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]',
  quiet: 'border-[var(--duties-border)] bg-transparent text-[var(--duties-text)] hover:border-[var(--duties-text)]',
  halt: 'border-[var(--duties-danger)] bg-[var(--duties-danger)] text-[var(--duties-bg)]',
};

export const Button = ({ tone = 'solid', className = '', ...props }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: ButtonTone }) => (
  <button
    {...props}
    className={`border px-3.5 py-1 font-mono text-xs font-medium disabled:cursor-not-allowed disabled:opacity-40 ${TONE[tone]} ${className}`}
    type={props.type || 'button'}
  />
);

/** 自管存盘的页面用：写的是节点文件或 runtime.yaml，不走顶部那条 qq.yaml 状态条。 */
export const SaveBar = ({ busy, dirty, label = '保存', note, onReset, onSave }: {
  busy: boolean;
  dirty: boolean;
  // 一页上有两块各自存盘时，两个都叫「保存」就分不清存的是哪一块。
  label?: string;
  note: string;
  onReset: () => void;
  onSave: () => void;
}) => (
  <div className="mt-3 flex items-center gap-2 border-t border-[var(--duties-border)] pt-3">
    <span className={`text-xs ${MUTED}`}>{note}</span>
    <span className="flex-1" />
    <Button disabled={!dirty || busy} onClick={onReset} tone="quiet">还原</Button>
    <Button disabled={!dirty || busy} onClick={onSave}>{busy ? '保存中…' : label}</Button>
  </div>
);

export type PipTone = 'live' | 'idle' | 'halt';

const PIP_TONE: Record<PipTone, string> = {
  live: 'bg-[var(--duties-live)]',
  idle: 'bg-[var(--duties-tertiary)]',
  halt: 'bg-[var(--duties-danger)]',
};

/** 状态灯。颜色是唯一区分手段时读屏用户什么也拿不到，所以文字必须自带状态。 */
export const Pip = ({ tone }: { tone: PipTone }) => (
  <span aria-hidden="true" className={`inline-block h-2 w-2 flex-none rounded-full ${PIP_TONE[tone]}`} />
);

export const StatusBar = ({ children, tone }: { children: ReactNode; tone: PipTone }) => (
  <div className="flex min-h-[44px] items-center gap-2 border-b border-[var(--duties-border)] px-4 font-mono text-xs">
    <Pip tone={tone} />
    {children}
  </div>
);

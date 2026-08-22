// 带草稿态的设置页共用件：分节、开关格、存盘栏。
// 与 settingsPagePrimitives 的分工：那边是即时生效的静态版式，这边是「改了要存」的那套。
import { forwardRef, type ReactNode } from 'react';

const MUTED = 'text-[var(--duties-secondary)]';

export const Block = ({ children, hint, title }: {
  children: ReactNode;
  hint?: string;
  title: string;
}) => (
  <section className="mb-8">
    <div className="mb-4 flex items-baseline gap-2.5 border-b border-[var(--duties-border)] pb-2">
      <h2 className="m-0 font-mono text-sm font-semibold tracking-[-0.02em]">{title}</h2>
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

/** Grid 之外的独立面板，用于整块只有一份名单、一组只读行的分区。 */
export const Panel = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <div className={`border border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-3.5 ${className}`}>
    {children}
  </div>
);

/** 逐行带分隔线的列表容器。末行不留边，靠 last: 变体处理，不用 :not(:last-child)。
 *
 * 是 ul/li 而不是 div：一屏十几条能力、槽位、连接，读屏要能报出「共 N 项、第 k 项」。 */
export const List = ({ children }: { children: ReactNode }) => (
  <ul className="m-0 list-none overflow-hidden border border-[var(--duties-border)] p-0">{children}</ul>
);

export const Row = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <li
    className={`flex items-center gap-2.5 border-b border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-3 last:border-b-0 ${className}`}
  >
    {children}
  </li>
);

/** 列表项的名字。是 h3 不是 span：一屏几十项，读屏靠标题跳转才找得到目标那一条。 */
export const ItemTitle = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <h3 className={`m-0 text-sm font-medium ${className}`}>{children}</h3>
);

/** 内容多于一行的列表项。Row 是单行 flex 版，两者只差排布。 */
export const Item = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <li
    className={`border-b border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-2.5 last:border-b-0 ${className}`}
  >
    {children}
  </li>
);

export const ErrorText = ({ children }: { children: ReactNode }) => (
  <p className="mt-2.5 text-xs leading-5 text-[var(--duties-danger)]">{children}</p>
);

export const Footnote = ({ children }: { children: ReactNode }) => (
  <p className={`mt-3 max-w-[62ch] text-xs leading-5 ${MUTED}`}>{children}</p>
);

export const Facts = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <p className={`mt-2.5 text-xs leading-5 ${MUTED} ${className}`}>{children}</p>
);

/** 开关名底下那行说明。缩进对齐复选框右侧，与 Option 的 desc 同一档。 */
export const Desc = ({ children, indent = true }: { children: ReactNode; indent?: boolean }) => (
  <p className={`mt-1.5 text-xs leading-5 ${indent ? 'ml-[25px]' : ''} ${MUTED}`}>{children}</p>
);

/** Grid 之外的松散区块，放不属于任何单个开关的配置。 */
export const Loose = ({ children }: { children: ReactNode }) => (
  <div className="mt-4">{children}</div>
);

const CHECKBOX = 'h-[15px] w-[15px] flex-none accent-[var(--duties-text)]';

interface OptionProps {
  checked: boolean;
  children?: ReactNode;
  desc?: string;
  name: string;
  onChange: (checked: boolean) => void;
}

// children 刻意留在 label 外面：词表和数字框都在这一层，包进 label 之后点它们旁边的
// 空白会连带翻转开关。
export const Option = ({ checked, children, desc, name, onChange }: OptionProps) => (
  <div className="bg-[var(--duties-panel)] px-4 py-3.5">
    <label className="block cursor-pointer">
      <span className="flex items-center gap-2.5">
        <input
          checked={checked}
          className={CHECKBOX}
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

/** 没有开关、只能调参数的一组配置。
 *  别拿勾不动的复选框冒充：整块会淡成灰的，看着不可用，其实里面的输入框改得动。 */
export const Fixed = ({ children, desc, name }: {
  children?: ReactNode;
  desc?: string;
  name: string;
}) => (
  <div className="bg-[var(--duties-panel)] px-4 py-3.5">
    <span className="flex items-center gap-2.5">
      {/* 占住复选框的位置，好和同一行里带开关的卡片对齐。 */}
      <span aria-hidden className="h-[15px] w-[15px] flex-none" />
      <span className="font-medium">{name}</span>
      <span className="flex-none border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-[0.55rem] text-[var(--duties-secondary)]">
        常开
      </span>
    </span>
    {desc && <p className={`ml-[25px] mt-1.5 text-xs leading-5 ${MUTED}`}>{desc}</p>}
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

// 变体走 prop 不走 className 追加：align-items 与 font-size 都是同属性覆盖，
// 谁赢取决于 Tailwind 生成 CSS 的顺序，不由 class 属性里的先后决定。
const CHECK_ALIGN = { center: 'items-center', start: 'items-start' } as const;
const CHECK_TONE = { default: 'text-xs', muted: 'text-[0.65rem] leading-5 ' + MUTED } as const;

/** 行内复选框。Option 那种整格命中区之外的零散开关用它。 */
export const Check = ({
  align = 'center', checked, children, disabled, onChange, tone = 'default',
}: {
  // 说明文字会换行时用 start 把方框顶到第一行，居中会让它浮在两行之间。
  align?: keyof typeof CHECK_ALIGN;
  checked: boolean;
  children: ReactNode;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
  tone?: keyof typeof CHECK_TONE;
}) => (
  <label
    className={`flex gap-1.5 ${CHECK_ALIGN[align]} ${CHECK_TONE[tone]} ${disabled ? '' : 'cursor-pointer'}`}
  >
    <input
      checked={checked}
      className={CHECKBOX}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
      type="checkbox"
    />
    <span>{children}</span>
  </label>
);

export type InputWidth = 'default' | 'wide' | 'tiny' | 'flex' | 'alias';

// 定宽是默认档：数字框排在开关下面，各自撑到内容宽会参差不齐。
// flex 留给同一行里要吃掉剩余空间的（模型名、地址、搜索框）—— 用定宽会把它们截断。
const INPUT_WIDTH: Record<InputWidth, string> = {
  default: 'w-[5.5rem]',
  wide: 'w-full',
  tiny: 'w-[3.25rem]',
  flex: 'min-w-0 flex-1',
  alias: 'w-40',
};

const CONTROL_SKIN =
  'border border-[var(--duties-border)] bg-[var(--duties-bg)] font-mono text-xs text-[var(--duties-text)]' +
  ' outline-none focus:border-[var(--duties-text)] disabled:bg-[var(--duties-muted)] disabled:text-[var(--duties-secondary)]';

// 转发 ref：上传完要清空文件选择框，那只能拿到 DOM 节点自己改 value。
export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement> & { width?: InputWidth }>(
  ({ width = 'default', className = '', ...props }, ref) => (
    <input {...props} className={`px-2 py-1 ${CONTROL_SKIN} ${INPUT_WIDTH[width]} ${className}`} ref={ref} />
  ),
);
Input.displayName = 'Input';

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement> & { width?: InputWidth }>(
  ({ width = 'default', className = '', ...props }, ref) => (
    <select {...props} className={`px-2 py-1 ${CONTROL_SKIN} ${INPUT_WIDTH[width]} ${className}`} ref={ref} />
  ),
);
Select.displayName = 'Select';

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className = '', ...props }, ref) => (
    <textarea {...props} className={`block w-full resize-y p-3 leading-6 ${CONTROL_SKIN} ${className}`} ref={ref} />
  ),
);
Textarea.displayName = 'Textarea';

/** 日志、进度这类只读长文本。往下追加的内容要能滚，且不能撑破面板。 */
export const Pre = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <pre
    className={`overflow-auto whitespace-pre-wrap break-all border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-[0.65rem] leading-5 ${className}`}
  >
    {children}
  </pre>
);

export type TagTone = 'neutral' | 'live';

// 底色和字色成对换。用 className 追加会撞 —— 同属性的 arbitrary utility 谁赢取决于
// Tailwind 生成 CSS 的顺序，不由 class 属性里的先后决定，赌输就是深绿底上灰字。
const TAG_TONE: Record<TagTone, string> = {
  neutral: `border-[var(--duties-border)] bg-[var(--duties-muted)] ${MUTED}`,
  live: 'border-[var(--duties-live)] bg-[var(--duties-live)] text-[var(--duties-bg)]',
};

/** 标识类文字：群号、匿名别名、条目类型。浅底 + 等宽，和可编辑的输入框区分开。 */
export const Tag = ({ children, className = '', tone = 'neutral' }: {
  children: ReactNode;
  className?: string;
  tone?: TagTone;
}) => (
  <span
    className={`flex-none border px-1.5 py-0.5 font-mono text-[0.65rem] tracking-[-0.01em] ${TAG_TONE[tone]} ${className}`}
  >
    {children}
  </span>
);

export const Chips = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <div className={`mb-2 flex flex-wrap gap-1.5 ${className}`}>{children}</div>
);

export const Chip = ({ children, onRemove, removeLabel }: {
  children: ReactNode;
  onRemove?: () => void;
  // 读屏念到的只有那个 ×，不说清删的是哪一个等于没法用。
  removeLabel?: string;
}) => (
  // 右内边距只在有删除按钮时收窄，否则纯展示的 chip 会左右不对称。
  <span
    className={`flex items-center gap-0.5 border border-[var(--duties-border)] bg-[var(--duties-muted)] py-0.5 pl-2 text-xs ${onRemove ? 'pr-0.5' : 'pr-2'}`}
  >
    <span className="min-w-0 break-all">{children}</span>
    {onRemove && (
      <button
        aria-label={removeLabel}
        className={`px-1.5 leading-tight ${MUTED} hover:text-[var(--duties-danger)]`}
        onClick={onRemove}
        type="button"
      >
        ×
      </button>
    )}
  </span>
);

/** 只读展示：改动需要重启进程的那几个值。 */
export const ReadOnlyRow = ({ hint, name, value }: { hint?: string; name: string; value: string }) => (
  <div className="flex flex-wrap items-baseline gap-2.5 py-1.5">
    <span className={`min-w-[9em] text-xs ${MUTED}`}>{name}</span>
    <code className="break-all text-xs">{value || '未设置'}</code>
    {hint && <span className={`text-[0.65rem] ${MUTED}`}>{hint}</span>}
  </div>
);

export type ButtonTone = 'solid' | 'quiet' | 'halt' | 'danger';
export type ButtonSize = 'default' | 'sm';

const TONE: Record<ButtonTone, string> = {
  solid: 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]',
  quiet: 'border-[var(--duties-border)] bg-transparent text-[var(--duties-text)] hover:border-[var(--duties-text)]',
  halt: 'border-[var(--duties-danger)] bg-[var(--duties-danger)] text-[var(--duties-bg)]',
  // 描边红：破坏性但还没确认的那一步。确认之后才换成实心的 halt。
  danger: 'border-[var(--duties-danger)] bg-transparent text-[var(--duties-danger)] hover:bg-[var(--duties-danger)] hover:text-[var(--duties-bg)]',
};

// 一张卡上挤四个操作时默认尺寸会把它们撑成四行。
const SIZE: Record<ButtonSize, string> = {
  default: 'px-3.5 py-1 text-xs',
  sm: 'px-2 py-0.5 text-[0.65rem]',
};

const buttonSkin = (tone: ButtonTone, size: ButtonSize, className: string) =>
  `inline-block border font-mono font-medium ${SIZE[size]} ${TONE[tone]} ${className}`;

export const Button = ({ tone = 'solid', size = 'default', className = '', ...props }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: ButtonTone; size?: ButtonSize }) => (
  <button
    {...props}
    className={buttonSkin(tone, size, `disabled:cursor-not-allowed disabled:opacity-40 ${className}`)}
    type={props.type || 'button'}
  />
);

/** 长得像按钮的链接。跨实例跳转要能中键开新页，那是 <a> 才有的行为。 */
export const LinkButton = ({ tone = 'quiet', size = 'default', className = '', ...props }:
  React.AnchorHTMLAttributes<HTMLAnchorElement> & { tone?: ButtonTone; size?: ButtonSize }) => (
  <a {...props} className={buttonSkin(tone, size, `no-underline ${className}`)} />
);

/** 横排档位。三四个选项摊开比点开下拉更快，边框合并成一条。 */
export const Segmented = <T extends string>({ choices, onPick, value }: {
  choices: ReadonlyArray<readonly [T, string]>;
  onPick: (value: T) => void;
  value: T;
}) => (
  <div className="flex flex-none border border-[var(--duties-border)]" role="group">
    {choices.map(([option, text]) => (
      <button
        aria-pressed={option === value}
        className={`whitespace-nowrap border-l border-[var(--duties-border)] px-2.5 py-0.5 font-mono text-[0.65rem] first:border-l-0 ${
          option === value
            ? 'bg-[var(--duties-text)] text-[var(--duties-bg)]'
            : `bg-[var(--duties-panel)] ${MUTED} hover:bg-[var(--duties-muted)] hover:text-[var(--duties-text)]`
        }`}
        key={option}
        onClick={() => onPick(option)}
        type="button"
      >
        {text}
      </button>
    ))}
  </div>
);

/** 会中断正在进行的工作的操作。红边把它和同页的普通设置隔开。 */
export const DangerPanel = ({ children }: { children: ReactNode }) => (
  <div className="flex flex-wrap items-center justify-between gap-3 border border-[var(--duties-danger)] bg-[var(--duties-panel)] px-4 py-3.5">
    {children}
  </div>
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

/** 列表底部的翻页条。装不满一页时整条不出现，省得留一排永远点不动的按钮。 */
export const Pager = ({ offset, onOffset, pageSize, total, unit }: {
  offset: number;
  onOffset: (next: number) => void;
  pageSize: number;
  total: number;
  /** 量词，跟在数字后面：条 / 张 / 个。 */
  unit: string;
}) => {
  if (total <= pageSize) return null;
  const to = Math.min(offset + pageSize, total);

  return (
    <div className="mt-3 flex items-center gap-2">
      <span className="text-[0.65rem] text-[var(--duties-tertiary)]">
        第 {offset + 1}–{to} {unit}，共 {total} {unit}
      </span>
      <span className="flex-1" />
      <Button disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - pageSize))} tone="quiet">
        上一页
      </Button>
      <Button disabled={to >= total} onClick={() => onOffset(offset + pageSize)} tone="quiet">
        下一页
      </Button>
    </div>
  );
};

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

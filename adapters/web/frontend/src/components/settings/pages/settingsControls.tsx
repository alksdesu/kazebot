// 设置页与控制台共用的一套组件：页壳、区块、面板、表单控件、按钮与状态条。
import { forwardRef, type ReactNode } from 'react';

const MUTED = 'text-[var(--duties-secondary)]';

// 一条规则管到哪些渠道。写错过一次：QQ 的权限被当成浏览器设置，没人敢动。
export type SettingScope = 'all-channels' | 'this-browser';

const SCOPE_TEXT: Record<SettingScope, string> = {
  'all-channels': '所有渠道',
  'this-browser': '仅此浏览器',
};

export const ScopeBadge = ({ scope }: { scope: SettingScope }) => (
  <span
    className={`flex-shrink-0 rounded border px-2 py-0.5 text-xs ${
      scope === 'all-channels'
        ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
        : 'border-[var(--duties-border)] text-[var(--duties-secondary)]'
    }`}
  >
    {SCOPE_TEXT[scope]}
  </span>
);

export type BlockFrame = 'ruled' | 'boxed';

/** 区块：一段带标题的设置。ruled 标题下压一条线，boxed 整段套框，选哪种看它要不要从页面里凸出来。 */
export const Block = ({ children, frame = 'ruled', hint, id, scope, title }: {
  children: ReactNode;
  frame?: BlockFrame;
  hint?: string;
  /** 深链锚点。别处链过来要能直接滚到这一块。 */
  id?: string;
  scope?: SettingScope;
  title?: string;
}) => (
  frame === 'boxed' ? (
    <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4" id={id}>
      {(title || hint) && (
        <div className="mb-3">
          {title && (
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold">{title}</h2>
              {scope && <ScopeBadge scope={scope} />}
            </div>
          )}
          {hint && <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">{hint}</p>}
        </div>
      )}
      {children}
    </section>
  ) : (
    <section className="mb-8" id={id}>
      <div className="mb-4 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 border-b border-[var(--duties-border)] pb-2">
        {title && <h2 className="m-0 text-base font-semibold">{title}</h2>}
        {hint && <span className={`text-xs ${MUTED}`}>{hint}</span>}
        {scope && <ScopeBadge scope={scope} />}
      </div>
      {children}
    </section>
  )
);

/** 开关格。1px 间隙靠容器底色透出来当分隔线，省掉每格各画一条边。 */
export const Grid = ({ children }: { children: ReactNode }) => (
  <div className="settings-control-grid grid min-w-0 gap-px overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-border)]">
    {children}
  </div>
);

export const Empty = ({ children }: { children: ReactNode }) => (
  <p className={`px-4 py-6 text-center text-sm ${MUTED}`}>{children}</p>
);

/** Grid 之外的独立面板，用于整块只有一份名单、一组只读行的分区。 */
export const Panel = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <div className={`border border-[var(--duties-border)] bg-[var(--duties-surface)] px-4 py-3.5 ${className}`}>
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
    className={`flex min-w-0 flex-wrap items-center gap-2.5 border-b border-[var(--duties-border)] bg-[var(--duties-surface)] px-4 py-3 last:border-b-0 ${className}`}
  >
    {children}
  </li>
);

/** 列表项的名字。是 h3 不是 span：一屏几十项，读屏靠标题跳转才找得到目标那一条。 */
export const ItemTitle = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <h3 className={`m-0 text-sm font-medium ${className}`}>{children}</h3>
);

/** 内容多于一行的列表项，与 Row 的横向布局区分。 */
export const Item = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <li
    className={`border-b border-[var(--duties-border)] bg-[var(--duties-surface)] px-4 py-2.5 last:border-b-0 ${className}`}
  >
    {children}
  </li>
);

/** role 给页面开着时才冒出来的错误用：读屏不会自己去看新长出来的一行字。 */
export const ErrorText = ({ children, role }: { children: ReactNode; role?: 'alert' }) => (
  <p className="mt-2.5 text-xs leading-5 text-[var(--duties-danger)]" role={role}>{children}</p>
);

export const Footnote = ({ children }: { children: ReactNode }) => (
  <p className={`mt-3 max-w-[var(--duties-prose-max-width)] text-xs leading-5 ${MUTED}`}>{children}</p>
);

export const StatusText = ({ message }: { message: string }) => (
  message ? <p role="status" aria-live="polite" className="mt-2 text-sm leading-6 text-[var(--duties-secondary)]">{message}</p> : null
);

/** role 同 ErrorText：操作结果这类中途出现的交代要让读屏念出来。 */
export const Facts = ({ children, className = '', role }: { children: ReactNode; className?: string; role?: 'status' }) => (
  <p
    aria-live={role === 'status' ? 'polite' : undefined}
    className={`mt-2.5 text-xs leading-5 ${MUTED} ${className}`}
    role={role}
  >
    {children}
  </p>
);

/** 开关名底下那行说明。缩进对齐复选框右侧，与 Option 的 desc 同一档。 */
export const Desc = ({ children, indent = true }: { children: ReactNode; indent?: boolean }) => (
  <p className={`mt-1.5 text-xs leading-5 ${indent ? 'ml-[var(--duties-option-indent)]' : ''} ${MUTED}`}>{children}</p>
);

/** Grid 之外的松散区块，放不属于任何单个开关的配置。 */
export const Loose = ({ children }: { children: ReactNode }) => (
  <div className="mt-4">{children}</div>
);

const CHECKBOX = 'h-[var(--duties-checkbox-size)] w-[var(--duties-checkbox-size)] flex-none accent-[var(--duties-text)]';

interface OptionProps {
  /** 开关名右边的东西，比如来源切换。 */
  aside?: ReactNode;
  checked: boolean;
  children?: ReactNode;
  desc?: ReactNode;
  name: string;
  onChange: (checked: boolean) => void;
}

// children 和 aside 刻意留在 label 外面：词表、数字框、来源切换包进 label 之后，
// 点它们或旁边的空白会连带翻转开关。
export const Option = ({ aside, checked, children, desc, name, onChange }: OptionProps) => (
  <div className="bg-[var(--duties-surface)] px-4 py-3.5">
    <div className="flex items-start gap-2.5">
      <label className="block min-w-0 flex-1 cursor-pointer text-sm">
        <span className="flex min-h-[var(--duties-control-size)] items-center gap-2.5">
          <input
            checked={checked}
            className={CHECKBOX}
            onChange={(event) => onChange(event.target.checked)}
            type="checkbox"
          />
          <span className="font-medium">{name}</span>
        </span>
        {desc && <p className={`ml-[var(--duties-option-indent)] mt-1.5 text-xs leading-5 ${MUTED}`}>{desc}</p>}
      </label>
      {aside && <span className="flex min-h-[var(--duties-control-size)] flex-none items-center">{aside}</span>}
    </div>
    {children}
  </div>
);

/** 没有开关、只能调参数的一组配置。
 *  别拿勾不动的复选框冒充：整块会淡成灰的，看着不可用，其实里面的输入框改得动。 */
export const Fixed = ({ children, desc, name }: {
  children?: ReactNode;
  desc?: ReactNode;
  name: string;
}) => (
  <div className="bg-[var(--duties-surface)] px-4 py-3.5">
    <span className="flex min-h-[var(--duties-control-size)] items-center gap-2.5">
      {/* 占住复选框的位置，好和同一行里带开关的卡片对齐。 */}
      <span aria-hidden className="h-[var(--duties-checkbox-size)] w-[var(--duties-checkbox-size)] flex-none" />
      <span className="font-medium">{name}</span>
      <span className="flex-none border border-[var(--duties-border)] px-1.5 py-0.5 text-xs text-[var(--duties-secondary)]">
        常开
      </span>
    </span>
    {desc && <p className={`ml-[var(--duties-option-indent)] mt-1.5 text-xs leading-5 ${MUTED}`}>{desc}</p>}
    {children}
  </div>
);

/** 附属于某个开关的次级选项。层级靠缩进表达。 */
export const Sub = ({ aside, checked, label, onChange }: {
  /** 跟在选项后面的东西。放在 label 外面，点它不会连带勾选。 */
  aside?: ReactNode;
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) => (
  <div className="ml-[var(--duties-option-indent)] mt-2.5 flex min-h-[var(--duties-control-size)] flex-wrap items-center gap-2.5">
    <label className={`flex cursor-pointer items-center gap-2.5 text-sm ${MUTED}`}>
      <input
        checked={checked}
        className={CHECKBOX}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
      <span>{label}</span>
    </label>
    {aside}
  </div>
);

type FieldProps = {
  children?: ReactNode;
  htmlFor?: string;
  label: ReactNode;
} & (
  // aside 只有 inline 一行里放得下：跟在控件后面，比如来源切换。
  | { aside?: ReactNode; layout?: 'inline' }
  | { aside?: never; layout: 'stacked' }
);

/** 带标签的字段。inline 是跟在开关下面的一行，stacked 是标签压在控件上方的表单式。 */
export const Field = ({ aside, children, htmlFor, label, layout = 'inline' }: FieldProps) => (
  layout === 'stacked' ? (
    <>
      <label className="mb-1 block text-sm font-medium text-[var(--duties-secondary)]" htmlFor={htmlFor}>{label}</label>
      {children}
    </>
  ) : (
    <span className="ml-[var(--duties-option-indent)] mt-2.5 flex min-w-0 flex-wrap items-center gap-2.5 text-sm">
      <label className={MUTED} htmlFor={htmlFor}>{label}</label>
      {children}
      {aside}
    </span>
  )
);

// 变体走 prop 不走 className 追加：align-items 与 font-size 都是同属性覆盖，
// 谁赢取决于 Tailwind 生成 CSS 的顺序，不由 class 属性里的先后决定。
const CHECK_ALIGN = { center: 'items-center', start: 'items-start' } as const;
const CHECK_TONE = { default: 'text-sm', muted: 'text-sm leading-5 ' + MUTED } as const;

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
    className={`flex min-h-[var(--duties-control-size)] gap-2.5 ${CHECK_ALIGN[align]} ${CHECK_TONE[tone]} ${disabled ? '' : 'cursor-pointer'}`}
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
  default: 'w-[var(--duties-input-width)]',
  wide: 'w-full',
  tiny: 'w-[var(--duties-input-tiny-width)]',
  flex: 'min-w-0 flex-1',
  alias: 'w-[var(--duties-input-alias-width)]',
};

const CONTROL_SKIN =
  'app-input min-w-0 max-w-full disabled:bg-[var(--duties-muted)] disabled:text-[var(--duties-secondary)]';

// 转发 ref：上传完要清空文件选择框，那只能拿到 DOM 节点自己改 value。
export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement> & { width?: InputWidth }>(
  ({ width = 'default', className = '', ...props }, ref) => (
    <input {...props} className={`${CONTROL_SKIN} ${INPUT_WIDTH[width]} ${className}`} ref={ref} />
  ),
);
Input.displayName = 'Input';

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement> & { width?: InputWidth }>(
  ({ width = 'default', className = '', ...props }, ref) => (
    <select {...props} className={`${CONTROL_SKIN} ${INPUT_WIDTH[width]} ${className}`} ref={ref} />
  ),
);
Select.displayName = 'Select';

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className = '', ...props }, ref) => (
    <textarea {...props} className={`block w-full resize-y leading-6 ${CONTROL_SKIN} ${className}`} ref={ref} />
  ),
);
Textarea.displayName = 'Textarea';

/** 日志、进度这类只读长文本。往下追加的内容要能滚，且不能撑破面板。 */
export const Pre = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <pre
    className={`overflow-auto whitespace-pre-wrap break-all border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 ${className}`}
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
    className={`flex-none border px-1.5 py-0.5 font-mono text-xs tracking-ident ${TAG_TONE[tone]} ${className}`}
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
        className={`app-icon-button leading-tight ${MUTED} hover:text-[var(--duties-danger)]`}
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
    <span className={`min-w-[var(--duties-label-min-width)] text-sm ${MUTED}`}>{name}</span>
    <code className="break-all text-xs">{value || '未设置'}</code>
    {hint && <span className={`text-xs ${MUTED}`}>{hint}</span>}
  </div>
);

export type ButtonTone = 'solid' | 'quiet' | 'halt' | 'danger';
export type ButtonSize = 'default' | 'sm';

// 一个保存范围只给一个 solid；丢弃、取消用 quiet；删除、清空、重启用 danger 并二次确认；
// halt 只留给「停掉正在进行的事」。强调色不做按钮色，它只表示判定为真或已生效。
const TONE: Record<ButtonTone, string> = {
  solid: 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]',
  quiet: 'border-[var(--duties-border)] bg-transparent text-[var(--duties-text)] hover:border-[var(--duties-text)]',
  halt: 'border-[var(--duties-danger)] bg-[var(--duties-danger)] text-[var(--duties-bg)]',
  danger: 'border-[var(--duties-danger-border)] bg-[var(--duties-danger-bg)] text-[var(--duties-danger)] hover:bg-[var(--duties-danger-hover)]',
};

// 小号只收窄横向留白，保留相同的可点击高度。
const SIZE: Record<ButtonSize, string> = {
  default: 'px-3.5 py-2',
  sm: 'px-2.5 py-1.5',
};

const buttonSkin = (tone: ButtonTone, size: ButtonSize, className: string) =>
  `app-button inline-flex min-h-[var(--duties-control-size)] items-center justify-center gap-2 border text-sm font-medium ${SIZE[size]} ${TONE[tone]} ${className}`;

export const Button = ({
  children, className = '', disabled, loading = false, size = 'default', tone = 'solid', ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  /** 请求在飞时转圈并自锁。文字不换，换了会让人以为点错了按钮。 */
  loading?: boolean;
  size?: ButtonSize;
  tone?: ButtonTone;
}) => (
  <button
    {...props}
    aria-busy={loading || undefined}
    className={buttonSkin(tone, size, `disabled:cursor-not-allowed disabled:opacity-40 ${className}`)}
    disabled={disabled || loading}
    type={props.type || 'button'}
  >
    {loading && <span aria-hidden="true" className="app-button-spinner" />}
    {children}
  </button>
);

/** 长得像按钮的链接。跨实例跳转要能中键开新页，那是 <a> 才有的行为。 */
export const LinkButton = ({ tone = 'quiet', size = 'default', className = '', ...props }:
  React.AnchorHTMLAttributes<HTMLAnchorElement> & { tone?: ButtonTone; size?: ButtonSize }) => (
  <a {...props} className={buttonSkin(tone, size, `no-underline ${className}`)} />
);

/** 横排档位。三四个选项摊开比点开下拉更快，边框合并成一条。 */
export type SegmentedSize = 'regular' | 'compact';

const SEGMENT_SIZE: Record<SegmentedSize, string> = {
  regular: 'settings-segment px-3 py-2 text-sm',
  compact: 'settings-segment-compact px-2 py-0.5 text-xs',
};

export const Segmented = <T extends string>({ choices, onPick, value, label, disabled = false, size = 'regular' }: {
  choices: ReadonlyArray<readonly [T, string]>;
  onPick: (value: T) => void;
  value: T;
  label?: string;
  disabled?: boolean;
  /** compact 给挂在字段标题旁的小切换用，和控件本身的分段按钮一眼分得开。 */
  size?: SegmentedSize;
}) => (
  <div className="flex min-w-0 max-w-full flex-wrap border border-[var(--duties-border)]" role="group" aria-label={label}>
    {choices.map(([option, text]) => (
      <button
        disabled={disabled}
        aria-pressed={option === value}
        className={`app-button border-l border-[var(--duties-border)] ${SEGMENT_SIZE[size]} first:border-l-0 ${
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
  <div className="flex flex-wrap items-center justify-between gap-3 border border-[var(--duties-danger)] bg-[var(--duties-surface)] px-4 py-3.5">
    {children}
  </div>
);

/** 区块自己保存时用：写的是节点文件或 runtime.yaml，改完立即生效，不走顶部那条 qq.yaml 状态条。 */
export const SaveBar = ({ busy, dirty, label = '保存', note, onReset, onSave }: {
  busy: boolean;
  dirty: boolean;
  // 一页上有两块各自保存时，两个都叫「保存」就分不清写的是哪一块。
  label?: string;
  note: string;
  onReset: () => void;
  onSave: () => void;
}) => (
  <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-[var(--duties-border)] pt-3">
    {/* 保存的结果写在这一句里，读屏要在它变的时候念出来。 */}
    <span aria-live="polite" className={`text-xs ${MUTED}`} role="status">{note}</span>
    <span className="flex-1" />
    <Button disabled={!dirty || busy} onClick={onReset} tone="quiet">丢弃</Button>
    <Button disabled={!dirty || busy} onClick={onSave}>{busy ? '正在保存…' : label}</Button>
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
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <span className="text-xs text-[var(--duties-tertiary)]">
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

/** 页顶状态条。status 是会变的那句状态，变了读屏会念；children 是靠右的按钮，不进播报。 */
export const StatusBar = ({ children, status, tone }: { children?: ReactNode; status: ReactNode; tone: PipTone }) => (
  <div className="flex min-h-[var(--duties-control-size)] flex-wrap items-center gap-2 border-b border-[var(--duties-border)] px-4 py-2 text-sm">
    <Pip tone={tone} />
    <span aria-live="polite" className="flex min-w-0 flex-wrap items-center gap-2" role="status">{status}</span>
    <span className="flex-1" />
    {children}
  </div>
);

export const PageHeader = ({ eyebrow, title, description, level = 1 }: {
  description: string;
  eyebrow?: string;
  level?: 1 | 2;
  title: string;
}) => {
  const Heading = level === 1 ? 'h1' : 'h2';
  return (
    <header aria-label={eyebrow ? `${eyebrow}：${title}` : undefined}>
      <Heading className={`${level === 1 ? 'text-2xl' : 'text-xl'} font-semibold tracking-tight`}>{title}</Heading>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">{description}</p>
    </header>
  );
};

interface PageShellProps {
  /** 未登录时正文整块不渲染，只留登录指引 —— 这些页读的都是要鉴权的接口。 */
  authed?: boolean;
  /** 状态栏不进滚动区：改完页尾的开关要能直接点应用，滚回顶部才找得到等于没有。 */
  bar?: ReactNode;
  children: ReactNode;
  description: string;
  error?: string;
  eyebrow?: string;
  notice?: string;
  title: string;
}

/** 设置页统一的壳：标题、说明、登录提示、错误区，平台页在顶上再挂一条状态栏。 */
export const PageShell = ({
  authed = true, bar, children, description, error, eyebrow, notice, title,
}: PageShellProps) => (
  <section className="flex h-full min-h-0 flex-col">
    {bar}
    <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-5 text-sm">
        <PageHeader description={description} eyebrow={eyebrow} title={title} />
        {authed ? (
          <>
            {error && <ErrorText role="alert">{error}</ErrorText>}
            {notice && <Facts role="status">{notice}</Facts>}
            {children}
          </>
        ) : (
          <Block frame="boxed">
            <p className="text-sm leading-6 text-[var(--duties-secondary)]">请先在通用页面登录 Admin Token</p>
          </Block>
        )}
      </div>
    </div>
  </section>
);

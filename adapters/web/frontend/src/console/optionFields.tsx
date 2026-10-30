// provider 参数的控件。清单由后端按 provider 类公布，这里只负责把一条 spec 变成一行界面。
// 全局、节点和备选链都按值读写：界面字符串和 yaml / JSON 里的值靠下面这组转换对齐。
import type { ProviderOptionSpec } from '../api/supervisorClient';
import { Input, Segmented } from './components';

/** JSON 值 → 界面字符串。 */
export function shownFromValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

/** 界面字符串 → JSON 值。null 表示删掉这一项。 */
export function valueFromShown(spec: ProviderOptionSpec, shown: string): unknown {
  const text = shown.trim();
  if (!text) return null;
  if (spec.kind === 'bool') return text === 'true';
  if (spec.kind === 'int' || spec.kind === 'float') {
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return text;
}

export function clampNumber(spec: ProviderOptionSpec, shown: string): string {
  const text = shown.trim();
  if (!text) return '';
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) return '';
  let value = parsed;
  if (spec.minimum !== undefined) value = Math.max(value, spec.minimum);
  if (spec.maximum !== undefined) value = Math.min(value, spec.maximum);
  return String(spec.kind === 'int' ? Math.round(value) : value);
}

/** 依赖项没取到指定值时这一项没有意义，不摆出来。 */
export function visibleSpecs(
  specs: ProviderOptionSpec[],
  shownOf: (key: string) => string,
): ProviderOptionSpec[] {
  return specs.filter((spec) => {
    if (!spec.depends_on) return true;
    const parent = specs.find((item) => item.key === spec.depends_on!.key);
    const current = shownOf(spec.depends_on.key);
    // 依赖项没配时看它的默认值，否则「思考预算」这类子项会在默认就该显示时被藏起来。
    const fallback = parent?.default === undefined || parent?.default === null
      ? '' : shownFromValue(parent.default);
    return (current || fallback) === shownFromValue(spec.depends_on.value);
  });
}

export const placeholderOf = (spec: ProviderOptionSpec): string => (
  spec.default === undefined || spec.default === null ? '不指定' : shownFromValue(spec.default)
);

const BOOL_CHOICES: ReadonlyArray<readonly [string, string]> = [
  ['', '不指定'],
  ['true', '开'],
  ['false', '关'],
];

const BoolControl = ({ value, onChange, label }: { value: string; onChange: (next: string) => void; label: string }) => (
  <Segmented label={label} choices={BOOL_CHOICES} onPick={onChange} value={value} />
);

const EnumControl = ({
  spec, value, onChange,
}: { spec: ProviderOptionSpec; value: string; onChange: (next: string) => void }) => {
  const choices: ReadonlyArray<readonly [string, string]> = (spec.choices || []).map(
    (choice) => [choice.value, choice.label] as const,
  );
  return (
    <Segmented
      label={spec.label}
      choices={choices}
      onPick={(picked) => onChange(value === picked ? '' : picked)}
      value={value}
    />
  );
};

export const OptionRow = ({
  spec, value, onChange, inherited = false,
}: {
  spec: ProviderOptionSpec;
  value: string;
  onChange: (next: string) => void;
  /** 这个值是默认那层的，本实例没写过。 */
  inherited?: boolean;
}) => (
  <div className="min-w-0 space-y-1">
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="font-medium">{spec.label}</span>
      <code className="border border-[var(--duties-live)] px-1.5 text-mini text-[var(--duties-live)]">
        {spec.key}
      </code>
      {inherited && <span className="text-mini text-[var(--duties-tertiary)]">跟随默认</span>}
      <span className="flex-1" />
      {spec.kind === 'bool' && <BoolControl label={spec.label} onChange={onChange} value={value} />}
      {spec.kind === 'enum' && <EnumControl onChange={onChange} spec={spec} value={value} />}
      {(spec.kind === 'int' || spec.kind === 'float') && (
        <Input
          aria-label={spec.label}
          inputMode="decimal"
          onBlur={(event) => onChange(clampNumber(spec, event.target.value))}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholderOf(spec)}
          value={value}
        />
      )}
      {spec.kind === 'text' && (
        <Input
          aria-label={spec.label}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholderOf(spec)}
          value={value}
          width="wide"
        />
      )}
    </div>
    {spec.desc && <p className="mt-1 text-xs text-[var(--duties-secondary)]">{spec.desc}</p>}
  </div>
);

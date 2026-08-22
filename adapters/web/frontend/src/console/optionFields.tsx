// provider 参数的控件。清单由后端按 provider 类公布，这里只负责把一条 spec 变成一行界面。
// 两个作用域共用：全局与节点存的是 yaml 标量文本，备选链存的是 JSON 值，靠下面两组转换对齐。
import type { ProviderOptionSpec } from '../api/supervisorClient';
import { Input, Segmented } from './components';

/** 界面字符串 → yaml 标量写法。空串表示删掉这一行。 */
export function toYamlScalar(spec: ProviderOptionSpec, shown: string): string {
  const text = shown.trim();
  if (!text) return '';
  if (spec.kind === 'text') return JSON.stringify(text);
  return text;
}

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

const BoolControl = ({ value, onChange }: { value: string; onChange: (next: string) => void }) => (
  <Segmented choices={BOOL_CHOICES} onPick={onChange} value={value} />
);

const EnumControl = ({
  spec, value, onChange,
}: { spec: ProviderOptionSpec; value: string; onChange: (next: string) => void }) => {
  const choices: ReadonlyArray<readonly [string, string]> = (spec.choices || []).map(
    (choice) => [choice.value, choice.label] as const,
  );
  return (
    <Segmented
      choices={choices}
      onPick={(picked) => onChange(value === picked ? '' : picked)}
      value={value}
    />
  );
};

export const OptionRow = ({
  spec, value, onChange,
}: {
  spec: ProviderOptionSpec;
  value: string;
  onChange: (next: string) => void;
}) => (
  <div className="flex items-center gap-2.5">
    <div className="flex items-center gap-2">
      <span className="font-medium">{spec.label}</span>
      <code className="border border-[var(--duties-live)] px-1.5 text-[0.65rem] text-[var(--duties-live)]">
        {spec.key}
      </code>
      <span className="flex-1" />
      {spec.kind === 'bool' && <BoolControl onChange={onChange} value={value} />}
      {spec.kind === 'enum' && <EnumControl onChange={onChange} spec={spec} value={value} />}
      {(spec.kind === 'int' || spec.kind === 'float') && (
        <Input
          inputMode="decimal"
          onBlur={(event) => onChange(clampNumber(spec, event.target.value))}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholderOf(spec)}
          value={value}
        />
      )}
      {spec.kind === 'text' && (
        <Input
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

// 绑定到某个热载键的表单件。各页共用，避免同一个键在两处有两套读写规则。
import type { ReactNode } from 'react';

import { Field, Option, Sub } from './components';
import { useConsoleStore, useLiveValue, useNote, useTristate } from './consoleStore';

interface BoolOptionProps {
  children?: ReactNode;
  configKey: string;
  desc?: string;
  fallback?: boolean;
  label: string;
}

export const BoolOption = ({ children, configKey, desc, fallback = false, label }: BoolOptionProps) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const checked = useLiveValue<boolean>(configKey, fallback);
  const note = useNote(configKey);
  return (
    <Option
      checked={checked}
      desc={[desc, note].filter(Boolean).join(' ')}
      name={label}
      onChange={(next) => setDraft(configKey, next)}
    >
      {children}
    </Option>
  );
};

/** 三态开关：null = 没配，由 bot 按旧的 group_mode 枚举推导。界面显示当前推导值。 */
export const TristateOption = ({
  configKey,
  desc,
  fallback,
  label,
}: {
  configKey: string;
  desc: string;
  fallback: boolean;
  label: string;
}) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const value = useTristate(configKey);
  const effective = value === null ? fallback : value;
  return (
    <Option
      checked={effective}
      desc={value === null ? `${desc}（当前跟随旧的触发模式）` : desc}
      name={label}
      onChange={(checked) => setDraft(configKey, checked)}
    />
  );
};

export const SubOption = ({
  configKey,
  fallback = false,
  label,
}: {
  configKey: string;
  fallback?: boolean;
  label: string;
}) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const checked = useLiveValue<boolean>(configKey, fallback);
  return <Sub checked={checked} label={label} onChange={(next) => setDraft(configKey, next)} />;
};

export const NumberField = ({
  configKey,
  label,
  step = 1,
  unit,
  scale = 1,
}: {
  configKey: string;
  label: string;
  step?: number;
  unit?: string;
  // 存的是字节、给人看的是 MB 这类换算。写回按 scale 还原成整数，界面上不出现 10485760。
  scale?: number;
}) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const stored = useLiveValue<number>(configKey, 0);
  return (
    <Field label={label}>
      <input
        className="qc-inp"
        onChange={(event) => {
          const next = Number.parseFloat(event.target.value);
          const shown = Number.isFinite(next) ? next : 0;
          setDraft(configKey, scale === 1 ? shown : Math.round(shown * scale));
        }}
        step={step}
        type="number"
        value={String(scale === 1 ? stored : stored / scale)}
      />
      {unit && <label>{unit}</label>}
    </Field>
  );
};

export const TextField = ({ configKey, label }: { configKey: string; label: string }) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const value = useLiveValue<string>(configKey, '');
  return (
    <Field label={label}>
      <input
        className="qc-inp qc-inp-wide"
        onChange={(event) => setDraft(configKey, event.target.value)}
        value={value}
      />
    </Field>
  );
};

/** 概率键存的是 0–1，界面按百分比给 —— 「0.02」要在脑子里换算，「2%」不用。 */
export const PercentField = ({ configKey, label }: { configKey: string; label: string }) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const value = useLiveValue<number>(configKey, 0);
  return (
    <Field label={label}>
      <input
        className="qc-inp"
        max={100}
        min={0}
        onChange={(event) => {
          const typed = Number.parseFloat(event.target.value);
          const ratio = Number.isFinite(typed) ? Math.min(100, Math.max(0, typed)) / 100 : 0;
          setDraft(configKey, ratio);
        }}
        step={0.5}
        type="number"
        // 0.15 * 100 会算出 15.000000000000002，定到小数点后两位再显示。
        value={String(Math.round(value * 10000) / 100)}
      />
      <label>%</label>
    </Field>
  );
};

/** 只读展示：改动需要重启的那几个键。 */
export const ReadOnlyRow = ({ hint, name, value }: { hint?: string; name: string; value: string }) => (
  <div className="qc-ro">
    <span className="qc-ro-name">{name}</span>
    <code className="qc-ro-value">{value || '未设置'}</code>
    {hint && <span className="qc-ro-hint">{hint}</span>}
  </div>
);

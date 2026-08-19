// 控制台外壳的基础件。样式全在 console.css，这里只管结构与语义。
import type { ReactNode } from 'react';

import { Icon } from '../components/common';
import { CONSOLE_DOMAINS, DOMAIN_ICONS, DOMAIN_LABELS, type ConsoleDomain } from './consoleStore';

interface RailProps {
  domain: ConsoleDomain;
  onSelect: (domain: ConsoleDomain) => void;
  ready: ReadonlySet<ConsoleDomain>;
  onBack: () => void;
  onExit: () => void;
}

export const Rail = ({ domain, onSelect, ready, onBack, onExit }: RailProps) => (
  <nav aria-label="控制台" className="qc-rail">
    <div className="qc-rail-head">
      <button className="qc-rail-back" onClick={onBack} type="button">
        <Icon name="arrow_back" size={15} />
        <span>返回设置</span>
      </button>
      <div className="qc-rail-brand">
        <Icon name="dashboard" size={18} />
        <span>
          <strong>QQ 控制台</strong>
          <small>bot 配置</small>
        </span>
      </div>
    </div>

    <div className="qc-rail-group">
      {CONSOLE_DOMAINS.map((item) => (
        <button
          aria-current={item === domain ? 'page' : undefined}
          className="qc-rail-item"
          disabled={!ready.has(item)}
          key={item}
          onClick={() => onSelect(item)}
          title={ready.has(item) ? undefined : '这一页还没做'}
          type="button"
        >
          <Icon className="qc-rail-icon" name={DOMAIN_ICONS[item]} size={16} />
          <span>{DOMAIN_LABELS[item]}</span>
        </button>
      ))}
    </div>

    <div className="qc-rail-foot">
      <button className="qc-rail-item" onClick={onExit} type="button">
        <Icon className="qc-rail-icon" name="keyboard_return" size={16} />
        <span>回到对话</span>
      </button>
    </div>
  </nav>
);

interface StatusBarProps {
  live: { published: boolean; stale?: boolean; applied?: boolean; reason?: string } | null;
  parseError: string;
  pending: number;
  saving: boolean;
  onApply: () => void;
  onDiscard: () => void;
}

/** pip 反映 bot 那边的生效状态，「N 项待应用」反映浏览器里还没提交的改动 —— 两个不同维度。 */
export const StatusBar = ({ live, parseError, pending, saving, onApply, onDiscard }: StatusBarProps) => {
  let pip = 'qc-pip qc-pip-idle';
  let state = '等待 bot 上报';
  if (parseError) {
    pip = 'qc-pip qc-pip-halt';
    state = '配置有语法错误，bot 仍在用上一份';
  } else if (live?.published && live.stale) {
    pip = 'qc-pip qc-pip-halt';
    state = 'bot 心跳已中断';
  } else if (live?.published && live.applied) {
    pip = 'qc-pip';
    state = '已生效';
  } else if (live?.published) {
    pip = 'qc-pip qc-pip-halt';
    state = 'bot 用的还是旧配置';
  }

  return (
    <div className="qc-bar">
      <span aria-hidden="true" className={pip} />
      <span className="qc-bar-state">{state}</span>
      {pending > 0 && (
        <>
          <span className="qc-bar-sep">·</span>
          <span className="qc-bar-pending">{pending} 项待应用</span>
        </>
      )}
      <span className="qc-bar-spacer" />
      <button
        className="qc-btn qc-btn-quiet"
        disabled={pending === 0 || saving}
        onClick={onDiscard}
        type="button"
      >
        丢弃
      </button>
      <button className="qc-btn" disabled={pending === 0 || saving} onClick={onApply} type="button">
        {saving ? '正在应用…' : '应用'}
      </button>
    </div>
  );
};

export const Page = ({ children, note, title }: { children: ReactNode; note?: string; title: string }) => (
  <div className="qc-page">
    <h1 className="qc-page-head">{title}</h1>
    {note && <p className="qc-page-note">{note}</p>}
    {children}
  </div>
);

export const Block = ({ children, hint, title }: { children: ReactNode; hint?: string; title: string }) => (
  <section className="qc-block">
    <div className="qc-block-head">
      <span className="qc-block-title">{title}</span>
      {hint && <span className="qc-block-hint">{hint}</span>}
    </div>
    {children}
  </section>
);

export const Grid = ({ children }: { children: ReactNode }) => <div className="qc-grid">{children}</div>;

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
  <div className={`qc-opt${disabled ? ' qc-opt-locked' : ''}`}>
    <label className="qc-opt-hit">
      <span className="qc-opt-row">
        <input
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          type="checkbox"
        />
        <span aria-hidden="true" className="qc-box" />
        <span className="qc-opt-name">{name}</span>
      </span>
      {desc && <p className="qc-opt-desc">{desc}</p>}
    </label>
    {children}
  </div>
);

export const Field = ({ children, label }: { children: ReactNode; label: string }) => (
  <span className="qc-field">
    <label>{label}</label>
    {children}
  </span>
);

/** 附属于某个开关的次级选项。复选框沿用 .qc-box 的样式，层级靠缩进表达。 */
export const Sub = ({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) => (
  <label className="qc-sub">
    <input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
    <span aria-hidden="true" className="qc-box" />
    <span>{label}</span>
  </label>
);

export const Empty = ({ children }: { children: ReactNode }) => <p className="qc-empty">{children}</p>;

/** 自管存盘的页面用：这些页面写的是节点文件或 runtime.yaml，不走顶部那条 qq.yaml 状态条。 */
export const SaveBar = ({
  busy,
  dirty,
  label = '保存',
  note,
  onReset,
  onSave,
}: {
  busy: boolean;
  dirty: boolean;
  // 一页上有两块各自存盘时，两个都叫「保存」就分不清存的是哪一块。
  label?: string;
  note: string;
  onReset: () => void;
  onSave: () => void;
}) => (
  <div className="qc-savebar">
    <span className="qc-facts">{note}</span>
    <span className="qc-bar-spacer" />
    <button className="qc-btn qc-btn-quiet" disabled={!dirty || busy} onClick={onReset} type="button">
      还原
    </button>
    <button className="qc-btn" disabled={!dirty || busy} onClick={onSave} type="button">
      {busy ? '保存中…' : label}
    </button>
  </div>
);

export const Footnote = ({ children }: { children: ReactNode }) => (
  <p className="qc-footnote">{children}</p>
);

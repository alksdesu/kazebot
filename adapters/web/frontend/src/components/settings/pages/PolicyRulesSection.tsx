// 服务端策略：决定每条路径要不要审批。QQ、控制台、定时任务共用这一份。
import { useEffect, useState } from 'react';

import {
  getPolicy,
  updatePolicy,
  type PolicyDecision,
  type PolicyDoc,
  type PolicyRule,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { ScopeBadge } from './settingsPagePrimitives';

type RuleSectionKey = 'read_file' | 'write_file';

interface RuleSection {
  default: PolicyDecision;
  rules: PolicyRule[];
}

const DECISIONS: Array<{ value: PolicyDecision; label: string }> = [
  { value: 'auto', label: '自动放行' },
  { value: 'approval_required', label: '需审批' },
  { value: 'deny', label: '拒绝' },
];

const SECTION_META: Record<RuleSectionKey, { title: string; hint: string }> = {
  read_file: { title: '读取', hint: 'read_file / list_dir / search_in_files 共用这一档。' },
  write_file: { title: '写入', hint: 'write_file / apply_diff 共用这一档。' },
};

const EMPTY_SECTION: RuleSection = { default: 'auto', rules: [] };

const INPUT_CLASS =
  'min-w-0 flex-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-[0.7rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const GHOST_BUTTON_CLASS =
  'border border-[var(--duties-border)] px-2 py-1 font-mono text-[0.65rem] text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)] disabled:opacity-40';
// 理由以前只藏在 hover 的 title 里，改了档位没人想得起来同步，留下一堆自相矛盾的规则。
const REASON_CLASS =
  'mt-1 w-full bg-transparent font-mono text-[0.6rem] text-[var(--duties-tertiary)] outline-none focus:text-[var(--duties-text)]';

const DecisionSelect = ({
  value, onChange, label,
}: { value: PolicyDecision; onChange: (next: PolicyDecision) => void; label: string }) => (
  <select
    aria-label={label}
    className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2 py-1 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
    onChange={(event) => onChange(event.target.value as PolicyDecision)}
    value={value}
  >
    {DECISIONS.map((item) => (
      <option key={item.value} value={item.value}>{item.label}</option>
    ))}
  </select>
);

const RuleRows = ({
  sectionKey, section, onChange,
}: {
  sectionKey: RuleSectionKey;
  section: RuleSection;
  onChange: (next: RuleSection) => void;
}) => {
  const [draft, setDraft] = useState('');
  const meta = SECTION_META[sectionKey];
  const defaultLabel = DECISIONS.find((item) => item.value === section.default)?.label || section.default;

  const addRule = () => {
    const pattern = draft.trim();
    if (!pattern) return;
    setDraft('');
    onChange({ ...section, rules: [...section.rules, { pattern, decision: 'approval_required' }] });
  };

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
        <h3 className="font-mono text-xs font-semibold text-[var(--duties-secondary)]">{meta.title}</h3>
        <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{meta.hint}</span>
      </div>

      <div className="space-y-1">
        {section.rules.map((rule, index) => (
          <div
            className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1.5"
            key={sectionKey + ':' + index + ':' + rule.pattern}
          >
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate font-mono text-[0.7rem] text-[var(--duties-text)]">
                {rule.pattern}
              </code>
              <DecisionSelect
                label={rule.pattern + ' 的档位'}
                onChange={(decision) => onChange({
                  ...section,
                  rules: section.rules.map((item, i) => (i === index ? { ...item, decision } : item)),
                })}
                value={rule.decision}
              />
              <button
                aria-label={'删除规则 ' + rule.pattern}
                className="px-1.5 py-1 font-mono text-[0.65rem] text-[var(--duties-tertiary)] transition-colors hover:text-red-600"
                onClick={() => onChange({ ...section, rules: section.rules.filter((_, i) => i !== index) })}
                type="button"
              >
                删除
              </button>
            </div>
            <input
              aria-label={rule.pattern + ' 的理由'}
              className={REASON_CLASS}
              onChange={(event) => onChange({
                ...section,
                rules: section.rules.map((item, i) => (
                  i === index ? { ...item, reason: event.target.value } : item
                )),
              })}
              placeholder="理由：会显示在审批卡片上，改了档位记得一并改"
              value={rule.reason || ''}
            />
          </div>
        ))}
      </div>

      <div className="mt-2 flex items-center gap-2">
        <input
          aria-label={meta.title + '新增路径'}
          className={INPUT_CLASS}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addRule(); } }}
          placeholder="新增路径，支持 glob，例如 data/**"
          value={draft}
        />
        <button className={GHOST_BUTTON_CLASS} onClick={addRule} type="button">添加</button>
      </div>
      <p className="mt-1 text-[0.6rem] text-[var(--duties-tertiary)]">
        自上而下第一条命中的生效，兜底规则要排在具体规则后面；都不命中时按「{defaultLabel}」处理。
      </p>
    </div>
  );
};

const SensitivePaths = ({
  paths, onChange,
}: { paths: string[]; onChange: (next: string[]) => void }) => {
  const [draft, setDraft] = useState('');

  const add = () => {
    const value = draft.trim();
    setDraft('');
    if (!value || paths.includes(value)) return;
    onChange([...paths, value]);
  };

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
        <h3 className="font-mono text-xs font-semibold text-[var(--duties-secondary)]">命令涉及的路径</h3>
        <span className="text-[0.6rem] text-[var(--duties-tertiary)]">
          上面的读取规则会自动并进来，这里只补工作区之外的位置。
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {paths.map((path) => (
          <span
            className="inline-flex items-center gap-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)]"
            key={path}
          >
            {path}
            <button
              aria-label={'删除 ' + path}
              className="text-[var(--duties-tertiary)] transition-colors hover:text-red-600"
              onClick={() => onChange(paths.filter((item) => item !== path))}
              type="button"
            >
              ×
            </button>
          </span>
        ))}
        {paths.length === 0 && (
          <span className="text-[0.65rem] text-[var(--duties-tertiary)]">已清空，命令不再按路径判敏感。</span>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          aria-label="新增命令敏感路径"
          className={INPUT_CLASS}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); add(); } }}
          placeholder="路径片段，例如 .ssh/"
          value={draft}
        />
        <button className={GHOST_BUTTON_CLASS} onClick={add} type="button">添加</button>
      </div>
    </div>
  );
};

export const PolicyRulesSection = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [doc, setDoc] = useState<PolicyDoc | null>(null);
  const [saved, setSaved] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return undefined;
    let alive = true;
    getPolicy(adminToken)
      .then((next) => {
        if (!alive) return;
        setDoc(next);
        setSaved(JSON.stringify(next));
      })
      .catch((error) => {
        if (alive) setMessage(error instanceof Error ? error.message : '读取策略失败');
      });
    return () => { alive = false; };
  }, [adminToken, isAuthenticated]);

  const dirty = Boolean(doc) && JSON.stringify(doc) !== saved;

  const save = async () => {
    if (!doc || !adminToken) return;
    setBusy(true);
    setMessage('');
    try {
      const next = await updatePolicy(adminToken, doc);
      setDoc(next);
      setSaved(JSON.stringify(next));
      setMessage('已保存，立即生效，不用重启');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    }
    setBusy(false);
  };

  if (!isAuthenticated || !adminToken) {
    return (
      <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
        <h2 className="font-mono text-sm font-semibold">服务端策略</h2>
        <p className="mt-1 text-xs text-[var(--duties-secondary)]">需要管理员令牌才能查看。</p>
      </section>
    );
  }

  return (
    <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-sm font-semibold">服务端策略</h2>
          <ScopeBadge scope="all-channels" />
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
          写进 data/policy.yaml，QQ、控制台、定时任务共用这一份。只管 read_file、write_file、
          execute_command、restart 这四类操作；外部脚本工具除非自己声明 guard，否则不受这里约束。
        </p>
      </div>

      {!doc ? (
        <p className="text-xs text-[var(--duties-tertiary)]">{message || '正在读取…'}</p>
      ) : (
        <div className="space-y-4">
          {(['read_file', 'write_file'] as RuleSectionKey[]).map((key) => (
            <RuleRows
              key={key}
              onChange={(next) => setDoc({ ...doc, [key]: next })}
              section={doc[key] || EMPTY_SECTION}
              sectionKey={key}
            />
          ))}

          <SensitivePaths
            onChange={(next) => setDoc({
              ...doc,
              execute_command: {
                ...(doc.execute_command || { default: 'approval_required' as PolicyDecision }),
                sensitive_path_patterns: next,
              },
            })}
            paths={doc.execute_command?.sensitive_path_patterns || []}
          />

          <p className="text-[0.6rem] leading-4 text-[var(--duties-tertiary)]">
            命令的硬拦截规则（deny_patterns）和各段默认档位不在这里，要改去「高级」页编辑 policy.yaml 原文。
          </p>

          <div className="flex items-center gap-2 border-t border-[var(--duties-border)] pt-3">
            <span className="min-w-0 flex-1 truncate text-[0.65rem] text-[var(--duties-tertiary)]">
              {message || (dirty ? '有未保存的改动' : '与服务器一致')}
            </span>
            <button
              className={GHOST_BUTTON_CLASS}
              disabled={!dirty || busy}
              onClick={() => { if (saved) { setDoc(JSON.parse(saved) as PolicyDoc); setMessage(''); } }}
              type="button"
            >
              还原
            </button>
            <button
              className="border border-[var(--duties-text)] bg-[var(--duties-text)] px-2.5 py-1 font-mono text-[0.65rem] text-[var(--duties-bg)] transition-opacity hover:opacity-80 disabled:opacity-40"
              disabled={!dirty || busy}
              onClick={() => void save()}
              type="button"
            >
              {busy ? '保存中' : '保存策略'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
};

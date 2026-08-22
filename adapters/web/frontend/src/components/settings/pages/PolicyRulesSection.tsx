// 服务端策略：决定每条路径要不要审批。QQ、控制台、定时任务共用这一份。
import { useEffect, useMemo, useState } from 'react';

import {
  getPolicy,
  updatePolicy,
  type PolicyDecision,
  type PolicyDoc,
  type PolicyRule,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { firstMatchIndex } from '../../../utils/globMatch';
import { ScopeBadge } from './settingsPagePrimitives';

type RuleSectionKey = 'read_file' | 'write_file';
type TabKey = RuleSectionKey | 'execute_command' | 'restart';
type DecisionFilter = PolicyDecision | 'all';
/** execute_command 下三组名单，各自是一列字符串。 */
type CommandListKey = 'deny_patterns' | 'sensitive_patterns' | 'sensitive_path_patterns';

interface RuleSection {
  default: PolicyDecision;
  rules: PolicyRule[];
}

const DECISIONS: Array<{ value: PolicyDecision; label: string }> = [
  { value: 'auto', label: '自动放行' },
  { value: 'approval_required', label: '需审批' },
  { value: 'deny', label: '拒绝' },
];

const DECISION_LABEL: Record<PolicyDecision, string> = {
  auto: '自动放行',
  approval_required: '需审批',
  deny: '拒绝',
};

const DECISION_TONE: Record<PolicyDecision, string> = {
  auto: 'text-[var(--duties-live)]',
  approval_required: 'text-[var(--duties-text)]',
  deny: 'text-[var(--duties-danger)]',
};

const DECISION_FILTERS: Array<{ value: DecisionFilter; label: string }> = [
  { value: 'all', label: '全部' },
  ...DECISIONS.map((item) => ({ value: item.value as DecisionFilter, label: item.label })),
];

const TAB_META: Record<TabKey, { title: string; hint: string }> = {
  read_file: { title: '读取', hint: 'read_file / list_dir / search_in_files 共用这一档。' },
  write_file: { title: '写入', hint: 'write_file / apply_diff 共用这一档。' },
  execute_command: { title: '命令', hint: 'execute_command。先过硬拦截，再看这条命令碰了哪些路径。' },
  restart: { title: '重启', hint: 'request_restart。重启会打断所有在跑的任务。' },
};

const COMMAND_LISTS: Array<{ key: CommandListKey; title: string; hint: string; placeholder: string }> = [
  {
    key: 'deny_patterns',
    title: '硬拦截',
    hint: '正则。命中就直接拒，管理员也绕不过去。',
    placeholder: '正则，例如 rm\\s+-rf\\s+/',
  },
  {
    key: 'sensitive_patterns',
    title: '判为敏感',
    hint: '正则。命中的命令按敏感处理，管理员也要点审批。',
    placeholder: '正则，例如 \\bcurl\\b.*\\|\\s*sh',
  },
  {
    key: 'sensitive_path_patterns',
    title: '敏感路径',
    hint: '路径片段。上面的读取规则会自动并进来，这里只补工作区之外的位置。',
    placeholder: '路径片段，例如 .ssh/',
  },
];

const EMPTY_SECTION: RuleSection = { default: 'auto', rules: [] };

const INPUT_CLASS =
  'min-w-0 flex-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-[0.7rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const GHOST_BUTTON_CLASS =
  'border border-[var(--duties-border)] px-2 py-1 font-mono text-[0.65rem] text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)] disabled:opacity-40';
const ICON_BUTTON_CLASS =
  'px-1 font-mono text-[0.7rem] leading-none text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)] disabled:opacity-30';
const REASON_CLASS =
  'w-full bg-transparent font-mono text-[0.6rem] text-[var(--duties-tertiary)] outline-none focus:text-[var(--duties-text)]';

const DecisionSelect = ({
  value, onChange, label,
}: { value: PolicyDecision; onChange: (next: PolicyDecision) => void; label: string }) => (
  <select
    aria-label={label}
    className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
    onChange={(event) => onChange(event.target.value as PolicyDecision)}
    value={value}
  >
    {DECISIONS.map((item) => (
      <option key={item.value} value={item.value}>{item.label}</option>
    ))}
  </select>
);

/** 输入一条路径，指出自上而下第一条命中的规则。判定和服务端同一套 fnmatch 语义。 */
const RuleProbe = ({ section }: { section: RuleSection }) => {
  const [path, setPath] = useState('');

  const probe = path.trim();
  const hit = probe ? firstMatchIndex(probe, section.rules.map((rule) => rule.pattern)) : -1;
  const rule = hit >= 0 ? section.rules[hit] : null;

  return (
    <div className="mt-3 border-t border-[var(--duties-border)] pt-3">
      <div className="flex items-center gap-2">
        <input
          aria-label="试一条路径"
          className={INPUT_CLASS}
          onChange={(event) => setPath(event.target.value)}
          placeholder="试一条路径，看它会落到哪条规则上"
          value={path}
        />
      </div>
      {probe && (
        <p className="mt-1 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
          {rule ? (
            <>
              第 {hit + 1} 条 <code className="text-[var(--duties-text)]">{rule.pattern}</code>
              {' → '}
              <span className={DECISION_TONE[rule.decision]}>{DECISION_LABEL[rule.decision]}</span>
            </>
          ) : (
            <>
              没有规则命中，按兜底的{' '}
              <span className={DECISION_TONE[section.default]}>{DECISION_LABEL[section.default]}</span> 处理
            </>
          )}
        </p>
      )}
      <p className="mt-1 text-[0.6rem] text-[var(--duties-tertiary)]">
        只看规则命中。工作区之外的路径服务端会先一步拒掉，不走这些规则。
      </p>
    </div>
  );
};

const RuleRows = ({
  sectionKey, section, onChange,
}: {
  sectionKey: RuleSectionKey;
  section: RuleSection;
  onChange: (next: RuleSection) => void;
}) => {
  const [draft, setDraft] = useState('');
  const [filter, setFilter] = useState('');
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>('all');
  const meta = TAB_META[sectionKey];

  const visible = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    return section.rules
      .map((rule, index) => ({ rule, index }))
      .filter(({ rule }) => (
        (decisionFilter === 'all' || rule.decision === decisionFilter)
        && (!keyword
          || rule.pattern.toLowerCase().includes(keyword)
          || (rule.reason || '').toLowerCase().includes(keyword))
      ));
  }, [section.rules, filter, decisionFilter]);

  const patch = (index: number, next: Partial<PolicyRule>) => {
    onChange({ ...section, rules: section.rules.map((item, i) => (i === index ? { ...item, ...next } : item)) });
  };

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= section.rules.length) return;
    const rules = [...section.rules];
    [rules[index], rules[target]] = [rules[target], rules[index]];
    onChange({ ...section, rules });
  };

  const addRule = () => {
    const pattern = draft.trim();
    if (!pattern) return;
    setDraft('');
    // 插到最前面。追加到末尾的话，兜底规则（read_file 末尾那条 data/**）会先命中，
    // 新加的规则一辈子不会生效，界面上却看着像已经配好了。
    onChange({ ...section, rules: [{ pattern, decision: 'approval_required' }, ...section.rules] });
  };

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <label className="font-mono text-[0.65rem] text-[var(--duties-secondary)]" htmlFor={`${sectionKey}-default`}>
          兜底档位
        </label>
        <select
          className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
          id={`${sectionKey}-default`}
          onChange={(event) => onChange({ ...section, default: event.target.value as PolicyDecision })}
          value={section.default}
        >
          {DECISIONS.map((item) => (
            <option key={item.value} value={item.value}>{item.label}</option>
          ))}
        </select>
        <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{meta.hint}</span>
      </div>

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input
          aria-label={`筛选${meta.title}规则`}
          className={INPUT_CLASS}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="按路径或理由筛选"
          value={filter}
        />
        <div className="flex gap-1">
          {DECISION_FILTERS.map((item) => (
            <button
              aria-pressed={decisionFilter === item.value}
              className={`border px-1.5 py-1 font-mono text-[0.6rem] transition-colors ${
                decisionFilter === item.value
                  ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
                  : 'border-[var(--duties-border)] text-[var(--duties-secondary)] hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]'
              }`}
              key={item.value}
              onClick={() => setDecisionFilter(item.value)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <ul className="m-0 max-h-[26rem] list-none space-y-1 overflow-y-auto p-0">
        {visible.map(({ rule, index }) => (
          <li
            className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1"
            key={`${sectionKey}:${index}:${rule.pattern}`}
          >
            <div className="flex items-center gap-1.5">
              <span className="flex flex-col leading-none">
                <button
                  aria-label={`把 ${rule.pattern} 上移`}
                  className={ICON_BUTTON_CLASS}
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                  type="button"
                >
                  ⌃
                </button>
                <button
                  aria-label={`把 ${rule.pattern} 下移`}
                  className={ICON_BUTTON_CLASS}
                  disabled={index === section.rules.length - 1}
                  onClick={() => move(index, 1)}
                  type="button"
                >
                  ⌄
                </button>
              </span>
              <span className="w-6 shrink-0 text-right font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
                {index + 1}
              </span>
              <code className="min-w-0 flex-1 truncate font-mono text-[0.7rem] text-[var(--duties-text)]">
                {rule.pattern}
              </code>
              <DecisionSelect
                label={`${rule.pattern} 的档位`}
                onChange={(decision) => patch(index, { decision })}
                value={rule.decision}
              />
              <button
                aria-label={`删除规则 ${rule.pattern}`}
                className={ICON_BUTTON_CLASS}
                onClick={() => onChange({ ...section, rules: section.rules.filter((_, i) => i !== index) })}
                type="button"
              >
                ×
              </button>
            </div>
            <input
              aria-label={`${rule.pattern} 的理由`}
              className={REASON_CLASS}
              onChange={(event) => patch(index, { reason: event.target.value })}
              placeholder="理由：会显示在审批卡片上，改了档位记得一并改"
              value={rule.reason || ''}
            />
          </li>
        ))}
        {section.rules.length > 0 && visible.length === 0 && (
          <li className="border border-dashed border-[var(--duties-border)] p-2 text-[0.65rem] text-[var(--duties-tertiary)]">
            没有符合条件的规则。
          </li>
        )}
        {section.rules.length === 0 && (
          <li className="border border-dashed border-[var(--duties-border)] p-2 text-[0.65rem] text-[var(--duties-tertiary)]">
            一条规则都没有，所有路径按兜底档位处理。
          </li>
        )}
      </ul>

      <div className="mt-2 flex items-center gap-2">
        <input
          aria-label={`${meta.title}新增路径`}
          className={INPUT_CLASS}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addRule(); } }}
          placeholder="新增路径，支持 glob，例如 data/**"
          value={draft}
        />
        <button className={GHOST_BUTTON_CLASS} onClick={addRule} type="button">插到最前</button>
      </div>
      <p className="mt-1 text-[0.6rem] text-[var(--duties-tertiary)]">
        自上而下第一条命中的生效。新规则插在最前面，要让它给谁让路就往下移。
      </p>

      <RuleProbe section={section} />
    </div>
  );
};

const PatternList = ({
  hint, items, label, onChange, placeholder,
}: {
  hint: string;
  items: string[];
  label: string;
  onChange: (next: string[]) => void;
  placeholder: string;
}) => {
  const [draft, setDraft] = useState('');

  const add = () => {
    const value = draft.trim();
    setDraft('');
    if (!value || items.includes(value)) return;
    onChange([...items, value]);
  };

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
        <h3 className="font-mono text-xs font-semibold text-[var(--duties-secondary)]">{label}</h3>
        <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{hint}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            className="inline-flex items-center gap-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)]"
            key={item}
          >
            {item}
            <button
              aria-label={`删除 ${item}`}
              className="text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-danger)]"
              onClick={() => onChange(items.filter((entry) => entry !== item))}
              type="button"
            >
              ×
            </button>
          </span>
        ))}
        {items.length === 0 && (
          <span className="text-[0.65rem] text-[var(--duties-tertiary)]">这一组是空的。</span>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          aria-label={`新增${label}`}
          className={INPUT_CLASS}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); add(); } }}
          placeholder={placeholder}
          value={draft}
        />
        <button className={GHOST_BUTTON_CLASS} onClick={add} type="button">添加</button>
      </div>
    </div>
  );
};

const CommandPanel = ({
  section, onChange,
}: {
  section: NonNullable<PolicyDoc['execute_command']>;
  onChange: (next: NonNullable<PolicyDoc['execute_command']>) => void;
}) => (
  <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-2">
      <label className="font-mono text-[0.65rem] text-[var(--duties-secondary)]" htmlFor="command-default">
        兜底档位
      </label>
      <select
        className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
        id="command-default"
        onChange={(event) => onChange({ ...section, default: event.target.value as PolicyDecision })}
        value={section.default}
      >
        {DECISIONS.map((item) => (
          <option key={item.value} value={item.value}>{item.label}</option>
        ))}
      </select>
      <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{TAB_META.execute_command.hint}</span>
    </div>

    {COMMAND_LISTS.map((list) => (
      <PatternList
        hint={list.hint}
        items={section[list.key] || []}
        key={list.key}
        label={list.title}
        onChange={(next) => onChange({ ...section, [list.key]: next })}
        placeholder={list.placeholder}
      />
    ))}
  </div>
);

const EMPTY_COMMAND: NonNullable<PolicyDoc['execute_command']> = { default: 'approval_required' };

export const PolicyRulesSection = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [doc, setDoc] = useState<PolicyDoc | null>(null);
  const [saved, setSaved] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [tab, setTab] = useState<TabKey>('write_file');

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

  const counts: Record<TabKey, number> = {
    read_file: doc?.read_file?.rules?.length || 0,
    write_file: doc?.write_file?.rules?.length || 0,
    execute_command: COMMAND_LISTS.reduce(
      (total, list) => total + (doc?.execute_command?.[list.key]?.length || 0),
      0,
    ),
    restart: 0,
  };

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
        <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
          同一条规则，谁触发结果不同：QQ 群里的非管理员一律拒绝，不看规则；QQ 管理员碰到
          「需审批」但没命中任何一条具体规则时会直接执行；定时任务没人点审批，需审批即拒绝。
        </p>
      </div>

      {!doc ? (
        <p className="text-xs text-[var(--duties-tertiary)]">{message || '正在读取…'}</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1 border-b border-[var(--duties-border)] pb-2">
            {(Object.keys(TAB_META) as TabKey[]).map((key) => (
              <button
                aria-pressed={tab === key}
                className={`border px-2 py-1 font-mono text-[0.65rem] transition-colors ${
                  tab === key
                    ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
                    : 'border-[var(--duties-border)] text-[var(--duties-secondary)] hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]'
                }`}
                key={key}
                onClick={() => setTab(key)}
                type="button"
              >
                {TAB_META[key].title}
                {counts[key] > 0 && <span className="ml-1 opacity-70">{counts[key]}</span>}
              </button>
            ))}
          </div>

          {(tab === 'read_file' || tab === 'write_file') && (
            <RuleRows
              onChange={(next) => setDoc({ ...doc, [tab]: next })}
              section={doc[tab] || EMPTY_SECTION}
              sectionKey={tab}
            />
          )}

          {tab === 'execute_command' && (
            <CommandPanel
              onChange={(next) => setDoc({ ...doc, execute_command: next })}
              section={doc.execute_command || EMPTY_COMMAND}
            />
          )}

          {tab === 'restart' && (
            <div className="flex flex-wrap items-center gap-2">
              <label className="font-mono text-[0.65rem] text-[var(--duties-secondary)]" htmlFor="restart-default">
                档位
              </label>
              <select
                className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                id="restart-default"
                onChange={(event) => setDoc({ ...doc, restart: { default: event.target.value as PolicyDecision } })}
                value={doc.restart?.default || 'approval_required'}
              >
                {DECISIONS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
              <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{TAB_META.restart.hint}</span>
            </div>
          )}

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

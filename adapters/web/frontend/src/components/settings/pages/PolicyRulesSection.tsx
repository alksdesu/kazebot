// 服务端策略：决定每条路径要不要审批。QQ、控制台、定时任务共用这一份。
import { useEffect, useMemo, useState } from 'react';

import {
  enableCodeWrite,
  getPolicy,
  isEditConflict,
  updatePolicy,
  type ApprovalGrantSettings,
  type PolicyDecision,
  type PolicyDoc,
  type PolicyRule,
} from '../../../api/supervisorClient';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { useRequestGuard } from '../../../hooks/useRequestGuard';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { useSettingsStore } from '../../../store/settingsStore';
import { firstMatchIndex } from '../../../utils/globMatch';
import { Button, DangerPanel, ErrorText, SaveBar, ScopeBadge } from './settingsControls';
import { errorMessage } from '../../../api/request';

type RuleSectionKey = 'read_file' | 'write_file';
type TabKey = RuleSectionKey | 'execute_command' | 'restart' | 'approval_grant';
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
  read_file: { title: '读取', hint: 'read_file / list_dir / glob / search_in_files 共用这一档。' },
  write_file: { title: '写入', hint: 'write_file / apply_diff 共用这一档。' },
  execute_command: { title: '命令', hint: 'execute_command。先过硬拦截，再看这条命令碰了哪些路径。' },
  restart: { title: '重启', hint: 'request_restart。重启会打断所有在跑的任务。' },
  approval_grant: {
    title: '审批授权',
    hint: '管理员在 QQ 上同意自己发起的审批时，顺带给自己一段免审时间。同意别人发起的请求不生成授权。',
  },
};

const GRANT_DEFAULTS: ApprovalGrantSettings = { default_minutes: 30, max_minutes: 1440 };
// 和服务端校验的上限一致：再长就等于永久免审。
const GRANT_MINUTES_CEILING = 525600;

type GrantField = keyof ApprovalGrantSettings;

const GRANT_FIELDS: Array<{ key: GrantField; label: string; hint: string; min: number }> = [
  {
    key: 'default_minutes',
    label: '默认时长（分钟）',
    hint: '同意时没写时长就按它。填 0 表示默认不授权，要授权得在同意时带上时长。',
    min: 0,
  },
  {
    key: 'max_minutes',
    label: '时长上限（分钟）',
    hint: `写了更长的时长也按它截断。至少 1，最多 ${GRANT_MINUTES_CEILING}（365 天）。`,
    min: 1,
  },
];

/** 两个时长里第一处填得不对的地方；都能保存时是 null。 */
const grantProblem = (grant: ApprovalGrantSettings): { field: GrantField; text: string } | null => {
  // 大到不精确的数到 1e21 起会被 JSON 写成 1e+21，服务端当成小数拒掉。
  if (!Number.isSafeInteger(grant.default_minutes) || grant.default_minutes < 0) {
    return { field: 'default_minutes', text: '默认时长要填 0 或正整数（分钟）。' };
  }
  if (!Number.isSafeInteger(grant.max_minutes) || grant.max_minutes < 1) {
    return { field: 'max_minutes', text: '时长上限要填正整数（分钟），至少 1。' };
  }
  if (grant.max_minutes > GRANT_MINUTES_CEILING) {
    return { field: 'max_minutes', text: `时长上限最多 ${GRANT_MINUTES_CEILING} 分钟（365 天）。` };
  }
  return null;
};

// 清空的框记成 NaN 而不是 0：存成 0 等于悄悄关掉默认授权。
const minutesOf = (raw: string): number => (raw.trim() === '' ? Number.NaN : Number(raw));

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
const KAZEBOT_ROOT = '/opt/kazebot';

const clonePolicy = (policy: PolicyDoc): PolicyDoc => JSON.parse(JSON.stringify(policy)) as PolicyDoc;

const allAutoPolicy = (policy: PolicyDoc): PolicyDoc => ({
  ...policy,
  read_file: {
    ...(policy.read_file || {}),
    default: 'auto',
    rules: (policy.read_file?.rules || []).map((rule) => ({ ...rule, decision: 'auto' })),
  },
  write_file: {
    ...(policy.write_file || {}),
    default: 'auto',
    rules: (policy.write_file?.rules || []).map((rule) => ({ ...rule, decision: 'auto' })),
  },
  execute_command: {
    ...(policy.execute_command || {}),
    default: 'auto',
    deny_patterns: [],
    sensitive_patterns: [],
    sensitive_path_patterns: [],
  },
  restart: { ...(policy.restart || {}), default: 'auto' },
});

const INPUT_CLASS =
  'min-w-0 flex-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const NUMBER_INPUT_CLASS =
  'w-[var(--duties-input-width)] border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const GHOST_BUTTON_CLASS =
  'border border-[var(--duties-border)] px-2 py-1 font-mono text-mini text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)] disabled:opacity-40';
const ICON_BUTTON_CLASS =
  'px-1 font-mono text-mini leading-none text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)] disabled:opacity-30';
const REASON_CLASS =
  'w-full bg-transparent font-mono text-micro text-[var(--duties-tertiary)] outline-none focus:text-[var(--duties-text)]';

const DecisionSelect = ({
  value, onChange, label,
}: { value: PolicyDecision; onChange: (next: PolicyDecision) => void; label: string }) => (
  <select
    aria-label={label}
    className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
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
        <p className="mt-1 font-mono text-mini text-[var(--duties-secondary)]">
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
      <p className="mt-1 text-micro text-[var(--duties-tertiary)]">
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
        <label className="font-mono text-mini text-[var(--duties-secondary)]" htmlFor={`${sectionKey}-default`}>
          兜底档位
        </label>
        <select
          className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
          id={`${sectionKey}-default`}
          onChange={(event) => onChange({ ...section, default: event.target.value as PolicyDecision })}
          value={section.default}
        >
          {DECISIONS.map((item) => (
            <option key={item.value} value={item.value}>{item.label}</option>
          ))}
        </select>
        <span className="text-micro text-[var(--duties-tertiary)]">{meta.hint}</span>
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
              className={`border px-1.5 py-1 font-mono text-micro transition-colors ${
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

      <ul className="m-0 max-h-[var(--duties-pane-height)] list-none space-y-1 overflow-y-auto p-0">
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
              <span className="w-6 shrink-0 text-right font-mono text-micro text-[var(--duties-tertiary)]">
                {index + 1}
              </span>
              <code className={`min-w-0 flex-1 truncate font-mono text-mini text-[var(--duties-text)] ${
                rule.pattern === KAZEBOT_ROOT || rule.pattern === `${KAZEBOT_ROOT}/**` ? 'font-bold' : ''
              }`}>
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
          <li className="border border-dashed border-[var(--duties-border)] p-2 text-mini text-[var(--duties-tertiary)]">
            没有符合条件的规则。
          </li>
        )}
        {section.rules.length === 0 && (
          <li className="border border-dashed border-[var(--duties-border)] p-2 text-mini text-[var(--duties-tertiary)]">
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
      <p className="mt-1 text-micro text-[var(--duties-tertiary)]">
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
        <span className="text-micro text-[var(--duties-tertiary)]">{hint}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            className="inline-flex items-center gap-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-1.5 py-0.5 font-mono text-mini text-[var(--duties-text)]"
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
          <span className="text-mini text-[var(--duties-tertiary)]">这一组是空的。</span>
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
      <label className="font-mono text-mini text-[var(--duties-secondary)]" htmlFor="command-default">
        兜底档位
      </label>
      <select
        className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
        id="command-default"
        onChange={(event) => onChange({ ...section, default: event.target.value as PolicyDecision })}
        value={section.default}
      >
        {DECISIONS.map((item) => (
          <option key={item.value} value={item.value}>{item.label}</option>
        ))}
      </select>
      <span className="text-micro text-[var(--duties-tertiary)]">{TAB_META.execute_command.hint}</span>
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

const GrantPanel = ({
  section, onChange,
}: {
  section: ApprovalGrantSettings;
  onChange: (next: ApprovalGrantSettings) => void;
}) => {
  const problem = grantProblem(section);
  const capped = !problem && section.default_minutes > section.max_minutes;

  return (
    <div className="space-y-3">
      <p className="text-micro leading-5 text-[var(--duties-tertiary)]">{TAB_META.approval_grant.hint}</p>
      {GRANT_FIELDS.map((field) => (
        <div className="flex flex-wrap items-center gap-2" key={field.key}>
          <label className="font-mono text-mini text-[var(--duties-secondary)]" htmlFor={`approval-grant-${field.key}`}>
            {field.label}
          </label>
          <input
            aria-describedby={`approval-grant-${field.key}-hint`}
            aria-invalid={problem?.field === field.key}
            className={NUMBER_INPUT_CLASS}
            id={`approval-grant-${field.key}`}
            inputMode="numeric"
            max={GRANT_MINUTES_CEILING}
            min={field.min}
            onChange={(event) => onChange({ ...section, [field.key]: minutesOf(event.target.value) })}
            step={1}
            type="number"
            value={Number.isFinite(section[field.key]) ? String(section[field.key]) : ''}
          />
          <span className="text-micro text-[var(--duties-tertiary)]" id={`approval-grant-${field.key}-hint`}>
            {field.hint}
          </span>
        </div>
      ))}
      {problem && <p className="text-micro text-[var(--duties-danger)]" role="alert">{problem.text}</p>}
      {capped && (
        <p className="text-micro text-[var(--duties-secondary)]">
          默认时长比上限还长，保存后按上限 {section.max_minutes} 分钟算。
        </p>
      )}
      <p className="text-micro leading-5 text-[var(--duties-tertiary)]">
        在 QQ 私聊里同意时可以带时长：「同意 2h」「/审批 同意 &lt;ID&gt; 30m」，「同意 单次」只批这一次；发「/审批 收回」提前收回。
        授权期间你发起的任务（连同派出去的子任务）原本要审批的操作直接放行，敏感操作也算；
        拒绝规则、群成员的硬拒绝和定时任务的敏感拒绝不受影响。
      </p>
    </div>
  );
};

const EMPTY_COMMAND: NonNullable<PolicyDoc['execute_command']> = { default: 'approval_required' };

export const PolicyRulesSection = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const requests = useRequestGuard();
  const [doc, setDoc] = useState<PolicyDoc | null>(null);
  const [saved, setSaved] = useState('');
  // 这份策略出自哪一版文件，保存时交回：共享组里别的实例中途改过就不写，免得整份盖掉。
  const [base, setBase] = useState<string | null>(null);
  // 「丢弃」重读服务器上现在那一份，保存报冲突之后靠它换成最新内容。
  const [generation, setGeneration] = useState(0);
  const [busy, setBusy] = useState(false);
  const { notice, error, setMessage, setError, setLoadError } = usePageNotice();
  const [tab, setTab] = useState<TabKey>('write_file');
  const [fullAutoPending, setFullAutoPending] = useState(false);
  const [fullAutoOriginal, setFullAutoOriginal] = useState<PolicyDoc | null>(null);
  const [codeWriteRequested, setCodeWriteRequested] = useState(false);

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return undefined;
    const request = requests.begin('policy');
    getPolicy(adminToken)
      .then(({ policy: next, hash }) => {
        if (!request.isCurrent()) return;
        setDoc(next);
        setSaved(JSON.stringify(next));
        setBase(hash);
        setFullAutoPending(false);
        setFullAutoOriginal(null);
        setCodeWriteRequested(false);
        setLoadError('');
      })
      .catch((caught) => {
        if (request.isCurrent()) setLoadError(errorMessage(caught, '读取策略失败'));
      });
    return () => { requests.drop('policy'); };
  }, [requests, adminToken, isAuthenticated, generation, setLoadError]);

  const dirty = Boolean(doc) && JSON.stringify(doc) !== saved;
  const confirmDiscard = useUnsavedChanges(dirty, '「服务端策略」还有未保存修改，确定离开或丢弃吗？');

  const save = async () => {
    if (!doc || !adminToken) return;
    const grantError = doc.approval_grant ? grantProblem(doc.approval_grant) : null;
    if (grantError) {
      setTab('approval_grant');
      setError(`没有保存：${grantError.text}`);
      return;
    }
    const enableCodeWriteAfterSave = fullAutoPending && codeWriteRequested;
    setBusy(true);
    setMessage('');
    try {
      const next = await updatePolicy(adminToken, doc, base);
      if (!requests.isCurrent()) return;
      setDoc(next.policy);
      setSaved(JSON.stringify(next.policy));
      setBase(next.hash);
      setFullAutoPending(false);
      setFullAutoOriginal(null);
      setCodeWriteRequested(false);
      if (enableCodeWriteAfterSave) {
        try {
          await enableCodeWrite(adminToken);
          if (requests.isCurrent()) setMessage('策略已保存，已提交 /opt/kazebot 写入权限并重启 miaomiao');
        } catch (caught) {
          if (requests.isCurrent()) setMessage('策略已保存。', `代码目录写权限没能启用：${errorMessage(caught, '宿主机操作失败')}`);
        }
      } else {
        setMessage('已保存，立即生效，不用重启');
      }
    } catch (caught) {
      if (!requests.isCurrent()) return;
      setError(isEditConflict(caught)
        ? '策略在打开之后被别处改过，没有保存：点「丢弃」读入最新内容，再把改动重做一遍。'
        : errorMessage(caught, '保存失败'));
    } finally {
      setBusy(false);
    }
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
    approval_grant: 0,
  };

  const prepareFullAuto = () => {
    if (!doc) return;
    setFullAutoOriginal(clonePolicy(doc));
    setDoc(allAutoPolicy(doc));
    setFullAutoPending(true);
    setMessage('已生成全自动放行草稿；请确认后保存。');
  };

  return (
    <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-sm font-semibold">服务端策略</h2>
          <ScopeBadge scope="all-channels" />
        </div>
        <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
          写进 config/policy.yaml，QQ、控制台、定时任务共用这一份。只管 read_file、write_file、
          execute_command、restart 这四类操作；外部脚本工具除非自己声明 guard，否则不受这里约束。
        </p>
        <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
          同一条规则，谁触发结果不同：QQ 群里的非管理员一律拒绝，不看规则；QQ 管理员碰到
          「需审批」但没命中任何一条具体规则时会直接执行；定时任务没人点审批，需审批即拒绝。
          QQ 管理员同意自己发起的审批后，一段时间里他发起的操作免审批，时长在「审批授权」里设。
        </p>
        <DangerPanel>
          <div className="min-w-0 flex-1">
            <p className="font-mono text-xs font-semibold">全自动放行策略规则</p>
            <p className="mt-1 text-micro leading-5 text-[var(--duties-secondary)]">
              将四类规则和三组命令列表改成自动放行并清空列表。代码级密钥、平台状态和受保护路径仍由服务端硬保护。
            </p>
          </div>
          {fullAutoPending ? (
            <div className="flex flex-col items-start gap-2">
              <label className="flex items-start gap-2 text-micro text-[var(--duties-secondary)]">
                <input
                  checked={codeWriteRequested}
                  disabled={busy}
                  onChange={(event) => setCodeWriteRequested(event.target.checked)}
                  type="checkbox"
                />
                <span>
                  同时开放 <code>/opt/kazebot</code> 写入并重启 miaomiao（高风险，仅本控制台管理员）。
                  一旦开启，Bot 将可以完全控制此设备（其它未修改的 systemd 保护仍按现状生效），仅限开发调试使用。
                </span>
              </label>
              <div className="flex flex-wrap gap-2">
                <Button disabled={busy} onClick={() => void save()} tone="danger">确认并保存全自动放行</Button>
                <Button disabled={busy} onClick={() => { if (fullAutoOriginal) setDoc(fullAutoOriginal); setFullAutoOriginal(null); setFullAutoPending(false); setCodeWriteRequested(false); setMessage('已取消全自动放行保存。'); }} tone="quiet">取消</Button>
              </div>
            </div>
          ) : (
            <Button disabled={busy} onClick={prepareFullAuto} tone="danger">一键全自动放行</Button>
          )}
        </DangerPanel>
      </div>

      {!doc ? (
        error ? <ErrorText role="alert">{error}</ErrorText> : <p className="text-xs text-[var(--duties-tertiary)]">{notice || '正在读取…'}</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1 border-b border-[var(--duties-border)] pb-2">
            {(Object.keys(TAB_META) as TabKey[]).map((key) => (
              <button
                aria-pressed={tab === key}
                className={`border px-2 py-1 font-mono text-mini transition-colors ${
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
              <label className="font-mono text-mini text-[var(--duties-secondary)]" htmlFor="restart-default">
                档位
              </label>
              <select
                className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-1.5 py-0.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                id="restart-default"
                onChange={(event) => setDoc({ ...doc, restart: { default: event.target.value as PolicyDecision } })}
                value={doc.restart?.default || 'approval_required'}
              >
                {DECISIONS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
              <span className="text-micro text-[var(--duties-tertiary)]">{TAB_META.restart.hint}</span>
            </div>
          )}

          {tab === 'approval_grant' && (
            <GrantPanel
              onChange={(next) => setDoc({ ...doc, approval_grant: next })}
              section={doc.approval_grant || GRANT_DEFAULTS}
            />
          )}

          <SaveBar
            busy={busy}
            dirty={dirty}
            label="保存策略"
            note={notice || (dirty ? '有未保存的改动' : '与服务器一致')}
            onReset={() => { if (saved && confirmDiscard()) { setMessage(''); setGeneration((value) => value + 1); } }}
            onSave={() => void save()}
          />
          {error && <ErrorText role="alert">{error}</ErrorText>}
        </div>
      )}
    </section>
  );
};

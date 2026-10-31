// 上下文压缩的阈值和保留量。只按键写 runtime.yaml 本实例那层，留空的项跟随默认。
import { useCallback, useEffect, useRef, useState } from 'react';
import { getRuntimeLayers, isEditConflict, type RuntimeLayers } from '../api/supervisorClient';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Desc, ErrorText, Field, Footnote, Input } from './components';
import { commitRuntimeKeys, runtimeInstanceLayer, StaleKeysError } from './yamlCommit';
import { parseYamlMapping, readYamlPath, type YamlKeyEdit } from './yamlRewrite';
import { savedMessage } from '../utils/sentence';
import { errorMessage } from '../api/request';

type CompactKey = 'threshold_tokens' | 'hard_threshold_tokens' | 'keep_recent_tokens' | 'keep_recent';

type CompactValues = Record<CompactKey, string>;

interface CompactField {
  key: CompactKey;
  label: string;
  /** 报错时怎么称呼它。 */
  name: string;
  min: number;
  max?: number;
  hint: string;
}

const COMPACT_PATH = ['engine', 'compact'];

// 0 各有含义：软阈值 0 关掉自动压缩，硬阈值 0 取软阈值的 1.25 倍，保留原文 0 改按段数算。
const FIELDS: readonly CompactField[] = [
  {
    key: 'threshold_tokens', label: '软阈值（token）', name: '软阈值', min: 0,
    hint: '超过就在后台静默压缩，当轮回复照常进行。填 0 关闭自动压缩。',
  },
  {
    key: 'hard_threshold_tokens', label: '硬阈值（token）', name: '硬阈值', min: 0,
    hint: '超过则挂起当轮同步压缩，用户会多等一次调用。填 0 取软阈值的 1.25 倍；比软阈值低时按软阈值算。',
  },
  {
    key: 'keep_recent_tokens', label: '保留原文（token）', name: '保留原文', min: 0,
    hint: '压缩时保留多少最新原文，更早的压成摘要。超过软阈值的 80% 时按 80% 算；填 0 改按下面的段数保留。',
  },
  // engine 读的时候夹在 2～50，超出的存得进去，跑起来却不是填的那个数
  {
    key: 'keep_recent', label: '保留段数', name: '保留段数', min: 2, max: 50,
    hint: '仅在上一项为 0 时生效：保留最近几个完整任务段，2～50。',
  },
];

const EMPTY: CompactValues = { threshold_tokens: '', hard_threshold_tokens: '', keep_recent_tokens: '', keep_recent: '' };

const CONFLICT_MESSAGE = '压缩设置已被其他页面修改，未覆盖你的草稿。请核对后重新载入压缩设置。';

const valuesOf = (read: (key: CompactKey) => string): CompactValues => (
  Object.fromEntries(FIELDS.map(({ key }) => [key, read(key)])) as CompactValues
);

/** 原样摆进输入框：写成 engine 读不成整数的样子也照实显示，交给校验指出来，在这里就能改好。 */
function shownText(raw: unknown): string {
  if (raw === undefined) return '';
  if (raw !== null && typeof raw === 'object') return JSON.stringify(raw);
  return String(raw);
}

/** 本实例那层的 engine.compact。路上哪一段写成了别的就不让在这里改：按键写回会把那一段整个换掉。 */
function ownCompact(file: RuntimeLayers): unknown {
  let section: unknown = parseYamlMapping(runtimeInstanceLayer(file));
  for (const [index, key] of COMPACT_PATH.entries()) {
    section = (section as Record<string, unknown>)[key];
    if (section === undefined || section === null) return undefined;
    if (typeof section !== 'object' || Array.isArray(section)) {
      throw new Error(`本实例 runtime.yaml 里的 ${COMPACT_PATH.slice(0, index + 1).join('.')} 不是一段配置（key: value），在这里改会把它整个换掉。请先到高级页的原文里改好。`);
    }
  }
  return section;
}

/** 留空是跟随默认；填了就得是 engine 照填的跑的整数。 */
function fieldProblem(field: CompactField, text: string): string {
  const value = text.trim();
  if (!value) return '';
  const number = Number(value);
  const fits = /^\d+$/.test(value) && Number.isSafeInteger(number)
    && number >= field.min && (field.max === undefined || number <= field.max);
  if (fits) return '';
  const range = field.max === undefined ? `不小于 ${field.min} 的整数` : ` ${field.min}～${field.max} 的整数`;
  return `${field.name}须为${range}，留空跟随默认。`;
}

/** 输入框回写成 yaml 的值：留空是删掉这个键，回到跟随默认。 */
const yamlValue = (text: string): number | undefined => (text.trim() ? Number(text.trim()) : undefined);

interface Snapshot {
  /** 读到的那一版原文，保存前逐键核对它在那之后有没有被别处改过。 */
  content: string;
  /** 本实例那层写的，没写是空串。 */
  own: CompactValues;
  /** 本实例没写时跟着的值，哪一层都没写是空串。 */
  defaults: CompactValues;
}

function readSnapshot(file: RuntimeLayers): Snapshot {
  if (typeof file.hash !== 'string') throw new Error('服务端未返回配置版本，无法安全保存，请更新服务后重试。');
  const own = ownCompact(file);
  return {
    content: file.content,
    own: valuesOf((key) => shownText(readYamlPath(own, [key]))),
    defaults: valuesOf((key) => shownText(readYamlPath(file.defaults, [...COMPACT_PATH, key]))),
  };
}

export function ContextCompactSettings() {
  const token = useSettingsStore(state => state.adminToken);
  const requests = useRequestGuard();
  const [snapshot, setSnapshot] = useState<{ key: string; value: Snapshot } | null>(null);
  const baseline = snapshot?.key === requests.key ? snapshot.value : null;
  const [draft, setDraft] = useState<CompactValues>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const mutating = useRef(false);
  const changed = baseline ? FIELDS.filter(({ key }) => draft[key] !== baseline.own[key]) : [];
  const dirty = changed.length > 0;
  const confirmDiscard = useUnsavedChanges(dirty, '上下文压缩设置尚未保存，确定丢弃草稿并继续吗？');
  const problems = baseline ? FIELDS.map((field) => fieldProblem(field, draft[field.key])).filter(Boolean) : [];

  // 保存和读取走同一路：保存一开始，还没回来的读取就作废。
  const load = useCallback(async () => {
    if (!token || !requests.isCurrent() || mutating.current) return;
    const request = requests.begin('compact');
    setLoading(true); setError(''); setNotice('');
    try {
      const file = await getRuntimeLayers(token);
      if (!request.isCurrent()) return;
      const next = readSnapshot(file);
      setSnapshot({ key: requests.key, value: next });
      setDraft(next.own); setConflict(false);
    } catch (reason) {
      if (request.isCurrent()) setError(errorMessage(reason));
    } finally {
      if (request.isCurrent()) setLoading(false);
    }
  }, [requests, token]);

  useEffect(() => {
    setSnapshot(null); setDraft(EMPTY); setError(''); setNotice(''); setConflict(false); setSaving(false);
    mutating.current = false;
    void load();
  }, [load]);

  const save = async () => {
    if (!token || !baseline || problems.length || !dirty || loading || conflict || mutating.current || !requests.isCurrent()) return;
    mutating.current = true;
    const request = requests.begin('compact');
    setSaving(true); setError(''); setNotice('');
    try {
      // 只写改过的键，落在本实例那层；读回来时这几个键已被别处改过就不覆盖。
      const edits: YamlKeyEdit[] = changed.map(({ key }) => ({ path: [...COMPACT_PATH, key], value: yamlValue(draft[key]) }));
      const { content, note, result, receiptProblem } = await commitRuntimeKeys(token, edits, {
        loaded: baseline.content,
        proceed: request.isCurrent,
      });
      if (!request.isCurrent()) return;
      if (result && !result.ok) throw new Error('未能确认保存版本，请重新载入压缩设置核对；草稿已保留。');
      const own = { ...baseline.own };
      for (const { key } of changed) own[key] = shownText(yamlValue(draft[key]));
      // 回执没读懂时下一次保存照样先读最新一版、按原文逐键核对，不靠这份回执。
      setSnapshot({ key: requests.key, value: { ...baseline, content, own } });
      setDraft(own); setNotice(savedMessage('压缩设置已保存。', note, result?.warnings)); setError(receiptProblem);
    } catch (reason) {
      if (request.isCurrent()) {
        if (reason instanceof StaleKeysError) {
          setConflict(true); setError(CONFLICT_MESSAGE);
        } else if (isEditConflict(reason)) {
          setConflict(true); setError('运行时配置在保存期间发生变化，草稿已保留。请重新载入压缩设置后重试。');
        } else setError(errorMessage(reason));
      }
    } finally {
      if (request.isCurrent()) {
        mutating.current = false; setSaving(false);
      }
    }
  };

  return <Block id="context-compact" title="上下文压缩" hint="当前实例">
    <Footnote>对话超过阈值后，较早的历史自动压成摘要。改完下一个任务就生效，不用重启。</Footnote>
    <div className="mt-3 space-y-3">
      {FIELDS.map((field) => {
        const id = `context-compact-${field.key}`;
        const fallback = baseline?.defaults[field.key];
        return <div key={field.key}>
          <Field htmlFor={id} label={field.label} layout="stacked">
            <Input
              disabled={!baseline || loading || saving}
              id={id}
              inputMode="numeric"
              onChange={event => { setDraft(current => ({ ...current, [field.key]: event.target.value })); setNotice(''); }}
              placeholder={fallback ? `默认 ${fallback}` : '跟随默认'}
              value={baseline ? draft[field.key] : ''}
            />
          </Field>
          <Desc indent={false}>{field.hint}</Desc>
        </div>;
      })}
    </div>
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <Button disabled={!baseline || problems.length > 0 || !dirty || loading || conflict} loading={saving} onClick={() => void save()}>保存压缩设置</Button>
      <Button tone="quiet" disabled={!token || saving} loading={loading} onClick={() => { if (confirmDiscard()) void load(); }}>重新载入压缩设置</Button>
    </div>
    <Footnote>留空的项跟随默认，输入框里的灰字就是默认值；保存只把改过的项写进本实例的 runtime.yaml。查得到模型的上下文窗口时（节点的 context_window、engine.compact.context_windows 或内置的常见模型表），两个阈值还会按窗口收紧。</Footnote>
    {problems.map(problem => <ErrorText key={problem} role="alert">{problem}</ErrorText>)}
    {error && <ErrorText role="alert">{error}</ErrorText>}
    {notice && <p className="mt-2 text-xs text-[var(--duties-secondary)]" role="status">{notice}</p>}
  </Block>;
}

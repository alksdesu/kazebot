// 高级配置：runtime.yaml 本实例那一层（表单和原文是同一份），以及 policy.yaml 的原文兜底。
import { useMemo, useRef, useState } from 'react';

import { errorMessage } from '../../../api/request';
import { getPolicyRaw, getRuntimeFile, isEditConflict, resetRuntime, updatePolicyRaw, updateRuntimeRaw, type RuntimeFile } from '../../../api/supervisorClient';
import { runtimeInstanceLayer } from '../../../console/yamlCommit';
import { unsavableReason, useRawForm, type RawFormCodec } from '../../../hooks/useRawForm';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { savedMessage } from '../../../utils/sentence';
import { YamlEditor } from '../../common';
import { RawFormStatus, RawSourceDetails } from '../RawSourceDetails';
import {
  hasLikelyYamlSyntaxIssue,
  parseRuntimeConfig,
  serializeRuntimeConfig,
  type EngineToolMode,
  type RuntimeConfigFormState,
} from '../settingsStructuredConfig';
import { Block, Button, ErrorText, PageShell, StatusText } from './settingsControls';

type FileKey = 'runtime' | 'policy';

const FILES: Array<{ key: FileKey; title: string; description: string }> = [
  { key: 'runtime', title: '运行时配置 (runtime.yaml)', description: '运行时参数、入口节点、工具模式、记忆和进程配置。' },
  { key: 'policy', title: '安全策略 (policy.yaml)', description: '原始 YAML 兜底。读写规则和敏感路径请在「工具与权限」页改，这里用于 deny_patterns 等结构化编辑覆盖不到的字段。' },
];

// runtime 逐键盖在仓库默认上面：这里编辑的只是本实例那一层，没写到的键跟着仓库更新。
const RUNTIME_SOURCE_HINT: Record<RuntimeFile['source'], string> = {
  default: '本实例还没有自己的 config/runtime.yaml，下面的原文是空的本实例层：只写要改的键，其余跟随仓库默认。保存后存成 config/runtime.yaml。',
  override: '本实例有自己的 config/runtime.yaml，逐键盖在仓库默认上面；没写到的键跟随仓库更新。',
};

const STRUCTURED_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const STRUCTURED_LABEL_CLASS = 'block mb-1 text-[var(--duties-tertiary)] text-mini';

const EMPTY_RUNTIME_CONFIG_FORM: RuntimeConfigFormState = {
  entry_node_id: '',
  tool_mode: '',
  max_workers: '',
  compact_threshold_tokens: '',
  compact_hard_threshold_tokens: '',
  compact_keep_recent_tokens: '',
  compact_keep_recent: '',
};

const RUNTIME_CODEC: RawFormCodec<RuntimeConfigFormState> = { parse: parseRuntimeConfig, serialize: serializeRuntimeConfig };

const conflictMessage = (filename: string) => `${filename} 在打开之后被别处改过，没有保存：点「加载」读入最新内容，再把改动重做一遍。`;

/** 一次读写的结果。失败的按错误显示，和成功的提示分开。 */
interface Outcome {
  text: string;
  failed: boolean;
}

const NO_OUTCOME: Outcome = { text: '', failed: false };

const failed = (text: string): Outcome => ({ text, failed: true });

const OutcomeText = ({ outcome }: { outcome: Outcome }) => (
  outcome.failed ? <ErrorText role="alert">{outcome.text}</ErrorText> : <StatusText message={outcome.text} />
);

const DirtyMark = ({ dirty }: { dirty: boolean }) => (
  dirty ? <p role="status" className="text-sm text-[var(--duties-secondary)]">有未保存修改</p> : null
);

const CompactNumberField = ({ hint, label, min, onChange, placeholder, value }: {
  hint: string;
  label: string;
  min: string;
  onChange: (value: string) => void;
  placeholder: string;
  value: string;
}) => (
  <label className="block">
    <span className={STRUCTURED_LABEL_CLASS}>{label}</span>
    <input className={STRUCTURED_INPUT_CLASS} min={min} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} type="number" value={value} />
    <span className="mt-1 block text-mini leading-4 text-[var(--duties-tertiary)]">{hint}</span>
  </label>
);

interface RuntimeFileState {
  loaded: boolean;
  loading: boolean;
  base: string | null;
  source: RuntimeFile['source'] | '';
  /** 仓库默认那份原文。本实例还没有自己那份时后端读回来的就是它，有了自己那份就看不到了。 */
  defaults: string;
  outcome: Outcome;
}

const RUNTIME_IDLE: RuntimeFileState = { loaded: false, loading: false, base: null, source: '', defaults: '', outcome: NO_OUTCOME };

const parseDefaults = (text: string): RuntimeConfigFormState | null => {
  try {
    return parseRuntimeConfig(text);
  } catch {
    return null;
  }
};

const RuntimeFileBody = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const source = useRawForm(RUNTIME_CODEC, EMPTY_RUNTIME_CONFIG_FORM);
  const { form, reset, update } = source;
  const [file, setFile] = useState<RuntimeFileState>(RUNTIME_IDLE);
  // 同步挡住连点：状态要等下一次渲染才看得到。
  const busy = useRef(false);
  const patch = (next: Partial<RuntimeFileState>) => setFile((current) => ({ ...current, ...next }));
  const defaults = useMemo(() => (file.defaults ? parseDefaults(file.defaults) : null), [file.defaults]);
  const dirty = file.loaded && source.dirty;
  useUnsavedChanges(dirty, '高级配置还有未保存修改，离开会丢失这些草稿。确定继续吗？');

  const placeholder = (key: Exclude<keyof RuntimeConfigFormState, 'tool_mode'>): string => (
    defaults?.[key] ? `仓库默认 ${defaults[key]}` : '留空跟随仓库默认'
  );

  const adopt = (runtime: RuntimeFile) => {
    reset(runtimeInstanceLayer(runtime));
    patch({ loaded: true, base: runtime.hash, source: runtime.source, defaults: runtime.source === 'default' ? runtime.content : '' });
  };

  const run = async (work: () => Promise<Outcome>) => {
    busy.current = true;
    patch({ loading: true, outcome: NO_OUTCOME });
    try {
      patch({ outcome: await work() });
    } finally {
      busy.current = false;
      patch({ loading: false });
    }
  };

  const load = async () => {
    if (!adminToken || busy.current) return;
    if (dirty && !window.confirm('重新加载 runtime.yaml 会丢弃这个文件的未保存修改，确定继续吗？')) return;
    await run(async () => {
      try {
        adopt(await getRuntimeFile(adminToken));
        return NO_OUTCOME;
      } catch (error) {
        return failed(errorMessage(error, '加载失败'));
      }
    });
  };

  const save = async () => {
    if (!adminToken || busy.current) return;
    if (!file.loaded) { patch({ outcome: failed('请先点「加载」读入当前内容，再保存') }); return; }
    const { raw, note } = source;
    const blocked = unsavableReason(source)
      || (raw.trim() ? '' : '运行时配置不能为空，原文件未修改。想全部跟随仓库默认，点「恢复默认」。');
    if (blocked) { patch({ outcome: failed(blocked) }); return; }
    await run(async () => {
      try {
        const result = await updateRuntimeRaw(adminToken, raw, file.base);
        reset(raw);
        patch({ base: result.hash, source: 'override' });
        return { text: savedMessage('已保存', note, result.warnings), failed: false };
      } catch (error) {
        return failed(isEditConflict(error) ? conflictMessage('runtime.yaml') : errorMessage(error, '保存失败'));
      }
    });
  };

  const resetToDefault = async () => {
    if (!adminToken || busy.current || file.source !== 'override') return;
    const unsaved = dirty ? '未保存的修改也会一起丢掉。' : '';
    if (!window.confirm(`删掉本实例的 config/runtime.yaml，全部参数恢复成仓库默认？${unsaved}`)) return;
    await run(async () => {
      try {
        await resetRuntime(adminToken);
        adopt(await getRuntimeFile(adminToken));
        return { text: '已恢复成仓库默认', failed: false };
      } catch (error) {
        return failed(errorMessage(error, '恢复默认失败'));
      }
    });
  };

  const followDefault = defaults?.tool_mode ? `跟随仓库默认（${defaults.tool_mode}）` : '跟随仓库默认';

  return (
    <fieldset disabled={file.loading} className="mt-3 min-w-0 space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button tone="quiet" disabled={file.loading} onClick={() => void load()}>{file.loading ? '正在加载…' : '加载'}</Button>
        <Button disabled={file.loading || !file.loaded || !source.raw.trim()} onClick={() => void save()}>保存运行时配置</Button>
        <Button tone="quiet" disabled={file.loading || file.source !== 'override'} onClick={() => void resetToDefault()}>恢复默认</Button>
      </div>
      {file.loaded && file.source && (
        <p className="text-mini leading-4 text-[var(--duties-tertiary)]">{RUNTIME_SOURCE_HINT[file.source]}</p>
      )}
      <fieldset disabled={!file.loaded || Boolean(source.parseError)} className="min-w-0 space-y-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-sm leading-5">
        <label className="block">
          <span className={STRUCTURED_LABEL_CLASS}>入口节点 ID</span>
          <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => update({ entry_node_id: event.target.value })} placeholder={placeholder('entry_node_id')} value={form.entry_node_id} />
        </label>
        <label className="block">
          <span className={STRUCTURED_LABEL_CLASS}>工具调用格式</span>
          <select className={STRUCTURED_INPUT_CLASS} onChange={(event) => update({ tool_mode: event.target.value as EngineToolMode | '' })} value={form.tool_mode}>
            <option value="">{followDefault}</option>
            <option value="fake-native">fake-native — 文本化工具调用，兼容性最好</option>
            <option value="native">native — 走 provider 原生 tool calling</option>
            <option value="json">json — 要求模型输出 JSON</option>
          </select>
        </label>
        <label className="block">
          <span className={STRUCTURED_LABEL_CLASS}>并发 task 处理数，留空使用默认值</span>
          <input className={STRUCTURED_INPUT_CLASS} min="1" onChange={(event) => update({ max_workers: event.target.value })} placeholder={placeholder('max_workers')} type="number" value={form.max_workers} />
        </label>
        <div className="space-y-3 border-t border-[var(--duties-border)] pt-3">
          <p className="font-mono text-mini font-semibold text-[var(--duties-text)]">上下文压缩</p>
          <p className="text-mini leading-4 text-[var(--duties-tertiary)]">对话超过阈值后自动摘要历史。改完下一个任务就生效，不用重启。全部留空则使用代码默认值。</p>
          <CompactNumberField
            hint="超过就在后台静默压缩，当轮回复照常进行。填 0 关闭自动压缩。"
            label="软阈值（token）"
            min="0"
            onChange={(value) => update({ compact_threshold_tokens: value })}
            placeholder={placeholder('compact_threshold_tokens')}
            value={form.compact_threshold_tokens}
          />
          <CompactNumberField
            hint="超过则挂起当轮同步压缩，用户会多等一次调用。填 0 表示取软阈值的 1.25 倍。"
            label="硬阈值（token）"
            min="0"
            onChange={(value) => update({ compact_hard_threshold_tokens: value })}
            placeholder={placeholder('compact_hard_threshold_tokens')}
            value={form.compact_hard_threshold_tokens}
          />
          <CompactNumberField
            hint="压缩时保留多少最新原文，更早的压成摘要。填 0 则改按下面的段数保留。"
            label="保留原文（token）"
            min="0"
            onChange={(value) => update({ compact_keep_recent_tokens: value })}
            placeholder={placeholder('compact_keep_recent_tokens')}
            value={form.compact_keep_recent_tokens}
          />
          <CompactNumberField
            hint="仅在上一项为 0 时生效：保留最近几个完整任务段。最小 2。"
            label="保留段数"
            min="2"
            onChange={(value) => update({ compact_keep_recent: value })}
            placeholder={placeholder('compact_keep_recent')}
            value={form.compact_keep_recent}
          />
        </div>
      </fieldset>
      <RawFormStatus source={source} />
      <RawSourceDetails height="18rem" label="runtime.yaml YAML 编辑器" readOnly={!file.loaded || file.loading} source={source} summary="高级 YAML 编辑" />
      {file.defaults && (
        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
          <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-tertiary)]">仓库默认 defaults/runtime.yaml（只读）</summary>
          <div className="mt-3">
            <YamlEditor aria-label="仓库默认 runtime.yaml" height="18rem" onChange={() => undefined} readOnly value={file.defaults} />
          </div>
        </details>
      )}
      <OutcomeText outcome={file.outcome} />
      <DirtyMark dirty={dirty} />
    </fieldset>
  );
};

interface PolicyFileState {
  value: string;
  saved: string;
  loaded: boolean;
  loading: boolean;
  base: string | null;
  outcome: Outcome;
}

const POLICY_IDLE: PolicyFileState = { value: '', saved: '', loaded: false, loading: false, base: null, outcome: NO_OUTCOME };

const PolicyFileBody = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [file, setFile] = useState<PolicyFileState>(POLICY_IDLE);
  const busy = useRef(false);
  const patch = (next: Partial<PolicyFileState>) => setFile((current) => ({ ...current, ...next }));
  const dirty = file.loaded && file.value !== file.saved;
  useUnsavedChanges(dirty, '高级配置还有未保存修改，离开会丢失这些草稿。确定继续吗？');

  const run = async (work: () => Promise<Outcome>) => {
    busy.current = true;
    patch({ loading: true, outcome: NO_OUTCOME });
    try {
      patch({ outcome: await work() });
    } finally {
      busy.current = false;
      patch({ loading: false });
    }
  };

  const load = async () => {
    if (!adminToken || busy.current) return;
    if (dirty && !window.confirm('重新加载 policy.yaml 会丢弃这个文件的未保存修改，确定继续吗？')) return;
    await run(async () => {
      try {
        const { content, hash } = await getPolicyRaw(adminToken);
        patch({ value: content, saved: content, loaded: true, base: hash });
        return NO_OUTCOME;
      } catch (error) {
        return failed(errorMessage(error, '加载失败'));
      }
    });
  };

  const save = async () => {
    if (!adminToken || busy.current) return;
    if (!file.loaded || !file.value) { patch({ outcome: failed('请先点「加载」读入当前内容，再保存') }); return; }
    if (!window.confirm('修改安全策略可能影响系统安全性')) return;
    const { value } = file;
    const issue = hasLikelyYamlSyntaxIssue(value);
    if (issue) { patch({ outcome: failed(issue) }); return; }
    await run(async () => {
      try {
        const result = await updatePolicyRaw(adminToken, value, file.base);
        patch({ saved: value, base: result.hash });
        return { text: savedMessage('已保存', '', result.warnings), failed: false };
      } catch (error) {
        return failed(isEditConflict(error) ? conflictMessage('policy.yaml') : errorMessage(error, '保存失败'));
      }
    });
  };

  return (
    <fieldset disabled={file.loading} className="mt-3 min-w-0 space-y-3">
      <p className="text-xs leading-5 text-[var(--duties-danger)]">警告：修改安全策略可能影响系统安全性。</p>
      <div className="flex flex-wrap gap-2">
        <Button tone="quiet" disabled={file.loading} onClick={() => void load()}>{file.loading ? '正在加载…' : '加载'}</Button>
        <Button disabled={file.loading || !file.loaded || !file.value} onClick={() => void save()}>保存策略 YAML</Button>
      </div>
      <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
        <summary className="cursor-pointer font-mono text-mini font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
        <div className="mt-3">
          <YamlEditor aria-label="policy.yaml YAML 编辑器" height="26rem" readOnly={!file.loaded || file.loading} onChange={(value) => patch({ value })} value={file.value} />
        </div>
      </details>
      <OutcomeText outcome={file.outcome} />
      <DirtyMark dirty={dirty} />
    </fieldset>
  );
};

export const AdvancedSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { setAdvancedFile } = useSettingsSelectionStore();
  return (
    <PageShell authed={isAuthenticated} description="运行时配置使用结构化表单编辑，安全策略保留在高级 YAML 折叠区内编辑。定时任务请在自动化页面管理。" title="高级配置">
      <div className="space-y-4">
        {FILES.map((file) => (
          <Block frame="boxed" hint={file.description} key={file.key}>
            <details onToggle={(event) => { if ((event.currentTarget as HTMLDetailsElement).open) setAdvancedFile(file.key); }}>
              <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-text)]">{file.title}</summary>
              {/* 换了令牌整块重建：上一个身份还没回来的读写落不到新身份的草稿上。 */}
              {file.key === 'runtime' ? <RuntimeFileBody key={adminToken || ''} /> : <PolicyFileBody key={adminToken || ''} />}
            </details>
          </Block>
        ))}
      </div>
    </PageShell>
  );
};

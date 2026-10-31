// 高级配置：runtime.yaml 本实例那一层（表单和原文是同一份），以及 policy.yaml 的原文兜底。
import { useMemo, useRef, useState } from 'react';

import { errorMessage } from '../../../api/request';
import { getPolicyRaw, getRuntimeFile, getRuntimeLayers, isEditConflict, receipted, resetRuntime, updatePolicyRaw, updateRuntimeRaw, type RuntimeFile, type RuntimeLayers } from '../../../api/supervisorClient';
import { runtimeInstanceLayer } from '../../../console/yamlCommit';
import { unsavableReason, useRawForm, type RawFormCodec } from '../../../hooks/useRawForm';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { appendSentence, reloadBeforeEditing, rereadMissed, savedMessage } from '../../../utils/sentence';
import { YamlEditor } from '../../common';
import { RawFormStatus, RawSourceDetails } from '../RawSourceDetails';
import {
  hasLikelyYamlSyntaxIssue,
  parseRuntimeConfig,
  runtimeConfigOf,
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
};

const RUNTIME_CODEC: RawFormCodec<RuntimeConfigFormState> = { parse: parseRuntimeConfig, serialize: serializeRuntimeConfig };

const conflictMessage = (filename: string) => `${filename} 在打开之后被别处改过，没有保存：点「加载」读入最新内容，再把改动重做一遍。`;

/** 一次读写的结果。失败的按错误显示，和成功的提示分开；problem 是做成了、但还有要交代的问题。 */
interface Outcome {
  text: string;
  failed: boolean;
  problem?: string;
}

const NO_OUTCOME: Outcome = { text: '', failed: false };

const failed = (text: string): Outcome => ({ text, failed: true });

const OutcomeText = ({ outcome }: { outcome: Outcome }) => (
  outcome.failed ? <ErrorText role="alert">{outcome.text}</ErrorText> : (
    <>
      <StatusText message={outcome.text} />
      {outcome.problem && <ErrorText role="alert">{outcome.problem}</ErrorText>}
    </>
  )
);

/** 存上了、回执没读懂：照「加载」重读一遍换上落盘那一版的指纹。重读也没成就留着旧指纹，下次保存撞 409，不会整份盖掉。 */
async function reloadAfterUnreadReceipt(receiptProblem: string, reload: () => Promise<void>): Promise<string> {
  try {
    await reload();
    return receiptProblem;
  } catch (error) {
    return appendSentence(reloadBeforeEditing(receiptProblem, '点「加载」读入最新内容'), rereadMissed(errorMessage(error)));
  }
}

const DirtyMark = ({ dirty }: { dirty: boolean }) => (
  dirty ? <p role="status" className="text-sm text-[var(--duties-secondary)]">有未保存修改</p> : null
);

interface RuntimeFileState {
  loaded: boolean;
  loading: boolean;
  base: string | null;
  source: RuntimeFile['source'] | '';
  /** 本实例没写的键跟着的那一层（代码默认 + 仓库默认），有没有自己那份都读得到。 */
  defaults: RuntimeLayers['defaults'] | null;
  /** 仓库默认那份原文。本实例还没有自己那份时后端读回来的就是它，有了自己那份就看不到了。 */
  bundled: string;
  outcome: Outcome;
}

const RUNTIME_IDLE: RuntimeFileState = {
  loaded: false, loading: false, base: null, source: '', defaults: null, bundled: '', outcome: NO_OUTCOME,
};

const RuntimeFileBody = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const source = useRawForm(RUNTIME_CODEC, EMPTY_RUNTIME_CONFIG_FORM);
  const { form, reset, update } = source;
  const [file, setFile] = useState<RuntimeFileState>(RUNTIME_IDLE);
  // 同步挡住连点：状态要等下一次渲染才看得到。
  const busy = useRef(false);
  const patch = (next: Partial<RuntimeFileState>) => setFile((current) => ({ ...current, ...next }));
  const defaults = useMemo(() => (file.defaults ? runtimeConfigOf(file.defaults) : null), [file.defaults]);
  const dirty = file.loaded && source.dirty;
  useUnsavedChanges(dirty, '高级配置还有未保存修改，离开会丢失这些草稿。确定继续吗？');

  const placeholder = (key: Exclude<keyof RuntimeConfigFormState, 'tool_mode'>): string => (
    defaults?.[key] ? `仓库默认 ${defaults[key]}` : '留空跟随仓库默认'
  );

  const adopt = (runtime: RuntimeLayers) => {
    reset(runtimeInstanceLayer(runtime));
    patch({
      loaded: true,
      base: runtime.hash,
      source: runtime.source,
      defaults: runtime.defaults,
      bundled: runtime.source === 'default' ? runtime.content : '',
    });
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
        adopt(await getRuntimeLayers(adminToken));
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
        const { result, receiptProblem } = await receipted(updateRuntimeRaw(adminToken, raw, file.base));
        reset(raw);
        patch({ source: 'override' });
        if (result) {
          patch({ base: result.hash });
          return { text: savedMessage('已保存', note, result.warnings), failed: false };
        }
        const problem = await reloadAfterUnreadReceipt(receiptProblem, async () => {
          const runtime = await getRuntimeFile(adminToken);
          reset(runtimeInstanceLayer(runtime));
          patch({ base: runtime.hash, source: runtime.source });
        });
        return { text: savedMessage('已保存', note), failed: false, problem };
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
      let receiptProblem: string;
      try {
        ({ receiptProblem } = await receipted(resetRuntime(adminToken)));
      } catch (error) {
        return failed(errorMessage(error, '恢复默认失败'));
      }
      // 已经删了：紧接着那次重读没成不能报成恢复失败，手上的指纹作废了，得先加载再改。
      const problem = await reloadAfterUnreadReceipt(receiptProblem, async () => { adopt(await getRuntimeLayers(adminToken)); });
      return { text: '已恢复成仓库默认', failed: false, problem };
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
      </fieldset>
      <RawFormStatus source={source} />
      <RawSourceDetails height="18rem" label="runtime.yaml YAML 编辑器" readOnly={!file.loaded || file.loading} source={source} summary="高级 YAML 编辑" />
      {file.bundled && (
        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
          <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-tertiary)]">仓库默认 defaults/runtime.yaml（只读）</summary>
          <div className="mt-3">
            <YamlEditor aria-label="仓库默认 runtime.yaml" height="18rem" onChange={() => undefined} readOnly value={file.bundled} />
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
        const { result, receiptProblem } = await receipted(updatePolicyRaw(adminToken, value, file.base));
        patch({ saved: value });
        if (result) {
          patch({ base: result.hash });
          return { text: savedMessage('已保存', '', result.warnings), failed: false };
        }
        const problem = await reloadAfterUnreadReceipt(receiptProblem, async () => {
          const { content, hash } = await getPolicyRaw(adminToken);
          patch({ value: content, saved: content, base: hash });
        });
        return { text: '已保存', failed: false, problem };
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
    <PageShell authed={isAuthenticated} description="运行时配置使用结构化表单编辑，安全策略保留在高级 YAML 折叠区内编辑。定时任务请在自动化页面管理，上下文压缩请在记忆页面设置。" title="高级配置">
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

// Advanced configuration supports structured fields and explicit raw YAML editing.
import { useEffect, useMemo, useRef, useState } from 'react';

import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { getPolicyRaw, getRuntimeRaw, updatePolicyRaw, updateRuntimeRaw } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { parseRuntimeConfig, serializeRuntimeConfig, type EngineToolMode, type RuntimeConfigFormState } from '../settingsStructuredConfig';
import { Button, YamlEditor } from '../../common';
import { AuthRequired, Card, PageHeader, PageShell, StatusText, hasLikelyYamlSyntaxIssue } from './settingsPagePrimitives';

type FileKey = 'runtime' | 'policy';

interface RawFileState {
  value: string;
  message: string;
  loading: boolean;
  loaded: boolean;
  saved: string;
}

const FILES: Array<{ key: FileKey; title: string; filename: string; description: string }> = [
  { key: 'runtime', title: '运行时配置 (runtime.yaml)', filename: 'runtime.yaml', description: '运行时参数、入口节点、工具模式、记忆和进程配置。' },
  { key: 'policy', title: '安全策略 (policy.yaml)', filename: 'policy.yaml', description: '原始 YAML 兜底。读写规则和敏感路径请在「工具与权限」页改，这里用于 deny_patterns 等结构化编辑覆盖不到的字段。' },
];

const STRUCTURED_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const STRUCTURED_LABEL_CLASS = 'block mb-1 text-[var(--duties-tertiary)] text-[0.65rem]';

const EMPTY_RUNTIME_CONFIG_FORM: RuntimeConfigFormState = {
  entry_node_id: '',
  tool_mode: 'fake-native',
  max_workers: '',
  compact_threshold_tokens: '',
  compact_hard_threshold_tokens: '',
  compact_keep_recent_tokens: '',
  compact_keep_recent: '',
};

const CompactNumberField = ({ hint, label, min, onChange, value }: {
  hint: string;
  label: string;
  min: string;
  onChange: (value: string) => void;
  value: string;
}) => (
  <label className="block">
    <span className={STRUCTURED_LABEL_CLASS}>{label}</span>
    <input className={STRUCTURED_INPUT_CLASS} min={min} onChange={(event) => onChange(event.target.value)} type="number" value={value} />
    <span className="mt-1 block text-[0.65rem] leading-4 text-[var(--duties-tertiary)]">{hint}</span>
  </label>
);

export const AdvancedSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { setAdvancedFile } = useSettingsSelectionStore();
  const [files, setFiles] = useState<Record<FileKey, RawFileState>>({
    runtime: { value: '', saved: '', message: '', loading: false, loaded: false },
    policy: { value: '', saved: '', message: '', loading: false, loaded: false },
  });
  const [runtimeConfigForm, setRuntimeConfigForm] = useState<RuntimeConfigFormState>(EMPTY_RUNTIME_CONFIG_FORM);

  const [runtimeBaseline, setRuntimeBaseline] = useState(EMPTY_RUNTIME_CONFIG_FORM);
  const [runtimeFormEdited, setRuntimeFormEdited] = useState(false);
  const operations = useRef({ runtime: { id: 0, busy: false }, policy: { id: 0, busy: false } });
  const mounted = useRef(true);
  const currentAuth = useRef(adminToken);
  currentAuth.current = adminToken;
  const fileDirty = (key: FileKey) => files[key].value !== files[key].saved || (key === 'runtime' && JSON.stringify(runtimeConfigForm) !== JSON.stringify(runtimeBaseline));
  useUnsavedChanges(fileDirty('runtime') || fileDirty('policy'), '高级配置还有未保存修改，离开会丢失这些草稿。确定继续吗？');

  useEffect(() => {
    mounted.current = true;
    for (const operation of Object.values(operations.current)) { operation.id++; operation.busy = false; }
    setFiles({
      runtime: { value: '', saved: '', message: '', loading: false, loaded: false },
      policy: { value: '', saved: '', message: '', loading: false, loaded: false },
    });
    setRuntimeConfigForm(EMPTY_RUNTIME_CONFIG_FORM); setRuntimeBaseline(EMPTY_RUNTIME_CONFIG_FORM); setRuntimeFormEdited(false);
    return () => {
      mounted.current = false;
      for (const operation of Object.values(operations.current)) { operation.id++; operation.busy = false; }
    };
  }, [adminToken, isAuthenticated]);

  const setFileState = (key: FileKey, patch: Partial<RawFileState>) => setFiles(current => ({ ...current, [key]: { ...current[key], ...patch } }));
  const isCurrent = (key: FileKey, id: number) => mounted.current && currentAuth.current === adminToken && operations.current[key].id === id;
  const begin = (key: FileKey) => {
    const operation = operations.current[key]; operation.busy = true; operation.id++;
    setFileState(key, { loading: true, message: '' }); return operation.id;
  };
  const finish = (key: FileKey, id: number) => { if (isCurrent(key, id)) { operations.current[key].busy = false; setFileState(key, { loading: false }); } };
  const updateRuntimeConfigForm = (patch: Partial<RuntimeConfigFormState>) => {
    if (!files.runtime.loaded || operations.current.runtime.busy) return;
    setRuntimeConfigForm(current => ({ ...current, ...patch })); setRuntimeFormEdited(true);
  };
  const runtimeEditorValue = useMemo(() => {
    if (!runtimeFormEdited) return files.runtime.value;
    try { return serializeRuntimeConfig(files.runtime.value, runtimeConfigForm); } catch { return files.runtime.value; }
  }, [files.runtime.value, runtimeConfigForm, runtimeFormEdited]);
  const editRaw = (key: FileKey, value: string) => {
    if (!files[key].loaded || operations.current[key].busy) return;
    setFileState(key, { value, message: '' });
    if (key === 'runtime') {
      setRuntimeFormEdited(false);
      try { setRuntimeConfigForm(parseRuntimeConfig(value)); } catch {}
    }
  };

  const loadOne = async (key: FileKey) => {
    if (!adminToken || !isAuthenticated || operations.current[key].busy) return;
    if (fileDirty(key) && !window.confirm(`重新加载 ${key}.yaml 会丢弃这个文件的未保存修改，确定继续吗？`)) return;
    const id = begin(key);
    try {
      const raw = key === 'runtime' ? await getRuntimeRaw(adminToken) : await getPolicyRaw(adminToken);
      if (!isCurrent(key, id)) return;
      setFileState(key, { value: raw, saved: raw, loaded: true, message: '' });
      if (key === 'runtime') {
        setRuntimeFormEdited(false); setRuntimeConfigForm(EMPTY_RUNTIME_CONFIG_FORM); setRuntimeBaseline(EMPTY_RUNTIME_CONFIG_FORM);
        const form = parseRuntimeConfig(raw);
        setRuntimeConfigForm(form); setRuntimeBaseline(form);
      }
    } catch (error) {
      if (isCurrent(key, id)) setFileState(key, { message: error instanceof Error ? error.message : '加载失败' });
    } finally { finish(key, id); }
  };

  const saveOne = async (key: FileKey) => {
    if (!adminToken || !isAuthenticated || operations.current[key].busy) return;
    if (!files[key].loaded || !files[key].value) { setFileState(key, { message: '请先点「加载」读入当前内容，再保存' }); return; }
    if (key === 'policy' && !window.confirm('修改安全策略可能影响系统安全性')) return;
    const id = begin(key);
    try {
      const value = key === 'runtime' && runtimeFormEdited ? serializeRuntimeConfig(files.runtime.value, runtimeConfigForm) : files[key].value;
      if (key === 'runtime' && !value.trim()) throw new Error('运行时配置不能为空，原文件未修改');
      const issue = hasLikelyYamlSyntaxIssue(value);
      if (issue) throw new Error(issue);
      const form = key === 'runtime' ? parseRuntimeConfig(value) : null;
      if (key === 'runtime') await updateRuntimeRaw(adminToken, value);
      else await updatePolicyRaw(adminToken, value);
      if (!isCurrent(key, id)) return;
      setFileState(key, { value, saved: value, message: '已保存' });
      if (form) { setRuntimeConfigForm(form); setRuntimeBaseline(form); setRuntimeFormEdited(false); }
    } catch (error) {
      if (isCurrent(key, id)) setFileState(key, { message: error instanceof Error ? error.message : '保存失败' });
    } finally { finish(key, id); }
  };

  return (
    <PageShell>
      <PageHeader description="运行时配置使用结构化表单编辑，安全策略保留在高级 YAML 折叠区内编辑。定时任务请在自动化页面管理。" title="高级配置" />
      {!isAuthenticated ? <AuthRequired /> : (
        <div className="space-y-4">
          {FILES.map((file) => {
            const state = files[file.key];
            return (
              <Card description={file.description} key={file.key}>
                <details onToggle={(event) => { if ((event.currentTarget as HTMLDetailsElement).open) setAdvancedFile(file.key); }}>
                  <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-text)]">{file.title}</summary>
                  <fieldset disabled={state.loading} className="mt-3 min-w-0 space-y-3">
                    {file.key === 'runtime' ? (
                      <>
                        <div className="flex flex-wrap gap-2"><Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : '加载'}</Button><Button disabled={state.loading || !state.loaded || !state.value} onClick={() => saveOne(file.key)} variant="primary">保存运行时配置</Button></div>
                        <fieldset disabled={!state.loaded} className="min-w-0 space-y-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-sm leading-5">
                          <label className="block">
                            <span className={STRUCTURED_LABEL_CLASS}>入口节点 ID</span>
                            <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateRuntimeConfigForm({ entry_node_id: event.target.value })} value={runtimeConfigForm.entry_node_id} />
                          </label>
                          <label className="block">
                            <span className={STRUCTURED_LABEL_CLASS}>工具调用格式</span>
                            <select className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateRuntimeConfigForm({ tool_mode: event.target.value as EngineToolMode })} value={runtimeConfigForm.tool_mode}>
                              <option value="fake-native">fake-native — 文本化工具调用，兼容性最好</option>
                              <option value="native">native — 走 provider 原生 tool calling</option>
                              <option value="json">json — 要求模型输出 JSON</option>
                            </select>
                          </label>
                          <label className="block">
                            <span className={STRUCTURED_LABEL_CLASS}>并发 task 处理数，留空使用默认值</span>
                            <input className={STRUCTURED_INPUT_CLASS} min="1" onChange={(event) => updateRuntimeConfigForm({ max_workers: event.target.value })} type="number" value={runtimeConfigForm.max_workers} />
                          </label>
                          <div className="space-y-3 border-t border-[var(--duties-border)] pt-3">
                            <p className="font-mono text-[0.65rem] font-semibold text-[var(--duties-text)]">上下文压缩</p>
                            <p className="text-[0.65rem] leading-4 text-[var(--duties-tertiary)]">对话超过阈值后自动摘要历史。改完下一个任务就生效，不用重启。全部留空则使用代码默认值。</p>
                            <CompactNumberField
                              hint="超过就在后台静默压缩，当轮回复照常进行。填 0 关闭自动压缩。"
                              label="软阈值（token）"
                              min="0"
                              onChange={(value) => updateRuntimeConfigForm({ compact_threshold_tokens: value })}
                              value={runtimeConfigForm.compact_threshold_tokens}
                            />
                            <CompactNumberField
                              hint="超过则挂起当轮同步压缩，用户会多等一次调用。填 0 表示取软阈值的 1.25 倍。"
                              label="硬阈值（token）"
                              min="0"
                              onChange={(value) => updateRuntimeConfigForm({ compact_hard_threshold_tokens: value })}
                              value={runtimeConfigForm.compact_hard_threshold_tokens}
                            />
                            <CompactNumberField
                              hint="压缩时保留多少最新原文，更早的压成摘要。填 0 则改按下面的段数保留。"
                              label="保留原文（token）"
                              min="0"
                              onChange={(value) => updateRuntimeConfigForm({ compact_keep_recent_tokens: value })}
                              value={runtimeConfigForm.compact_keep_recent_tokens}
                            />
                            <CompactNumberField
                              hint="仅在上一项为 0 时生效：保留最近几个完整任务段。最小 2。"
                              label="保留段数"
                              min="2"
                              onChange={(value) => updateRuntimeConfigForm({ compact_keep_recent: value })}
                              value={runtimeConfigForm.compact_keep_recent}
                            />
                          </div>
                        </fieldset>
                        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                          <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
                          <div className="mt-3">
                            <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height="18rem" readOnly={!state.loaded || state.loading} onChange={(value) => editRaw(file.key, value)} value={runtimeEditorValue} />
                          </div>
                        </details>
                      </>
                    ) : (
                      <>
                        <p className="text-xs leading-5 text-[var(--duties-danger)]">警告：修改安全策略可能影响系统安全性。</p>
                        <div className="flex flex-wrap gap-2"><Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : '加载'}</Button><Button disabled={state.loading || !state.loaded || !state.value} onClick={() => saveOne(file.key)} variant="primary">保存策略 YAML</Button></div>
                        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                          <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
                          <div className="mt-3">
                            <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height="26rem" readOnly={!state.loaded || state.loading} onChange={(value) => editRaw(file.key, value)} value={state.value} />
                          </div>
                        </details>
                      </>
                    )}
                    <StatusText message={state.message} />
                    {fileDirty(file.key) && <p role="status" className="text-sm text-[var(--duties-secondary)]">有未保存修改</p>}
                  </fieldset>
                </details>
              </Card>
            );
          })}
        </div>
      )}
    </PageShell>
  );
};

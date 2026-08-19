// [2026-06-02] Advanced raw configuration settings page.
// Why: runtime, policy, and schedules are cross-cutting YAML files that need direct
// editing for advanced operators. How: render accordion sections backed by raw admin
// endpoints and run lightweight checks before saving. Purpose: risky configuration
// edits remain centralized and visible without hiding backend validation results.
import { useEffect, useState } from 'react';

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
}

const FILES: Array<{ key: FileKey; title: string; filename: string; description: string }> = [
  { key: 'runtime', title: '运行时配置 (runtime.yaml)', filename: 'runtime.yaml', description: '运行时参数、入口节点、工具模式、记忆和进程配置。' },
  { key: 'policy', title: '安全策略 (policy.yaml)', filename: 'policy.yaml', description: '工具、文件、命令等安全策略配置。' },
];

// [2026-06-02] Shared compact field styles for the Advanced structured editor.
// Why: runtime.yaml is now edited primarily through direct form controls. How: keep
// the required input and label classes in constants reused by the runtime fields.
// Purpose: the structured form matches the requested Settings visual language.
const STRUCTURED_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const STRUCTURED_LABEL_CLASS = 'block mb-1 text-[var(--duties-tertiary)] text-[0.65rem]';

const EMPTY_RUNTIME_CONFIG_FORM: RuntimeConfigFormState = {
  entry_node_id: '',
  tool_mode: 'fake-native',
  max_workers: '',
};

export const AdvancedSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { setAdvancedFile } = useSettingsSelectionStore();
  const [files, setFiles] = useState<Record<FileKey, RawFileState>>({
    runtime: { value: '', message: '', loading: false },
    policy: { value: '', message: '', loading: false },
  });
  const [runtimeConfigForm, setRuntimeConfigForm] = useState<RuntimeConfigFormState>(EMPTY_RUNTIME_CONFIG_FORM);

  const setFileState = (key: FileKey, patch: Partial<RawFileState>) => {
    // [2026-06-02] Patch one raw file state entry without touching the other file.
    // Why: runtime and policy load independently inside separate accordion sections.
    // How: merge the provided partial state into the keyed entry. Purpose: structured
    // runtime saves and raw policy edits can share the same state container safely.
    setFiles((current) => ({ ...current, [key]: { ...current[key], ...patch } }));
  };

  const updateRuntimeConfigForm = (patch: Partial<RuntimeConfigFormState>) => {
    // [2026-06-02] Keep runtime field edits separate from raw YAML fallback edits.
    // Why: the main runtime editor is a form and the raw YAML area is only advanced
    // fallback. How: merge field-level changes into runtimeConfigForm. Purpose: save
    // can serialize the form into the loaded YAML while preserving unrelated fields.
    setRuntimeConfigForm((current) => ({ ...current, ...patch }));
  };

  const fetchRaw = async (key: FileKey): Promise<string> => {
    if (!adminToken) return '';
    if (key === 'runtime') return getRuntimeRaw(adminToken);
    return getPolicyRaw(adminToken);
  };

  const saveRaw = async (key: FileKey, value: string): Promise<void> => {
    if (!adminToken) return;
    if (key === 'runtime') { await updateRuntimeRaw(adminToken, value); return; }
    await updatePolicyRaw(adminToken, value);
  };

  const loadOne = async (key: FileKey) => {
    setFileState(key, { loading: true, message: '' });
    try {
      // [2026-06-02] Parse runtime YAML into form fields immediately after loading.
      // Why: runtime.yaml must no longer be presented as a primary raw editor. How:
      // keep the raw value for fallback and initialize the three structured fields
      // from parseRuntimeConfig. Purpose: the user can save common runtime settings
      // through the form while advanced YAML remains available below.
      const raw = await fetchRaw(key);
      setFileState(key, { value: raw, message: '' });
      if (key === 'runtime') setRuntimeConfigForm(parseRuntimeConfig(raw));
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '加载失败' });
    } finally {
      setFileState(key, { loading: false });
    }
  };

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return;
    // [2026-06-02] Do not auto-load raw configuration files on page mount.
    // Why: this P0 Advanced tab is an accordion whose sections should be collapsed by
    // default and loaded only when the operator asks. How: keep the authentication
    // effect as a no-op placeholder for future per-file preflight checks. Purpose:
    // opening Advanced does not fetch or expose raw configuration until requested.
  }, [adminToken, isAuthenticated]);

  const saveOne = async (key: FileKey) => {
    // 没加载过就保存 = 拿空字符串当基底序列化，163 行的 runtime.yaml 会被截成三行，
    // engine.tool_mode 还会静默翻回 fake-native。按钮也禁用了，这里是第二道。
    if (!files[key].value) {
      setFileState(key, { message: '请先点「加载」读入当前内容，再保存' });
      return;
    }
    if (key === 'policy' && !window.confirm('修改安全策略可能影响系统安全性')) return;
    // [2026-06-02] Save runtime through the form serializer and policy through raw YAML.
    // Why: runtime has stable common fields, while policy changes shape frequently.
    // How: serialize runtimeConfigForm into the loaded YAML only for runtime; keep
    // policy's raw editor as the saved value. Purpose: the main runtime path is a
    // structured form and policy remains an advanced fallback-oriented editor.
    const value = key === 'runtime' ? serializeRuntimeConfig(files.runtime.value, runtimeConfigForm) : files.policy.value;
    const issue = hasLikelyYamlSyntaxIssue(value);
    if (issue) { setFileState(key, { message: issue }); return; }
    setFileState(key, { loading: true, message: '' });
    try {
      await saveRaw(key, value);
      setFileState(key, { value, message: '已保存' });
      if (key === 'runtime') setRuntimeConfigForm(parseRuntimeConfig(value));
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '保存失败' });
    } finally {
      setFileState(key, { loading: false });
    }
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
                  <div className="mt-3 space-y-3">
                    {file.key === 'runtime' ? (
                      <>
                        <div className="flex flex-wrap gap-2"><Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : '加载'}</Button><Button disabled={state.loading || !state.value} onClick={() => saveOne(file.key)} variant="primary">保存运行时配置</Button></div>
                        <div className="space-y-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5">
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
                        </div>
                        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                          <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
                          <div className="mt-3">
                            {/* [2026-06-02] Keep runtime raw YAML only as a fallback editor.
                                Why: the requested primary runtime surface is a structured
                                form. How: place YamlEditor inside a collapsed details
                                section while saving still serializes the form fields into
                                this raw value. Purpose: operators can preserve uncommon
                                runtime settings without treating YAML as the main editor. */}
                            <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height="18rem" onChange={(value) => setFileState(file.key, { value })} value={state.value} />
                          </div>
                        </details>
                      </>
                    ) : (
                      <>
                        <p className="text-xs leading-5 text-orange-400">警告：修改安全策略可能影响系统安全性。</p>
                        <div className="flex flex-wrap gap-2"><Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : '加载'}</Button><Button disabled={state.loading || !state.value} onClick={() => saveOne(file.key)} variant="primary">保存策略 YAML</Button></div>
                        <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                          <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
                          <div className="mt-3">
                            {/* [2026-06-02] Keep policy editing raw but collapsed.
                                Why: policy.yaml has a variable safety-rule structure and
                                the task allows retaining YAML for this panel. How: move
                                the raw editor under an advanced details block and keep
                                the confirmation on save. Purpose: policy remains editable
                                without showing raw YAML as an always-open main editor. */}
                            <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height="26rem" onChange={(value) => setFileState(file.key, { value })} value={state.value} />
                          </div>
                        </details>
                      </>
                    )}
                    <StatusText message={state.message} />
                  </div>
                </details>
              </Card>
            );
          })}
        </div>
      )}
    </PageShell>
  );
};

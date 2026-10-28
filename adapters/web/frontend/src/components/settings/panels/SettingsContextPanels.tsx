// 设置页右栏：跟着当前选中的节点、工具、技能、MCP Client 和定时任务显示详情，并就地编辑。
import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';

import { errorMessage } from '../../../api/request';
import { channelChoices, getMcpClientsRaw, getNodeRaw, getNodes, getProviders, getSchedulesRaw, getSkillRaw, getToolRaw, isEditConflict, listUpstreamModels, receipted, reloadTools, updateMcpClientsRaw, updateNodeRaw, updateSchedulesRaw, updateSkillRaw, updateToolRaw, skillDir, type AdminSkill, type ChannelChoice, type OverlaySource, type ToolOrigin } from '../../../api/supervisorClient';
import { ChannelOptions, type ChannelTableState } from '../../../console/channelFields';
import { commitYamlKeys } from '../../../console/yamlCommit';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { unsavableReason, useRawForm, type RawFormCodec } from '../../../hooks/useRawForm';
import { useRequestGuard } from '../../../hooks/useRequestGuard';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { mergeModelChoices, modelsFromNodes, modelsFromProviders } from '../../../utils/modelChoices';
import { reloadBeforeEditing, savedMessage } from '../../../utils/sentence';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { Button } from '../pages/settingsControls';
import { RawFormStatus, RawSourceDetails } from '../RawSourceDetails';
import {
  mcpClientUpdateEdits,
  parseMcpClients,
  parseNodeConfig,
  parseSchedules,
  parseSkillQuickFields,
  scheduleUpdateEdits,
  serializeNodeConfig,
  serializeSkillQuickFields,
  type McpClientFormState,
  type NodeConfigFormState,
  type NodeConfigType,
  type ScheduleFormState,
  type SkillQuickFields,
} from '../settingsStructuredConfig';

const PanelShell = ({ title, eyebrow, disabled, children }: { title: string; eyebrow?: string; disabled?: boolean; children: ReactNode }) => (
  <section className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
    <h2 aria-label={eyebrow ? `${eyebrow}：${title}` : title} className="mb-3 text-base font-semibold">{title}</h2>
    <fieldset disabled={disabled} className="min-w-0">{children}</fieldset>
  </section>
);

interface EditorProps {
  onReload: () => void;
}

/** 右栏编辑器的「刷新」：换一代 key 让编辑器整个重建、重读文件，保存报冲突之后靠它换成最新内容。 */
function useReloadGeneration(): [number, () => void] {
  const [generation, setGeneration] = useState(0);
  return [generation, () => setGeneration((value) => value + 1)];
}

function staleItemMessage(label: string): string {
  const gap = /[A-Za-z0-9]$/.test(label) ? ' ' : '';
  return `${label}${gap}打开之后被别处改过，点「刷新」读入最新内容再改。`;
}

// 「刷新」会重建编辑器，刚保存的这句提示跟着没了，所以不替人点：留着旧指纹，下次保存撞 409，不会整份盖掉。
const unreadReceipt = (receiptProblem: string): string => reloadBeforeEditing(receiptProblem, '点「刷新」读入最新内容');

const JsonBlock = ({ value }: { value: unknown }) => (
  <pre className="max-h-80 overflow-auto border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 font-mono text-xs leading-4 text-[var(--duties-secondary)]">
    {JSON.stringify(value || {}, null, 2)}
  </pre>
);

// [2026-06-02] Shared classes for the structured settings form controls.
// Why: the node right panel now edits YAML-backed fields through direct inputs and
// must match the requested compact Settings styling. How: keep the exact input and
// label class names in constants used by the new controls. Purpose: new form fields
// stay visually consistent and are easy to audit against the task requirements.
const STRUCTURED_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const STRUCTURED_LABEL_CLASS = 'block mb-1 text-[var(--duties-tertiary)] text-xs';

// 编辑仓库默认的那份时说清楚保存落到哪：存成本实例自己的一份，从此不再随仓库更新。
const NODE_SOURCE_HINT: Partial<Record<OverlaySource, string>> = {
  default: '这是仓库默认节点。保存后会在 config/nodes 下存一份本实例的版本，从此不再随仓库更新。',
  override: '本实例改过的版本，存在 config/nodes 下，盖住仓库默认；在节点管理页可以恢复默认。',
};

const TOOL_ORIGIN_HINT: Partial<Record<ToolOrigin, string>> = {
  bundled: '仓库自带的工具。保存后会在 config/tools 下存一份本实例的版本，盖住仓库这份，从此不再随仓库更新。',
  override: '本实例改过的版本，存在 config/tools 下，盖住仓库自带的那份；在工具页可以恢复默认。',
};

const EMPTY_NODE_CONFIG_FORM: NodeConfigFormState = {
  id: '',
  name: '',
  description: '',
  type: 'ai',
  model: '',
  provider: '',
  memory_book: '',
  persistent: false,
  delegate_targetsText: '',
  prompt: '',
};

export const SystemSettingsRightPanel = () => {
  const logs = useSettingsSelectionStore(state => state.systemLogs);
  return (
    <PanelShell eyebrow="系统" title="最近操作日志">
      {logs.length === 0 ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">暂无配置重载或重启操作记录。</p>
      ) : (
        <div className="space-y-2">
          {logs.map((log, index) => (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5 text-[var(--duties-secondary)]" key={`${log}-${index}`}>
              {log}
            </div>
          ))}
        </div>
      )}
    </PanelShell>
  );
};

export const ApprovalsSettingsRightPanel = () => {
  const approval = useSettingsSelectionStore(state => state.selectedApproval);
  return (
    <PanelShell eyebrow="审批" title="审批详情">
      {!approval ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个待审批项后，这里会显示完整参数。</p>
      ) : (
        <div className="space-y-3">
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5">
            <p><span className="text-[var(--duties-tertiary)]">审批 ID：</span><span className="font-mono">{approval.approval_id}</span></p>
            <p><span className="text-[var(--duties-tertiary)]">工具调用：</span><span className="font-mono">{approval.tool_call_id || '未提供'}</span></p>
            <p><span className="text-[var(--duties-tertiary)]">节点：</span><span className="font-mono">{approval.node_id || '未提供'}</span></p>
          </div>
          <JsonBlock value={approval.details} />
        </div>
      )}
    </PanelShell>
  );
};

export const AgentsSettingsRightPanel = () => {
  const id = useSettingsSelectionStore(state => state.selectedNode?.id);
  const token = useSettingsStore(state => state.adminToken);
  const [generation, reload] = useReloadGeneration();
  return <AgentsEditor key={`${token}\0${id || ''}\0${generation}`} onReload={reload} />;
};

const nodeConfigCodec = (nodeId: string): RawFormCodec<NodeConfigFormState> => ({
  parse: (raw) => parseNodeConfig(raw, nodeId),
  serialize: serializeNodeConfig,
});

const AgentsEditor = ({ onReload }: EditorProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const node = useSettingsSelectionStore(state => state.selectedNode);
  const codec = useMemo(() => nodeConfigCodec(node?.id || ''), [node?.id]);
  const source = useRawForm(codec, { ...EMPTY_NODE_CONFIG_FORM, id: node?.id || '' });
  const { form: nodeConfigForm, reset: resetSource, update: updateNodeConfigForm } = source;
  // 保存时交回这一版的指纹：中途被别处改过就报冲突，不静默盖掉。
  const [nodeBase, setNodeBase] = useState<string | null>(null);
  const [nodeYamlEditable, setNodeYamlEditable] = useState(false);
  const [nodeYamlLoading, setNodeYamlLoading] = useState(false);
  const [nodeYamlSaving, setNodeYamlSaving] = useState(false);
  const { notice: nodeNotice, error: nodeError, setMessage: setNodeMessage, setError: setNodeError } = usePageNotice();
  const confirmDiscard = useUnsavedChanges(nodeYamlEditable && source.dirty);
  const reload = () => { if (confirmDiscard()) onReload(); };
  const requests = useRequestGuard();
  const [modelChoices, setModelChoices] = useState<string[]>([]);
  const [channels, setChannels] = useState<ChannelChoice[]>([]);
  const [wires, setWires] = useState<string[]>([]);
  const [channelTable, setChannelTable] = useState<{ state: ChannelTableState; problem: string }>({ state: 'loading', problem: '' });
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const { notice: modelFetchNote, error: modelFetchError, setMessage: setModelFetchNote, setError: setModelFetchError } = usePageNotice();
  // 拉回来时选的渠道已经换了，这份候选就不认。
  const modelFetch = useRequestGuard(nodeConfigForm.provider);
  const modelListId = useId();
  // 刚从上游拉回来的排前面：那才是这个渠道真有的。
  const modelOptions = useMemo(
    () => [...new Set([...fetchedModels, ...modelChoices])],
    [fetchedModels, modelChoices],
  );

  const fetchModels = async () => {
    const provider = nodeConfigForm.provider.trim();
    if (!adminToken || !provider || fetchingModels) return;
    const request = modelFetch.begin('models');
    setFetchingModels(true);
    setModelFetchNote('');
    try {
      // 地址和密钥后端按渠道名自己取，这里不该也没必要碰它们。
      const models = await listUpstreamModels(adminToken, provider, {});
      if (!request.isCurrent()) return;
      setFetchedModels(models);
      setModelFetchNote(`拉到 ${models.length} 个模型。`);
    } catch (caught) {
      if (request.isCurrent()) setModelFetchError(errorMessage(caught, '拉取模型失败'));
    } finally {
      setFetchingModels(false);
    }
  };

  useEffect(() => {
    if (!node) return undefined;
    if (!adminToken) {
      setNodeError('缺少管理员令牌，无法加载节点配置。');
      return undefined;
    }
    const request = requests.begin('node');
    setNodeYamlLoading(true);
    getNodeRaw(adminToken, node.id)
      .then(({ content, hash }) => {
        if (!request.isCurrent()) return;
        resetSource(content);
        setNodeBase(hash);
        setNodeYamlEditable(true);
      })
      .catch((caught) => {
        if (!request.isCurrent()) return;
        setNodeError(errorMessage(caught, '节点配置加载失败，请重试'));
        setNodeYamlEditable(false);
      })
      .finally(() => {
        if (request.isCurrent()) setNodeYamlLoading(false);
      });
    return () => { requests.drop('node'); };
  }, [requests, adminToken, node?.id, resetSource]);

  // 候选与选中的节点无关，只跟令牌走。写的是节点文件的 model，所以别的节点那些 $ENV{...}
  // 也是有效候选 —— 节点路径会经 resolve_env_ref 展开。
  useEffect(() => {
    if (!adminToken) {
      setModelChoices([]);
      setChannels([]);
      setWires([]);
      return undefined;
    }
    const request = requests.begin('choices');
    setChannelTable({ state: 'loading', problem: '' });
    Promise.all([
      getProviders(adminToken).then(
        (providers) => ({ providers, problem: '' }),
        (caught: unknown) => ({ providers: undefined, problem: errorMessage(caught, '渠道表读取失败') }),
      ),
      getNodes(adminToken).catch(() => undefined),
    ]).then(([{ providers, problem }, nodes]) => {
      if (!request.isCurrent()) return;
      setModelChoices(mergeModelChoices(
        modelsFromProviders(providers),
        modelsFromNodes(nodes, node?.id || ''),
      ));
      const choices = channelChoices(providers);
      setChannels(choices.channels);
      setWires(choices.wires);
      setChannelTable(providers ? { state: 'ready', problem: '' } : { state: 'failed', problem });
    });
    return () => { requests.drop('choices'); };
  }, [requests, adminToken, node?.id]);

  const saveNode = async () => {
    if (!adminToken || !node || !nodeYamlEditable || nodeYamlSaving) return;
    const blocked = unsavableReason(source);
    if (blocked) {
      setNodeError(blocked);
      return;
    }
    const { raw, note } = source;
    setNodeYamlSaving(true);
    setNodeMessage('正在保存…');
    try {
      const { result, receiptProblem } = await receipted(updateNodeRaw(adminToken, node.id, raw, nodeBase));
      resetSource(raw);
      if (result) setNodeBase(result.hash);
      window.dispatchEvent(new Event('settings:nodes-updated'));
      setNodeMessage(savedMessage('节点配置已保存。', note, result?.warnings), result ? '' : unreadReceipt(receiptProblem));
    } catch (caught) {
      setNodeError(errorMessage(caught, '保存节点配置失败'));
    } finally {
      setNodeYamlSaving(false);
    }
  };

  return (
    <PanelShell disabled={nodeYamlSaving} eyebrow="节点" title="节点结构化编辑">
      {!node ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个节点后，这里会显示节点属性、委派关系和工具权限表单。</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          {nodeYamlLoading ? (
            <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">正在加载节点配置。</p>
          ) : !nodeYamlEditable ? (
            <div className="space-y-2">
              {nodeError
                ? <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-danger)]" role="alert">{nodeError}</p>
                : <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">系统内建节点不可编辑</p>}
              <Button onClick={reload} size="sm" tone="quiet">刷新</Button>
            </div>
          ) : (
            <>
              <fieldset disabled={Boolean(source.parseError)} className="min-w-0 space-y-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                <div>
                  <span className={STRUCTURED_LABEL_CLASS}>节点 ID</span>
                  <p aria-label="节点 ID" className="font-mono text-xs">{nodeConfigForm.id}</p>
                </div>
                {node.source && NODE_SOURCE_HINT[node.source] && (
                  <p className="text-[var(--duties-secondary)]">{NODE_SOURCE_HINT[node.source]}</p>
                )}
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>名称</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ name: event.target.value })} value={nodeConfigForm.name} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>描述</span>
                  <textarea className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ description: event.target.value })} rows={3} value={nodeConfigForm.description} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>类型</span>
                  <select className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ type: event.target.value as NodeConfigType })} value={nodeConfigForm.type}>
                    <option value="ai">ai</option>
                    <option value="tool">tool</option>
                  </select>
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>渠道，可选</span>
                  <select
                    aria-label="渠道"
                    className={STRUCTURED_INPUT_CLASS}
                    onChange={(event) => {
                      updateNodeConfigForm({ provider: event.target.value });
                      setFetchedModels([]);
                      setModelFetchNote('');
                    }}
                    value={nodeConfigForm.provider}
                  >
                    <option value="">跟随活跃渠道</option>
                    <ChannelOptions channels={channels} current={nodeConfigForm.provider} table={channelTable.state} wires={wires} />
                  </select>
                  <span className="mt-1 block text-xs text-[var(--duties-secondary)]">
                    选已配渠道会连它的地址密钥一起用；选线格式只定请求怎么发，地址得另填。
                  </span>
                  {channelTable.state === 'failed' && (
                    <span className="mt-1 block text-xs text-[var(--duties-danger)]" role="alert">
                      渠道表没读到，下拉里列不出可选的渠道：{channelTable.problem}
                    </span>
                  )}
                </label>
                {/* 不用 label 包：里头那颗按钮会连带触发 label 的聚焦。 */}
                <div className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>模型</span>
                  <div className="flex gap-2">
                    <input
                      aria-label="模型"
                      className={STRUCTURED_INPUT_CLASS}
                      list={modelOptions.length ? modelListId : undefined}
                      onChange={(event) => updateNodeConfigForm({ model: event.target.value })}
                      value={nodeConfigForm.model}
                    />
                    <Button
                      className="flex-shrink-0"
                      disabled={!nodeConfigForm.provider.trim() || fetchingModels}
                      onClick={() => void fetchModels()}
                      size="sm"
                      title={nodeConfigForm.provider.trim() ? '问这个渠道有哪些模型' : '先选一个渠道'}
                      tone="quiet"
                    >
                      {fetchingModels ? '正在拉取…' : '拉取模型'}
                    </Button>
                  </div>
                  {modelOptions.length > 0 && (
                    <datalist id={modelListId}>
                      {modelOptions.map((choice) => <option key={choice} value={choice} />)}
                    </datalist>
                  )}
                  {modelFetchError && (
                    <p className="mt-1 text-xs text-[var(--duties-danger)]" role="alert">{modelFetchError}</p>
                  )}
                  {modelFetchNote && (
                    <p className="mt-1 text-xs text-[var(--duties-tertiary)]">{modelFetchNote}</p>
                  )}
                </div>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>记忆簿，可选</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ memory_book: event.target.value })} value={nodeConfigForm.memory_book} />
                </label>
                <label className="flex items-center justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                  <span>持久化</span>
                  <input checked={nodeConfigForm.persistent} onChange={(event) => updateNodeConfigForm({ persistent: event.target.checked })} type="checkbox" />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>系统提示词</span>
                  {nodeConfigForm.promptIsStructured && <span className="block text-xs text-[var(--duties-secondary)]">此提示词使用结构化块，请保留 JSON 数组格式。</span>}
                  <textarea className={`${STRUCTURED_INPUT_CLASS} min-h-[var(--duties-pane-min-height)]`} onChange={(event) => updateNodeConfigForm({ prompt: event.target.value })} rows={12} value={nodeConfigForm.prompt} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>委派目标，使用英文逗号分隔</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ delegate_targetsText: event.target.value })} value={nodeConfigForm.delegate_targetsText} />
                </label>
                <p className="text-xs text-[var(--duties-tertiary)]">
                  工具权限（tool_access）在下面的 YAML 原文里改。表单只改动过的那几个字段，其余原文和注释照旧。
                </p>
              </fieldset>
              <RawFormStatus source={source} />
              <RawSourceDetails height="16rem" label="节点 YAML 编辑器" source={source} summary="高级 YAML 编辑" />
              <div className="flex flex-wrap gap-2">
                <Button disabled={nodeYamlSaving} onClick={saveNode} size="sm">保存节点配置</Button>
                <Button disabled={nodeYamlSaving} onClick={reload} size="sm" tone="quiet">刷新</Button>
              </div>
              {nodeError && <p className="text-[var(--duties-danger)]" role="alert">{nodeError}</p>}
              {nodeNotice && <p className="text-[var(--duties-tertiary)]">{nodeNotice}</p>}
            </>
          )}
        </div>
      )}
    </PanelShell>
  );
};

export const ToolsSettingsRightPanel = () => {
  const id = useSettingsSelectionStore(state => state.selectedTool?.name);
  const token = useSettingsStore(state => state.adminToken);
  const [generation, reload] = useReloadGeneration();
  return <ToolsEditor key={`${token}\0${id || ''}\0${generation}`} onReload={reload} />;
};

const ToolsEditor = ({ onReload }: EditorProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const tool = useSettingsSelectionStore(state => state.selectedTool);
  const [toolScript, setToolScript] = useState('');
  const [toolBaseline, setToolBaseline] = useState('');
  const [toolBase, setToolBase] = useState<string | null>(null);
  const [toolScriptLoaded, setToolScriptLoaded] = useState(false);
  const [toolScriptLoading, setToolScriptLoading] = useState(false);
  const [toolScriptSaving, setToolScriptSaving] = useState(false);
  const [toolReloading, setToolReloading] = useState(false);
  const { notice: toolNotice, error: toolError, setMessage: setToolMessage, setError: setToolError } = usePageNotice();
  const confirmToolDiscard = useUnsavedChanges(toolScriptLoaded && toolScript !== toolBaseline);
  const reload = () => { if (confirmToolDiscard()) onReload(); };
  const requests = useRequestGuard();

  useEffect(() => {
    setToolScript('');
    setToolBase(null);
    setToolScriptLoaded(false);
    setToolMessage('');
    if (!tool) return undefined;
    // 内置和插件工具没有源码文件，请求只会换回一句 409。
    if (!tool.editable) return undefined;
    if (!adminToken) {
      setToolError('缺少管理员令牌，无法加载工具脚本。');
      return undefined;
    }
    const request = requests.begin('script');
    setToolScriptLoading(true);
    // [2026-06-02] Load the raw Python script for the selected tool.
    // Why: the Tools right panel must support direct edits while keeping the current
    // name, risk, schema, and full-name display. How: fetch the backend raw script when
    // the selected tool name changes and store it in a controlled textarea. Purpose:
    // users can update one tool implementation without leaving the context panel.
    getToolRaw(adminToken, tool.name)
      .then(({ content: raw, hash }) => {
        if (!request.isCurrent()) return;
        setToolScript(raw);
        setToolBaseline(raw);
        setToolBase(hash);
        setToolScriptLoaded(true);
      })
      .catch((caught) => {
        if (!request.isCurrent()) return;
        setToolError(errorMessage(caught, '加载工具脚本失败'));
        setToolScriptLoaded(false);
      })
      .finally(() => {
        if (request.isCurrent()) setToolScriptLoading(false);
      });
    return () => { requests.drop('script'); };
  }, [requests, adminToken, tool?.name]);

  const saveToolScript = async () => {
    if (!adminToken || !tool || !toolScriptLoaded || toolScriptSaving || toolReloading) return;
    setToolScriptSaving(true);
    setToolMessage('正在保存…');
    try {
      // [2026-06-02] Persist edited tool code and notify settings lists.
      // Why: saving from the right rail should update the selected tool file and let
      // the rest of Settings reload parsed metadata. How: PUT the controlled Python
      // script and dispatch settings:tools-updated after success. Purpose: the tool
      // list can refresh after a raw script save without requiring an engine restart.
      const { result, receiptProblem } = await receipted(updateToolRaw(adminToken, tool.name, toolScript, toolBase));
      setToolBaseline(toolScript);
      if (result) setToolBase(result.hash);
      window.dispatchEvent(new Event('settings:tools-updated'));
      setToolMessage(savedMessage('工具脚本已保存。', '', result?.warnings), result ? '' : unreadReceipt(receiptProblem));
    } catch (caught) {
      setToolError(errorMessage(caught, '保存工具脚本失败'));
    } finally {
      setToolScriptSaving(false);
    }
  };

  const reloadToolRegistry = async () => {
    if (!adminToken || toolScriptSaving || toolReloading || !confirmToolDiscard()) return;
    setToolReloading(true);
    setToolMessage('正在重载工具…');
    try {
      // [2026-06-02] Expose tool reload beside the raw script editor.
      // Why: edited Python tools usually need the backend registry to reload before
      // calls use the new code. How: call the existing reloadTools admin endpoint from
      // the panel. Purpose: users can save and reload tools without requesting an
      // engine restart.
      await reloadTools(adminToken);
      setToolMessage('工具已重载。');
    } catch (caught) {
      setToolError(errorMessage(caught, '重载工具失败'));
    } finally {
      setToolReloading(false);
    }
  };

  return (
    <PanelShell disabled={toolScriptSaving || toolReloading} eyebrow="工具" title="工具说明">
      <div className="space-y-3">
        {tool ? (
          <div className="space-y-2">
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono font-semibold">{tool.name}</span>
                <span className={`border px-1.5 py-0.5 font-mono text-xs ${riskClassName(inferToolRisk(tool.name))}`}>{riskLabel(inferToolRisk(tool.name))}</span>
              </div>
              <p className="mt-1 text-[var(--duties-secondary)]">{tool.description || '无描述'}</p>
            </div>
            <JsonBlock value={tool.input_schema || {}} />
            {!tool.editable ? (
              <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5 text-[var(--duties-secondary)]">
                {tool.source === 'plugin' ? '插件' : '内置'}工具的实现跟着源码走，改它要改仓库代码。
                这里能做的只有决定哪些节点可以调用它，以及调用要不要审批。
              </p>
            ) : (
            <div className="space-y-2 text-xs leading-5">
              <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-tertiary)]">Python 脚本编辑</h3>
              {tool.origin && TOOL_ORIGIN_HINT[tool.origin] && (
                <p className="text-[var(--duties-secondary)]">{TOOL_ORIGIN_HINT[tool.origin]}</p>
              )}
              {/* [2026-06-02] Add the requested raw Python editor below schema details.
                  Why: tool metadata alone cannot change implementation code. How: bind
                  the selected tool's raw script to the specified monospace textarea and
                  keep save and reload actions beside it. Purpose: the first editable
                  Tools batch changes only this panel while preserving current context. */}
              <textarea
                className="w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 font-mono text-xs"
                aria-label="工具脚本"
                disabled={!toolScriptLoaded || toolScriptLoading}
                onChange={(event) => setToolScript(event.target.value)}
                rows={16}
                spellCheck={false}
                value={toolScriptLoading ? '正在加载工具脚本。' : toolScript}
              />
              <div className="flex flex-wrap gap-2">
                <Button disabled={!toolScriptLoaded || toolScriptLoading || toolScriptSaving} onClick={saveToolScript} size="sm">保存</Button>
                <Button disabled={toolScriptLoading || toolScriptSaving || toolReloading} onClick={reload} size="sm" tone="quiet">刷新</Button>
                <Button disabled={!adminToken || toolReloading} onClick={reloadToolRegistry} size="sm" tone="quiet">重载工具</Button>
              </div>
              {toolError && <p className="text-[var(--duties-danger)]" role="alert">{toolError}</p>}
              {toolNotice && <p className="text-[var(--duties-tertiary)]">{toolNotice}</p>}
            </div>
            )}
          </div>
        ) : (
          <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个工具后，这里会显示 input_schema 文档。</p>
        )}

      </div>
    </PanelShell>
  );
};

const SKILL_CODEC: RawFormCodec<SkillQuickFields> = { parse: parseSkillQuickFields, serialize: serializeSkillQuickFields };

/** 原文读回来之前先按列表里那份摘要摆着，读回来再换成文件里的值。 */
const skillSnapshotFields = (skill: AdminSkill | null): SkillQuickFields => ({
  enabled: skill?.enabled !== false,
  strategy: skill?.strategy || 'normal',
  keywords: (skill?.keywords || []).join(', '),
  order: '0',
  priority: '0',
  scan_depth: '0',
});

const SKILL_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono';

const SKILL_STRATEGIES: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'normal', label: 'normal' },
  { value: 'constant', label: 'constant' },
  { value: 'output', label: 'output（模型输出命中 trigger 时重答）' },
];

/** 文件里写的是别的策略时也列出来：选择框对不上任何一项会显示成第一项，看着像 normal。 */
const skillStrategyOptions = (current: string) => (
  SKILL_STRATEGIES.some((option) => option.value === current) || !current
    ? SKILL_STRATEGIES
    : [...SKILL_STRATEGIES, { value: current, label: current }]
);

export const SkillsSettingsRightPanel = () => {
  const id = useSettingsSelectionStore(state => (state.selectedSkill ? skillDir(state.selectedSkill) : ''));
  const token = useSettingsStore(state => state.adminToken);
  const [generation, reload] = useReloadGeneration();
  return <SkillsEditor key={`${token}\0${id || ''}\0${generation}`} onReload={reload} />;
};

const SkillsEditor = ({ onReload }: EditorProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const skill = useSettingsSelectionStore(state => state.selectedSkill);
  const dir = skill ? skillDir(skill) : '';
  const source = useRawForm(SKILL_CODEC, skillSnapshotFields(skill));
  const { form: edit, reset: resetSource, update } = source;
  const [skillBase, setSkillBase] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(Boolean(adminToken && skill));
  const [saving, setSaving] = useState(false);
  const { notice, error, setMessage, setError } = usePageNotice();
  const confirmDiscard = useUnsavedChanges(loaded && source.dirty);
  const reload = () => { if (confirmDiscard()) onReload(); };
  const requests = useRequestGuard();

  useEffect(() => {
    if (!adminToken || !skill) return undefined;
    const request = requests.begin('skill');
    setLoading(true);
    getSkillRaw(adminToken, dir)
      .then(({ content, hash }) => {
        if (!request.isCurrent()) return;
        resetSource(content);
        setSkillBase(hash);
        setLoaded(true);
      })
      .catch((caught) => { if (request.isCurrent()) { setLoaded(false); setError(errorMessage(caught, '加载技能失败')); } })
      .finally(() => { if (request.isCurrent()) setLoading(false); });
    return () => { requests.drop('skill'); };
  }, [requests, adminToken, dir, resetSource]);

  const save = async () => {
    if (!adminToken || !skill || !loaded || saving) return;
    const blocked = unsavableReason(source);
    if (blocked) {
      setError(blocked);
      return;
    }
    const { raw, note } = source;
    setSaving(true);
    setMessage('正在保存…');
    try {
      const { result, receiptProblem } = await receipted(updateSkillRaw(adminToken, dir, raw, skillBase));
      resetSource(raw);
      if (result) setSkillBase(result.hash);
      window.dispatchEvent(new Event('settings:skills-updated'));
      setMessage(savedMessage('技能已保存。', note, result?.warnings), result ? '' : unreadReceipt(receiptProblem));
    } catch (caught) {
      setError(errorMessage(caught, '保存技能失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <PanelShell disabled={saving || loading} eyebrow="技能" title="Frontmatter 快捷编辑">
      {!skill ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个技能后，这里会显示启用状态、策略和关键词。</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">名称：</span><span className="font-mono">{skill.name}</span></p>
          {dir !== skill.name && <p><span className="text-[var(--duties-tertiary)]">目录：</span><span className="font-mono">{dir}</span></p>}
          {!loaded && !loading ? (
            <div className="space-y-2">
              {error
                ? <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-danger)]" role="alert">{error}</p>
                : <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">技能没有读到。</p>}
              <Button onClick={reload} size="sm" tone="quiet">刷新</Button>
            </div>
          ) : (
            <>
              <fieldset disabled={!loaded || Boolean(source.parseError)} className="min-w-0 space-y-3">
                <label className="flex items-center justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                  <span>启用</span>
                  <input checked={edit.enabled} onChange={(event) => update({ enabled: event.target.checked })} type="checkbox" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[var(--duties-tertiary)]">策略</span>
                  <select className={SKILL_INPUT_CLASS} onChange={(event) => update({ strategy: event.target.value })} value={edit.strategy}>
                    {skillStrategyOptions(edit.strategy).map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-[var(--duties-tertiary)]">关键词，使用英文逗号分隔</span>
                  <input className={SKILL_INPUT_CLASS} onChange={(event) => update({ keywords: event.target.value })} value={edit.keywords} />
                </label>
                <div className="grid grid-cols-3 gap-2">
                  {(['order', 'priority', 'scan_depth'] as const).map((key) => (
                    <label className="block" key={key}>
                      <span className="mb-1 block text-[var(--duties-tertiary)]">{key}</span>
                      <input className={SKILL_INPUT_CLASS} onChange={(event) => update({ [key]: event.target.value })} value={edit[key]} />
                    </label>
                  ))}
                </div>
              </fieldset>
              <RawFormStatus source={source} />
              <RawSourceDetails height="16rem" label="技能 Raw Markdown 编辑器" loading={!loaded} source={source} summary="Raw Markdown 编辑（高级）" />
              <div className="flex flex-wrap gap-2">
                <Button disabled={saving || !loaded} onClick={save} size="sm">保存技能</Button>
                <Button disabled={loading || saving} onClick={reload} size="sm" tone="quiet">刷新</Button>
              </div>
              {error && <p className="text-[var(--duties-danger)]" role="alert">{error}</p>}
              {notice && <p className="text-[var(--duties-tertiary)]">{notice}</p>}
            </>
          )}
        </div>
      )}
    </PanelShell>
  );
};

const fieldClass = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const labelClass = 'block mb-1 text-[var(--duties-tertiary)] text-xs';

function displayEnvText(envText: string): string {
  // [2026-06-02] Convert parsed MCP env rows from object style to KEY=VALUE rows.
  // Why: parseMcpClients returns loose objects as `key: value`, while this panel must
  // show stdio env as one KEY=VALUE pair per line. How: replace only the first colon
  // separator on each non-empty row. Purpose: users can edit environment variables in
  // the requested shell-like format without changing the shared parser.
  return envText.split('\n').map((line) => {
    const index = line.indexOf(':');
    if (index < 0) return line;
    return `${line.slice(0, index).trim()}=${line.slice(index + 1).trim()}`;
  }).join('\n');
}

function serializeEnvTextForYaml(envText: string): string {
  // [2026-06-02] Convert KEY=VALUE MCP env rows back to loose object rows.
  // Why: serializeMcpClients parses envText with colon separators. How: replace the
  // first equals sign in each non-empty row with `: ` and leave already-colon rows
  // unchanged for operator tolerance. Purpose: environment variables save as a YAML
  // mapping while the visible form remains KEY=VALUE based.
  return envText.split('\n').map((line) => {
    const trimmed = line.trim();
    if (!trimmed) return '';
    if (trimmed.includes(':') && !trimmed.includes('=')) return trimmed;
    const index = trimmed.indexOf('=');
    if (index < 0) return trimmed;
    return `${trimmed.slice(0, index).trim()}: ${trimmed.slice(index + 1).trim()}`;
  }).join('\n');
}

export const McpSettingsRightPanel = () => {
  const id = useSettingsSelectionStore(state => state.selectedMcpClient?.id);
  const token = useSettingsStore(state => state.adminToken);
  const [generation, reload] = useReloadGeneration();
  return <McpEditor key={`${token}\0${id || ''}\0${generation}`} onReload={reload} />;
};

/** 表单上 env 按 KEY=VALUE 一行一个显示，写回前换回解析器认的「key: value」。 */
const mcpFormForYaml = (form: McpClientFormState): McpClientFormState => ({ ...form, envText: serializeEnvTextForYaml(form.envText) });

const McpEditor = ({ onReload }: EditorProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const client = useSettingsSelectionStore(state => state.selectedMcpClient);
  // 打开时（和上次保存后）文件里这一项的样子：保存时拿它核对有没有被别处改过，也拿它比出改了哪些字段。
  const [opened, setOpened] = useState<McpClientFormState | null>(null);
  const [form, setForm] = useState<McpClientFormState | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const { notice, error, setMessage, setError } = usePageNotice();
  const confirmDiscard = useUnsavedChanges(Boolean(form) && JSON.stringify(form) !== JSON.stringify(opened));
  const reload = () => { if (confirmDiscard()) onReload(); };
  const requests = useRequestGuard();
  const shareHintId = useId();

  useEffect(() => {
    if (!client) return undefined;
    if (!adminToken) {
      setError('缺少管理员令牌，无法加载 MCP Client。');
      return undefined;
    }
    const request = requests.begin('client');
    setLoading(true);
    getMcpClientsRaw(adminToken)
      .then(({ content: raw }) => {
        if (!request.isCurrent()) return;
        const selected = parseMcpClients(raw).find((item) => item.id === client.id) || null;
        if (!selected) {
          setError('未在 MCP YAML 中找到此 Client。');
          return;
        }
        const shown = { ...selected, envText: displayEnvText(selected.envText) };
        setOpened(shown);
        setForm(shown);
      })
      .catch((caught) => {
        if (!request.isCurrent()) return;
        setError(errorMessage(caught, '加载 MCP Client 失败'));
      })
      .finally(() => {
        if (request.isCurrent()) setLoading(false);
      });
    return () => { requests.drop('client'); };
  }, [requests, adminToken, client?.id]);

  const save = async () => {
    if (!adminToken || !client || !form || !opened || saving) return;
    setSaving(true);
    setMessage('正在保存…');
    try {
      const before = mcpFormForYaml(opened);
      const after = mcpFormForYaml(form);
      const { content, note, result, receiptProblem } = await commitYamlKeys({
        read: () => getMcpClientsRaw(adminToken),
        write: (next, hash) => updateMcpClientsRaw(adminToken, next, hash),
        edits: (base) => mcpClientUpdateEdits(base, before, after),
      });
      const original = parseMcpClients(content).find((item) => item.id === client.id)?.original;
      const saved = { ...form, original };
      setForm(saved);
      setOpened(saved);
      window.dispatchEvent(new Event('settings:mcp-updated'));
      setMessage(savedMessage('MCP Client 已保存。', note, result?.warnings), receiptProblem);
    } catch (caught) {
      setError(isEditConflict(caught) ? staleItemMessage('这个 Client') : errorMessage(caught, '保存 MCP Client 失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="MCP" title="连接信息" disabled={saving}>
      {!client ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个 MCP Client 后，这里会显示连接配置。</p>
      ) : loading ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">正在加载 MCP Client。</p>
      ) : !form ? (
        <div className="space-y-2">
          {error
            ? <p className="text-xs leading-5 text-[var(--duties-danger)]" role="alert">{error}</p>
            : <p className="text-xs leading-5 text-[var(--duties-secondary)]">未找到可编辑的 MCP Client。</p>}
          <Button onClick={reload} size="sm" tone="quiet">刷新</Button>
        </div>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">ID：</span><span className="font-mono">{form.id}</span></p>
          <label className="block">
            <span className={labelClass}>description</span>
            <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, description: event.target.value }))} value={form.description} />
          </label>
          <label className="flex items-center gap-2">
            <input checked={form.enabled} onChange={(event) => setForm((current) => current && ({ ...current, enabled: event.target.checked }))} type="checkbox" />
            <span>enabled</span>
          </label>
          <div className="space-y-1">
            <label className="flex items-center gap-2">
              <input aria-describedby={shareHintId} checked={form.share_sessions} onChange={(event) => setForm((current) => current && ({ ...current, share_sessions: event.target.checked }))} type="checkbox" />
              <span>share_sessions：所有对话共用会话</span>
            </label>
            <p className="text-xs text-[var(--duties-secondary)]" id={shareHintId}>勾上后所有对话共用这个 Client 的会话，适合无状态、启动又慢的服务；有状态的服务（浏览器、记忆等）会把一个对话的状态带到别的对话。不勾时每个对话各用各的会话。改动保存后，这个 Client 已经开着的会话会全部重开。</p>
          </div>
          <label className="block">
            <span className={labelClass}>transport</span>
            <select className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, transport: event.target.value as McpClientFormState['transport'] }))} value={form.transport}>
              <option value="stdio">stdio</option>
              <option value="sse">sse</option>
              <option value="streamable_http">streamable_http</option>
            </select>
          </label>
          {form.transport === 'stdio' ? (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>command</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, command: event.target.value }))} value={form.command} />
              </label>
              <label className="block">
                <span className={labelClass}>启动参数（JSON 字符串数组）</span>
                <textarea rows={3} className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, argsText: event.target.value }))} value={form.argsText} />
                <span className="text-xs text-[var(--duties-secondary)]">例如 ["--port", "8080"]，参数内的逗号、空格和换行均保留。</span>
              </label>
              <label className="block">
                <span className={labelClass}>env，每行 KEY=VALUE</span>
                <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, envText: event.target.value }))} rows={6} value={form.envText} />
              </label>
            </div>
          ) : (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>url</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, url: event.target.value }))} value={form.url} />
              </label>
              <label className="block">
                <span className={labelClass}>headers，每行 Key: Value</span>
                <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, headersText: event.target.value }))} rows={6} value={form.headersText} />
              </label>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button disabled={saving} onClick={save} size="sm">保存</Button>
            <Button disabled={saving} onClick={reload} size="sm" tone="quiet">刷新</Button>
          </div>
          {error && <p className="text-[var(--duties-danger)]" role="alert">{error}</p>}
          {notice && <p className="text-[var(--duties-tertiary)]">{notice}</p>}
        </div>
      )}
    </PanelShell>
  );
};

export const AutomationSettingsRightPanel = () => {
  const id = useSettingsSelectionStore(state => state.selectedScheduleId);
  const token = useSettingsStore(state => state.adminToken);
  const [generation, reload] = useReloadGeneration();
  return <AutomationEditor key={`${token}\0${id || ''}\0${generation}`} onReload={reload} />;
};

const AutomationEditor = ({ onReload }: EditorProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const selectedScheduleId = useSettingsSelectionStore(state => state.selectedScheduleId);
  // 打开时（和上次保存后）文件里这一项的样子：保存时拿它核对有没有被别处改过，也拿它比出改了哪些字段。
  const [opened, setOpened] = useState<ScheduleFormState | null>(null);
  const [form, setForm] = useState<ScheduleFormState | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const { notice, error, setMessage, setError } = usePageNotice();
  const confirmDiscard = useUnsavedChanges(Boolean(form) && JSON.stringify(form) !== JSON.stringify(opened));
  const reload = () => { if (confirmDiscard()) onReload(); };
  const requests = useRequestGuard();

  useEffect(() => {
    if (!selectedScheduleId) return undefined;
    if (!adminToken) {
      setError('缺少管理员令牌，无法加载定时任务。');
      return undefined;
    }
    const request = requests.begin('schedule');
    setLoading(true);
    getSchedulesRaw(adminToken)
      .then(({ content: raw }) => {
        if (!request.isCurrent()) return;
        const selected = parseSchedules(raw).find((item) => item.id === selectedScheduleId) || null;
        setOpened(selected);
        setForm(selected);
        if (!selected) setError('未在 schedules.yaml 中找到此定时任务。');
      })
      .catch((caught) => {
        if (!request.isCurrent()) return;
        setError(errorMessage(caught, '加载定时任务失败'));
      })
      .finally(() => {
        if (request.isCurrent()) setLoading(false);
      });
    return () => { requests.drop('schedule'); };
  }, [requests, adminToken, selectedScheduleId]);

  const save = async () => {
    if (!adminToken || !selectedScheduleId || !form || !opened || saving) return;
    setSaving(true);
    setMessage('正在保存…');
    try {
      const before = opened;
      const after = form;
      const { content, note, result, receiptProblem } = await commitYamlKeys({
        read: () => getSchedulesRaw(adminToken),
        write: (next, hash) => updateSchedulesRaw(adminToken, next, hash),
        edits: (base) => scheduleUpdateEdits(base, before, after),
      });
      const original = parseSchedules(content).find((item) => item.id === selectedScheduleId)?.original;
      const saved = { ...form, original };
      setForm(saved);
      setOpened(saved);
      window.dispatchEvent(new Event('settings:schedules-updated'));
      setMessage(savedMessage('定时任务已保存。', note, result?.warnings), receiptProblem);
    } catch (caught) {
      setError(isEditConflict(caught) ? staleItemMessage('这个定时任务') : errorMessage(caught, '保存定时任务失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="自动化" title="定时任务编辑" disabled={saving}>
      {!selectedScheduleId ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个定时任务后，这里会显示可编辑字段。</p>
      ) : loading ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">正在加载定时任务。</p>
      ) : !form ? (
        <div className="space-y-2">
          {error
            ? <p className="text-xs leading-5 text-[var(--duties-danger)]" role="alert">{error}</p>
            : <p className="text-xs leading-5 text-[var(--duties-secondary)]">未找到可编辑的定时任务。</p>}
          <Button onClick={reload} size="sm" tone="quiet">刷新</Button>
        </div>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">ID：</span><span className="font-mono">{form.id}</span></p>
          <label className="block">
            <span className={labelClass}>cron</span>
            <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, cron: event.target.value }))} placeholder="0 0 * * *" value={form.cron} />
          </label>
          <label className="block">
            <span className={labelClass}>type</span>
            <select className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, type: event.target.value as ScheduleFormState['type'] }))} value={form.type}>
              <option value="message">message</option>
              <option value="script">script</option>
            </select>
          </label>
          <label className="block">
            <span className={labelClass}>text</span>
            <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, text: event.target.value }))} rows={4} value={form.text} />
          </label>
          {form.type === 'script' && (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>command</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, command: event.target.value }))} value={form.command} />
              </label>
              <label className="block">
                <span className={labelClass}>timeout</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, timeout: event.target.value }))} value={form.timeout} />
              </label>
              <label className="flex items-center gap-2">
                <input checked={form.silent} onChange={(event) => setForm((current) => current && ({ ...current, silent: event.target.checked }))} type="checkbox" />
                <span>silent</span>
              </label>
            </div>
          )}
          <label className="flex items-center gap-2">
            <input checked={form.enabled} onChange={(event) => setForm((current) => current && ({ ...current, enabled: event.target.checked }))} type="checkbox" />
            <span>enabled</span>
          </label>
          <label className="flex items-center gap-2">
            <input checked={form.once} onChange={(event) => setForm((current) => current && ({ ...current, once: event.target.checked }))} type="checkbox" />
            <span>once</span>
          </label>
          <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
            <summary className="cursor-pointer font-mono text-xs text-[var(--duties-tertiary)]">高级字段</summary>
            <div className="mt-3 space-y-3">
              {(['conversation_key', 'entry_node_id', 'workflow_id'] as const).map((key) => (
                <label className="block" key={key}>
                  <span className={labelClass}>{key}</span>
                  <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, [key]: event.target.value }))} value={form[key]} />
                </label>
              ))}
            </div>
          </details>
          <div className="flex flex-wrap gap-2">
            <Button disabled={saving} onClick={save} size="sm">保存</Button>
            <Button disabled={saving} onClick={reload} size="sm" tone="quiet">刷新</Button>
          </div>
          {error && <p className="text-[var(--duties-danger)]" role="alert">{error}</p>}
          {notice && <p className="text-[var(--duties-tertiary)]">{notice}</p>}
        </div>
      )}
    </PanelShell>
  );
};

export const AdvancedSettingsRightPanel = () => {
  const file = useSettingsSelectionStore(state => state.advancedFile);
  const descriptions: Record<typeof file, string> = {
    runtime: 'Runtime Config 控制引擎运行参数、入口节点、工具模式、记忆整理和进程管理等运行时行为。',
    policy: 'Policy 控制工具调用、文件访问、命令执行等安全策略。错误配置可能导致审批行为改变。',
  };
  return (
    <PanelShell eyebrow="高级" title="当前编辑文件说明">
      <p className="text-xs leading-5 text-[var(--duties-secondary)]">{descriptions[file]}</p>
      <p className="mt-3 text-xs leading-5 text-[var(--duties-secondary)]">保存前会执行轻量检查；完整 YAML 与字段校验由后端读取配置时完成。</p>
    </PanelShell>
  );
};

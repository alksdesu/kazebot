// [2026-06-01] Embedded session configuration panel for the right column.
// Why: node switching and model overrides are session-scoped actions, but the old
// UI used separate modal panels that hid the chat. How: this compact component
// combines session identity, connection status, node switching, and provider
// override editing in the 60% upper section of the right panel. Purpose: Header
// text clicks and the default right panel all operate on the current session.
import { useEffect, useId, useMemo, useState } from 'react';

import {
  activeProviderConfig,
  clearSessionProviderOverride,
  getActiveNode,
  getAppConfig,
  getNodes,
  getProviders,
  getSessionProviderOverride,
  switchNode,
  updateSessionProviderOverride,
} from '../../api/supervisorClient';
import { useChatStore } from '../../store/chatStore';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import type { NodeDef } from '../../types';
import { describeEnvRef, envRefNames } from '../../utils/envRef';
import { literalModelsOnly, mergeModelChoices, modelsFromProviders } from '../../utils/modelChoices';
import { Button } from '../common';

interface SessionConfigPanelProps {
  sessionId: string;
  focus?: 'default' | 'node' | 'model';
}

interface SessionModelEdit {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
}

function getSwitchableNodes(allNodes: NodeDef[]): NodeDef[] {
  // [2026-06-01] Reuse the modal's root-node filtering in embedded form.
  // Why: users should see the same practical node list as before. How: hide system
  // and command-review nodes, then prefer nodes that are not delegated children.
  // Purpose: the compact picker stays useful without listing internal workers first.
  const aiNodes = allNodes.filter(n =>
    n.type === 'ai' && !n.id.startsWith('system.') && !n.id.startsWith('bootstrap.cmd'),
  );
  const delegated = new Set<string>();
  for (const n of allNodes) {
    for (const t of (n.delegate_targets || [])) delegated.add(t);
  }
  const roots = aiNodes.filter(n => !delegated.has(n.id));
  return roots.length > 0 ? roots : aiNodes;
}

function stringField(source: Record<string, unknown> | null | undefined, key: string): string {
  const value = source?.[key];
  return typeof value === 'string' ? value : '';
}

function safeSessionId(value: string): string {
  return value && value !== 'no-session' ? value : '';
}

function shortSessionId(value: string): string {
  // [2026-06-01] Collapse the long session identifier into the summary line.
  // Why: the full id is useful for copying but too wide for the default right
  // panel. How: show only the first eight characters until the details row is
  // opened. Purpose: the session area stops taking a whole bordered card by default.
  return value ? value.slice(0, 8) : '无';
}

function connectionLabel(status: string): string {
  // [2026-06-01] Only abnormal connection states get text labels.
  // Why: "WebSocket open" and "WebSocket idle" are implementation details that
  // add noise for users. How: return an empty label for healthy/open or idle states,
  // and reserve short labels for connecting, reconnecting, and disconnected states.
  // Purpose: normal status is represented by a quiet dot, while problems remain clear.
  // [2026-06-01] Why: connection status labels are visible in the session panel.
  // How: translate display labels while preserving the original status enum checks.
  // Purpose: the compact status row remains localized without changing store data.
  if (status === 'connecting') return '连接中';
  if (status === 'reconnecting') return '重连中';
  if (status === 'closed') return '已断开';
  return '';
}

function connectionDotClass(status: string): string {
  // [2026-06-01] Keep idle and healthy states visually quiet.
  // Why: idle means no socket is currently needed after normal cleanup, while closed
  // means an unexpected disconnect. How: map open to green, transient states to
  // yellow, closed to red, and idle to a muted gray dot. Purpose: the panel avoids
  // false red warnings after successful turns or cancellations.
  if (status === 'open') return 'bg-green-500';
  if (status === 'connecting' || status === 'reconnecting') return 'bg-yellow-500';
  if (status === 'closed') return 'bg-red-500';
  return 'bg-[var(--duties-tertiary)] opacity-40';
}

function connectionAriaLabel(status: string, visibleLabel: string): string {
  // [2026-06-01] Preserve accessible status names without adding visual noise.
  // Why: open and idle intentionally do not render text labels, but the dot still
  // needs an understandable name for assistive tools and tests. How: return explicit
  // labels for silent states and reuse the visible label otherwise. Purpose: compact
  // UI remains accessible while normal states stay quiet.
  // [2026-06-01] Why: these aria labels are read by assistive tools and tests.
  // How: localize labels without changing the status enum values. Purpose: silent
  // connection dots stay accessible in the same language as the visible UI.
  if (status === 'open') return '已连接';
  if (status === 'idle') return '空闲';
  return visibleLabel || '已断开';
}

function inheritedPlaceholder(inherited: string, fallback: string): string {
  // 继承值是 $ENV{...} 时不能原样当 placeholder：会话覆盖不展开它，照抄进输入框必挂。
  const names = envRefNames(inherited);
  if (names.length) return `继承自环境变量 ${names.join(' 或 ')}`;
  return inherited || fallback;
}

function inheritedSourceLabel(source: 'session' | 'node' | 'global' | 'default', nodeId: string): string {
  // [2026-06-01] Make fallback provenance explicit beside effective values.
  // Why: the data was already real, but users could not tell whether a model came
  // from a session override, the active node, global config, or a hard default. How:
  // return the compact source label rendered next to Model and Base URL. Purpose:
  // inherited values are understandable without opening raw configuration files.
  // [2026-06-01] Why: inherited source hints are shown beside model values.
  // How: keep the source logic and translate only the labels. Purpose: users can
  // understand where a value comes from without reading English UI fragments.
  if (source === 'session') return '（会话覆盖）';
  if (source === 'node') return `（节点：${nodeId || '当前节点'}）`;
  if (source === 'global') return '（全局）';
  return '（默认）';
}

export const SessionConfigPanel = ({ sessionId, focus = 'default' }: SessionConfigPanelProps) => {
  const sid = safeSessionId(sessionId);
  const {
    adminToken,
    availableNodes,
    activeNodeId,
    activeNodeIsOverride,
    defaultNodeId,
    entryNodeId,
    globalModel,
    globalBaseUrl,
    sessionProviderOverride,
    setActiveNode,
    setAvailableNodes,
    setGlobalConfig,
    setModelConfig,
    setSessionProviderOverride,
  } = useSettingsStore();
  const setSettingsTab = useViewStore(state => state.setSettingsTab);

  const [copyMsg, setCopyMsg] = useState('');
  const [nodeMsg, setNodeMsg] = useState('');
  const [nodeSaving, setNodeSaving] = useState(false);
  const [modelSaving, setModelSaving] = useState(false);
  const [modelMsg, setModelMsg] = useState('');
  const [apiKeyPresent, setApiKeyPresent] = useState(false);
  const [edit, setEdit] = useState<SessionModelEdit>({ provider: '', model: '', base_url: '', api_key: '' });
  const [modelChoices, setModelChoices] = useState<string[]>([]);
  const [providerChoices, setProviderChoices] = useState<string[]>([]);
  const [inheritedProvider, setInheritedProvider] = useState('');
  const connectionStatus = useChatStore((state) => state.connectionStatus);
  const modelListId = useId();
  const providerListId = useId();

  const switchableNodes = useMemo(() => getSwitchableNodes(availableNodes), [availableNodes]);
  const displayNodeId = activeNodeId || entryNodeId || defaultNodeId;
  const activeNode = availableNodes.find(n => n.id === displayNodeId);
  const nodeModel = (activeNode as any)?.model || '';
  const nodeBaseUrl = (activeNode as any)?.base_url || '';
  const overrideModel = stringField(sessionProviderOverride, 'model');
  const overrideBaseUrl = stringField(sessionProviderOverride, 'base_url');
  const effectiveModelSource = edit.model || overrideModel ? 'session' : nodeModel ? 'node' : globalModel ? 'global' : 'default';
  const effectiveBaseUrlSource = edit.base_url || overrideBaseUrl ? 'session' : nodeBaseUrl ? 'node' : globalBaseUrl ? 'global' : 'default';
  // 节点写的是 $ENV{...}，展开发生在引擎侧，前端拿不到值 —— 摆变量名比摆模板可读。
  const effectiveModel = describeEnvRef(edit.model || overrideModel || nodeModel || globalModel) || '（默认）';
  const effectiveBaseUrl = describeEnvRef(edit.base_url || overrideBaseUrl || nodeBaseUrl || globalBaseUrl) || '（默认）';
  const effectiveModelSourceLabel = inheritedSourceLabel(effectiveModelSource, displayNodeId);
  const effectiveBaseUrlSourceLabel = inheritedSourceLabel(effectiveBaseUrlSource, displayNodeId);
  const connectionStatusLabel = connectionLabel(connectionStatus);
  const hasSessionOverride = Boolean(sessionProviderOverride && Object.keys(sessionProviderOverride).length > 0);

  useEffect(() => {
    getAppConfig()
      .then(r => setGlobalConfig(r.openai?.model || '', r.openai?.base_url || ''))
      .catch(() => {});
  }, [setGlobalConfig]);

  // 节点清单和全局渠道都不随会话变，跟着 sid 重拉只会让面板白闪一次。
  useEffect(() => {
    if (!adminToken) return;
    // 读当前值而不是列进依赖：列进去就会被本 effect 自己的 setAvailableNodes 触发重跑。
    if (useSettingsStore.getState().availableNodes.length === 0) {
      getNodes(adminToken)
        .then(nodes => setAvailableNodes(nodes.filter((n: NodeDef) => n.type === 'ai' && !n.id.startsWith('system.'))))
        .catch(() => {});
    }
  }, [adminToken, setAvailableNodes]);

  useEffect(() => {
    // 换会话先清掉上一个会话的覆盖再拉新的。cancelled 挡住乱序响应：慢的那个回来时
    // 面板可能已经指向别的会话了。
    let cancelled = false;
    setSessionProviderOverride(null);
    setEdit({ provider: '', model: '', base_url: '', api_key: '' });
    setApiKeyPresent(false);

    if (sid) {
      getActiveNode(sid)
        .then(r => { if (!cancelled) setActiveNode(r.node_id, r.is_override, r.default_node_id); })
        .catch(() => {});
    }
    if (sid && adminToken) {
      getSessionProviderOverride(sid, adminToken)
        .then(override => {
          if (cancelled) return;
          setSessionProviderOverride(override);
          setEdit({
            provider: stringField(override, 'provider') || stringField(override, 'provider_type'),
            model: stringField(override, 'model'),
            base_url: stringField(override, 'base_url'),
            api_key: '',
          });
          // 只认会话自己那把钥匙：全局配了 key 不代表这个会话有专属覆盖。
          setApiKeyPresent(Boolean(override.api_key));
        })
        .catch(() => {});
    }
    return () => { cancelled = true; };
  }, [adminToken, setActiveNode, setSessionProviderOverride, sid]);

  // 候选只来自渠道配置：会话覆盖原样送上游，节点那套 $ENV{} 展开在这条路上不生效。
  useEffect(() => {
    if (!adminToken) {
      setModelChoices([]);
      setProviderChoices([]);
      setInheritedProvider('');
      return;
    }
    getProviders(adminToken)
      .then(providers => {
        setModelChoices(literalModelsOnly(mergeModelChoices(modelsFromProviders(providers))));
        setProviderChoices(providers.registered || []);
        setInheritedProvider(providers.active_provider || '');
        setModelConfig(activeProviderConfig(providers));
      })
      .catch(() => { setModelChoices([]); setProviderChoices([]); setInheritedProvider(''); });
  }, [adminToken, setModelConfig]);

  const handleCopySessionId = async () => {
    if (!sid) return;
    setCopyMsg('');
    try {
      await navigator.clipboard?.writeText(sid);
      setCopyMsg('已复制');
    } catch {
      setCopyMsg('复制失败');
    }
  };

  const handleSwitchNode = async (targetId: string) => {
    setNodeMsg('');
    if (!sid) {
      setActiveNode(targetId, Boolean(targetId), defaultNodeId || '');
      setNodeMsg('已为下一条消息保存');
      return;
    }
    setNodeSaving(true);
    try {
      const result = await switchNode(sid, targetId);
      if (result?.ok) {
        setActiveNode(result.target_node_id, result.is_override, result.default_node_id || '');
        setNodeMsg('已保存');
      } else {
        setNodeMsg('切换失败');
      }
    } catch (err) {
      setNodeMsg(err instanceof Error ? err.message : '切换失败');
    } finally {
      setNodeSaving(false);
    }
  };

  const handleSaveModel = async () => {
    setModelMsg('');
    if (!sid) { setModelMsg('没有活动会话'); return; }
    if (!adminToken) { setModelMsg('需要管理员令牌'); return; }

    setModelSaving(true);
    try {
      const next = { ...(sessionProviderOverride || {}) } as Record<string, unknown>;
      for (const key of ['provider', 'model', 'base_url'] as const) {
        const value = edit[key].trim();
        if (value) next[key] = value;
        else delete next[key];
      }
      if (edit.api_key.trim()) next.api_key = edit.api_key.trim();

      const saved = await updateSessionProviderOverride(sid, adminToken, next);
      setSessionProviderOverride(saved);
      setEdit({
        provider: stringField(saved, 'provider') || stringField(saved, 'provider_type'),
        model: stringField(saved, 'model'),
        base_url: stringField(saved, 'base_url'),
        api_key: '',
      });
      setApiKeyPresent(Boolean(saved.api_key));
      setModelMsg('已保存');
    } catch (err) {
      setModelMsg(err instanceof Error ? err.message : '保存失败');
    } finally {
      setModelSaving(false);
    }
  };

  const handleClearModel = async () => {
    setModelMsg('');
    if (!sid) { setModelMsg('没有活动会话'); return; }
    if (!adminToken) { setModelMsg('需要管理员令牌'); return; }

    setModelSaving(true);
    try {
      const cleared = await clearSessionProviderOverride(sid, adminToken);
      setSessionProviderOverride(cleared);
      setEdit({ provider: '', model: '', base_url: '', api_key: '' });
      setApiKeyPresent(false);
      setModelMsg('已清除');
    } catch (err) {
      setModelMsg(err instanceof Error ? err.message : '清除失败');
    } finally {
      setModelSaving(false);
    }
  };

  return (
    <section aria-label="会话配置面板" className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-mono text-[0.65rem] font-semibold uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">会话配置</h2>
        {focus !== 'default' && (
          <button
            className="text-[0.65rem] text-[var(--duties-tertiary)] hover:text-[var(--duties-text)]"
            onClick={() => {
              // [2026-06-01] Focused session panels can appear inside settings or
              // the modal overlay. Why: earlier right-rail routing has been removed.
              // How: route Back to the General settings tab without touching the
              // chat dashboard. Purpose: focused panels stay reusable without a
              // second layout state machine.
              setSettingsTab('general');
            }}
            type="button"
          >
            返回
          </button>
        )}
      </div>

      {(focus === 'default' || focus === 'node') && (
        <details className="mb-3 group">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-2 border border-[var(--duties-border)] px-2.5 py-1.5 text-[0.68rem] text-[var(--duties-secondary)] hover:border-[var(--duties-text)] [&::-webkit-details-marker]:hidden">
            <span className="min-w-0 truncate font-mono">
              会话：{shortSessionId(sid)}
            </span>
            <span className="flex flex-shrink-0 items-center gap-1.5 text-[0.62rem]">
              <span aria-label={connectionAriaLabel(connectionStatus, connectionStatusLabel)} className={`inline-block h-1.5 w-1.5 rounded-full ${connectionDotClass(connectionStatus)}`} />
              {connectionStatusLabel && <span>{connectionStatusLabel}</span>}
            </span>
          </summary>
          <div className="border-x border-b border-[var(--duties-border)] p-2.5">
            <p className="mb-1 font-mono text-[0.58rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">完整会话 ID</p>
            <button
              className="w-full truncate border border-[var(--duties-border)] px-2 py-1.5 text-left font-mono text-[0.62rem] text-[var(--duties-secondary)] hover:border-[var(--duties-text)]"
              disabled={!sid}
              onClick={handleCopySessionId}
              title={sid || '没有活动会话'}
              type="button"
            >
              {sid || '没有活动会话'}
            </button>
            {copyMsg && <p className="mt-1 text-[0.62rem] text-[var(--duties-tertiary)]">{copyMsg}</p>}
          </div>
        </details>
      )}

      {(focus === 'default' || focus === 'node') && (
        <div className={`mb-3 border p-2.5 ${focus === 'node' ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
          <div className="mb-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="font-mono text-[0.58rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">活动节点</p>
              <p className="mt-1 truncate text-xs font-semibold">{activeNode?.name || displayNodeId || '默认节点'}</p>
              <p className="mt-0.5 truncate font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{displayNodeId || '（未设置）'}</p>
            </div>
            <span className="flex-shrink-0 font-mono text-[0.55rem] uppercase tracking-[0.12em] text-[var(--duties-tertiary)]">
              {activeNodeIsOverride ? '覆盖' : '默认'}
            </span>
          </div>
          <select
            className="mb-2 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            disabled={nodeSaving}
            onChange={e => handleSwitchNode(e.target.value)}
            value={activeNodeIsOverride ? activeNodeId : ''}
          >
            <option value="">使用默认节点</option>
            {switchableNodes.map(n => (
              <option key={n.id} value={n.id}>{n.name || n.id}</option>
            ))}
          </select>
          {nodeMsg && <p className="text-[0.62rem] text-[var(--duties-tertiary)]">{nodeMsg}</p>}
        </div>
      )}

      {(focus === 'default' || focus === 'model') && (
        <div className={`border p-2.5 ${focus === 'model' ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
          <div className="mb-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="font-mono text-[0.58rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">会话模型</p>
              <p className="mt-1 truncate text-xs font-semibold">
                {effectiveModel} <span className="font-normal text-[0.62rem] text-[var(--duties-tertiary)]">{effectiveModelSourceLabel}</span>
              </p>
              <p className="mt-0.5 truncate font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
                {effectiveBaseUrl} <span>{effectiveBaseUrlSourceLabel}</span>
              </p>
            </div>
            <span className="flex-shrink-0 font-mono text-[0.55rem] uppercase tracking-[0.12em] text-[var(--duties-tertiary)]">
              {hasSessionOverride ? '会话' : '继承'}
            </span>
          </div>

          <label className="mb-1 block text-[0.62rem] text-[var(--duties-secondary)]">供应商</label>
          <input
            aria-label="供应商"
            className="mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            list={providerChoices.length ? providerListId : undefined}
            onChange={e => setEdit(p => ({ ...p, provider: e.target.value }))}
            placeholder={stringField(sessionProviderOverride, 'provider')
              || inheritedPlaceholder(inheritedProvider, '继承的供应商')}
            value={edit.provider}
          />
          {providerChoices.length > 0 && (
            <datalist id={providerListId}>
              {providerChoices.map(provider => <option key={provider} value={provider} />)}
            </datalist>
          )}

          <label className="mb-1 block text-[0.62rem] text-[var(--duties-secondary)]">模型</label>
          <input
            className="mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            list={modelChoices.length ? modelListId : undefined}
            onChange={e => setEdit(p => ({ ...p, model: e.target.value }))}
            placeholder={inheritedPlaceholder(nodeModel || globalModel, '继承的模型')}
            value={edit.model}
          />
          {modelChoices.length > 0 && (
            <datalist id={modelListId}>
              {modelChoices.map(model => <option key={model} value={model} />)}
            </datalist>
          )}

          <label className="mb-1 block text-[0.62rem] text-[var(--duties-secondary)]">基础 URL</label>
          <input
            className="mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={e => setEdit(p => ({ ...p, base_url: e.target.value }))}
            placeholder={inheritedPlaceholder(nodeBaseUrl || globalBaseUrl, '继承的基础 URL')}
            value={edit.base_url}
          />

          <label className="mb-1 block text-[0.62rem] text-[var(--duties-secondary)]">API 密钥 {apiKeyPresent ? '（已设置）' : '（可选）'}</label>
          <input
            className="mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-[0.65rem] text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={e => setEdit(p => ({ ...p, api_key: e.target.value }))}
            placeholder="留空以保留当前值"
            type="password"
            value={edit.api_key}
          />

          {modelMsg && <p className="mb-2 text-[0.62rem] text-[var(--duties-tertiary)]">{modelMsg}</p>}

          {/* Save/Clear only visible when user has modified something */}
          {(edit.provider.trim() !== '' || edit.model.trim() !== '' || edit.base_url.trim() !== '' || edit.api_key.trim() !== '' || hasSessionOverride) && (
            <div className="flex gap-2">
              <Button className="h-8 flex-1 px-2 text-[0.55rem]" disabled={modelSaving || (!edit.provider.trim() && !edit.model.trim() && !edit.base_url.trim() && !edit.api_key.trim())} onClick={handleSaveModel} variant="primary">
                {modelSaving ? '保存中…' : '保存'}
              </Button>
              {hasSessionOverride && (
                <Button className="h-8 flex-1 px-2 text-[0.55rem]" disabled={modelSaving} onClick={handleClearModel} variant="ghost">
                  清除
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
};

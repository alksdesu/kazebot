// [2026-06-01] Embedded session configuration panel for the right column.
// Why: node switching and model overrides are session-scoped actions, but the old
// UI used separate modal panels that hid the chat. How: this compact component
// combines session identity, connection status, node switching, and provider
// override editing in the 60% upper section of the right panel. Purpose: Header
// text clicks and the default right panel all operate on the current session.
import { useEffect, useId, useMemo, useState } from 'react';

import {
  channelChoices,
  clearSessionProviderOverride,
  switchNode,
  updateSessionProviderOverride,
} from '../../api/supervisorClient';
import { useChatStore } from '../../store/chatStore';
import { useRequestGuard } from '../../hooks/useRequestGuard';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { readActiveNode, readGlobalConfig, readNodeList, readProviderTable, readSessionOverride } from '../../store/settingsReads';
import {
  inheritanceSourcesOf,
  sessionLayerOf,
  sessionNodeLabel,
  sessionNodeRowOf,
  sessionOverrideOf,
  useSettingsStore,
} from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { rootEntryNodes } from '../../utils/entryNodes';
import { describeEnvRef, envRefNames } from '../../utils/envRef';
import { inheritedField, inheritedProvider, type Inherited, type InheritedSource, type LayerStatus } from '../../utils/inheritedValue';
import { literalModelsOnly, mergeModelChoices, modelsFromProviders } from '../../utils/modelChoices';
import { connectionDotClass } from '../../utils/systemStatus';
import { Button } from '../settings/pages/settingsControls';
import { errorMessage } from '../../api/request';

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

/** 面板自己读的几路：会话的节点、会话的覆盖，和顶栏也在用的那几份共用数据。 */
type ReadLane = 'node' | 'override' | 'shared';

interface ReadFailure {
  readonly name: string;
  readonly reason: string;
  readonly lane: ReadLane;
}

const LAYER_NAMES: Record<InheritedSource, string> = {
  session: '会话覆盖',
  node: '节点',
  channel: '渠道表',
  global: '全局配置',
};

/** 生效值还不知道时怎么说。 */
const PENDING_NOTES: Record<Exclude<LayerStatus, 'loaded'>, string> = {
  loading: '读取中',
  failed: '没读到',
  missing: '节点找不到',
};

function stringField(source: Record<string, unknown> | null | undefined, key: string): string {
  const value = source?.[key];
  return typeof value === 'string' ? value : '';
}

function editOf(override: Record<string, unknown> | null | undefined): SessionModelEdit {
  return {
    provider: stringField(override, 'provider') || stringField(override, 'provider_type'),
    model: stringField(override, 'model'),
    base_url: stringField(override, 'base_url'),
    api_key: '',
  };
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

function inheritedPlaceholder(inherited: Inherited, fallback: string): string {
  if (!inherited.known) return `${fallback}（${PENDING_NOTES[inherited.status]}）`;
  // 继承值是 $ENV{...} 时不能原样当 placeholder：会话覆盖不展开它，照抄进输入框必挂。
  const names = envRefNames(inherited.value);
  if (names.length) return `继承自环境变量 ${names.join(' 或 ')}`;
  return inherited.value || fallback;
}

function inheritedText(inherited: Inherited): string {
  if (!inherited.known) return inherited.status === 'loading' ? '读取中…' : PENDING_NOTES[inherited.status];
  // 节点写的是 $ENV{...}，展开发生在引擎侧，前端拿不到值 —— 摆变量名比摆模板可读。
  return describeEnvRef(inherited.value) || '（默认）';
}

function inheritedSourceLabel(inherited: Inherited): string {
  if (!inherited.known) {
    if (inherited.status === 'missing') return `（节点 ${inherited.origin} 不在节点表里）`;
    return inherited.status === 'failed' ? `（${LAYER_NAMES[inherited.source]}读取失败）` : '';
  }
  if (inherited.source === 'session') return '（会话覆盖）';
  if (inherited.source === 'node') return `（节点：${inherited.origin || '当前节点'}）`;
  if (inherited.source === 'channel') return `（渠道：${inherited.origin}）`;
  if (inherited.source === 'global') return '（全局）';
  return '（默认）';
}

export const SessionConfigPanel = ({ sessionId, focus = 'default' }: SessionConfigPanelProps) => {
  const sid = safeSessionId(sessionId);
  const settings = useSettingsStore();
  const {
    adminToken,
    availableNodes,
    nodeListRead,
    defaultNodeId,
    globalConfigRead,
    providerTable,
    providerTableRead,
    setActiveNode,
    setSessionProviderOverride,
  } = settings;
  // 节点清单和渠道候选跟着登录走；会话覆盖、切节点跟着会话走，换会话就作废。
  const requests = useRequestGuard();
  const session = useRequestGuard(sid);
  const setSettingsTab = useViewStore(state => state.setSettingsTab);

  const [copyMsg, setCopyMsg] = useState('');
  const [nodeMsg, setNodeMsg] = useState('');
  const [nodeSaving, setNodeSaving] = useState(false);
  const [modelSaving, setModelSaving] = useState(false);
  const [modelLoaded, setModelLoaded] = useState(false);
  const [modelMsg, setModelMsg] = useState('');
  const [apiKeyPresent, setApiKeyPresent] = useState(false);
  const [edit, setEdit] = useState<SessionModelEdit>(() => editOf(null));
  const [savedEdit, setSavedEdit] = useState<SessionModelEdit>(() => editOf(null));
  useUnsavedChanges(JSON.stringify(edit) !== JSON.stringify(savedEdit), '会话模型还有未保存修改，确定离开吗？');
  const [nodeError, setNodeError] = useState('');
  const [overrideError, setOverrideError] = useState('');
  const [nodeAttempt, setNodeAttempt] = useState(0);
  const [overrideAttempt, setOverrideAttempt] = useState(0);
  const [sharedAttempt, setSharedAttempt] = useState(0);
  const connectionStatus = useChatStore((state) => state.connectionStatus);
  const modelListId = useId();
  const providerListId = useId();

  const node = sessionNodeRowOf(settings, sid);
  const override = sessionOverrideOf(settings, sid);
  const switchableNodes = useMemo(() => rootEntryNodes(availableNodes), [availableNodes]);
  // 会话记着的是节点的旧 id（aliases）时，下拉里对得上的是表里那一行的 id
  const currentNodeId = node.row?.id || node.nodeId;
  // 会话被切到了清单外的节点上，下拉里也得有一项对得上它，不然显示出来就成了「使用默认节点」
  const unlistedNodeId = node.known && node.isOverride && currentNodeId && !switchableNodes.some(item => item.id === currentNodeId) ? currentNodeId : '';
  // 覆盖读成以后看编辑框：生效值和占位都按存下去以后的样子算
  const sessionLayer = modelLoaded ? { status: 'loaded' as const, fields: edit } : sessionLayerOf(settings, sid, overrideError);
  const sources = inheritanceSourcesOf(settings, sid, sessionLayer, nodeError);
  const effectiveModel = inheritedField(sources, 'model');
  const effectiveBaseUrl = inheritedField(sources, 'base_url');
  const inheritedModel = inheritedField(sources, 'model', false);
  const inheritedBaseUrl = inheritedField(sources, 'base_url', false);
  const inheritedChannel = inheritedProvider(sources, false);
  const connectionStatusLabel = connectionLabel(connectionStatus);
  const hasSessionOverride = Boolean(override.override && Object.keys(override.override).length > 0);

  // 候选只来自渠道配置：会话覆盖原样送上游，节点那套 $ENV{} 展开在这条路上不生效。
  const candidateTable = adminToken && providerTableRead.loaded ? providerTable : null;
  const modelChoices = useMemo(
    () => (candidateTable ? literalModelsOnly(mergeModelChoices(modelsFromProviders(candidateTable))) : []),
    [candidateTable],
  );
  const providerChoices = useMemo(() => {
    if (!candidateTable) return [];
    // 已配渠道排前面：它们带着地址密钥，裸线格式还得自己填。
    const choices = channelChoices(candidateTable);
    return [...choices.channels.map((c) => c.value), ...choices.wires];
  }, [candidateTable]);

  const showNode = focus === 'default' || focus === 'node';
  const showModel = focus === 'default' || focus === 'model';
  // 全局配置只在哪个值真的追到了那一层时才算挡住了；渠道表还管着候选，模型区露出来就算
  const fieldChains = [effectiveModel, effectiveBaseUrl, inheritedModel, inheritedBaseUrl];
  const failures: ReadFailure[] = [];
  // 节点那一路顶栏替它读成了也算数，所以只看 store 里有没有这个会话的节点
  if (!node.known && nodeError) failures.push({ name: '当前节点', reason: nodeError, lane: 'node' });
  if (nodeListRead.error && (showNode || (node.known && node.nodeId))) failures.push({ name: '节点清单', reason: nodeListRead.error, lane: 'shared' });
  // 覆盖那一路要本面板自己读成才能编辑，顶栏读成了也照样报
  if (showModel && !modelLoaded && overrideError) failures.push({ name: '会话覆盖', reason: overrideError, lane: 'override' });
  if (showModel && providerTableRead.error) failures.push({ name: '渠道表', reason: providerTableRead.error, lane: 'shared' });
  if (showModel && globalConfigRead.error && fieldChains.some(chain => !chain.known && chain.status === 'failed' && chain.source === 'global')) {
    failures.push({ name: '全局配置', reason: globalConfigRead.error, lane: 'shared' });
  }

  const retryFailed = () => {
    const lanes = new Set(failures.map(failure => failure.lane));
    if (lanes.has('node')) setNodeAttempt(value => value + 1);
    if (lanes.has('override')) setOverrideAttempt(value => value + 1);
    if (lanes.has('shared')) setSharedAttempt(value => value + 1);
  };

  useEffect(() => {
    void readGlobalConfig(requests.begin('config'));
  }, [requests, sharedAttempt]);

  // 节点表不随会话变，读成过就不再拉；顶栏和登录后加载读到的也是这一份。
  useEffect(() => {
    if (!adminToken || nodeListRead.loaded) return;
    void readNodeList(adminToken, requests.begin('nodes'));
    return () => { requests.drop('nodes'); };
  }, [requests, adminToken, nodeListRead.loaded, sharedAttempt]);

  // 渠道表每次打开都重读，候选跟得上刚在控制台改过的渠道；重读不成就沿用读成过的那份。
  useEffect(() => {
    if (!adminToken) return;
    void readProviderTable(adminToken, requests.begin('providers'));
  }, [requests, adminToken, sharedAttempt]);

  useEffect(() => {
    setNodeSaving(false); setModelSaving(false); setModelLoaded(false); setModelMsg(sid ? '' : '没有活动会话'); setNodeMsg('');
    setEdit(editOf(null));
    setSavedEdit(editOf(null));
    setApiKeyPresent(false);
  }, [session, sid]);

  useEffect(() => {
    setNodeError('');
    if (!sid) return;
    const request = session.begin('node');
    void readActiveNode(sid, request).then(outcome => {
      if (outcome && !outcome.ok && request.isCurrent()) setNodeError(outcome.error);
    });
  }, [session, sid, nodeAttempt]);

  // 上一个会话慢回来的响应由按会话换代的守卫挡掉；store 里留着的别的会话的覆盖不算这个会话的，不用先清。
  useEffect(() => {
    setOverrideError('');
    if (!sid || !adminToken) return;
    const request = session.begin('override');
    void readSessionOverride(sid, adminToken, request).then(outcome => {
      if (!outcome || !request.isCurrent()) return;
      if (!outcome.ok) {
        setOverrideError(outcome.error);
        return;
      }
      setEdit(editOf(outcome.value));
      setSavedEdit(editOf(outcome.value));
      // 只认会话自己那把钥匙：全局配了 key 不代表这个会话有专属覆盖。
      setApiKeyPresent(Boolean(outcome.value.api_key));
      setModelLoaded(true);
    });
  }, [session, sid, adminToken, overrideAttempt]);

  const handleCopySessionId = async () => {
    if (!sid) return;
    setCopyMsg('');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('浏览器不支持复制');
      await navigator.clipboard.writeText(sid);
      setCopyMsg('已复制');
    } catch {
      setCopyMsg('复制失败');
    }
  };

  const handleSwitchNode = async (targetId: string) => {
    if (nodeSaving) return;
    setNodeMsg('');
    if (!sid) {
      setActiveNode(targetId, Boolean(targetId), defaultNodeId || '', '');
      setNodeMsg('已为下一条消息保存');
      return;
    }
    setNodeSaving(true);
    try {
      const result = await switchNode(sid, targetId);
      if (!session.isCurrent()) return;
      if (result?.ok) {
        setActiveNode(result.target_node_id, result.is_override, result.default_node_id || '', sid);
        setNodeMsg('已保存');
      } else {
        setNodeMsg('切换失败');
      }
    } catch (err) {
      if (!session.isCurrent()) return;
      setNodeMsg(errorMessage(err, '切换失败'));
    } finally {
      if (session.isCurrent()) setNodeSaving(false);
    }
  };

  const handleSaveModel = async () => {
    if (modelSaving || !modelLoaded) return;
    setModelMsg('');
    if (!sid) { setModelMsg('没有活动会话'); return; }
    if (!adminToken) { setModelMsg('需要管理员令牌'); return; }

    setModelSaving(true);
    try {
      const next = { ...(override.override || {}) } as Record<string, unknown>;
      for (const key of ['provider', 'model', 'base_url'] as const) {
        const value = edit[key].trim();
        if (value) next[key] = value;
        else delete next[key];
      }
      if (edit.api_key.trim()) next.api_key = edit.api_key.trim();

      const saved = await updateSessionProviderOverride(sid, adminToken, next);
      if (!session.isCurrent()) return;
      setSessionProviderOverride(saved, sid);
      setEdit(editOf(saved));
      setSavedEdit(editOf(saved));
      setApiKeyPresent(Boolean(saved.api_key));
      setModelMsg('已保存');
    } catch (err) {
      if (!session.isCurrent()) return;
      setModelMsg(errorMessage(err, '保存失败'));
    } finally {
      if (session.isCurrent()) setModelSaving(false);
    }
  };

  const handleClearModel = async () => {
    if (modelSaving || !modelLoaded || !window.confirm('清除当前会话的模型、地址和密钥覆盖，恢复继承配置？')) return;
    setModelMsg('');
    if (!sid) { setModelMsg('没有活动会话'); return; }
    if (!adminToken) { setModelMsg('需要管理员令牌'); return; }

    setModelSaving(true);
    try {
      const cleared = await clearSessionProviderOverride(sid, adminToken);
      if (!session.isCurrent()) return;
      setSessionProviderOverride(cleared, sid);
      setEdit(editOf(null));
      setSavedEdit(editOf(null));
      setApiKeyPresent(false);
      setModelMsg('已清除');
    } catch (err) {
      if (!session.isCurrent()) return;
      setModelMsg(errorMessage(err, '清除失败'));
    } finally {
      if (session.isCurrent()) setModelSaving(false);
    }
  };

  const nodeState = nodeError ? '当前节点没读到' : '读取节点…';

  return (
    <section aria-label="会话配置面板" className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-mono text-mini font-semibold uppercase tracking-caps text-[var(--duties-tertiary)]">会话配置</h2>
        {focus !== 'default' && (
          <button
            className="text-mini text-[var(--duties-tertiary)] hover:text-[var(--duties-text)]"
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

      {failures.length > 0 && (
        <div className="mb-3 text-micro text-[var(--duties-danger)]" role="alert">
          {failures.map(failure => <p key={failure.name}>{failure.name}读取失败：{failure.reason}</p>)}
          <button className="mt-1 underline underline-offset-2" onClick={retryFailed} type="button">重试</button>
        </div>
      )}

      {showNode && (
        <details className="mb-3 group">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-2 border border-[var(--duties-border)] px-2.5 py-1.5 text-mini text-[var(--duties-secondary)] hover:border-[var(--duties-text)] [&::-webkit-details-marker]:hidden">
            <span className="min-w-0 truncate font-mono">
              会话：{shortSessionId(sid)}
            </span>
            <span className="flex flex-shrink-0 items-center gap-1.5 text-micro">
              <span aria-label={connectionAriaLabel(connectionStatus, connectionStatusLabel)} className={`inline-block h-1.5 w-1.5 rounded-full ${connectionDotClass(connectionStatus)}`} />
              {connectionStatusLabel && <span>{connectionStatusLabel}</span>}
            </span>
          </summary>
          <div className="border-x border-b border-[var(--duties-border)] p-2.5">
            <p className="mb-1 font-mono text-nano uppercase tracking-caps text-[var(--duties-tertiary)]">完整会话 ID</p>
            <button
              className="w-full truncate border border-[var(--duties-border)] px-2 py-1.5 text-left font-mono text-micro text-[var(--duties-secondary)] hover:border-[var(--duties-text)]"
              disabled={!sid}
              onClick={handleCopySessionId}
              title={sid || '没有活动会话'}
              type="button"
            >
              {sid || '没有活动会话'}
            </button>
            {copyMsg && <p className="mt-1 text-micro text-[var(--duties-tertiary)]">{copyMsg}</p>}
          </div>
        </details>
      )}

      {showNode && (
        <div className={`mb-3 border p-2.5 ${focus === 'node' ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
          <div className="mb-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="font-mono text-nano uppercase tracking-caps text-[var(--duties-tertiary)]">活动节点</p>
              <p className="mt-1 truncate text-xs font-semibold">{node.known ? sessionNodeLabel(node) || '默认节点' : nodeState}</p>
              {node.known && <p className="mt-0.5 truncate font-mono text-micro text-[var(--duties-tertiary)]">{node.nodeId || '（未设置）'}</p>}
            </div>
            {node.known && (
              <span className="flex-shrink-0 font-mono text-nano uppercase tracking-caps text-[var(--duties-tertiary)]">
                {node.isOverride ? '覆盖' : '默认'}
              </span>
            )}
          </div>
          <select
            className="app-input mb-2 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            disabled={nodeSaving || !node.known}
            onChange={e => handleSwitchNode(e.target.value)}
            value={node.known && node.isOverride ? currentNodeId : ''}
          >
            <option value="">{node.known ? '使用默认节点' : nodeState}</option>
            {unlistedNodeId && (
              <option value={unlistedNodeId}>{sessionNodeLabel(node)}{node.row ? '（不在可切换的节点里）' : ''}</option>
            )}
            {switchableNodes.map(n => (
              <option key={n.id} value={n.id}>{n.name || n.id}</option>
            ))}
            {adminToken && !nodeListRead.loaded && (
              <option disabled>{nodeListRead.error ? '节点清单没读到' : '正在读取节点清单…'}</option>
            )}
          </select>
          {nodeMsg && <p className="text-micro text-[var(--duties-tertiary)]">{nodeMsg}</p>}
        </div>
      )}

      {showModel && (
        <div className={`border p-2.5 ${focus === 'model' ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
          <div className="mb-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="font-mono text-nano uppercase tracking-caps text-[var(--duties-tertiary)]">会话模型</p>
              <p className="mt-1 truncate text-xs font-semibold">
                {inheritedText(effectiveModel)} <span className="font-normal text-micro text-[var(--duties-tertiary)]">{inheritedSourceLabel(effectiveModel)}</span>
              </p>
              <p className="mt-0.5 truncate font-mono text-micro text-[var(--duties-tertiary)]">
                {inheritedText(effectiveBaseUrl)} <span>{inheritedSourceLabel(effectiveBaseUrl)}</span>
              </p>
            </div>
            {sessionLayer.status === 'loaded' && (
              <span className="flex-shrink-0 font-mono text-nano uppercase tracking-caps text-[var(--duties-tertiary)]">
                {hasSessionOverride ? '会话' : '继承'}
              </span>
            )}
          </div>

          <label className="mb-1 block text-micro text-[var(--duties-secondary)]">供应商</label>
          <input
            aria-label="供应商"
            className="app-input mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            list={providerChoices.length ? providerListId : undefined}
            disabled={modelSaving || !modelLoaded} onChange={e => setEdit(p => ({ ...p, provider: e.target.value }))}
            placeholder={inheritedPlaceholder(inheritedChannel, '继承的供应商')}
            value={edit.provider}
          />
          {providerChoices.length > 0 && (
            <datalist id={providerListId}>
              {providerChoices.map(provider => <option key={provider} value={provider} />)}
            </datalist>
          )}

          <label className="mb-1 block text-micro text-[var(--duties-secondary)]">模型</label>
          <input
            className="app-input mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            list={modelChoices.length ? modelListId : undefined}
            disabled={modelSaving || !modelLoaded} aria-label="模型" onChange={e => setEdit(p => ({ ...p, model: e.target.value }))}
            placeholder={inheritedPlaceholder(inheritedModel, '继承的模型')}
            value={edit.model}
          />
          {modelChoices.length > 0 && (
            <datalist id={modelListId}>
              {modelChoices.map(model => <option key={model} value={model} />)}
            </datalist>
          )}

          <label className="mb-1 block text-micro text-[var(--duties-secondary)]">基础 URL</label>
          <input
            className="app-input mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            disabled={modelSaving || !modelLoaded} aria-label="基础 URL" onChange={e => setEdit(p => ({ ...p, base_url: e.target.value }))}
            placeholder={inheritedPlaceholder(inheritedBaseUrl, '继承的基础 URL')}
            value={edit.base_url}
          />

          <label className="mb-1 block text-micro text-[var(--duties-secondary)]">API 密钥 {apiKeyPresent ? '（已设置）' : '（可选）'}</label>
          <input
            className="app-input mb-2 w-full border border-[var(--duties-border)] bg-transparent px-2 py-1.5 font-mono text-mini text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            disabled={modelSaving || !modelLoaded} aria-label="API 密钥" onChange={e => setEdit(p => ({ ...p, api_key: e.target.value }))}
            placeholder="留空以保留当前值"
            type="password"
            value={edit.api_key}
          />

          {modelMsg && <p className="mb-2 text-micro text-[var(--duties-tertiary)]">{modelMsg}</p>}

          {/* Save/Clear only visible when user has modified something */}
          {(edit.provider.trim() !== '' || edit.model.trim() !== '' || edit.base_url.trim() !== '' || edit.api_key.trim() !== '' || hasSessionOverride) && (
            <div className="flex gap-2">
              <Button className="h-8 flex-1 px-2 text-nano" disabled={modelSaving || !modelLoaded || (!edit.provider.trim() && !edit.model.trim() && !edit.base_url.trim() && !edit.api_key.trim())} onClick={handleSaveModel}>
                {modelSaving ? '正在保存…' : '保存'}
              </Button>
              {hasSessionOverride && (
                <Button tone="danger" className="h-8 flex-1 px-2 text-nano" disabled={modelSaving || !modelLoaded} onClick={handleClearModel}>
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

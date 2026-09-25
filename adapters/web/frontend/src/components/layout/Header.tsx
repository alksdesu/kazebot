import { useEffect, useState } from 'react';

import { getActiveNode, getAppConfig, getNodes, getSessionProviderOverride } from '../../api/supervisorClient';
import { confirmNavigation, useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useSettingsStore } from '../../store/settingsStore';
import { describeEnvRef } from '../../utils/envRef';
import { Button, Icon, Modal } from '../common';
import { SessionConfigModal } from '../settings/SessionConfigModal';

interface HeaderProps {
  title: string;
  sessionId: string;
  isGenerating: boolean;
  isCancelling?: boolean;
  onCancel?: () => void;
  onReset?: () => void;
  onTitleChange?: (newTitle: string) => void;
  viewingChildNodeId?: string;
  onExitChildSession?: () => void;
}

export const Header = ({ title, sessionId, isGenerating, isCancelling = false, onCancel, onReset, onTitleChange, viewingChildNodeId, onExitChildSession }: HeaderProps) => {
  const {
    adminToken, availableNodes, activeNodeId, activeNodeSessionId, entryNodeId, globalModel,
    sessionProviderOverride, sessionProviderOverrideSessionId, setActiveNode, setGlobalConfig,
    setAvailableNodes, setSessionProviderOverride,
  } = useSettingsStore();
  const [configModalFocus, setConfigModalFocus] = useState<'node' | 'model' | 'title' | null>(null);
  const [draftTitle, setDraftTitle] = useState(title);
  const [nodeError, setNodeError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const confirmDiscardTitle = useUnsavedChanges(configModalFocus === 'title' && draftTitle.trim() !== title, '对话标题尚未保存，确定放弃修改吗？');
  const sid = sessionId === 'no-session' ? '' : sessionId;
  const nodeResolved = !sid || activeNodeSessionId === sid;
  const displayNodeId = nodeResolved ? (activeNodeSessionId === sid ? activeNodeId : '') || entryNodeId : '';
  const activeNode = availableNodes.find(node => node.id === displayNodeId);
  const nodeModel = typeof activeNode?.model === 'string' ? activeNode.model : '';
  const sessionModel = sessionProviderOverrideSessionId === sid && typeof sessionProviderOverride?.model === 'string' ? sessionProviderOverride.model : '';
  const displayModel = describeEnvRef(sessionModel || nodeModel || globalModel) || '跟随默认模型';

  useEffect(() => { setDraftTitle(title); }, [title]);
  useEffect(() => { setConfigModalFocus(null); }, [sessionId]);
  useEffect(() => {
    let active = true;
    setNodeError('');
    if (sid) {
      getActiveNode(sid).then(result => {
        if (active) setActiveNode(result.node_id, result.is_override, result.default_node_id, sid);
      }).catch(() => { if (active) setNodeError('节点状态读取失败'); });
    }
    if (sid && adminToken) {
      getSessionProviderOverride(sid, adminToken).then(result => {
        if (active) setSessionProviderOverride(result, sid);
      }).catch(() => { if (active) setSessionProviderOverride(null, sid); });
    }
    return () => { active = false; };
  }, [sid, adminToken, refresh, setActiveNode, setSessionProviderOverride]);
  useEffect(() => {
    let active = true;
    getAppConfig().then(result => {
      if (active) setGlobalConfig(result.openai?.model || '', result.openai?.base_url || '');
    }).catch(() => {});
    return () => { active = false; };
  }, [setGlobalConfig]);
  useEffect(() => {
    if (availableNodes.length > 0 || !adminToken) return;
    let active = true;
    getNodes(adminToken).then(nodes => {
      if (active) setAvailableNodes(nodes.filter(node => node.type === 'ai' && !node.id.startsWith('system.')));
    }).catch(() => {});
    return () => { active = false; };
  }, [adminToken, availableNodes.length, setAvailableNodes]);

  const closeConfig = () => {
    if (configModalFocus === 'title' ? !confirmDiscardTitle() : !confirmNavigation()) return;
    setConfigModalFocus(null);
  };
  const saveTitle = () => {
    const trimmed = draftTitle.trim();
    if (!trimmed) return;
    if (trimmed !== title) onTitleChange?.(trimmed);
    setConfigModalFocus(null);
  };

  return <>
    <header className="app-header">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h2 className="min-w-0 truncate text-base font-semibold">
          {onTitleChange && !viewingChildNodeId ? <button className="max-w-full truncate text-left" title="点击编辑标题" type="button" onClick={() => { setDraftTitle(title); setConfigModalFocus('title'); }}>{title}</button> : title}
        </h2>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--duties-secondary)]">
          <button className="inline-flex max-w-full items-center gap-1.5 text-left" type="button" onClick={() => setConfigModalFocus('node')} title="切换节点">
            <Icon name="hub" size={15} /><span className="truncate">{!nodeResolved ? '读取节点…' : activeNode?.name || displayNodeId || '选择节点'}</span>
          </button>
          <button className="inline-flex max-w-full items-center gap-1.5 text-left" type="button" onClick={() => setConfigModalFocus('model')} title="模型配置">
            <Icon name="model_training" size={15} /><span className="truncate">{displayModel}</span>
          </button>
          {nodeError && <button type="button" className="text-[var(--duties-danger)] underline" onClick={() => setRefresh(value => value + 1)}>{nodeError}，重试</button>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {viewingChildNodeId && onExitChildSession && <Button onClick={onExitChildSession}><Icon name="arrow_back" size={16} /><span className="hidden sm:inline">返回父会话</span><span className="sm:hidden">返回</span></Button>}
        {!viewingChildNodeId && isGenerating && onCancel && <Button loading={isCancelling} onClick={onCancel}><Icon name="cancel" size={16} />取消</Button>}
        {!viewingChildNodeId && !isGenerating && onReset && <Button aria-label="重置对话" onClick={onReset} title="重置对话"><Icon name="refresh" size={16} /></Button>}
      </div>
    </header>
    <Modal open={configModalFocus === 'title'} title="编辑对话标题" onClose={closeConfig} maxWidth="max-w-sm">
      <div className="space-y-4 p-4">
        <label className="block text-sm" htmlFor="conversation-title">对话标题</label>
        <input id="conversation-title" data-autofocus="true" className="app-input w-full" value={draftTitle} maxLength={200} onChange={event => setDraftTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) saveTitle(); }} />
        <div className="flex justify-end gap-2"><Button onClick={closeConfig}>取消</Button><Button variant="primary" disabled={!draftTitle.trim()} onClick={saveTitle}>保存</Button></div>
      </div>
    </Modal>
    {(configModalFocus === 'node' || configModalFocus === 'model') && <SessionConfigModal focus={configModalFocus} onClose={closeConfig} sessionId={sessionId} />}
  </>;
};

import { useEffect, useState } from 'react';

import { useRequestGuard } from '../../hooks/useRequestGuard';
import { confirmNavigation, useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { readActiveNode, readGlobalConfig, readNodeList, readProviderTable, readSessionOverride } from '../../store/settingsReads';
import { inheritanceSourcesOf, sessionLayerOf, sessionNodeLabel, sessionNodeRowOf, useSettingsStore } from '../../store/settingsStore';
import { describeEnvRef } from '../../utils/envRef';
import { inheritedField, type Inherited } from '../../utils/inheritedValue';
import { Icon, Modal } from '../common';
import { SessionConfigModal } from '../settings/SessionConfigModal';
import { Button } from '../settings/pages/settingsControls';

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

function modelText(model: Inherited): string {
  if (model.known) return describeEnvRef(model.value) || '跟随默认模型';
  if (model.status === 'missing') return '节点找不到';
  return model.status === 'failed' ? '模型没读到' : '读取模型…';
}

export const Header = ({ title, sessionId, isGenerating, isCancelling = false, onCancel, onReset, onTitleChange, viewingChildNodeId, onExitChildSession }: HeaderProps) => {
  const settings = useSettingsStore();
  const { adminToken, nodeListRead, globalConfigRead, providerTableRead } = settings;
  const [configModalFocus, setConfigModalFocus] = useState<'node' | 'model' | 'title' | null>(null);
  const [draftTitle, setDraftTitle] = useState(title);
  // 这两路只在 store 里还没有这个会话的那份时才报：会话面板替它读成了，这里的红字自己就该消失
  const [nodeError, setNodeError] = useState('');
  const [overrideError, setOverrideError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const requests = useRequestGuard();
  const confirmDiscardTitle = useUnsavedChanges(configModalFocus === 'title' && draftTitle.trim() !== title, '对话标题尚未保存，确定放弃修改吗？');
  const sid = sessionId === 'no-session' ? '' : sessionId;

  const node = sessionNodeRowOf(settings, sid);
  const model = inheritedField(inheritanceSourcesOf(settings, sid, sessionLayerOf(settings, sid, overrideError), nodeError), 'model');
  const nodeLabel = !node.known ? (nodeError ? '节点没读到' : '读取节点…') : sessionNodeLabel(node) || '选择节点';
  const modelLabel = modelText(model);

  // 只报挡住了顶栏上这两项的失败：没用上的那几路读不读得到，这里看不出差别；节点表里认不出节点不是读失败，重试也读不出来
  const failures = new Map<string, string>();
  if (!node.known && nodeError) failures.set('当前节点', nodeError);
  if (!model.known && model.status === 'failed') {
    if (model.source === 'session') failures.set('会话覆盖', overrideError);
    if (model.source === 'node' && node.known) failures.set('节点清单', nodeListRead.error);
    if (model.source === 'channel') failures.set('渠道表', providerTableRead.error);
    if (model.source === 'global') failures.set('全局配置', globalConfigRead.error);
  }
  const failureNames = [...failures.keys()].join('、');
  const failureDetail = [...failures].map(([name, reason]) => `${name}：${reason}`).join('\n');

  useEffect(() => { setDraftTitle(title); }, [title]);
  useEffect(() => { setConfigModalFocus(null); }, [sessionId]);
  useEffect(() => {
    const request = requests.begin('session');
    setNodeError('');
    setOverrideError('');
    if (sid) {
      void readActiveNode(sid, request).then(outcome => {
        if (outcome && !outcome.ok && request.isCurrent()) setNodeError(outcome.error);
      });
    }
    if (sid && adminToken) {
      void readSessionOverride(sid, adminToken, request).then(outcome => {
        if (outcome && !outcome.ok && request.isCurrent()) setOverrideError(outcome.error);
      });
    }
    return () => { requests.drop('session'); };
  }, [requests, sid, adminToken, refresh]);
  useEffect(() => {
    void readGlobalConfig(requests.begin('config'));
  }, [requests, refresh]);
  useEffect(() => {
    if (nodeListRead.loaded || !adminToken) return;
    void readNodeList(adminToken, requests.begin('nodes'));
    return () => { requests.drop('nodes'); };
  }, [requests, adminToken, nodeListRead.loaded, refresh]);
  useEffect(() => {
    if (providerTableRead.loaded || !adminToken) return;
    void readProviderTable(adminToken, requests.begin('providers'));
    return () => { requests.drop('providers'); };
  }, [requests, adminToken, providerTableRead.loaded, refresh]);

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
            <Icon name="hub" size={15} /><span className="truncate">{nodeLabel}</span>
          </button>
          <button className="inline-flex max-w-full items-center gap-1.5 text-left" type="button" onClick={() => setConfigModalFocus('model')} title="模型配置">
            <Icon name="model_training" size={15} /><span className="truncate">{modelLabel}</span>
          </button>
          {failureNames && <button type="button" className="text-[var(--duties-danger)] underline" title={failureDetail} onClick={() => setRefresh(value => value + 1)}>{failureNames}读取失败，重试</button>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {viewingChildNodeId && onExitChildSession && <Button tone="quiet" onClick={onExitChildSession}><Icon name="arrow_back" size={16} /><span className="hidden sm:inline">返回父会话</span><span className="sm:hidden">返回</span></Button>}
        {!viewingChildNodeId && isGenerating && onCancel && <Button tone="quiet" loading={isCancelling} onClick={onCancel}><Icon name="cancel" size={16} />取消</Button>}
        {!viewingChildNodeId && !isGenerating && onReset && <Button tone="quiet" aria-label="重置对话" onClick={onReset} title="重置对话"><Icon name="refresh" size={16} /></Button>}
      </div>
    </header>
    <Modal open={configModalFocus === 'title'} title="编辑对话标题" onClose={closeConfig} maxWidth="max-w-sm">
      <div className="space-y-4 p-4">
        <label className="block text-sm" htmlFor="conversation-title">对话标题</label>
        <input id="conversation-title" data-autofocus="true" className="app-input w-full" value={draftTitle} maxLength={200} onChange={event => setDraftTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) saveTitle(); }} />
        <div className="flex justify-end gap-2"><Button tone="quiet" onClick={closeConfig}>取消</Button><Button disabled={!draftTitle.trim()} onClick={saveTitle}>保存</Button></div>
      </div>
    </Modal>
    {(configModalFocus === 'node' || configModalFocus === 'model') && <SessionConfigModal focus={configModalFocus} onClose={closeConfig} sessionId={sessionId} />}
  </>;
};

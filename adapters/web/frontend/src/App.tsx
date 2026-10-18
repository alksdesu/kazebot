import { useEffect, useRef, useState } from 'react';

import { checkHealth, getActiveNode, MOUNT, resetConversation } from './api/supervisorClient';
import { LoginPage } from './components/auth/LoginPage';
import { Icon, Modal } from './components/common';
import { PageBoundary } from './components/common/PageBoundary';
import { AppLayout } from './components/layout';
import { useChat } from './hooks/useChat';
import { usePlatformRegistry } from './platforms/usePlatformRegistry';
import { useChatStore } from './store/chatStore';
import { useClientPrefsStore } from './store/clientPrefsStore';
import { useSettingsStore } from './store/settingsStore';
import { useViewStore, type ViewMode } from './store/viewStore';
import type { Attachment } from './types';
import { viewRegistry, type AppViewContext, type AppViewDefinition } from './views/viewRegistry';
import { Button } from './components/settings/pages/settingsControls';
import { extensions, useExtensionRevision } from './plugins/registry';
import { PluginRender } from './plugins/PluginRender';
import { useBrowserPlugins } from './plugins/status';
import { useFrontendPlugins } from './plugins/runtime';
import { bindCommands, executeCommand } from './plugins/commands';
import type { LayoutRenderer, SlotRenderer } from './plugins/sdk';
import { InstanceDirectory } from './components/instances/InstanceDirectory';
import { InstanceDirectoryProvider, useInstanceDirectory } from './components/instances/useInstanceDirectory';
import { isInstanceEntry, rememberInstance } from './instances/navigation';

const defaultLayout: LayoutRenderer = (props) => <AppLayout {...props} />;
extensions.builtin('slots', 'app.layout', defaultLayout);

const MainApp = ({ viewMode }: { viewMode: ViewMode }) => {
  useExtensionRevision();
  const pluginState = useBrowserPlugins();
  const {
    conversations, activeConversationId, activeConversation, messages, isGenerating,
    selectConversation, createConversation, deleteConversation, renameConversation, sendMessage, cancelCurrentTask,
  } = useChat();
  const toolsById = useChatStore(state => state.toolExecutionsById);
  const viewingChildSessionId = useChatStore(state => state.viewingChildSessionId);
  const childNodes = useChatStore(state => state.childNodes);
  const exitChildSession = useChatStore(state => state.exitChildSession);
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const storageWarning = useSettingsStore(state => state.storageWarning);
  const preferenceWarning = useClientPrefsStore(state => state.storageWarning);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [resetTarget, setResetTarget] = useState<{ id: string; title: string; sessionId: string } | null>(null);
  const [resetting, setResetting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const resetLock = useRef(false);
  const cancelLock = useRef(false);
  const viewingChildNode = viewingChildSessionId ? childNodes[viewingChildSessionId] : undefined;
  const activeSessionId = viewingChildSessionId || activeConversation?.sessionId || '';
  const activeTitle = viewingChildSessionId ? `子节点: ${viewingChildNode?.nodeId || viewingChildSessionId}` : activeConversation?.title || '未选择对话';

  useEffect(() => { void useChatStore.getState().loadStartup(); }, []);
  const setConnected = useSettingsStore(state => state.setConnected);
  useEffect(() => {
    let active = true; let checking = false;
    let request: AbortController | null = null;
    const check = async () => {
      if (checking) return;
      checking = true;
      const controller = new AbortController(); request = controller;
      const timeout = setTimeout(() => controller.abort(), 10000);
      try { await checkHealth(controller.signal); if (active) setConnected(true); }
      catch { if (active) setConnected(false); }
      finally { clearTimeout(timeout); checking = false; if (request === controller) request = null; }
    };
    void check(); const timer = setInterval(() => void check(), 10000);
    return () => { active = false; clearInterval(timer); request?.abort(); };
  }, [setConnected]);

  const handleSend = async (text: string, attachments?: Attachment[]) => {
    const original = useChatStore.getState();
    if (original.viewingChildSessionId) throw new Error('子会话为查看模式，请先返回父会话。');
    const id = original.activeConversationId;
    const sessionId = original.conversations.find(item => item.id === id)?.sessionId || '';
    const settings = useSettingsStore.getState();
    let nodeId = !sessionId && !settings.activeNodeSessionId ? settings.activeNodeId || settings.entryNodeId : settings.entryNodeId;
    if (sessionId) {
      if (settings.activeNodeSessionId === sessionId) nodeId = settings.activeNodeId;
      else {
        const result = await getActiveNode(sessionId);
        const current = useChatStore.getState();
        if (current.activeConversationId !== id || current.viewingChildSessionId || current.conversations.find(item => item.id === id)?.sessionId !== sessionId) {
          throw new Error('发送前会话已变化，草稿已保留，请确认当前会话后重试。');
        }
        nodeId = result.node_id;
        useSettingsStore.getState().setActiveNode(result.node_id, result.is_override, result.default_node_id, sessionId);
      }
    }
    await sendMessage(text, attachments, nodeId || undefined);
  };
  const handleCancel = async () => {
    if (cancelLock.current || useChatStore.getState().viewingChildSessionId) return;
    cancelLock.current = true; setCancelling(true); setNotice(null);
    try { await cancelCurrentTask(); setNotice({ text: '已提交取消请求。' }); }
    catch (error) { setNotice({ text: error instanceof Error ? error.message : '取消失败，请重试。', error: true }); }
    finally { cancelLock.current = false; setCancelling(false); }
  };
  const confirmReset = async () => {
    if (!resetTarget || resetLock.current) return;
    const target = resetTarget;
    resetLock.current = true; setResetting(true); setNotice(null);
    try {
      const current = useChatStore.getState().conversations.find(item => item.id === target.id);
      if (!current || current.sessionId !== target.sessionId) throw new Error('会话已变化，请重新选择要重置的对话。');
      await resetConversation(`web:${target.id}`);
      useChatStore.getState().resetConversationView(target.id, target.sessionId);
      setResetTarget(null); setNotice({ text: `已重置“${target.title}”，下一条消息将开始新的上下文。` });
    } catch (error) { setNotice({ text: error instanceof Error ? error.message : '重置失败，请重试。', error: true }); }
    finally { resetLock.current = false; setResetting(false); }
  };

  const knownView = Boolean(extensions.resolve('views', viewMode) || viewRegistry[viewMode]);
  useEffect(() => {
    if (!knownView && pluginState.settled && (pluginState.generation || pluginState.error)) useViewStore.getState().openWorkspace('chat');
  }, [knownView, pluginState.settled, pluginState.generation, pluginState.error]);
  const requestReset = () => {
    if (!activeConversation || viewingChildSessionId) return;
    setNotice(null); setResetTarget({ id: activeConversation.id, title: activeConversation.title, sessionId: activeConversation.sessionId || '' });
  };
  bindCommands({ 'chat.send': handleSend, 'chat.cancel': handleCancel, 'chat.reset': requestReset, 'chat.reset.confirm': confirmReset });
  const viewContext: AppViewContext = {
    sessionId: activeSessionId, title: activeTitle, conversations, activeConversationId, messages,
    toolsById, isGenerating, isCancelling: cancelling, viewingChildSessionId, viewingChildNodeId: viewingChildNode?.nodeId,
    activeSettingsTab, onExitChildSession: exitChildSession,
    onCreateConversation: createConversation,
    onSelectConversation: selectConversation, onDeleteConversation: deleteConversation,
    onSendMessage: (text, attachments) => executeCommand<Promise<void>>('chat.send', text, attachments), onCancel: () => executeCommand('chat.cancel'),
    onReset: activeConversation && !viewingChildSessionId ? () => executeCommand('chat.reset') : undefined,
    onTitleChange: activeConversationId ? title => renameConversation(activeConversationId, title) : undefined,
  };
  const pageKey = `${viewMode}:${activeSettingsTab}:${activeSessionId}`;
  const fallbackView = viewRegistry[viewMode] || { ...viewRegistry.chat, main: () => <p className="p-4" role="status">{pluginState.settled ? '插件工作区暂时无法显示，请在插件设置中查看状态。' : '正在读取插件工作区…'}</p> };
  return <PluginRender<AppViewDefinition> kind="views" id={viewMode} base={fallbackView} resetKey={pageKey} render={(view) => {
    const hasRightPanel = extensions.hasContribution('slots', `${viewMode}.rightTop`) || extensions.hasContribution('slots', `${viewMode}.rightBottom`) || (view.hasRightPanel?.(viewContext) ?? true);
    const slot = (name: 'sidebar' | 'header' | 'main' | 'composer' | 'rightTop' | 'rightBottom') => {
      if (!view[name] && !extensions.hasContribution('slots', `${viewMode}.${name}`)) return undefined;
      return <PageBoundary label={name} resetKey={pageKey}><PluginRender<SlotRenderer> kind="slots" id={`${viewMode}.${name}`} base={view[name]} resetKey={pageKey} render={(render) => render(viewContext)} /></PageBoundary>;
    };
    const layoutProps = {
      navigationKey: `${viewMode}:${activeSettingsTab}:${activeConversationId || ''}:${viewingChildSessionId || ''}`,
    };
    return <>
    <PluginRender<LayoutRenderer> kind="slots" id="app.layout" base={defaultLayout} resetKey={pageKey} render={(Layout) => Layout({
      ...layoutProps, composer: slot('composer'), header: slot('header'), sidebar: slot('sidebar'),
      logPanel: hasRightPanel ? slot('rightBottom') : undefined,
      rightPanel: hasRightPanel ? slot('rightTop') : undefined,
      children:
      <div className="flex h-full min-h-0 flex-col">
        {(storageWarning || preferenceWarning) && <p className="app-notice app-notice-error shrink-0" role="status">{storageWarning || preferenceWarning}</p>}
        {notice && <div className={`app-notice flex shrink-0 items-center justify-between gap-3 ${notice.error ? 'app-notice-error' : ''}`} role={notice.error ? 'alert' : 'status'}>
          <span>{notice.text}</span><button className="app-icon-button shrink-0" aria-label="关闭提示" type="button" onClick={() => setNotice(null)}><Icon name="close" size={18} /></button>
        </div>}
        <div className="relative min-h-0 flex-1">{slot('main')}</div>
      </div>
    })} />
    <Modal open={Boolean(resetTarget)} title="重置对话" onClose={() => { if (!resetLock.current) setResetTarget(null); }}>
      <div className="space-y-4 p-4">
        <p>重置“{resetTarget?.title}”的上下文？原上下文与正在进行的任务将被清理，其他对话不受影响。</p>
        {notice?.error && <p className="text-sm text-[var(--duties-danger)]" role="alert">{notice.text}</p>}
        <div className="flex justify-end gap-2"><Button tone="quiet" disabled={resetting} onClick={() => setResetTarget(null)}>保留对话</Button><Button tone="danger" loading={resetting} onClick={() => void executeCommand('chat.reset.confirm')}>确认重置</Button></div>
      </div>
    </Modal>
  </>;
  }} />;
};

const ConsoleApp = () => {
  const isAuthenticated = useSettingsStore(state => state.isAuthenticated);
  const viewMode = useViewStore(state => state.viewMode);
  const directory = useInstanceDirectory();
  usePlatformRegistry();
  useFrontendPlugins();
  useEffect(() => { if (isAuthenticated && directory.status === 'ready') rememberInstance(MOUNT); }, [isAuthenticated, directory.status]);
  return isAuthenticated ? <MainApp viewMode={viewMode} /> : <LoginPage />;
};

const App = () => <InstanceDirectoryProvider>{isInstanceEntry() ? <InstanceDirectory /> : <ConsoleApp />}</InstanceDirectoryProvider>;

export default App;

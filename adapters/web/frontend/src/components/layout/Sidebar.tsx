// [2026-05-16] Upgraded: connection status from settingsStore, settings toggle, delete conversation.
// [2026-05-31] Step 3 accepts V2 ConversationMeta rows instead of legacy
// Conversation objects. Why: chatStore keeps message bodies normalized outside the
// sidebar list. How: render title, session preview, and updated time without reading a
// messages array. Purpose: let App switch stores without changing sidebar behavior.
import { useEffect, useMemo, useRef, useState } from 'react';
import { WorkspaceNav } from '../../features/WorkspaceNav';

import { useChatStore, type ChildNodeState, type ConversationMeta } from '../../store/chatStore';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Button, getChildNodeStatusLabel, Icon, StatusDot } from '../common';

interface SidebarProps {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  onCreateConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => Promise<void> | void;
  childNodesByConversation?: Record<string, ChildNodeState[]>;
}

const formatTime = (isoDate: string) => {
  const date = new Date(isoDate);
  return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date);
};

function groupChildNodesByConversation(
  childNodes: Readonly<Record<string, ChildNodeState>>,
  conversations: ConversationMeta[],
  now: number,
): Record<string, ChildNodeState[]> {
  // [2026-06-03] Sidebar receives a flat normalized childNodes map from chatStore.
  // Why: hooks cannot be called inside conversations.map for each row. How: group the
  // map once per render and sort each child list by startedAt. Purpose: each parent
  // conversation can render a stable tree without violating React hook rules.
  const knownConversationIds = new Set(conversations.map((conversation) => conversation.id));
  const grouped: Record<string, ChildNodeState[]> = {};

  Object.values(childNodes).forEach((child) => {
    if (!knownConversationIds.has(child.parentConversationId)) return;
    // [AutoC 2026-06-04] Filter out system nodes and stale terminal nodes.
    if (child.nodeId.startsWith('system.')) return;
    if (child.completedAt) {
      const elapsed = now - new Date(child.completedAt).getTime();
      if (elapsed > 30_000) return;
    }
    grouped[child.parentConversationId] = [...(grouped[child.parentConversationId] || []), child];
  });

  Object.values(grouped).forEach((children) => {
    children.sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
  });

  return grouped;
}

export const Sidebar = ({
  conversations, activeConversationId,
  onCreateConversation, onSelectConversation, onDeleteConversation,
  childNodesByConversation: providedChildNodesByConversation,
}: SidebarProps) => {
  const isConnected = useSettingsStore(state => state.isConnected);
  const openSettings = useViewStore(state => state.openSettings);
  const childNodeMap = useChatStore(state => state.childNodes);
  const viewChildSession = useChatStore(state => state.viewChildSession);
  const startupError = useChatStore(state => state.startupError);
  const loadStartup = useChatStore(state => state.loadStartup);
  const [now, setNow] = useState(Date.now);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const deleting = useRef(false);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  useEffect(() => {
    if (!Object.values(childNodeMap).some(child => child.completedAt)) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [childNodeMap]);
  const remove = async (conversation: ConversationMeta) => {
    if (deleting.current || !window.confirm(`删除“${conversation.title}”及其上下文？此操作无法撤销。`)) return;
    deleting.current = true; setPendingDelete(conversation.id); setError('');
    try { await onDeleteConversation(conversation.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败，请重试。'); }
    finally { deleting.current = false; setPendingDelete(null); }
  };
  const visible = conversations.filter(conversation => conversation.title.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const groupedChildNodes = useMemo(
    () => providedChildNodesByConversation || groupChildNodesByConversation(childNodeMap, conversations, now),
    [childNodeMap, conversations, providedChildNodesByConversation, now],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2.5">
          <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-9 w-9 rounded-lg" />
          <div>
            <h1 className="text-base font-semibold">Clonoth</h1>
            <p className="text-xs text-[var(--duties-tertiary)]">调度器网页界面</p>
          </div>
        </div>
        <Button className="mt-3 w-full" onClick={onCreateConversation} variant="primary">
          新对话
        </Button>
      </div>

      <WorkspaceNav />
      <div className="px-3 pb-2 pt-3"><label className="sr-only" htmlFor="conversation-search">搜索对话</label><input id="conversation-search" type="search" className="app-input w-full" placeholder="搜索对话" value={search} onChange={event => setSearch(event.target.value)} /></div>
      {error && <p className="mx-3 mb-2 text-sm text-[var(--duties-danger)]" role="alert">{error}</p>}
      {/* Conversation list */}
      <nav aria-label="对话" className="min-h-0 flex-1 overflow-y-auto">
        {startupError && <div className="mx-3 mb-3 space-y-2">
          <p className="break-words text-sm text-[var(--duties-danger)]" role="alert">会话列表未加载：{startupError}</p>
          <Button className="w-full" onClick={loadStartup}>重试加载</Button>
        </div>}
        {visible.length === 0 && !startupError && (
          <p className="p-3 text-center text-xs text-[var(--duties-tertiary)]">{search ? '没有匹配的对话' : '暂无对话'}</p>
        )}
        {visible.map((conv) => {
          const isActive = conv.id === activeConversationId;
          const childNodes = groupedChildNodes[conv.id] || [];
          return (
            <div
              className={`group relative border-b border-[var(--duties-border)] transition-colors hover:bg-[var(--duties-accent)] ${
                isActive ? 'bg-[var(--duties-muted)]' : 'bg-transparent'
              }`}
              key={conv.id}
            >
              <button
                aria-current={isActive ? 'page' : undefined}
                className="block min-h-12 w-full p-3 pr-12 text-left"
                onClick={() => onSelectConversation(conv.id)}
                type="button"
              >
                <span className="block truncate text-sm font-semibold">{conv.title}</span>
                <span className="mt-1.5 block truncate text-xs text-[var(--duties-tertiary)]">
                  {conv.sessionId ? `会话 ${conv.sessionId.slice(0, 8)}` : '暂无会话'}
                </span>
                <span className="mt-2 block text-xs text-[var(--duties-tertiary)]">
                  {formatTime(conv.updatedAt)}
                </span>
              </button>
              {/* Delete button — visible on hover (desktop) or always visible (mobile).
                  [2026-06-01] Why: remove the literal close glyph used for deletion.
                  How: render the Material Symbols delete icon through Icon. Purpose:
                  destructive conversation controls are visually clear and consistent. */}
              <button
                aria-label={`删除对话 ${conv.title}`}
                disabled={pendingDelete !== null}
                aria-busy={pendingDelete === conv.id || undefined}
                className="app-icon-button absolute right-1 top-1 text-[var(--duties-secondary)] hover:text-[var(--duties-danger)] md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100"
                onClick={(e) => { e.stopPropagation(); void remove(conv); }}
                title="删除对话"
                type="button"
              >
                <Icon name="delete" size={15} />
              </button>
              {childNodes.length > 0 && childNodes.map((child) => (
                <div
                  className="ml-3 border-l border-[var(--duties-border)] pl-6"
                  key={child.sessionId}
                >
                  <button
                    aria-label={`子节点 ${child.nodeId}`}
                    className="block w-full p-2 text-left text-xs text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)]"
                    onClick={(event) => {
                      event.stopPropagation();
                      // [2026-06-03] Why: the sidebar child row should open the same
                      // child stream as the floating panel. How: call the store-level
                      // navigation action with the child session id. Purpose: users can
                      // inspect delegated work directly from the conversation tree.
                      viewChildSession(child.sessionId);
                    }}
                    type="button"
                  >
                    {/* [2026-06-03] Render child sessions as navigable tree rows.
                        Why: Phase 3 adds child-session chat streams. How: keep the
                        shared status dot, node id, and start time while wiring the row
                        to chatStore.viewChildSession. Purpose: users can see and open
                        delegated work without creating a separate sidebar conversation. */}
                    <span className="flex items-center gap-1.5">
                      <StatusDot
                        label={`子节点 ${child.nodeId} 状态：${getChildNodeStatusLabel(child.status)}`}
                        status={child.status}
                      />
                      <span className="font-mono font-medium">{child.nodeId}</span>
                    </span>
                    {child.startedAt && (
                      <span className="mt-0.5 block text-[var(--duties-tertiary)]">
                        {formatTime(child.startedAt)}
                      </span>
                    )}
                  </button>
                </div>
              ))}
            </div>
          );
        })}
      </nav>

      {/* Bottom — connection status + settings */}
      <div className="border-t border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2 text-xs text-[var(--duties-tertiary)]">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-[var(--duties-live)]' : 'bg-[var(--duties-danger)]'}`} />
          {isConnected ? '已连接' : '已断开'}
        </div>
        <button
          className="mt-2 flex min-h-10 w-full items-center gap-2 px-2 py-2 text-sm text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)]"
          onClick={() => {
            // [2026-06-01] Settings is now a full registered view, not a legacy
            // right-panel toggle. Why: the left sidebar must be replaced by
            // settings categories while the right panel remains independent. How:
            // route through viewStore.openSettings(). Purpose: AppLayout receives
            // settings slots from viewRegistry without Sidebar knowing the details.
            openSettings();
          }}
          type="button"
        >
          {/* [2026-06-01] Why: replace the settings gear emoji with Material Symbols.
              How: render the shared Icon using the settings symbol. Purpose: sidebar
              actions stay on the same icon system as layout and message controls. */}
          <Icon name="settings" size={16} />
          <span>设置</span>
        </button>
      </div>
    </div>
  );
};

// [2026-05-31] Reducer-backed chat store for the Step 2A frontend refactor.
// Why: the legacy store mixes WebSocket transport, stream previews, and rendered
// messages in one mutable path. How: keep conversation/session actions here while
// every SupervisorEvent is replayed through eventReducer into ChatState. Purpose:
// let the new message model run beside the old store until the UI migration is done.
import { create } from 'zustand';

import { connectGlobalWS, disconnectGlobalWS } from '../api';
import {
  cancelActiveTasks,
  decideApproval,
  deleteSession,

  getSessionChildren,
  getSessionHistory,
  listSessions,
  postInbound,
  uploadAttachment,
  type ChildSessionInfo,
  type StructuredMessage,
  type StructuredThinkingBlock,
} from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';
import type {
  Attachment,
  ChatState,
  MessageStatus,
  RenderBlock,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ToolExecution,
  ToolStatus,
  WsMessage,
} from '../types/message';
import { shouldAutoApproveToolCall, useClientPrefsStore, type ApprovalMatch } from './clientPrefsStore';
import { useSettingsStore } from './settingsStore';
import { scopedKey } from './storageKey';
import { createInitialChatState, findToolByCallId, reduceChatEvent } from './eventReducer';

export interface ConversationMeta {
  id: string;
  sessionId: string;
  title: string;
  updatedAt: string;
}

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export type ChildNodeStatus = 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'cancelled';
export type TaskActivityPhase = 'idle' | 'thinking' | 'generating' | 'tool_call' | 'awaiting_approval';

export interface TaskActivity {
  // [AutoC 2026-06-04] Why: ActiveTasksModal needs a small live status snapshot,
  // not the full event stream. How: store only the current phase, a short detail,
  // and the event timestamp. Purpose: modal rendering stays cheap and transient.
  phase: TaskActivityPhase;
  detail: string;
  lastEventAt: number;
}

export interface ChildNodeState {
  // [2026-06-03] Why: dispatched child agents run in independent Supervisor sessions.
  // How: keep the runtime session id as the stable map key and visible identifier.
  // Purpose: later UI phases can group scout/smith activity under the parent chat.
  sessionId: string;
  nodeId: string;
  parentConversationId: string;
  status: ChildNodeStatus;
  taskId?: string;
  startedAt?: string;
  completedAt?: string;
}

export interface ChatStoreState extends ChatState {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  isGenerating: boolean;
  connectionStatus: ConnectionStatus;
  generatingBySession: Readonly<Record<string, boolean>>;
  childNodes: Readonly<Record<string, ChildNodeState>>;
  viewingChildSessionId: string | null;
  childSessionMessages: Readonly<Record<string, WsMessage[]>>;
  taskActivities: Readonly<Record<string, TaskActivity>>;

  selectConversation: (id: string) => void;
  selectChildNodes: (conversationId: string) => ChildNodeState[];
  selectHasActiveChildNodes: (conversationId: string) => boolean;
  createConversation: () => string;
  deleteConversation: (id: string) => void;
  renameConversation: (id: string, newTitle: string) => void;
  sendMessage: (text: string, attachments?: any[], entryNodeId?: string) => Promise<void>;
  cancelCurrentTask: () => Promise<void>;
  resetState: () => void;
  viewChildSession: (sessionId: string, taskId?: string) => void;
  exitChildSession: () => void;
  loadStartup: () => void;
}

type StoreSetter = (
  partial:
    | Partial<ChatStoreState>
    | ((state: ChatStoreState) => Partial<ChatStoreState>),
) => void;
type StoreGetter = () => ChatStoreState;

type HistoryToolResult = {
  status: 'success' | 'error';
  result?: string;
  rawInline?: string;
  rejected?: boolean;
  isAutoResult?: boolean;
};

type HistoryToolCall = {
  id?: string;
  name: string;
  arguments?: Record<string, unknown>;
};

type HistoryThinkingSegment = {
  text: string;
  startedAt?: string;
  endedAt?: string;
};

// Why: ask is also a control tool that ends the assistant turn by requesting
// additional input. How: keep it with finish/reply/switch_node for history summaries
// and automatic result hiding. Purpose: historical ask calls do not appear as noisy
// ordinary tool executions.
const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
const INTERNAL_USER_MESSAGE_TYPES = new Set(['tool_result', 'system', 'summary']);
const TERMINAL_TASK_EVENTS = new Set(['task_completed', 'task_cancelled', 'task_failed']);
const CHILD_NODE_ACTIVE_STATUSES = new Set<ChildNodeStatus>(['running', 'awaiting_approval']);
const CHILD_NODE_STATUS_BY_EVENT: Readonly<Record<string, ChildNodeStatus | undefined>> = {
  // [2026-06-03] Why: child-agent lifecycle events arrive on the global websocket
  // before any UI tree exists. How: translate the small event vocabulary into a
  // stable data-layer status. Purpose: later renderers can read one normalized state.
  task_created: 'running',
  task_started: 'running',
  approval_requested: 'awaiting_approval',
  approval_decided: 'running',
  task_completed: 'completed',
  task_failed: 'failed',
  task_cancelled: 'cancelled',
};
const LS_KEY_TITLES = scopedKey('clonoth_conversation_titles');
const LS_KEY_AUTO_APPROVED = scopedKey('clonoth_auto_approved_ids');

let startupLoaded = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const autoApprovedApprovalIds = loadAutoApproved();

function loadTitleCache(): Record<string, string> {
  // [2026-06-02] Why: backend session metadata does not carry frontend-generated
  // first-message titles. How: read a small browser-local title map defensively.
  // Purpose: refreshing the page can restore readable conversation titles.
  try {
    const raw = localStorage.getItem(LS_KEY_TITLES);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveTitleCache(titles: Record<string, string>) {
  // [2026-06-02] Why: conversation titles should survive refreshes but not grow
  // without bound. How: keep insertion order and persist only the newest 100 values.
  // Purpose: localStorage remains small while recent sidebar titles are retained.
  try {
    const entries = Object.entries(titles);
    const trimmed = entries.length > 100 ? Object.fromEntries(entries.slice(-100)) : titles;
    localStorage.setItem(LS_KEY_TITLES, JSON.stringify(trimmed));
  } catch {}
}

function loadAutoApproved(): Set<string> {
  // [2026-06-02] Why: automatically submitted approval ids must not reset on browser
  // refresh. How: parse the localStorage array into the same Set used at runtime.
  // Purpose: the client does not resubmit or re-show controls for already handled ids.
  try {
    const raw = localStorage.getItem(LS_KEY_AUTO_APPROVED);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function saveAutoApproved(ids: Set<string>) {
  // [2026-06-02] Why: approval ids are browser-local bookkeeping and can accumulate.
  // How: serialize the Set as an array and retain only the newest 200 entries.
  // Purpose: refresh persistence stays bounded without changing backend approval data.
  try {
    const arr = [...ids];
    const trimmed = arr.length > 200 ? arr.slice(-200) : arr;
    localStorage.setItem(LS_KEY_AUTO_APPROVED, JSON.stringify(trimmed));
  } catch {}
}

function clearReconnectTimer() {
  // [2026-06-03] Why: global WebSocket reconnect timers are module-level so they
  // survive Zustand state updates. How: clear the pending retry before an explicit
  // full teardown or a fresh connection attempt. Purpose: resetState cannot leave a
  // stale timer that silently opens another all-session socket.
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function stopGlobalRealtimeConnection() {
  // [2026-06-03] Why: task completion, cancellation, and conversation switching must
  // not close realtime delivery anymore. How: reserve this cleanup for full store
  // reset and test teardown only. Purpose: the long-lived /v1/ws stream continues
  // receiving events for every web session during normal chat lifecycle changes.
  clearReconnectTimer();
  disconnectGlobalWS();
}

function createConversationId(): string {
  const randomPart = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `conv-${randomPart}`;
}

function createConversationMeta(id = createConversationId(), sessionId = ''): ConversationMeta {
  const timestamp = new Date().toISOString();
  // [2026-06-01] Why: the default conversation title is visible in the sidebar.
  // How: keep the same creation flow and change only the default display text.
  // Purpose: new conversations appear localized before the first user message.
  return { id, sessionId, title: '新对话', updatedAt: timestamp };
}

function createStoreBase(): Pick<ChatStoreState, 'conversations' | 'activeConversationId' | 'isGenerating' | 'connectionStatus' | 'generatingBySession' | 'childNodes' | 'viewingChildSessionId' | 'childSessionMessages' | 'taskActivities'> {
  return {
    conversations: [],
    activeConversationId: null,
    isGenerating: false,
    connectionStatus: 'idle',
    generatingBySession: {},
    // [2026-06-03] Why: child-node state is frontend-only routing metadata, not part
    // of reducer ChatState. How: initialize it beside other store-owned maps.
    // Purpose: resetState and startup create a clean child session tracker.
    childNodes: {},
    // [2026-06-03] Why: Phase 3 can temporarily show a child session's independent
    // chat stream while the parent conversation remains selected. How: keep the
    // viewed child id and normalized message cache outside reducer ChatState. Purpose:
    // switching in and out of child streams does not mutate the sidebar conversation.
    viewingChildSessionId: null,
    childSessionMessages: {},
    // [AutoC 2026-06-04] Why: task activity is a browser-local live overlay on top
    // of the polled active-task list. How: reset it with the rest of store-owned
    // metadata. Purpose: page refresh or reset never shows stale task phases.
    taskActivities: {},
  };
}

function normalizeConversationKey(value: string): string {
  if (!value) return '';
  // [AutoC 2026-06-03] Why: web:{conversationId} is the backend routing key
  // contract, not display text. How: strip only the protocol prefix before using the
  // id in frontend maps. Purpose: keep protocol parsing explicit and centralized.
  return value.startsWith('web:') ? value.slice(4) : value;
}

function isEntryBranchSessionId(sessionId: string): boolean {
  // [2026-06-03] Why: entry branches are temporary runtime sessions and must not
  // become user-facing conversations. How: recognize the supervisor's branch_*
  // ids, plus the older branch-* fixture spelling. Purpose: startup and routing can
  // keep branch traffic attached to the durable parent web session.
  return /^branch[_-]/.test(sessionId.trim());
}

function titleFromSession(conversationKey: string, sessionId: string): string {
  const normalized = normalizeConversationKey(conversationKey);
  if (normalized && normalized !== sessionId) return normalized.length > 30 ? `${normalized.slice(0, 30)}…` : normalized;
  // [AutoC 2026-06-03] Why: discord:{id} is a backend channel key prefix. How:
  // parse that protocol marker only for fallback labels, never localized message
  // content. Purpose: retained string matching remains tied to structured metadata.
  if (conversationKey.startsWith('discord:')) return `Discord ${conversationKey.slice(8)}`;
  return sessionId ? `网页 ${sessionId.slice(0, 8)}` : '新对话';
}

function truncateTitle(text: string, limit = 30): string {
  const compact = text.replace(/\s+/g, ' ').trim();
  return compact ? Array.from(compact).slice(0, limit).join('') : '新对话';
}

function getInitialTitleFromClientPrefs(text: string, currentTitle: string | undefined): string | undefined {
  // [2026-06-01] Title behavior is now controlled by browser-local preferences.
  // Why: users asked for build-local frontend choices without backend schema changes.
  // How: first-message uses the first 50 characters, while manual and auto keep the
  // existing title until a future explicit title editor or LLM generator is wired.
  // Purpose: the current default becomes first-message without inventing a backend API.
  if (currentTitle && currentTitle !== '新对话' && currentTitle !== 'New conversation') return currentTitle;
  const mode = useClientPrefsStore.getState().titleGeneration;
  if (mode === 'first-message') return text ? truncateTitle(text, 50) : currentTitle;
  return currentTitle;
}

function upsertConversationMeta(
  conversations: readonly ConversationMeta[],
  patch: Partial<ConversationMeta> & Pick<ConversationMeta, 'id'>,
): ConversationMeta[] {
  const existing = conversations.find((conversation) => conversation.id === patch.id);
  const timestamp = patch.updatedAt || new Date().toISOString();

  if (patch.title && patch.title !== '新对话' && patch.title !== 'New conversation') {
    // [2026-06-02] Why: title upserts are the single path for generated sidebar
    // titles. How: mirror non-default titles into the browser-local cache. Purpose:
    // startup can restore them even when Zustand memory was cleared by refresh.
    const cache = loadTitleCache();
    cache[patch.id] = patch.title;
    saveTitleCache(cache);
  }

  if (!existing) {
    return [{ sessionId: '', title: '新对话', updatedAt: timestamp, ...patch }, ...conversations];
  }

  const updated = {
    ...existing,
    ...patch,
    sessionId: patch.sessionId !== undefined ? patch.sessionId : existing.sessionId,
    title: patch.title !== undefined ? patch.title : existing.title,
    updatedAt: timestamp,
  };

  return conversations.map((conversation) => (conversation.id === patch.id ? updated : conversation));
}

function getActiveConversation(state: ChatStoreState): ConversationMeta | undefined {
  return state.activeConversationId
    ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
    : undefined;
}

function sortConversationsByRecency(conversations: readonly ConversationMeta[]): ConversationMeta[] {
  // [2026-06-03] Why: the backend lists sessions by created_at, and in-place meta
  // updates never reorder the sidebar, so a freshly active conversation stays buried.
  // How: sort a copy by updatedAt descending (newest first). Purpose: the sidebar is
  // sorted once on initial load and again after each task terminal event, matching the
  // recency the user expects without reordering on every streaming delta.
  return [...conversations].sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
}

function getChildConversationId(sessionId: string): string {
  // [2026-06-03] Why: child session messages must use the same normalized reducer
  // tables without becoming sidebar conversations. How: namespace their internal
  // conversation id with a child: prefix. Purpose: MessageList can render child
  // streams while ConversationMeta remains parent-only.
  return `child:${sessionId}`;
}

function selectOrderedMessagesFromState(state: ChatState, conversationId: string): WsMessage[] {
  // [2026-06-03] Why: chatStore needs the same ordered message array that React
  // selectors derive, but importing eventSelectors would create a broader dependency.
  // How: read the reducer order table directly and drop missing ids defensively.
  // Purpose: childSessionMessages caches render-ready arrays after history or WS replay.
  return (state.messageOrderByConversation[conversationId] || [])
    .map((messageId) => state.messagesById[messageId])
    .filter((message): message is WsMessage => Boolean(message));
}

function normalizeChildNodeStatus(status: string | undefined): ChildNodeStatus {
  // [2026-06-03] Why: backend task status names and restored child rows may differ
  // from the compact frontend badge vocabulary. How: preserve supported terminal and
  // approval states, and treat every active or unknown state as running. Purpose:
  // child status rebuilds remain stable across supervisor versions.
  if (status === 'awaiting_approval' || status === 'completed' || status === 'failed' || status === 'cancelled') return status;
  return 'running';
}

function mergeChildNodesFromSessionChildren(
  state: ChatStoreState,
  conversationId: string,
  children: readonly ChildSessionInfo[],
): Readonly<Record<string, ChildNodeState>> {
  // [2026-06-03] Why: browser refresh loses childNodes but the supervisor registry
  // still knows child sessions. How: merge registry rows into the normalized child map
  // while preserving fresher WebSocket timestamps already present in memory. Purpose:
  // history loading rebuilds child panels without erasing live status updates.
  if (children.length === 0) return state.childNodes;

  let changed = false;
  const next = { ...state.childNodes };
  for (const child of children) {
    const sessionId = getStringValue(child.session_id);
    if (!sessionId) continue;
    const previous = next[sessionId];
    next[sessionId] = {
      sessionId,
      nodeId: getStringValue(child.node_id) || previous?.nodeId || 'unknown',
      parentConversationId: conversationId,
      status: normalizeChildNodeStatus(getStringValue(child.status)),
      taskId: getStringValue(child.task_id) || previous?.taskId,
      startedAt: previous?.startedAt || getStringValue(child.started_at) || getStringValue(child.updated_at),
      completedAt: getStringValue(child.completed_at) || previous?.completedAt,
    };
    changed = true;
  }

  return changed ? next : state.childNodes;
}

function isDispatchResultHistoryMessage(message: StructuredMessage, _text: string): boolean {
  // [AutoC 2026-06-03] Why: dispatch-result recognition belongs to backend-owned
  // structured metadata, not localized notification text. How: check the new
  // message_type contract, with only async_dispatch: as a legacy protocol-id fallback
  // for stored rows created before message_type existed. Purpose: remove brittle
  // frontend text and dispatch_origin id heuristics while preserving old history.
  const messageType = getStringValue(message.message_type);
  return messageType === 'dispatch_result'
    || message.id.startsWith('async_dispatch:');
}

function shouldPreserveConversationMessagesDuringHistoryLoad(
  state: ChatStoreState,
  conversationId: string,
  sessionId: string,
): boolean {
  // [2026-06-03] Why: the WebSocket is now global and always open, so socket
  // presence no longer means this conversation is actively streaming. How: preserve
  // existing cards only when this session is marked generating, or while the active
  // composer still has an optimistic send pending. Purpose: normal history loads can
  // replace stale data, but in-flight streams are not erased by a late fetch.
  return Boolean(state.generatingBySession[sessionId]) || (state.isGenerating && state.activeConversationId === conversationId);
}

function getLastEventConversationId(state: ChatState, event: SupervisorEvent, fallbackConversationId: string): string {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  const logEntry = state.eventLog.find((entry) => entry.eventId === eventId);
  return logEntry?.conversationId || fallbackConversationId;
}

function syncConversationsAfterEvent(
  conversations: readonly ConversationMeta[],
  nextChatState: ChatState,
  event: SupervisorEvent,
  fallbackConversationId: string,
): ConversationMeta[] {
  // Why: ChatState intentionally stores only normalized render data, not sidebar
  // metadata. How: after reducer replay, mirror the event's conversation/session into
  // ConversationMeta. Purpose: the new store keeps the old session list behavior while
  // message content remains fully reducer-owned.
  const conversationId = getLastEventConversationId(nextChatState, event, fallbackConversationId);
  const payload = event.payload || {};
  const isInbound = event.type === 'inbound_message';
  const existing = conversations.find((conversation) => conversation.id === conversationId);

  if (!existing && conversationId !== fallbackConversationId) {
    // Why: branch sessions and child-agent sessions can emit reducer events under their
    // own session ids. How: only allow metadata updates for conversations the sidebar
    // already knows about, or for the active fallback conversation that initiated the
    // WebSocket. Purpose: internal sessions can still hydrate reducer state without
    // polluting the user-facing conversation list.
    return [...conversations];
  }

  const inboundText = typeof payload.text === 'string' ? payload.text : '';
  // [2026-06-03] Fix: use undefined instead of null/empty for non-inbound events
  // so that upsertConversationMeta preserves the existing title via its
  // `patch.title !== undefined` guard. Previously, passing existing?.title
  // (which could be undefined for brand-new conversations) would overwrite
  // the default '新对话' with undefined.
  const title = isInbound && inboundText && (!existing || existing.title === '新对话' || existing.title === 'New conversation')
    ? getInitialTitleFromClientPrefs(inboundText, existing?.title)
    : undefined; // let upsertConversationMeta keep the existing title
  const sessionIdForSidebar = existing?.sessionId && existing.sessionId !== event.session_id
    ? existing.sessionId
    : event.session_id;

  return upsertConversationMeta(conversations, {
    id: conversationId,
    // [2026-06-03] Why: the global WebSocket can route branch-session events into
    // their parent web conversation, and those branch ids must not replace the
    // visible parent session in the sidebar. How: preserve an existing sidebar
    // session id unless this event is already from that same session or the row is
    // brand new. Purpose: child or branch task streams remain visible without
    // breaking future history loads for the real web session.
    sessionId: sessionIdForSidebar,
    title,
    updatedAt: event.ts || new Date().toISOString(),
  });
}

function isTerminalTaskEvent(event: SupervisorEvent): boolean {
  // [2026-06-03] Why: outbound_message can arrive before task cleanup finishes. How:
  // only task terminal events end the local generating state. Purpose: the composer is
  // not unlocked early while tools, routing, or branch cleanup can still be running.
  return TERMINAL_TASK_EVENTS.has(event.type);
}

function getApprovalMatch(state: ChatStoreState, event: SupervisorEvent): ApprovalMatch {
  const payload = event.payload || {};
  const operation = typeof payload.operation === 'string' ? payload.operation : '';
  const toolCallId = typeof payload.tool_call_id === 'string' ? payload.tool_call_id : '';

  // The payload only carries the policy operation, and several tools share one: list_dir
  // and search_in_files both report read_file, apply_diff reports write_file. Resolving the
  // ToolExecution is the only way to learn which tool the user actually ticked; an approval
  // with no matching tool card stays manual.
  return { toolName: findToolByCallId(state, toolCallId)?.name || '', operation };
}

function maybeAutoApproveApprovalRequest(event: SupervisorEvent, get: StoreGetter) {
  if (event.type !== 'approval_requested') return;
  const payload = event.payload || {};
  const approvalId = typeof payload.approval_id === 'string' ? payload.approval_id : '';
  if (!approvalId || autoApprovedApprovalIds.has(approvalId)) return;

  const state = get();
  const prefs = useClientPrefsStore.getState();
  if (!shouldAutoApproveToolCall(getApprovalMatch(state, event), prefs.autoApproveTools)) return;

  autoApprovedApprovalIds.add(approvalId);
  saveAutoApproved(autoApprovedApprovalIds);
  // [2026-06-01] Why: frontend auto-approval should behave like a local click, not
  // a backend policy change. How: submit the normal approval decision endpoint and
  // ignore transport errors so the pending card remains available for manual action.
  // Purpose: low-risk local rules can proceed while high-risk tools still need users.
  const token = useSettingsStore.getState().adminToken || '';
  void decideApproval(token, approvalId, 'allow', 'auto-approved by client preference').catch(() => {
    autoApprovedApprovalIds.delete(approvalId);
    saveAutoApproved(autoApprovedApprovalIds);
  });
}

const ACTIVE_TASK_EVENTS = new Set(['task_created', 'task_started', 'task_requeued', 'task_resumed', 'task_suspended']);

function getStringValue(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

function getNumberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function getEventPayload(event: SupervisorEvent): Record<string, unknown> {
  return isRecord(event.payload) ? event.payload : {};
}

function getTaskActivityTimestamp(event: SupervisorEvent): number {
  const parsed = event.ts ? new Date(event.ts).getTime() : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function getTaskActivityKeys(event: SupervisorEvent, payload: Record<string, unknown>): string[] {
  // [AutoC 2026-06-04] Why: some realtime events carry task_id, while stream_end
  // and approval events may only carry node/session identity. How: index the same
  // activity by task id plus node fallbacks. Purpose: the modal can find the newest
  // status even when later cleanup events omit task_id.
  const taskId = getStringValue(payload.task_id);
  const nodeId = getStringValue(payload.node_id);
  const keys = [
    taskId,
    event.session_id && nodeId ? `${event.session_id}:${nodeId}` : '',
    nodeId,
    taskId || nodeId ? '' : event.session_id,
  ];
  return keys.filter((key, index, all): key is string => Boolean(key) && all.indexOf(key) === index);
}

function getTaskActivityDetail(event: SupervisorEvent, payload: Record<string, unknown>): string {
  if (event.type === 'tool_call_start') {
    return getStringValue(payload.tool_name) || getStringValue(payload.name) || getStringValue(payload.operation);
  }
  if (event.type === 'approval_requested') {
    return getStringValue(payload.tool_name) || getStringValue(payload.operation) || getStringValue(payload.name);
  }
  return '';
}

function getTaskActivityPhase(event: SupervisorEvent, payload: Record<string, unknown>): TaskActivityPhase | null {
  if (event.type === 'stream_delta') {
    const deltaType = getStringValue(payload.type);
    if (deltaType === 'thinking') return 'thinking';
    if (deltaType === 'text') return 'generating';
    return null;
  }
  if (event.type === 'tool_call_start') return 'tool_call';
  if (event.type === 'approval_requested') return 'awaiting_approval';
  if (event.type === 'stream_end' || event.type === 'tool_call_end' || event.type === 'approval_decided') return 'idle';
  return null;
}

function updateTaskActivitiesByEvent(
  current: Readonly<Record<string, TaskActivity>>,
  event: SupervisorEvent,
): Record<string, TaskActivity> {
  const payload = getEventPayload(event);
  const keys = getTaskActivityKeys(event, payload);
  if (keys.length === 0) return { ...current };

  const isTerminal = event.type === 'task_completed' || event.type === 'task_cancelled' || event.type === 'task_failed';
  const hasTaskId = Boolean(getStringValue(payload.task_id));
  const phase = getTaskActivityPhase(event, payload);
  if (!isTerminal && !phase) return { ...current };

  const next = { ...current };
  const lastEventAt = getTaskActivityTimestamp(event);

  if (isTerminal && hasTaskId) {
    // [AutoC 2026-06-04] Why: completed or cancelled tasks should disappear from
    // the realtime overlay once the authoritative active-task list catches up. How:
    // delete all known keys when task_id is present. Purpose: stale live labels do
    // not outlive terminal task events.
    for (const key of keys) delete next[key];
    return next;
  }

  const activity: TaskActivity = {
    phase: isTerminal ? 'idle' : phase || 'idle',
    detail: isTerminal ? '' : getTaskActivityDetail(event, payload),
    lastEventAt,
  };
  for (const key of keys) next[key] = activity;
  return next;
}

function getNestedRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key];
  return isRecord(value) ? value : {};
}

function getTaskContext(payload: Record<string, unknown>): Record<string, unknown> {
  const input = getNestedRecord(payload, 'input');
  return getNestedRecord(input, 'task_context');
}

function getEventConversationKey(payload: Record<string, unknown>): string {
  // [2026-06-03] Why: task lifecycle events usually carry conversation metadata
  // inside input.task_context rather than at the payload top level. How: check the
  // direct key first, then the persisted task context. Purpose: global WS routing can
  // identify web sessions even when the event.session_id is a branch session.
  return getStringValue(payload.conversation_key)
    || getStringValue(getNestedRecord(payload, 'input').conversation_key)
    || getStringValue(getTaskContext(payload).conversation_key)
    || getStringValue(getTaskContext(payload).route_conversation_key);
}

function getEventParentSessionId(payload: Record<string, unknown>): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getTaskContext(payload);
  return getStringValue(payload.parent_session_id)
    || getStringValue(input.parent_session_id)
    || getStringValue(taskContext.parent_session_id);
}

function getEventBranchSessionId(payload: Record<string, unknown>): string {
  // [2026-06-03] Why: branch_created is delivered on the parent session, while later
  // task snapshots and stream events may use the branch runtime id. How: read the
  // branch id from the same top-level, input, and task_context locations used by
  // backend events. Purpose: the global WebSocket can register branch→conversation
  // routes before branch_1 events arrive.
  const input = getNestedRecord(payload, 'input');
  const taskContext = getTaskContext(payload);
  return getStringValue(payload.branch_session_id)
    || getStringValue(input.branch_session_id)
    || getStringValue(taskContext.branch_session_id);
}

function getEventSourceInboundSeq(payload: Record<string, unknown>): number | undefined {
  const seq = getNumberValue(payload.source_inbound_seq);
  return seq !== undefined && seq > 0 ? seq : undefined;
}

function findConversationIdBySession(state: ChatStoreState, sessionId: string): string {
  if (!sessionId) return '';
  return state.conversationIdsBySession[sessionId]
    || state.conversations.find((conversation) => conversation.sessionId === sessionId)?.id
    || '';
}

function resolveWebConversationKeyToId(state: ChatStoreState, conversationKey: string): string {
  // [2026-06-03] Why: SDK route metadata uses web:{conversationId}, while this store
  // indexes conversations by the bare conversation id. How: accept only web keys,
  // strip the prefix, and prefer an existing sidebar row when present. Purpose:
  // agent child sessions can route to the parent chat without inventing a new map.
  if (!conversationKey.startsWith('web:')) return '';
  const normalized = normalizeConversationKey(conversationKey);
  return state.conversations.find((conversation) => conversation.id === normalized)?.id || normalized;
}

function getStructuredAgentRouteConversationKey(payload: Record<string, unknown>): string {
  // [2026-06-03] Why: dispatch child sessions store their own agent: conversation
  // key, but routing metadata preserves the visible parent web conversation. How:
  // read the same structured fallback order requested for the web frontend.
  // Purpose: frontend routing matches the SDK router without parsing agent strings.
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  const inputDispatchOrigin = getNestedRecord(input, '_dispatch_origin');
  const payloadDispatchOrigin = getNestedRecord(payload, 'dispatch_origin');
  const candidates = [
    getStringValue(taskContext.route_conversation_key),
    getStringValue(inputDispatchOrigin.parent_conversation_key),
    getStringValue(payloadDispatchOrigin.parent_conversation_key),
    getStringValue(payload.route_conversation_key),
    getStringValue(payload.parent_conversation_key),
  ];

  // [AutoC 2026-06-03] Why: these candidates are backend route metadata fields.
  // How: accept only the web:{conversationId} protocol form and ignore display text.
  // Purpose: child-event routing stays protocol-driven after removing text hacks.
  return candidates.find((candidate) => candidate.startsWith('web:')) || '';
}

function resolveAgentRouteConversationId(state: ChatStoreState, payload: Record<string, unknown>): string {
  // [2026-06-03] Why: resolveEventConversationId used to drop agent:* keys before
  // checking route_conversation_key. How: isolate the child-agent fallback so both
  // routing and child-state tracking share the same decision. Purpose: one rule
  // determines whether a child event belongs to a visible parent conversation.
  return resolveWebConversationKeyToId(state, getStructuredAgentRouteConversationKey(payload));
}

function isAgentEventRoutedToConversation(
  state: ChatStoreState,
  payload: Record<string, unknown>,
  conversationId: string,
): boolean {
  // [2026-06-03] Why: child status updates must only run for events that were routed
  // through the agent fallback, not ordinary branch or parent-session events. How:
  // require an agent conversation key and a web parent route that resolves to the
  // current conversation. Purpose: unrelated websocket traffic cannot alter childNodes.
  return getEventConversationKey(payload).startsWith('agent:')
    && Boolean(conversationId)
    && resolveAgentRouteConversationId(state, payload) === conversationId;
}

function resolveEventConversationId(state: ChatStoreState, event: SupervisorEvent): string {
  // [2026-06-03] Why: /v1/ws emits every Supervisor session, including Discord and
  // internal branch sessions. How: prefer explicit web conversation metadata, then
  // source inbound and parent-session metadata, before falling back to direct
  // session routes. Purpose: a stale branch_1→branch_1 route cannot override the
  // parent web conversation carried by branch task events.
  const payload = getEventPayload(event);
  const conversationKey = getEventConversationKey(payload);
  // [AutoC 2026-06-03] Why: conversation_key prefixes are supervisor protocol
  // markers. How: route web:* directly, route agent:* only through structured parent
  // metadata, and reject other channels here. Purpose: retained prefix checks do not
  // depend on localized UI text.
  if (conversationKey.startsWith('web:')) {
    return normalizeConversationKey(conversationKey);
  }
  if (conversationKey && !conversationKey.startsWith('agent:')) {
    return '';
  }
  if (conversationKey.startsWith('agent:')) {
    const routedConversationId = resolveAgentRouteConversationId(state, payload);
    if (routedConversationId) return routedConversationId;
  }

  const sourceInboundSeq = getEventSourceInboundSeq(payload);
  if (sourceInboundSeq !== undefined) {
    const userMessageId = state.userMessageByInboundSeq[String(sourceInboundSeq)];
    const userMessage = userMessageId ? state.messagesById[userMessageId] : undefined;
    if (userMessage) return userMessage.conversationId;
  }

  const parentConversationId = findConversationIdBySession(state, getEventParentSessionId(payload));
  if (parentConversationId) return parentConversationId;

  const direct = findConversationIdBySession(state, event.session_id);
  if (direct) return direct;

  return findConversationIdBySession(state, getEventBranchSessionId(payload));
}

function collectEventRouteSessionIds(event: SupervisorEvent, payload: Record<string, unknown>): string[] {
  // [2026-06-03] Why: child dispatch events can identify the same runtime through
  // event, payload, input, task_context, and dispatch_origin fields. How: collect
  // every documented location once. Purpose: approvals and later lifecycle events
  // can resolve back to the parent conversation even if they only carry a child id.
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  const payloadDispatchOrigin = getNestedRecord(payload, 'dispatch_origin');
  const inputDispatchOrigin = getNestedRecord(input, '_dispatch_origin');
  const routeSessionIds = [
    event.session_id,
    getStringValue(payload.session_id),
    getStringValue(payload.parent_session_id),
    getStringValue(payload.branch_session_id),
    getStringValue(payload.runtime_session_id),
    getStringValue(payload.child_session_id),
    getStringValue(input.parent_session_id),
    getStringValue(input.branch_session_id),
    getStringValue(input.child_session_id),
    getStringValue(taskContext.session_id),
    getStringValue(taskContext.parent_session_id),
    getStringValue(taskContext.branch_session_id),
    getStringValue(taskContext.child_session_id),
    getStringValue(payloadDispatchOrigin.parent_session_id),
    getStringValue(inputDispatchOrigin.parent_session_id),
  ];

  return routeSessionIds.filter((sessionId, index, all) => sessionId && all.indexOf(sessionId) === index);
}

function seedConversationRouteForEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): ChatStoreState {
  // [2026-06-03] Why: branch_created arrives on the parent session, but later live
  // branch events may arrive with event.session_id=branch_1. How: seed every route id
  // carried by the event, including branch_session_id, parent_session_id, and
  // runtime_session_id, before reducer replay. Purpose: reducer event-log rows and
  // messages land on the parent conversation without creating a branch_1 chat.
  if (!conversationId) return state;

  const payload = getEventPayload(event);
  const routeSessionIds = collectEventRouteSessionIds(event, payload);

  const conversationIdsBySession = { ...state.conversationIdsBySession };
  let changed = false;
  for (const sessionId of routeSessionIds) {
    if (conversationIdsBySession[sessionId] !== conversationId) {
      conversationIdsBySession[sessionId] = conversationId;
      changed = true;
    }
  }

  return changed ? { ...state, conversationIdsBySession } : state;
}

function getChildNodeIdFromAgentConversationKey(conversationKey: string): string {
  // [2026-06-03] Why: older child events may omit input.entry_node_id and
  // task_context.node_id while still carrying agent:{node_id}:... storage keys.
  // How: parse only the bounded prefix segment as a last-resort fallback. Purpose:
  // childNodes remains useful for legacy routed events without backend changes.
  if (!conversationKey.startsWith('agent:')) return '';
  const rest = conversationKey.slice('agent:'.length);
  const separator = rest.indexOf(':');
  return separator > 0 ? rest.slice(0, separator) : '';
}

function getChildNodeSessionId(event: SupervisorEvent, payload: Record<string, unknown>): string {
  // [2026-06-03] Why: task snapshots run on a branch or parent runtime session while
  // the actual child chat history is stored under input.child_session_id. How: prefer
  // child_session_id from every documented payload location before falling back to the
  // event runtime id. Purpose: childNodes keys match the session id users can open.
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  return getStringValue(payload.child_session_id)
    || getStringValue(input.child_session_id)
    || getStringValue(taskContext.child_session_id)
    || getStringValue(payload.session_id)
    || event.session_id;
}

function getChildNodeId(payload: Record<string, unknown>, previous?: ChildNodeState): string {
  // [2026-06-03] Why: node identity is stored inside the dispatch input, not on a
  // dedicated child-state event. How: read the documented fields first, then keep an
  // existing value or parse agent:{node_id}:... as compatibility. Purpose: status
  // updates do not lose the scout/smith label after the first event.
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  const conversationKey = getEventConversationKey(payload);
  return getStringValue(input.entry_node_id)
    || getStringValue(taskContext.node_id)
    || previous?.nodeId
    || getChildNodeIdFromAgentConversationKey(conversationKey)
    || 'unknown';
}

function updateChildNodesByEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): Readonly<Record<string, ChildNodeState>> {
  // [2026-06-03] Why: Phase 1 needs data-layer child-session status before adding
  // sidebar or floating UI. How: only update childNodes for events proven to have
  // routed from an agent:* key to a web:* parent through structured metadata.
  // Purpose: parent conversations can expose child-agent activity without backend work.
  const payload = getEventPayload(event);
  if (!isAgentEventRoutedToConversation(state, payload, conversationId)) return state.childNodes;

  const status = CHILD_NODE_STATUS_BY_EVENT[event.type];
  if (!status) return state.childNodes;

  const sessionId = getChildNodeSessionId(event, payload);
  if (!sessionId) return state.childNodes;

  const previous = state.childNodes[sessionId];
  const isTerminal = status === 'completed' || status === 'failed' || status === 'cancelled';
  return {
    ...state.childNodes,
    [sessionId]: {
      sessionId,
      nodeId: getChildNodeId(payload, previous),
      parentConversationId: conversationId,
      status,
      taskId: getStringValue(payload.task_id) || previous?.taskId,
      startedAt: previous?.startedAt || event.ts,
      completedAt: isTerminal ? event.ts : previous?.completedAt,
    },
  };
}

function createReducerEventForConversation(
  event: SupervisorEvent,
  payload: Record<string, unknown>,
  conversationId: string,
  isAgentChildRoute: boolean,
): SupervisorEvent {
  // [2026-06-03] Why: eventReducer intentionally has no agent-route fallback and
  // prefers payload.conversation_key when present. How: for routed child events only,
  // pass a shallow event copy whose visible conversation_key is the resolved web
  // parent while preserving the raw child key. Purpose: keep eventReducer unchanged
  // and prevent agent:* conversation ids from polluting normalized chat state.
  if (!isAgentChildRoute) return event;
  return {
    ...event,
    payload: {
      ...event.payload,
      conversation_key: `web:${conversationId}`,
      child_conversation_key: getEventConversationKey(payload),
    },
  };
}

function createReducerEventForChildSession(
  event: SupervisorEvent,
  payload: Record<string, unknown>,
  childSessionId: string,
): SupervisorEvent {
  const childConversationId = getChildConversationId(childSessionId);
  // [2026-06-03] Why: parent-routed agent events are hidden from the parent chat, but
  // must be replayed when the user is viewing the child stream. How: clone the event
  // with a child-namespaced conversation_key and a distinct event id. Purpose: the
  // reducer can build child messages without colliding with the parent audit event.
  return {
    ...event,
    event_id: `${event.event_id || `${event.session_id}:${event.seq}:${event.type}`}:child-view:${childSessionId}`,
    payload: {
      ...event.payload,
      conversation_key: `web:${childConversationId}`,
      child_conversation_key: getEventConversationKey(payload),
      child_session_id: childSessionId,
    },
  };
}

function appendAgentRouteEventLog(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): ChatStoreState {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  if (state.processedEventIds[eventId]) return state;
  // [2026-06-03] Why: routed child events should remain visible in the event log, but
  // reducing them normally would render child chat cards in the parent conversation.
  // How: stamp a minimal audit row and processed marker without applying message
  // reducers. Purpose: parent event logs stay useful while parent messages stay clean.
  return {
    ...state,
    processedEventIds: { ...state.processedEventIds, [eventId]: true },
    lastSeqBySession: {
      ...state.lastSeqBySession,
      [event.session_id]: Math.max(state.lastSeqBySession[event.session_id] || 0, event.seq || 0),
    },
    eventLog: [
      ...state.eventLog,
      {
        id: `log:${eventId}`,
        eventId,
        seq: event.seq,
        ts: event.ts,
        sessionId: event.session_id,
        conversationId,
        type: event.type,
        component: event.component,
        payload: event.payload || {},
      },
    ].slice(-3000),
  };
}

function selectChildNodesFromState(state: ChatStoreState, conversationId: string): ChildNodeState[] {
  // [2026-06-03] Why: childNodes is keyed by session for efficient updates, while UI
  // consumers need conversation-grouped lists. How: filter by parentConversationId and
  // keep a deterministic startedAt order. Purpose: sidebar or status components can
  // render stable child-node groups without duplicating filtering logic.
  return Object.values(state.childNodes)
    .filter((child) => child.parentConversationId === conversationId)
    .sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
}

function selectHasActiveChildNodesFromState(state: ChatStoreState, conversationId: string): boolean {
  // [2026-06-03] Why: callers often only need to know whether a parent chat has
  // active child work. How: reuse the grouped selector and check normalized active
  // statuses. Purpose: components avoid hard-coding lifecycle status names.
  return selectChildNodesFromState(state, conversationId).some((child) => CHILD_NODE_ACTIVE_STATUSES.has(child.status));
}

function getAffectedSessionIds(state: ChatStoreState, event: SupervisorEvent, conversationId: string): string[] {
  // [2026-06-03] Why: a task may run in an entry-branch session while the composer
  // lock belongs to the visible parent web session. How: collect event, payload,
  // parent, and sidebar session ids. Purpose: terminal task events unlock the correct
  // active conversation without closing the global WebSocket.
  const payload = getEventPayload(event);
  const ids = new Set<string>();
  const add = (value: string) => { if (value) ids.add(value); };

  add(event.session_id);
  add(getStringValue(payload.session_id));
  add(getStringValue(payload.runtime_session_id));
  add(getEventParentSessionId(payload));
  add(getEventBranchSessionId(payload));

  const conversation = state.conversations.find((item) => item.id === conversationId);
  add(conversation?.sessionId || '');

  return [...ids];
}

function updateGeneratingByEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): Record<string, boolean> {
  const shouldMarkActive = ACTIVE_TASK_EVENTS.has(event.type);
  const shouldMarkDone = isTerminalTaskEvent(event);
  if (!shouldMarkActive && !shouldMarkDone) return { ...state.generatingBySession };

  const next = { ...state.generatingBySession };
  for (const sessionId of getAffectedSessionIds(state, event, conversationId)) {
    next[sessionId] = shouldMarkActive && !shouldMarkDone;
  }
  return next;
}

function isConversationGenerating(
  conversations: readonly ConversationMeta[],
  activeConversationId: string | null,
  generatingBySession: Readonly<Record<string, boolean>>,
  fallback: boolean,
): boolean {
  const active = activeConversationId ? conversations.find((conversation) => conversation.id === activeConversationId) : undefined;
  return active?.sessionId ? Boolean(generatingBySession[active.sessionId]) : fallback;
}

function startGlobalWebSocket(set: StoreSetter, get: StoreGetter) {
  clearReconnectTimer();
  if (get().connectionStatus !== 'open') {
    set({ connectionStatus: 'connecting' });
  }

  // [2026-06-03] Why: WS is now a pure live-forward stream with no replay.
  // The backend no longer sends historical events on connect. All events
  // received are real-time, so auto-approve runs unconditionally.
  connectGlobalWS(
    0,
    (event) => {
      let terminalConversationId = '';
      let terminalSessionId = '';

      set((state) => {
        const taskActivities = updateTaskActivitiesByEvent(state.taskActivities, event);
        // [AutoC 2026-06-04] Why: active-task activity should update for every
        // global WebSocket frame, including sessions that are not currently routed
        // to a visible chat. How: merge taskActivities before normal conversation
        // routing and preserve it through reducer returns. Purpose: the System modal
        // can show live phases independent of selected conversation state.
        const stateWithTaskActivity = { ...state, taskActivities };
        const conversationId = resolveEventConversationId(stateWithTaskActivity, event);
        if (!conversationId) {
          return { connectionStatus: 'open', taskActivities };
        }

        const payload = getEventPayload(event);
        const isAgentChildRoute = isAgentEventRoutedToConversation(stateWithTaskActivity, payload, conversationId);
        const reducerEvent = createReducerEventForConversation(event, payload, conversationId, isAgentChildRoute);
        const routedState = seedConversationRouteForEvent(stateWithTaskActivity, event, conversationId);
        const childNodes = updateChildNodesByEvent(routedState, event, conversationId);

        // [2026-06-03] Why: child-agent events (agent:*) routed to a parent conversation
        // must NOT be rendered as chat messages in the parent's message list. Their
        // inbound/outbound would otherwise appear as "你" messages or assistant cards
        // mixed into the parent stream. How: keep an audit log for the parent and, only
        // when the user is viewing that child session, replay a cloned event into the
        // child message cache. Purpose: parent chat stays clean while child navigation
        // still receives live streaming updates.
        if (isAgentChildRoute) {
          const childSessionId = getChildNodeSessionId(event, payload);
          let nextState = appendAgentRouteEventLog({ ...routedState, childNodes }, event, conversationId);
          if (childSessionId && state.viewingChildSessionId === childSessionId) {
            const childConversationId = getChildConversationId(childSessionId);
            const childEvent = createReducerEventForChildSession(event, payload, childSessionId);
            const reducedChildState = reduceChatEvent(nextState, childEvent);
            nextState = {
              ...(reducedChildState as ChatStoreState),
              // [2026-06-03] Why: child-view reducer replay may map a branch runtime
              // session to child:*, but parent routing should keep using the route map
              // built above. How: restore the parent route table after extracting child
              // messages. Purpose: later branch events cannot escape parent routing.
              conversationIdsBySession: nextState.conversationIdsBySession,
              childSessionMessages: {
                ...nextState.childSessionMessages,
                [childSessionId]: selectOrderedMessagesFromState(reducedChildState, childConversationId),
              },
            };
          }
          return {
            ...nextState,
            taskActivities,
            connectionStatus: 'open' as const,
          };
        }

        const reducedState = reduceChatEvent(routedState, reducerEvent);
        const conversations = syncConversationsAfterEvent(state.conversations, reducedState, reducerEvent, conversationId);
        const generatingBySession = updateGeneratingByEvent({ ...state, conversations }, reducerEvent, conversationId);
        const isGenerating = isConversationGenerating(
          conversations,
          state.activeConversationId,
          generatingBySession,
          state.isGenerating,
        );

        if (isTerminalTaskEvent(event)) {
          const conversation = conversations.find((item) => item.id === conversationId);
          const historySessionId = conversation?.sessionId || getEventParentSessionId(getEventPayload(event)) || event.session_id;
          terminalConversationId = conversationId;
          terminalSessionId = historySessionId;
        }

        return {
          ...reducedState,
          conversations,
          generatingBySession,
          childNodes,
          taskActivities,
          connectionStatus: 'open',
          isGenerating,
        };
      });

      maybeAutoApproveApprovalRequest(event, get);

      if (terminalConversationId && terminalSessionId) {
        // [AutoC 2026-06-04] Why: each LLM request card is finalized in place when
        // stream_end/outbound_message arrives, so task completion must not clear and
        // rebuild the whole conversation from /history. How: keep only the sidebar
        // recency sort on terminal events and leave the reducer-owned messages intact.
        // Purpose: completing a task no longer causes visible card jumps.
        set((state) => ({ conversations: sortConversationsByRecency(state.conversations) }));
      }
    },
    () => {
      // [2026-06-03] Why: connectionStatus now describes the global event stream,
      // not a selected task. How: mark open as soon as /v1/ws accepts the cursor.
      // Purpose: switching conversations does not make transport status ambiguous.
      set({ connectionStatus: 'open' });
    },
    () => {
      // [2026-06-03] Why: the global WS should remain alive for all sessions. How:
      // reconnect after unexpected close regardless of task state. Purpose: task
      // completion and cancellation no longer control realtime lifetime.
      set({ connectionStatus: 'reconnecting' });
      reconnectTimer = setTimeout(() => startGlobalWebSocket(set, get), 2000);
    },
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringifyContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function collapseForPreview(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function truncateForPreview(value: string, limit: number): string {
  const chars = Array.from(value);
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : value;
}

function summarizeArguments(toolName: string, args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '';
  if (CONTROL_TOOL_NAMES.has(toolName) && typeof args.text === 'string') return '';

  return Object.entries(args)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncateForPreview(collapseForPreview(stringifyContent(value)), 80)}`)
    .join(', ');
}

function buildHistoryToolResultIndex(messages: readonly StructuredMessage[]): Map<string, HistoryToolResult> {
  const resultIndex = new Map<string, HistoryToolResult>();

  for (const message of messages) {
    if (message.role !== 'tool' || !message.tool_call_id) continue;

    const toolName = message.tool_name || message.name || '';
    const rawContent = stringifyContent(message.content);
    const trimmed = rawContent.trim();
    // [2026-06-01] Why: old tool result history can start with a cross-mark emoji,
    // but the migrated frontend should not generate emoji-prefixed results. How:
    // preserve legacy detection while accepting plain rejected text. Purpose: Material
    // Symbols migration remains compatible with existing stored conversations.
    const rejected = /^\s*(?:❌\s*)?REJECTED(?:\b|:)/i.test(trimmed);
    const status: HistoryToolResult['status'] = rejected || /^ERROR(?:\b|:)/i.test(trimmed) ? 'error' : 'success';
    const isAutoResult = status === 'success' && CONTROL_TOOL_NAMES.has(toolName) && trimmed.toLowerCase() === 'ok';

    resultIndex.set(message.tool_call_id, {
      status,
      rawInline: rawContent,
      rejected: rejected || undefined,
      isAutoResult: isAutoResult || undefined,
      result: isAutoResult
        ? undefined
        : status === 'error'
          ? rawContent
          : truncateForPreview(collapseForPreview(rawContent), 120) || undefined,
    });
  }

  return resultIndex;
}

function normalizeHistoryToolCalls(value: StructuredMessage['tool_calls']): HistoryToolCall[] {
  if (!Array.isArray(value)) return [];

  return value.map((toolCall, index) => ({
    id: toolCall.id || `history-tool-${index}`,
    name: toolCall.name || 'unknown',
    arguments: isRecord(toolCall.arguments) ? toolCall.arguments : undefined,
  }));
}

function normalizeHistoryThinkingSegments(message: StructuredMessage): HistoryThinkingSegment[] {
  const normalizeBlock = (block: StructuredThinkingBlock): HistoryThinkingSegment | null => {
    const text = typeof block.text === 'string' ? block.text : '';
    if (!text.trim()) return null;
    return {
      text,
      // [AutoC 2026-06-04] Why: /history has used both snake_case and camelCase
      // timestamp keys during the streaming refactor. How: normalize both shapes into
      // the render block fields. Purpose: refreshed thinking blocks show elapsed time
      // instead of the character-count fallback.
      startedAt: typeof block.started_at === 'string' ? block.started_at : typeof block.startedAt === 'string' ? block.startedAt : undefined,
      endedAt: typeof block.ended_at === 'string' ? block.ended_at : typeof block.endedAt === 'string' ? block.endedAt : undefined,
    };
  };

  const structuredBlocks = Array.isArray(message.thinking_blocks)
    ? message.thinking_blocks.map(normalizeBlock).filter((block): block is HistoryThinkingSegment => Boolean(block))
    : [];
  if (structuredBlocks.length > 0) return structuredBlocks;

  if (isRecord(message.thinking)) {
    const normalized = normalizeBlock(message.thinking as StructuredThinkingBlock);
    return normalized ? [normalized] : [];
  }

  const text = typeof message.thinking === 'string' ? message.thinking : message.thinking_text || '';
  if (!text.trim()) return [];

  // [AutoC 2026-06-04] Why: older /history rows exposed reasoning as a flat string
  // plus optional top-level timestamps. How: normalize that legacy shape into the
  // same segment model used by thinking_blocks. Purpose: hydration has one path and
  // can keep elapsed-time metadata whenever the backend provides it.
  return [{
    text,
    startedAt: message.reasoning_started_at,
    endedAt: message.reasoning_ended_at,
  }];
}

function extractControlToolText(toolCalls: readonly HistoryToolCall[]): { text: string; toolName?: string } {
  for (const toolCall of toolCalls) {
    if (!CONTROL_TOOL_NAMES.has(toolCall.name)) continue;
    const text = typeof toolCall.arguments?.text === 'string' ? toolCall.arguments.text : '';
    if (text) return { text, toolName: toolCall.name };
  }
  return { text: '' };
}

function historyToolStatus(result: HistoryToolResult | undefined): ToolStatus {
  if (!result) return 'success';
  return result.status;
}

function createHistoryToolExecutions(
  toolCalls: readonly HistoryToolCall[],
  resultIndex: Map<string, HistoryToolResult>,
  messageId: string,
  createdAt: string,
  sessionId: string,
  nodeId?: string,
): ToolExecution[] {
  return toolCalls.map((toolCall, index) => {
    const result = toolCall.id ? resultIndex.get(toolCall.id) : undefined;
    const stableId = `${messageId}|history-tool:${toolCall.id || index}`;
    return {
      stableId,
      messageId,
      id: toolCall.id,
      index,
      name: toolCall.name,
      status: historyToolStatus(result),
      arguments: toolCall.arguments,
      argumentsText: toolCall.arguments ? JSON.stringify(toolCall.arguments) : undefined,
      summary: summarizeArguments(toolCall.name, toolCall.arguments),
      result: result?.result,
      rawInline: result?.rawInline,
      rejected: result?.rejected,
      // [2026-06-02] Why: rejected control tools explain why a final action failed
      // and must not be treated like the automatic successful "ok" result. How: keep
      // the existing auto-result hiding but exclude rejected tool results. Purpose:
      // historical rejected finish/reply/ask calls remain visible in the tool list.
      hidden: CONTROL_TOOL_NAMES.has(toolCall.name) && result?.isAutoResult && !result?.rejected,
      nodeId,
      createdAt,
      updatedAt: createdAt,
      eventIds: [`history:${sessionId}:${messageId}:tool:${index}`],
    } satisfies ToolExecution;
  });
}

function createTextBlock(
  id: string,
  createdAt: string,
  text: string,
  delivery: TextBlock['delivery'] = 'history',
): TextBlock {
  return {
    id,
    kind: 'text',
    text,
    // [2026-06-02] Why: hydrated reply and finish messages need the same delivery
    // metadata as live reducer messages. How: keep history as the default while
    // allowing callers to pass intermediate for reply and final for finish. Purpose:
    // refresh and reconnect rebuilds preserve the visual reply and finish semantics.
    delivery,
    streaming: false,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function createThinkingBlock(
  id: string,
  createdAt: string,
  text: string,
  startedAt?: string,
  endedAt?: string,
): ThinkingBlock {
  return {
    id,
    kind: 'thinking',
    text,
    streaming: false,
    startedAt,
    endedAt,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function createToolBlock(id: string, createdAt: string, toolIds: readonly string[]): ToolBlock {
  return {
    id,
    kind: 'tool',
    toolIds: [...toolIds],
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function appendHistoryMessage(state: ChatState, message: WsMessage, tools: readonly ToolExecution[]): ChatState {
  const order = state.messageOrderByConversation[message.conversationId] || [];
  const nextToolExecutionsById = { ...state.toolExecutionsById };
  const nextToolExecutionOrder = [...state.toolExecutionOrder];

  for (const tool of tools) {
    nextToolExecutionsById[tool.stableId] = tool;
    if (!nextToolExecutionOrder.includes(tool.stableId)) nextToolExecutionOrder.push(tool.stableId);
  }

  return {
    ...state,
    messagesById: { ...state.messagesById, [message.id]: message },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [message.conversationId]: order.includes(message.id) ? order : [...order, message.id],
    },
    toolExecutionsById: nextToolExecutionsById,
    toolExecutionOrder: nextToolExecutionOrder,
  };
}

function removeConversationMessages(state: ChatState, conversationId: string): ChatState {
  const removedMessageIds = new Set(state.messageOrderByConversation[conversationId] || []);
  if (removedMessageIds.size === 0) return state;

  const messagesById = { ...state.messagesById };
  for (const messageId of removedMessageIds) delete messagesById[messageId];

  const messageOrderByConversation = { ...state.messageOrderByConversation };
  delete messageOrderByConversation[conversationId];

  const toolExecutionsById = { ...state.toolExecutionsById };
  const toolExecutionOrder = state.toolExecutionOrder.filter((toolId) => {
    const tool = state.toolExecutionsById[toolId];
    if (tool && removedMessageIds.has(tool.messageId)) {
      delete toolExecutionsById[toolId];
      return false;
    }
    return true;
  });

  return { ...state, messagesById, messageOrderByConversation, toolExecutionsById, toolExecutionOrder };
}

function getHistoryEventId(sessionId: string, sourceMessageId: string | undefined, messageId: string): string {
  // [2026-06-02] Why: active history hydration now compares persisted rows against
  // live WebSocket messages. How: keep the exact synthetic source id in one helper so
  // dedupe checks and hydrated messages use the same value. Purpose: repeated history
  // loads cannot create a second card for an already-hydrated source message.
  return `history:${sessionId}:${sourceMessageId || messageId}`;
}

function getMessageTextForHistoryDedupe(message: WsMessage): string {
  // [2026-06-02] Why: live messages do not share ConversationStore UUIDs with
  // structured history rows. How: compare only rendered text blocks and leave tool-only
  // rows to source-id matching. Purpose: stream cards and history cards with the same
  // visible text do not render twice during active-session races.
  return message.blocks
    .filter((block): block is TextBlock => block.kind === 'text')
    .map((block) => block.text)
    .join('\n');
}

function createHistoryContentSignature(role: WsMessage['role'], text: string): string | undefined {
  // [2026-06-02] Why: content dedupe must not collapse user and assistant messages
  // that happen to contain the same text. How: normalize whitespace and include the
  // role in the signature. Purpose: matching stays narrow while catching stream/history
  // duplicates that differ only in delivery metadata.
  const compact = collapseForPreview(text);
  return compact ? `${role}:${compact}` : undefined;
}

function hydrateStructuredHistory(
  state: ChatState,
  sessionId: string,
  conversationId: string,
  history: readonly StructuredMessage[],
  preserveExistingMessages = false,
): ChatState {
  // Why: /history returns persisted ConversationStore messages, not SupervisorEvent
  // records. How: convert that structured history into the same WsMessage blocks and
  // ToolExecution tables used by the reducer. Purpose: cold startup can show existing
  // sessions while live WebSocket events continue to use reduceChatEvent only.
  const resultIndex = buildHistoryToolResultIndex(history);
  // Why: normal cold-start hydration should replace stale history, but an active
  // generation may already have live WebSocket messages in this conversation. How:
  // the caller can skip the removal step for active sessions. Purpose: late history
  // responses do not erase in-flight stream or tool blocks.
  let nextState = preserveExistingMessages ? state : removeConversationMessages(state, conversationId);
  const existingSourceIds = new Set<string>();
  const existingMessageIds = new Set<string>();
  const existingContentSignatures = new Set<string>();

  if (preserveExistingMessages) {
    // [2026-06-02] Why: preserving live WebSocket messages avoids deleting active
    // streams, but appending every persisted history row creates duplicate cards. How:
    // collect source ids, generated message ids, and role-scoped visible text from the
    // current conversation before hydration. Purpose: late history responses can add
    // missing old rows without duplicating messages that are already visible.
    for (const id of nextState.messageOrderByConversation[conversationId] || []) {
      const existingMessage = nextState.messagesById[id];
      if (!existingMessage) continue;
      existingMessageIds.add(existingMessage.id);
      for (const eventId of existingMessage.eventIds || []) {
        // [AutoC 2026-06-03] Why: history:{sessionId}:{rowId} is the store's own
        // source-id namespace. How: compare only that generated protocol prefix.
        // Purpose: dedupe history rows without inspecting user-visible content.
        if (eventId.startsWith(`history:${sessionId}:`)) existingSourceIds.add(eventId);
      }
      const signature = createHistoryContentSignature(existingMessage.role, getMessageTextForHistoryDedupe(existingMessage));
      if (signature) existingContentSignatures.add(signature);
    }
  }

  const shouldSkipPreservedHistoryMessage = (
    sourceEventId: string,
    messageId: string,
    role: WsMessage['role'],
    text: string,
  ): boolean => {
    if (!preserveExistingMessages) return false;
    if (existingSourceIds.has(sourceEventId) || existingMessageIds.has(messageId)) return true;
    const signature = createHistoryContentSignature(role, text);
    return Boolean(signature && existingContentSignatures.has(signature));
  };

  const rememberHydratedHistoryMessage = (sourceEventId: string, messageId: string) => {
    // [2026-06-02] Why: a malformed or replayed history response can contain the same
    // persisted source more than once. How: record source and generated ids as each row
    // is accepted, without adding content signatures that would collapse legitimate
    // repeated text inside one history payload. Purpose: preserve duplicate human text
    // while still avoiding duplicate source rows.
    if (!preserveExistingMessages) return;
    existingSourceIds.add(sourceEventId);
    existingMessageIds.add(messageId);
  };

  let accumulatedThinking: HistoryThinkingSegment[] = [];
  let accumulatedTools: ToolExecution[] = [];
  let accumulatedCreatedAt = '';

  const resetAccumulatedAssistant = () => {
    accumulatedThinking = [];
    accumulatedTools = [];
    accumulatedCreatedAt = '';
  };

  const pushAssistantMessage = (
    sourceMessage: StructuredMessage,
    text: string,
    currentTools: readonly ToolExecution[],
    completionType?: WsMessage['completionType'],
  ) => {
    const createdAt = sourceMessage.created_at || accumulatedCreatedAt || new Date().toISOString();
    const messageId = `message:${conversationId}:history:${sourceMessage.id || nextState.messageOrderByConversation[conversationId]?.length || 0}`;
    const historyEventId = getHistoryEventId(sessionId, sourceMessage.id, messageId);
    if (shouldSkipPreservedHistoryMessage(historyEventId, messageId, 'assistant', text)) {
      resetAccumulatedAssistant();
      return;
    }
    const thinkingSegments = [
      ...accumulatedThinking,
      ...normalizeHistoryThinkingSegments(sourceMessage),
    ].filter((item) => item.text.trim());
    const tools = [...accumulatedTools, ...currentTools].map((tool) => ({
      ...tool,
      // Why: a tool-only assistant history entry may be merged into a later visible
      // assistant message. How: keep the stable tool id from its source entry but
      // update messageId to the visible message that owns the rendered tool block.
      // Purpose: avoid duplicate ids while preserving selector lookups for tools.
      messageId,
    }));
    const blocks: RenderBlock[] = [];

    for (const [index, thinking] of thinkingSegments.entries()) {
      // [AutoC 2026-06-04] Why: backend history can now return multiple structured
      // reasoning blocks, each with its own timing. How: create one ThinkingBlock per
      // segment instead of flattening everything into a string separated by markers.
      // Purpose: refreshed history keeps the same elapsed-time display model as live
      // streaming cards.
      blocks.push(createThinkingBlock(
        `${messageId}|block:thinking:history:${index}`,
        createdAt,
        thinking.text,
        thinking.startedAt,
        thinking.endedAt,
      ));
    }
    if (tools.length > 0) {
      const blockId = `${messageId}|block:tool:history`;
      blocks.push(createToolBlock(blockId, createdAt, tools.map((tool) => tool.stableId)));
      tools.forEach((tool, index) => { tools[index] = { ...tool, blockId }; });
    }
    const textDelivery: TextBlock['delivery'] = completionType === 'reply'
      ? 'intermediate'
      : completionType === 'finish'
        ? 'final'
        : 'history';
    if (text) {
      // [2026-06-02] Why: rebuilt control-tool messages lost their reply/finish
      // delivery after refresh because every history text block used history. How:
      // derive the text delivery from completionType before creating the block.
      // Purpose: reply rebuilds as intermediate and finish rebuilds as final.
      blocks.push(createTextBlock(`${messageId}|block:text:history`, createdAt, text, textDelivery));
    }

    const status: MessageStatus = 'completed';
    const message: WsMessage = {
      id: messageId,
      conversationId,
      sessionId,
      role: 'assistant',
      status,
      createdAt,
      updatedAt: createdAt,
      source: {
        // [AutoC 2026-06-04] Why: request-scoped live cards can later be compared
        // with hydrated assistant rows. How: preserve llm_request_id from /history
        // when the backend provides it. Purpose: task-level metadata is no longer the
        // only way to identify a historical assistant card.
        llmRequestId: sourceMessage.llm_request_id || undefined,
        taskId: sourceMessage.source_task_id || undefined,
        nodeId: sourceMessage.source_node_id || undefined,
      },
      blocks,
      eventIds: [historyEventId],
      hydratedFromHistory: true,
      ...(completionType && { completionType }),
    };

    nextState = appendHistoryMessage(nextState, message, tools.map((tool) => ({
      ...tool,
      eventIds: [`history:${sessionId}:${messageId}:tool:${tool.index ?? 0}`],
    })));
    rememberHydratedHistoryMessage(historyEventId, messageId);
    resetAccumulatedAssistant();
  };

  const flushDanglingAssistant = () => {
    if (accumulatedThinking.length === 0 && accumulatedTools.length === 0) return;
    // Why: ConversationStore can end or switch turns after a tool-only assistant
    // entry. How: emit an empty assistant message carrying the accumulated thinking
    // and tool blocks before processing the next user row. Purpose: preserve history
    // that would otherwise disappear during cold startup hydration.
    pushAssistantMessage({
      id: `dangling-${nextState.messageOrderByConversation[conversationId]?.length || 0}`,
      role: 'assistant',
      content: '',
      created_at: accumulatedCreatedAt || new Date().toISOString(),
    }, '', []);
  };

  for (const message of history) {
    if (message.role === 'user') {
      flushDanglingAssistant();
      if (INTERNAL_USER_MESSAGE_TYPES.has(message.message_type || '')) continue;

      const createdAt = message.created_at || new Date().toISOString();
      const messageId = `message:${conversationId}:history:${message.id || `user-${nextState.messageOrderByConversation[conversationId]?.length || 0}`}`;
      const historyEventId = getHistoryEventId(sessionId, message.id, messageId);
      const text = stringifyContent(message.content);
      // [AutoC 2026-06-03] Why: dispatch-result history rows are backend callback
      // notifications rather than generic system notices. How: map the structured
      // message_type to the dedicated dispatch_callback role. Purpose: refreshed
      // history uses the same label, purple styling, and child-session action as live
      // WebSocket delivery.
      const hydratedRole: WsMessage['role'] = isDispatchResultHistoryMessage(message, text) ? 'dispatch_callback' : 'user';
      if (shouldSkipPreservedHistoryMessage(historyEventId, messageId, hydratedRole, text)) continue;
      const wsMessage: WsMessage = {
        id: messageId,
        conversationId,
        sessionId,
        role: hydratedRole,
        status: 'completed',
        createdAt,
        updatedAt: createdAt,
        source: {
          // [AutoC 2026-06-04] Why: history rows now expose child_* callback metadata
          // in addition to legacy dispatch_* fields. How: prefer child_node_id and
          // child_task_id, then fall back to old names and generic source ids. Purpose:
          // refreshed dispatch cards use the same structured source as live events.
          nodeId: message.child_node_id || message.dispatch_node_id || message.source_node_id || undefined,
          childNodeId: message.child_node_id || message.dispatch_node_id || undefined,
          taskId: message.child_task_id || message.dispatch_task_id || message.source_task_id || undefined,
          childTaskId: message.child_task_id || message.dispatch_task_id || undefined,
          callerNodeId: message.caller_node_id || undefined,
          summary: message.summary || undefined,
          // [AutoC 2026-06-03] Why: the dispatch callback button needs a stable
          // child-session target after refresh. How: read child_session_id from the
          // structured history row emitted by the backend. Purpose: the renderer does
          // not need to infer the child session from localized text or child-node names.
          childSessionId: message.child_session_id || undefined,
        },
        // [2026-06-03] Why: dispatch-result inbound rows are control notifications,
        // not human input. How: they keep their text block but use the structured
        // callback role computed above. Purpose: refreshed history no longer labels
        // them as "你" or as a generic system notice.
        blocks: [createTextBlock(`${messageId}|block:text:history`, createdAt, text)],
        eventIds: [historyEventId],
        hydratedFromHistory: true,
      };
      nextState = appendHistoryMessage(nextState, wsMessage, []);
      rememberHydratedHistoryMessage(historyEventId, messageId);
      continue;
    }

    if (message.role !== 'assistant') continue;

    const pendingMessageId = `message:${conversationId}|history-pending:${message.id || 'assistant'}`;
    const toolCalls = normalizeHistoryToolCalls(message.tool_calls);
    const currentTools = createHistoryToolExecutions(toolCalls, resultIndex, pendingMessageId, message.created_at || new Date().toISOString(), sessionId, message.source_node_id);
    const controlText = extractControlToolText(toolCalls);
    const contentText = stringifyContent(message.content);
    const displayText = contentText.trim() ? contentText : controlText.text;
    const hasControlTool = toolCalls.some((toolCall) => CONTROL_TOOL_NAMES.has(toolCall.name));

    if (hasControlTool && controlText.text) {
      // Why: finish/reply/ask/switch_node are persisted as standalone assistant tool
      // calls by the backend control-tool guard. How: flush any accumulated tool-only
      // assistant history before rendering the visible control text. Purpose: final
      // control-tool output cannot merge into the previous tool card during hydration.
      flushDanglingAssistant();
      // Determine completionType from the control tool name
      const ctName = controlText.toolName;
      const ctType: WsMessage['completionType'] =
        ctName === 'finish' ? 'finish' : ctName === 'ask' ? 'ask' : ctName === 'reply' ? 'reply' : undefined;
      // [2026-06-03] Why: free prose (content) duplicates or precedes the reply/finish
      // text but has no user-facing value. How: always prefer the control tool text over
      // the raw content when a control tool is present. Purpose: the card shows the
      // authoritative reply/finish text, not the internal LLM reasoning.
      pushAssistantMessage(message, controlText.text, currentTools, ctType);
      continue;
    }

    if (!displayText && !hasControlTool) {
      accumulatedThinking.push(...normalizeHistoryThinkingSegments(message));
      if (currentTools.length > 0) accumulatedTools.push(...currentTools);
      if (!accumulatedCreatedAt) accumulatedCreatedAt = message.created_at || '';
      continue;
    }

    pushAssistantMessage(message, displayText, currentTools);
  }

  flushDanglingAssistant();
  return {
    ...nextState,
    conversationIdsBySession: {
      ...nextState.conversationIdsBySession,
      [sessionId]: conversationId,
    },
  };
}

async function loadSessionHistoryIntoStore(conversationId: string, sessionId: string, set: StoreSetter, get: StoreGetter) {
  // [2026-06-03] Why: the old event ring buffer is fragile and size-limited for
  // history rebuilds. How: reconstruct only from getSessionHistory, backed by
  // persistent JSONL. Purpose: final cards use backend-authoritative history while
  // WebSocket sequence cursors remain transport bookkeeping only.
  let history: StructuredMessage[] = [];
  let children: ChildSessionInfo[] = [];

  try {
    [history, children] = await Promise.all([
      getSessionHistory(sessionId, 200),
      getSessionChildren(sessionId),
    ]);
  } catch {
    return;
  }

  set((state) => {
    const preserveExistingMessages = shouldPreserveConversationMessagesDuringHistoryLoad(state, conversationId, sessionId);
    const hydrated = history.length > 0
      ? hydrateStructuredHistory(state, sessionId, conversationId, history, preserveExistingMessages)
      : state;
    const hydratedStore = hydrated as ChatStoreState;
    const childNodes = mergeChildNodesFromSessionChildren(hydratedStore, conversationId, children);
    return {
      ...hydrated,
      childNodes,
      conversations: upsertConversationMeta(state.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
    };
  });
}

async function loadChildSessionHistoryIntoStore(sessionId: string, set: StoreSetter, taskId?: string) {
  let history: StructuredMessage[] = [];
  try {
    history = await getSessionHistory(sessionId, 200, taskId);
  } catch {
    return;
  }

  set((state) => {
    const conversationId = getChildConversationId(sessionId);
    // [AutoC 2026-06-04] Why: hydrateStructuredHistory writes sessionId into
    // conversationIdsBySession, which causes WS events for child sessions to be
    // routed into the sidebar via syncConversationsAfterEvent, creating ghost
    // entries with empty titles. How: hydrate into a temporary copy, extract only
    // the message data we need (messagesById, messageOrderByConversation,
    // toolExecutionsById), and discard the polluted conversationIdsBySession.
    // Purpose: virtual child session view never contaminates the sidebar.
    const hydrated = hydrateStructuredHistory(state, sessionId, conversationId, history, false);
    const messages = selectOrderedMessagesFromState(hydrated, conversationId);
    return {
      // Only take message-level state, NOT conversationIdsBySession or conversations
      messagesById: hydrated.messagesById,
      messageOrderByConversation: hydrated.messageOrderByConversation,
      toolExecutionsById: hydrated.toolExecutionsById,
      childSessionMessages: {
        ...state.childSessionMessages,
        [sessionId]: messages,
      },
    };
  });
}

async function loadStartupSessions(set: StoreSetter, get: StoreGetter) {
  if (startupLoaded) return;
  startupLoaded = true;

  const serverSessions = await listSessions('web', 50);
  const userSessions = (serverSessions || []).filter((session) => !isEntryBranchSessionId(session.session_id));
  if (userSessions.length === 0) {
    startGlobalWebSocket(set, get);
    return;
  }

  const titleCache = loadTitleCache();
  const seenConversationIds = new Set<string>();
  const conversations = userSessions.flatMap((session): ConversationMeta[] => {
    const conversationId = normalizeConversationKey(session.conversation_key) || session.session_id;
    if (seenConversationIds.has(conversationId)) {
      // [2026-06-03] Why: a bad runtime list can contain both parent and internal
      // rows for the same web conversation. How: keep the first sorted row only.
      // Purpose: the sidebar cannot render duplicate conversations for one session.
      return [];
    }
    seenConversationIds.add(conversationId);
    return [{
      id: conversationId,
      sessionId: session.session_id,
      // [2026-06-02] Why: listSessions cannot return frontend-only generated
      // titles. How: prefer the browser cache before falling back to session ids.
      // Purpose: refresh keeps the user's readable sidebar title.
      title: titleCache[conversationId] || titleFromSession(session.conversation_key, session.session_id),
      updatedAt: session.updated_at || session.created_at || new Date().toISOString(),
    }];
  });

  // [2026-06-03] Defensive title re-save: after building the conversations list
  // from titleCache + server sessions, write the cache back. This ensures any
  // titles that were restored from cache survive even if a subsequent WS event
  // temporarily replaces the conversation list before user interaction.
  const restoredCache = loadTitleCache();
  let cacheUpdated = false;
  for (const conv of conversations) {
    if (conv.title && conv.title !== '新对话' && conv.title !== 'New conversation' && restoredCache[conv.id] !== conv.title) {
      restoredCache[conv.id] = conv.title;
      cacheUpdated = true;
    }
  }
  if (cacheUpdated) saveTitleCache(restoredCache);

  // [2026-06-03] Sort once on initial load so the sidebar opens with the most
  // recently updated conversations on top, regardless of backend list order.
  const sortedConversations = sortConversationsByRecency(conversations);
  set((state) => ({
    conversations: sortedConversations,
    activeConversationId: state.activeConversationId || sortedConversations[0]?.id || null,
    conversationIdsBySession: sortedConversations.reduce<Record<string, string>>((acc, conversation) => {
      if (conversation.sessionId) acc[conversation.sessionId] = conversation.id;
      return acc;
    }, { ...state.conversationIdsBySession }),
  }));

  // [2026-06-03] Why: realtime is now an all-session subscription that should
  // exist as soon as the web app starts. How: open /v1/ws after session metadata
  // has seeded session routing. Purpose: existing and future sessions can receive
  // events without waiting for a selected task.
  startGlobalWebSocket(set, get);

  const first = sortedConversations[0];
  if (first?.sessionId) {
    await loadSessionHistoryIntoStore(first.id, first.sessionId, set, get);
  }
}

export const useChatStore = create<ChatStoreState>((set, get) => ({
  ...createInitialChatState(),
  ...createStoreBase(),

  resetState: () => {
    stopGlobalRealtimeConnection();
    startupLoaded = false;
    autoApprovedApprovalIds.clear();
    saveAutoApproved(autoApprovedApprovalIds);
    set({
      ...createInitialChatState(),
      ...createStoreBase(),
    });
  },

  selectConversation: (id) => {
    const target = get().conversations.find((conversation) => conversation.id === id);
    set((state) => ({
      // [2026-06-03] Why: multiple sessions can generate concurrently, so the
      // composer lock must follow the selected session rather than a single global
      // active task. How: recompute isGenerating from generatingBySession when the
      // user switches chats. Purpose: switching away from a running session does not
      // incorrectly disable another conversation.
      activeConversationId: id,
      viewingChildSessionId: null,
      isGenerating: target?.sessionId ? Boolean(state.generatingBySession[target.sessionId]) : false,
    }));
    if (target?.sessionId) {
      // [2026-06-03] Why: selecting a conversation is a view action, not a
      // transport lifecycle action. How: load only the selected session history here
      // and leave the long-lived global WebSocket to startup, sendMessage, and reset.
      // Purpose: switching chats cannot accidentally create, replace, or recover a
      // realtime connection outside the global connection lifecycle.
      void loadSessionHistoryIntoStore(target.id, target.sessionId, set, get);
    }
  },

  selectChildNodes: (conversationId) => {
    // [2026-06-03] Why: components should not know that childNodes is keyed by
    // session id. How: expose a store selector that delegates to the shared helper.
    // Purpose: later UI work can read grouped child state without duplicating logic.
    return selectChildNodesFromState(get(), conversationId);
  },

  selectHasActiveChildNodes: (conversationId) => {
    // [2026-06-03] Why: active-child checks will be used by badges and controls.
    // How: expose a boolean selector over normalized child statuses. Purpose:
    // consumers do not hard-code running or awaiting_approval status names.
    return selectHasActiveChildNodesFromState(get(), conversationId);
  },

  viewChildSession: (sessionId, taskId) => {
    const trimmed = sessionId.trim();
    if (!trimmed) return;
    // [AutoC 2026-06-04] Why: this is a virtual temporary session overlay — it must
    // not pollute the sidebar conversation list or leave stale state from a previous
    // virtual session. How: immediately set the new viewingChildSessionId and clear
    // any cached messages for the new target so the UI shows a loading state while
    // history is fetched. Purpose: switching between tasks in the monitor modal
    // correctly replaces the chat stream.
    set((state) => ({
      viewingChildSessionId: trimmed,
      childSessionMessages: {
        ...state.childSessionMessages,
        [trimmed]: [],  // clear target to avoid showing stale data
      },
    }));
    void loadChildSessionHistoryIntoStore(trimmed, set, taskId?.trim() || undefined);
  },

  exitChildSession: () => {
    // [2026-06-03] Why: returning from child view should reveal the still-selected
    // parent conversation. How: clear only the view marker and leave message caches in
    // place. Purpose: reopening the same child can reuse cached messages while history
    // refreshes in the background on the next view action.
    set({ viewingChildSessionId: null });
  },

  createConversation: () => {
    const conversation = createConversationMeta();
    set((state) => ({
      conversations: [conversation, ...state.conversations],
      activeConversationId: conversation.id,
      viewingChildSessionId: null,
    }));
    return conversation.id;
  },

  deleteConversation: (id) => {
    const conversation = get().conversations.find((item) => item.id === id);
    if (conversation?.sessionId) {
      void deleteSession(conversation.sessionId).catch(() => undefined);
    }

    set((state) => {
      const conversations = state.conversations.filter((item) => item.id !== id);
      const activeConversationId = state.activeConversationId === id ? conversations[0]?.id || null : state.activeConversationId;
      const nextChatState = removeConversationMessages(state, id);
      const conversationIdsBySession = Object.fromEntries(
        // [2026-06-03] Why: child and branch sessions can be registered to the same
        // parent conversation. How: remove every session route whose value is the
        // deleted conversation id. Purpose: later global events cannot revive a
        // deleted parent conversation through stale child-session mappings.
        Object.entries(nextChatState.conversationIdsBySession).filter(([, conversationId]) => conversationId !== id),
      );
      const childNodes = Object.fromEntries(
        // [2026-06-03] Why: childNodes is keyed independently from conversations.
        // How: drop entries grouped under the deleted parent conversation. Purpose:
        // selectors cannot report stale scout/smith work after a chat is removed.
        Object.entries(state.childNodes).filter(([, child]) => child.parentConversationId !== id),
      );

      const generatingBySession = conversation?.sessionId
        ? { ...state.generatingBySession, [conversation.sessionId]: false }
        : state.generatingBySession;

      return {
        ...nextChatState,
        conversationIdsBySession,
        conversations,
        activeConversationId,
        viewingChildSessionId: state.viewingChildSessionId && childNodes[state.viewingChildSessionId]
          ? state.viewingChildSessionId
          : null,
        generatingBySession,
        childNodes,
        // [2026-06-03] Why: generation is tracked per session now. How: after a
        // deletion chooses a new active conversation, derive the composer lock from
        // that conversation's session flag. Purpose: deleting a running or old chat
        // cannot leave the next selected chat incorrectly disabled.
        isGenerating: isConversationGenerating(conversations, activeConversationId, generatingBySession, false),
      };
    });
  },

  renameConversation: (id, newTitle) => {
    const trimmed = newTitle.trim();
    if (!trimmed) return;
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, title: trimmed, updatedAt: new Date().toISOString() } : c,
      ),
    }));
    // Persist to localStorage
    const cache = loadTitleCache();
    cache[id] = trimmed;
    saveTitleCache(cache);
  },

  sendMessage: async (text, attachments, entryNodeId) => {
    const trimmed = text.trim();
    if (!trimmed && (!attachments || attachments.length === 0)) return;

    const state = get();
    const conversationId = state.activeConversationId || state.createConversation();
    const conversationKey = `web:${conversationId}`;
    const existingConversation = get().conversations.find((conversation) => conversation.id === conversationId);

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId: existingConversation?.sessionId || '',
        title: (existingConversation?.title === '新对话' || existingConversation?.title === 'New conversation') && trimmed
          ? getInitialTitleFromClientPrefs(trimmed, existingConversation?.title)
          : existingConversation?.title,
        updatedAt: new Date().toISOString(),
      }),
      activeConversationId: conversationId,
      isGenerating: true,
      connectionStatus: current.connectionStatus === 'open' ? 'open' : 'connecting',
    }));

    let sessionId = '';
    try {
      // Upload files that have a raw File object, then collect server paths
      let uploadedAttachments: { name: string; size: number; type?: string; path?: string; mime_type?: string }[] | undefined;
      if (attachments && attachments.length > 0) {
        const uploaded = await Promise.all(
          attachments.map(async (attachment: Attachment) => {
            if (attachment.file) {
              const result = await uploadAttachment(attachment.file, conversationKey, useSettingsStore.getState().adminToken || '');
              return {
                name: result.name,
                size: result.size,
                type: result.type,
                path: result.path,
                mime_type: result.mime_type,
              };
            }
            return {
              name: attachment.name,
              size: attachment.size ?? 0,
              type: attachment.type,
              path: attachment.path,
              mime_type: attachment.mime_type,
            };
          }),
        );
        uploadedAttachments = uploaded;
      }

      const result = await postInbound({
        conversation_key: conversationKey,
        text: trimmed,
        attachments: uploadedAttachments,
        use_context: true,
        entry_node_id: entryNodeId,
      });
      sessionId = result.session_id;
      // [2026-06-03] Optimistic user message injection. Why: the global WS may
      // take a moment to deliver the inbound_message event, leaving the chat
      // visually empty after the user presses send. How: construct a WsMessage
      // with the same ID format that applyInboundMessage would use, then upsert
      // it into the store immediately. Purpose: when the real inbound_message
      // arrives via WS, the existing-branch merge (L201-209 in eventReducer)
      // will update the message in place without duplication.
      const inboundSeq = result.inbound_seq;
      if (inboundSeq != null) {
        const optimisticId = `message:${conversationId}:user:inbound:${inboundSeq}`;
        const now = new Date().toISOString();
        set((current) => {
          // Skip if reducer already processed the real event
          if (current.messagesById[optimisticId]) return current;
          return {
            ...current,
            messagesById: {
              ...current.messagesById,
              [optimisticId]: {
                id: optimisticId,
                conversationId,
                sessionId,
                role: 'user' as const,
                status: 'completed' as const,
                createdAt: now,
                updatedAt: now,
                source: { inboundSeq },
                blocks: [{
                  id: `${optimisticId}|block:text:optimistic`,
                  kind: 'text' as const,
                  text: trimmed,
                  delivery: 'final' as const,
                  streaming: false,
                  createdAt: now,
                  updatedAt: now,
                  eventIds: [],
                }],
                attachments: (uploadedAttachments || []).map(a => ({ name: a.name, url: a.path || '' })),
                eventIds: [],
              },
            },
            messageOrderByConversation: {
              ...current.messageOrderByConversation,
              [conversationId]: [
                ...(current.messageOrderByConversation[conversationId] || []),
                optimisticId,
              ],
            },
            userMessageByInboundSeq: {
              ...current.userMessageByInboundSeq,
              [String(inboundSeq)]: optimisticId,
            },
          };
        });
      }
    } catch (error) {
      // [2026-06-03] Why: an inbound HTTP failure is not proof that the global
      // WebSocket is closed. How: clear only the optimistic generating flag and keep
      // an already-open realtime status intact. Purpose: one failed send cannot make
      // the all-session event stream look disconnected.
      set((current) => ({
        isGenerating: false,
        connectionStatus: current.connectionStatus === 'open' ? 'open' : 'closed',
      }));
      return;
    }

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
      conversationIdsBySession: {
        ...current.conversationIdsBySession,
        [sessionId]: conversationId,
      },
      lastSeqBySession: {
        ...current.lastSeqBySession,
        [sessionId]: current.lastSeqBySession[sessionId] || 0,
      },
      generatingBySession: {
        ...current.generatingBySession,
        [sessionId]: true,
      },
    }));

    // [2026-06-03] Why: sending a message should not replace another session's
    // realtime listener. How: keep the existing global /v1/ws connection or open it
    // if this is the first message in the tab. Purpose: multiple sessions can run
    // concurrently through one event stream.
    startGlobalWebSocket(set, get);
  },

  cancelCurrentTask: async () => {
    const activeConversation = getActiveConversation(get());
    if (!activeConversation?.sessionId) return;

    try {
      await cancelActiveTasks(activeConversation.sessionId);
    } catch {
      // Why: cancel is a best-effort transport action. How: ignore API failures here
      // and still clear the local realtime state. Purpose: the composer should not
      // remain locked if the cancel endpoint races with task completion.
    }

    // [2026-06-03] Why: cancellation should not manage the global WebSocket and
    // should unlock the active composer immediately. How: clear only the active
    // session's local generating flags after the cancel API returns or races with
    // completion. Purpose: resetState remains the only normal path that disconnects
    // realtime transport, while cancel stays a task action.
    set((state) => ({
      isGenerating: false,
      generatingBySession: {
        ...state.generatingBySession,
        [activeConversation.sessionId]: false,
      },
    }));
  },

  loadStartup: () => {
    void loadStartupSessions(set, get);
  },
}));

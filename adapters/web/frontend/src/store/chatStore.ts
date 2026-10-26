// Chat store: sidebar conversations, the global realtime stream, and history sync.
// Every SupervisorEvent goes through eventReducer; /history pages are merged by historyHydration.
import { create } from 'zustand';
import { commandAction } from '../plugins/commands';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { submitApprovalDecision } from '../hooks/useApprovalDecision';

import { connectGlobalWS, disconnectGlobalWS } from '../api';
import { errorMessage, isNotFound } from '../api/request';
import { RequestScope, type RequestTicket } from '../api/requestGuard';
import {
  cancelActiveTasks,
  deleteSession,
  getAdminState,
  getSessionChildren,
  getSessionHistory,
  getSessionRunningTasks,
  listSessions,
  postInbound,
  uploadAttachment,
  type AdminApproval,
  type ChildSessionInfo,
  type SessionListItem,
  type SessionRunningTask,
  type StructuredMessage,
} from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';
import type { Attachment, ChatState, WsMessage } from '../types/message';
import { shouldAutoApproveToolCall, useClientPrefsStore, type ApprovalMatch } from './clientPrefsStore';
import { useSettingsStore } from './settingsStore';
import { scopedKey } from './storageKey';
import { createInitialChatState, findToolByCallId, reduceChatEvent } from './eventReducer';
import {
  buildHistoryEntries,
  historyRowKey,
  mergeHistoryEntries,
  mergeLatestHistoryPage,
  mergeOlderHistoryPage,
  type HistoryWindow,
} from './historyHydration';

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

/** History of one view: a parent conversation, or `child:<sessionId>` while a child session is open. */
export interface HistoryView {
  loading: boolean;
  error: string;
  hasMore: boolean;
  loadingOlder: boolean;
  olderError: string;
}

export interface SessionPaging {
  hasMore: boolean;
  loading: boolean;
  error: string;
}

export interface ChatStoreState extends ChatState {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  isGenerating: boolean;
  connectionStatus: ConnectionStatus;
  startupError: string;
  generatingBySession: Readonly<Record<string, boolean>>;
  sendingByConversation: Readonly<Record<string, boolean>>;
  deletingByConversation: Readonly<Record<string, boolean>>;
  childNodes: Readonly<Record<string, ChildNodeState>>;
  viewingChildSessionId: string | null;
  childSessionMessages: Readonly<Record<string, WsMessage[]>>;
  taskActivities: Readonly<Record<string, TaskActivity>>;
  historyByConversation: Readonly<Record<string, HistoryView>>;
  sessionPaging: SessionPaging;

  selectConversation: (id: string) => void;
  selectChildNodes: (conversationId: string) => ChildNodeState[];
  selectHasActiveChildNodes: (conversationId: string) => boolean;
  createConversation: () => string;
  deleteConversation: (id: string) => Promise<void>;
  resetConversationView: (id: string, previousSessionId: string) => void;
  renameConversation: (id: string, newTitle: string) => void;
  sendMessage: (text: string, attachments?: any[], entryNodeId?: string) => Promise<void>;
  cancelCurrentTask: () => Promise<void>;
  resetState: () => void;
  viewChildSession: (sessionId: string, taskId?: string) => void;
  exitChildSession: () => void;
  loadStartup: () => void;
  loadMoreConversations: () => Promise<void>;
  /** Pages older history into the open parent conversation. */
  loadOlderHistory: () => Promise<void>;
  /** Reloads whatever history is on screen: the open conversation or the open child session. */
  retryHistory: () => void;
}

type StoreSetter = (
  partial:
    | ChatStoreState
    | Partial<ChatStoreState>
    | ((state: ChatStoreState) => ChatStoreState | Partial<ChatStoreState>),
) => void;
type StoreGetter = () => ChatStoreState;

type HistoryCache = {
  sessionId: string;
  window: HistoryWindow;
  anchors: ReadonlyMap<string, string>;
};

const HISTORY_PAGE_SIZE = 200;
const SESSION_PAGE_SIZE = 50;
// Stream deltas arrive dozens per second; an unchanged phase is re-stamped at most once per interval.
const TASK_ACTIVITY_REFRESH_MS = 1000;
const IDLE_HISTORY: HistoryView = { loading: false, error: '', hasMore: false, loadingOlder: false, olderError: '' };
const TERMINAL_TASK_EVENTS = new Set(['task_completed', 'task_cancelled', 'task_failed']);
// Only these move a conversation's sidebar timestamp; stream deltas would re-render the sidebar every frame.
const SIDEBAR_ACTIVITY_EVENTS = new Set(['inbound_message', 'outbound_message', 'intermediate_reply', ...TERMINAL_TASK_EVENTS]);
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
// Session-list reads and deletes belong to one startup; resetState voids whatever is still in flight.
const startupRequests = new RequestScope();
let sessionOffset = 0;
const autoApprovedApprovalIds = loadAutoApproved();
// Outside zustand on purpose: they change with every page or frame and nothing renders them.
const historyCaches = new Map<string, HistoryCache>();
// One lane per view: a newer request for the same view aborts the older one; only the newest response may land.
const historyRequests = new RequestScope();
// Bumped per view whenever a live event changes its messages / its task lifecycle.
const realtimeRevisions = new Map<string, number>();
const lifecycleRevisions = new Map<string, number>();
// Conversations whose live cards were kept over history; they get one more sync once their task ends.
const unsettledConversations = new Set<string>();
const childViewTaskIds = new Map<string, string | undefined>();

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

function forgetHistory(viewKey: string) {
  historyRequests.drop(viewKey);
  historyCaches.delete(viewKey);
  realtimeRevisions.delete(viewKey);
  lifecycleRevisions.delete(viewKey);
  unsettledConversations.delete(viewKey);
}

// Only a full reset closes the stream; switching conversations or finishing a task keeps it open.
function stopRealtimeAndHistory() {
  disconnectGlobalWS();
  historyRequests.reset();
  historyCaches.clear();
  realtimeRevisions.clear();
  lifecycleRevisions.clear();
  unsettledConversations.clear();
  childViewTaskIds.clear();
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

type StoreBaseKeys = 'conversations' | 'activeConversationId' | 'isGenerating' | 'connectionStatus' | 'startupError'
  | 'generatingBySession' | 'sendingByConversation' | 'deletingByConversation' | 'childNodes' | 'viewingChildSessionId'
  | 'childSessionMessages' | 'taskActivities' | 'historyByConversation' | 'sessionPaging';

function createStoreBase(): Pick<ChatStoreState, StoreBaseKeys> {
  return {
    conversations: [],
    activeConversationId: null,
    isGenerating: false,
    connectionStatus: 'idle',
    startupError: '',
    generatingBySession: {},
    sendingByConversation: {},
    deletingByConversation: {},
    childNodes: {},
    viewingChildSessionId: null,
    childSessionMessages: {},
    taskActivities: {},
    historyByConversation: {},
    sessionPaging: { hasMore: false, loading: false, error: '' },
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

function sameChildNode(left: ChildNodeState, right: ChildNodeState): boolean {
  return left.nodeId === right.nodeId
    && left.parentConversationId === right.parentConversationId
    && left.status === right.status
    && left.taskId === right.taskId
    && left.startedAt === right.startedAt
    && left.completedAt === right.completedAt;
}

function mergeChildNodesFromSessionChildren(
  state: ChatStoreState,
  conversationId: string,
  children: readonly ChildSessionInfo[],
): Readonly<Record<string, ChildNodeState>> {
  // A start time already seen live is more precise than the registry's.
  let next: Record<string, ChildNodeState> | null = null;
  for (const child of children) {
    const sessionId = getStringValue(child.session_id);
    if (!sessionId) continue;
    const previous = (next || state.childNodes)[sessionId];
    const merged: ChildNodeState = {
      sessionId,
      nodeId: getStringValue(child.node_id) || previous?.nodeId || 'unknown',
      parentConversationId: conversationId,
      status: normalizeChildNodeStatus(getStringValue(child.status)),
      taskId: getStringValue(child.task_id) || previous?.taskId,
      startedAt: previous?.startedAt || getStringValue(child.started_at) || getStringValue(child.updated_at),
      completedAt: getStringValue(child.completed_at) || previous?.completedAt,
    };
    if (previous && sameChildNode(previous, merged)) continue;
    next = next || { ...state.childNodes };
    next[sessionId] = merged;
  }
  return next || state.childNodes;
}

function getLastEventConversationId(state: ChatState, event: SupervisorEvent, fallbackConversationId: string): string {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  // The reducer appends the row for this event last; scanning the whole log costs O(3000) per frame.
  const last = state.eventLog[state.eventLog.length - 1];
  const logEntry = last?.eventId === eventId ? last : state.eventLog.find((entry) => entry.eventId === eventId);
  return logEntry?.conversationId || fallbackConversationId;
}

function isDefaultTitle(title: string | undefined): boolean {
  return title === '新对话' || title === 'New conversation';
}

function syncConversationsAfterEvent(
  conversations: ConversationMeta[],
  nextChatState: ChatState,
  event: SupervisorEvent,
  fallbackConversationId: string,
): ConversationMeta[] {
  const conversationId = getLastEventConversationId(nextChatState, event, fallbackConversationId);
  const existing = conversations.find((conversation) => conversation.id === conversationId);
  // Branch and child sessions reduce under their own ids; only known rows (or the routed one) reach the sidebar.
  if (!existing && conversationId !== fallbackConversationId) return conversations;

  const payload = event.payload || {};
  const inboundText = event.type === 'inbound_message' && typeof payload.text === 'string' ? payload.text : '';
  const title = inboundText && (!existing || isDefaultTitle(existing.title))
    ? getInitialTitleFromClientPrefs(inboundText, existing?.title)
    : undefined;
  // A branch session must not replace the parent session the sidebar loads history from.
  const sessionId = existing?.sessionId && existing.sessionId !== event.session_id ? existing.sessionId : event.session_id;
  if (existing
    && !SIDEBAR_ACTIVITY_EVENTS.has(event.type)
    && sessionId === existing.sessionId
    && (title === undefined || title === existing.title)) {
    return conversations;
  }

  return upsertConversationMeta(conversations, {
    id: conversationId,
    sessionId,
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

  // The payload only carries the policy operation, and several tools share one: list_dir,
  // glob and search_in_files all report read_file, apply_diff reports write_file. Resolving the
  // ToolExecution is the only way to learn which tool the user actually ticked; an approval
  // with no matching tool card stays manual.
  return { toolName: findToolByCallId(state, toolCallId)?.name || '', operation };
}

function maybeAutoApproveApprovalRequest(event: SupervisorEvent, get: StoreGetter) {
  if (event.type !== 'approval_requested') return;
  const token = useSettingsStore.getState().adminToken;
  // Without a token the decision is a guaranteed 401 that also counts toward the auth-failure backoff.
  if (!token) return;
  const payload = event.payload || {};
  const approvalId = typeof payload.approval_id === 'string' ? payload.approval_id : '';
  if (!approvalId || autoApprovedApprovalIds.has(approvalId)) return;

  const state = get();
  const prefs = useClientPrefsStore.getState();
  if (!shouldAutoApproveToolCall(getApprovalMatch(state, event), prefs.autoApproveTools)) return;

  autoApprovedApprovalIds.add(approvalId);
  saveAutoApproved(autoApprovedApprovalIds);
  // A failed submission leaves the card pending so the user can still decide by hand.
  void submitApprovalDecision(approvalId, 'allow', 'auto-approved by client preference', token).catch(() => {
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

/** Returns `current` itself when nothing visible changes, so subscribers are not notified. */
function updateTaskActivitiesByEvent(
  current: Readonly<Record<string, TaskActivity>>,
  event: SupervisorEvent,
): Readonly<Record<string, TaskActivity>> {
  const payload = getEventPayload(event);
  const keys = getTaskActivityKeys(event, payload);
  if (keys.length === 0) return current;

  const isTerminal = isTerminalTaskEvent(event);
  const phase = getTaskActivityPhase(event, payload);
  if (!isTerminal && !phase) return current;

  if (isTerminal && getStringValue(payload.task_id)) {
    if (!keys.some((key) => key in current)) return current;
    const next = { ...current };
    for (const key of keys) delete next[key];
    return next;
  }

  const activity: TaskActivity = {
    phase: isTerminal ? 'idle' : phase || 'idle',
    detail: isTerminal ? '' : getTaskActivityDetail(event, payload),
    lastEventAt: getTaskActivityTimestamp(event),
  };
  const unchanged = keys.every((key) => {
    const previous = current[key];
    return previous
      && previous.phase === activity.phase
      && previous.detail === activity.detail
      && activity.lastEventAt - previous.lastEventAt < TASK_ACTIVITY_REFRESH_MS;
  });
  if (unchanged) return current;
  const next = { ...current };
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
  // [2026-06-03] Why: /v1/ws emits every Supervisor session, including QQ and
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
  // Only events routed from an agent:* key to this web parent through structured metadata count.
  const payload = getEventPayload(event);
  if (!isAgentEventRoutedToConversation(state, payload, conversationId)) return state.childNodes;

  const status = CHILD_NODE_STATUS_BY_EVENT[event.type];
  if (!status) return state.childNodes;

  const sessionId = getChildNodeSessionId(event, payload);
  if (!sessionId) return state.childNodes;

  const previous = state.childNodes[sessionId];
  const isTerminal = status === 'completed' || status === 'failed' || status === 'cancelled';
  const next: ChildNodeState = {
    sessionId,
    nodeId: getChildNodeId(payload, previous),
    parentConversationId: conversationId,
    status,
    taskId: getStringValue(payload.task_id) || previous?.taskId,
    startedAt: previous?.startedAt || event.ts,
    completedAt: isTerminal ? event.ts : previous?.completedAt,
  };
  if (previous && sameChildNode(previous, next)) return state.childNodes;
  return { ...state.childNodes, [sessionId]: next };
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
): Readonly<Record<string, boolean>> {
  const shouldMarkActive = ACTIVE_TASK_EVENTS.has(event.type);
  const shouldMarkDone = isTerminalTaskEvent(event);
  if (!shouldMarkActive && !shouldMarkDone) return state.generatingBySession;

  const value = shouldMarkActive && !shouldMarkDone;
  let next: Record<string, boolean> | null = null;
  for (const sessionId of getAffectedSessionIds(state, event, conversationId)) {
    if (Boolean(state.generatingBySession[sessionId]) === value) continue;
    next = next || { ...state.generatingBySession };
    next[sessionId] = value;
  }
  return next || state.generatingBySession;
}

function isSessionGenerating(state: Pick<ChatStoreState, 'generatingBySession' | 'sendingByConversation'>, conversation: ConversationMeta): boolean {
  return Boolean(state.sendingByConversation[conversation.id] || (conversation.sessionId && state.generatingBySession[conversation.sessionId]));
}

function isConversationGenerating(
  conversations: readonly ConversationMeta[],
  activeConversationId: string | null,
  generatingBySession: Readonly<Record<string, boolean>>,
  fallback: boolean,
  sendingByConversation: Readonly<Record<string, boolean>> = {},
): boolean {
  if (activeConversationId && sendingByConversation[activeConversationId]) return true;
  const active = activeConversationId ? conversations.find((conversation) => conversation.id === activeConversationId) : undefined;
  return active?.sessionId ? Boolean(generatingBySession[active.sessionId]) : fallback;
}

function bump(revisions: Map<string, number>, viewKey: string) {
  revisions.set(viewKey, (revisions.get(viewKey) || 0) + 1);
}

function bumpRealtimeRevision(viewKey: string) {
  bump(realtimeRevisions, viewKey);
}

function withChildMessages(
  current: Readonly<Record<string, WsMessage[]>>,
  childSessionId: string,
  messages: WsMessage[],
): Readonly<Record<string, WsMessage[]>> {
  const previous = current[childSessionId];
  if (previous && previous.length === messages.length && previous.every((message, index) => message === messages[index])) return current;
  return { ...current, [childSessionId]: messages };
}

// Child-agent events never render in the parent chat: the parent keeps an audit row, and the
// open child view (if it is this child) gets a replay under its own child:* conversation.
function applyAgentChildEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  payload: Record<string, unknown>,
  conversationId: string,
): ChatStoreState {
  const logged = appendAgentRouteEventLog(state, event, conversationId);
  const childSessionId = getChildNodeSessionId(event, payload);
  if (!childSessionId || state.viewingChildSessionId !== childSessionId) return logged;
  const childConversationId = getChildConversationId(childSessionId);
  const reduced = reduceChatEvent(logged, createReducerEventForChildSession(event, payload, childSessionId));
  if (reduced === logged) return logged;
  bumpRealtimeRevision(childConversationId);
  return {
    ...(reduced as ChatStoreState),
    // The replay maps the runtime session to child:*; parent routing keeps its own table.
    conversationIdsBySession: logged.conversationIdsBySession,
    childSessionMessages: withChildMessages(logged.childSessionMessages, childSessionId, selectOrderedMessagesFromState(reduced, childConversationId)),
  };
}

function applyRealtimeEvent(event: SupervisorEvent, set: StoreSetter, get: StoreGetter) {
  if (event.type === 'execution_updated') {
    window.dispatchEvent(new CustomEvent('clonoth:execution-updated', { detail: event.payload }));
  }
  let finishedConversationId = '';

  // Returning the untouched state skips the store notification entirely.
  set((state) => {
    const taskActivities = updateTaskActivitiesByEvent(state.taskActivities, event);
    const base = taskActivities === state.taskActivities ? state : { ...state, taskActivities };
    const conversationId = resolveEventConversationId(base, event);
    if (!conversationId) return base;

    const payload = getEventPayload(event);
    const routed = seedConversationRouteForEvent(base, event, conversationId);
    if (isAgentEventRoutedToConversation(routed, payload, conversationId)) {
      const childNodes = updateChildNodesByEvent(routed, event, conversationId);
      return applyAgentChildEvent(childNodes === routed.childNodes ? routed : { ...routed, childNodes }, event, payload, conversationId);
    }

    const reduced = reduceChatEvent(routed, event) as ChatStoreState;
    if (reduced === routed) return routed;
    bumpRealtimeRevision(conversationId);
    if (ACTIVE_TASK_EVENTS.has(event.type) || isTerminalTaskEvent(event)) bump(lifecycleRevisions, conversationId);
    const conversations = syncConversationsAfterEvent(state.conversations, reduced, event, conversationId);
    const generatingBySession = updateGeneratingByEvent({ ...state, conversations }, event, conversationId);
    if (isTerminalTaskEvent(event)) finishedConversationId = conversationId;
    return {
      ...reduced,
      conversations,
      generatingBySession,
      isGenerating: isConversationGenerating(conversations, state.activeConversationId, generatingBySession, state.isGenerating, state.sendingByConversation),
    };
  });

  maybeAutoApproveApprovalRequest(event, get);
  if (finishedConversationId) afterTaskFinished(finishedConversationId, set, get);
}

function afterTaskFinished(conversationId: string, set: StoreSetter, get: StoreGetter) {
  set((state) => {
    const sorted = sortConversationsByRecency(state.conversations);
    return sorted.every((conversation, index) => conversation === state.conversations[index]) ? state : { conversations: sorted };
  });
  if (!unsettledConversations.has(conversationId)) return;
  const state = get();
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (conversation && !isSessionGenerating(state, conversation)) void syncConversation(conversationId, set, get, true);
}

function startGlobalWebSocket(set: StoreSetter, get: StoreGetter) {
  if (get().connectionStatus !== 'open') set({ connectionStatus: 'connecting' });
  connectGlobalWS({
    onEvent: (event) => applyRealtimeEvent(event, set, get),
    onOpen: ({ reconnected }) => {
      set({ connectionStatus: 'open' });
      // The stream only carries events from now on; anything that happened while it was down is fetched again.
      if (reconnected) void resyncAfterReconnect(set, get);
    },
    onClose: () => set({ connectionStatus: 'reconnecting' }),
  });
}

async function resyncAfterReconnect(set: StoreSetter, get: StoreGetter) {
  const state = get();
  const targets = new Set<string>();
  if (state.activeConversationId) targets.add(state.activeConversationId);
  for (const conversation of state.conversations) {
    if (isSessionGenerating(state, conversation) || selectHasActiveChildNodesFromState(state, conversation.id)) targets.add(conversation.id);
  }
  const childSessionId = state.viewingChildSessionId;
  await Promise.all([
    ...[...targets].map((conversationId) => syncConversation(conversationId, set, get)),
    ...(childSessionId ? [loadChildHistory(childSessionId, set, get)] : []),
  ]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function removeConversationMessages<S extends ChatState>(state: S, conversationId: string): S {
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

function withHistoryView(
  views: Readonly<Record<string, HistoryView>>,
  viewKey: string,
  patch: Partial<HistoryView>,
): Readonly<Record<string, HistoryView>> {
  const current = views[viewKey];
  const next = { ...(current || IDLE_HISTORY), ...patch };
  if (current && (Object.keys(next) as Array<keyof HistoryView>).every((key) => next[key] === current[key])) return views;
  return { ...views, [viewKey]: next };
}

function patchHistoryView(set: StoreSetter, viewKey: string, patch: Partial<HistoryView>) {
  set((state) => {
    const historyByConversation = withHistoryView(state.historyByConversation, viewKey, patch);
    return historyByConversation === state.historyByConversation ? state : { historyByConversation };
  });
}

function withoutHistoryView(views: Readonly<Record<string, HistoryView>>, viewKey: string): Readonly<Record<string, HistoryView>> {
  if (!(viewKey in views)) return views;
  const next = { ...views };
  delete next[viewKey];
  return next;
}

function applyHistoryPage<S extends ChatState>(
  state: S,
  viewKey: string,
  sessionId: string,
  page: readonly StructuredMessage[],
  kind: 'latest' | 'older',
  keepLive: boolean,
): { chat: S; hasMore: boolean } {
  const previous = historyCaches.get(viewKey);
  const cached = previous?.sessionId === sessionId ? previous : undefined;
  let window: HistoryWindow;
  let freshRowIds: Set<string>;
  if (kind === 'older' && cached) {
    window = mergeOlderHistoryPage(cached.window, page);
    // Older rows predate everything live, so none of them can be a live message's persisted copy.
    freshRowIds = new Set();
  } else {
    window = mergeLatestHistoryPage(cached?.window, page);
    const known = new Set(cached?.window.rows.map(historyRowKey) || []);
    freshRowIds = new Set(page.map(historyRowKey).filter((key) => key && !known.has(key)));
  }
  const merged = mergeHistoryEntries(state, viewKey, buildHistoryEntries(window.rows, sessionId, viewKey), {
    keepLive: keepLive || kind === 'older',
    freshRowIds,
    anchors: cached?.anchors || new Map(),
  });
  historyCaches.set(viewKey, { sessionId, window, anchors: merged.anchors });
  return { chat: merged.state, hasMore: window.hasMore };
}

function seedBranchRoutes(
  routes: Readonly<Record<string, string>>,
  conversationId: string,
  tasks: readonly SessionRunningTask[],
): Readonly<Record<string, string>> {
  let next: Record<string, string> | null = null;
  for (const task of tasks) {
    const branchSessionId = task.branch_session_id;
    if (!branchSessionId || routes[branchSessionId] === conversationId) continue;
    next = next || { ...routes };
    next[branchSessionId] = conversationId;
  }
  return next || routes;
}

function syntheticApprovalEvent(approval: AdminApproval, conversationId: string): SupervisorEvent {
  return {
    seq: 0,
    event_id: `approval-sync:${approval.approval_id}`,
    ts: approval.requested_at || new Date().toISOString(),
    session_id: approval.session_id || '',
    type: 'approval_requested',
    component: 'supervisor',
    payload: { ...approval, conversation_key: `web:${conversationId}` },
  };
}

// Approvals live only in the event stream; one requested while the stream was down never reached this page.
function missedApprovals(
  state: ChatStoreState,
  approvals: readonly AdminApproval[],
  conversationId: string,
  sessionIds: ReadonlySet<string>,
): AdminApproval[] {
  return approvals.filter((approval) => {
    const sessionId = approval.session_id || '';
    if (approval.status && approval.status !== 'pending') return false;
    if (state.approvalBlockById[approval.approval_id]) return false;
    return sessionIds.has(sessionId) || state.conversationIdsBySession[sessionId] === conversationId;
  });
}

/**
 * Re-reads one conversation from the server: history, child sessions, running tasks and
 * pending approvals. Live messages are kept when events arrived during the request or a task
 * is still running; otherwise the history replaces them.
 */
async function syncConversation(conversationId: string, set: StoreSetter, get: StoreGetter, settling = false): Promise<void> {
  const sessionId = get().conversations.find((item) => item.id === conversationId)?.sessionId;
  if (!sessionId) return;
  const request = historyRequests.begin(conversationId);
  const revision = realtimeRevisions.get(conversationId) || 0;
  const lifecycleRevision = lifecycleRevisions.get(conversationId) || 0;
  const token = useSettingsStore.getState().adminToken;
  patchHistoryView(set, conversationId, { loading: true, error: '', loadingOlder: false, olderError: '' });

  const [history, children, running, admin] = await Promise.allSettled([
    getSessionHistory(sessionId, { limit: HISTORY_PAGE_SIZE, signal: request.signal }),
    getSessionChildren(sessionId),
    getSessionRunningTasks(sessionId),
    token ? getAdminState(token) : Promise.resolve(null),
  ]);
  if (!request.isCurrent()) return;

  // A session the server no longer knows runs nothing; any other failure leaves the flag as it is.
  const runningTasks = running.status === 'fulfilled' ? running.value : isNotFound(running.reason) ? [] : null;
  let replay: AdminApproval[] = [];
  let keepLive = false;
  let busy = false;
  set((state) => {
    if (state.conversations.find((item) => item.id === conversationId)?.sessionId !== sessionId) {
      return { historyByConversation: withHistoryView(state.historyByConversation, conversationId, { loading: false }) };
    }
    const realtimeMoved = (realtimeRevisions.get(conversationId) || 0) !== revision;
    // A task event that arrived meanwhile is newer than this snapshot of the running tasks.
    const lifecycleMoved = (lifecycleRevisions.get(conversationId) || 0) !== lifecycleRevision;
    const running = runningTasks && !lifecycleMoved ? runningTasks.length > 0 : Boolean(state.generatingBySession[sessionId]);
    busy = Boolean(state.sendingByConversation[conversationId]) || running;
    keepLive = realtimeMoved || busy;

    const generatingBySession = Boolean(state.generatingBySession[sessionId]) === running
      ? state.generatingBySession
      : { ...state.generatingBySession, [sessionId]: running };

    let chat: ChatStoreState = state;
    const view: Partial<HistoryView> = { loading: false, error: '' };
    if (history.status === 'fulfilled') {
      const applied = applyHistoryPage(state, conversationId, sessionId, history.value, 'latest', keepLive);
      chat = applied.chat;
      view.hasMore = applied.hasMore;
      if (keepLive) unsettledConversations.add(conversationId);
      else unsettledConversations.delete(conversationId);
    } else {
      view.error = errorMessage(history.reason, '历史消息加载失败');
    }

    let conversationIdsBySession = runningTasks ? seedBranchRoutes(chat.conversationIdsBySession, conversationId, runningTasks) : chat.conversationIdsBySession;
    if (conversationIdsBySession[sessionId] !== conversationId) conversationIdsBySession = { ...conversationIdsBySession, [sessionId]: conversationId };
    const routedState = { ...chat, conversationIdsBySession };
    if (admin.status === 'fulfilled' && admin.value) {
      const sessionIds = new Set([sessionId, ...(runningTasks || []).map((task) => task.branch_session_id).filter(Boolean)]);
      replay = missedApprovals(routedState, admin.value.pending_approvals || [], conversationId, sessionIds);
    }

    return {
      ...routedState,
      childNodes: children.status === 'fulfilled' ? mergeChildNodesFromSessionChildren(state, conversationId, children.value) : state.childNodes,
      generatingBySession,
      isGenerating: isConversationGenerating(state.conversations, state.activeConversationId, generatingBySession, state.isGenerating, state.sendingByConversation),
      historyByConversation: withHistoryView(state.historyByConversation, conversationId, view),
    };
  });

  for (const approval of replay) applyRealtimeEvent(syntheticApprovalEvent(approval, conversationId), set, get);
  // Live cards were kept although nothing runs any more: read once more so history becomes the source of truth.
  if (keepLive && !busy && !settling) void syncConversation(conversationId, set, get, true);
}

async function loadChildHistory(childSessionId: string, set: StoreSetter, get: StoreGetter): Promise<void> {
  const viewKey = getChildConversationId(childSessionId);
  const request = historyRequests.begin(viewKey);
  const revision = realtimeRevisions.get(viewKey) || 0;
  patchHistoryView(set, viewKey, { loading: true, error: '' });
  try {
    const page = await getSessionHistory(childSessionId, { limit: HISTORY_PAGE_SIZE, taskId: childViewTaskIds.get(childSessionId), signal: request.signal });
    if (!request.isCurrent()) return;
    set((state) => {
      const keepLive = (realtimeRevisions.get(viewKey) || 0) !== revision;
      const { chat } = applyHistoryPage(state, viewKey, childSessionId, page, 'latest', keepLive);
      // Only message tables: a child view must not register routes that would surface it in the sidebar.
      return {
        messagesById: chat.messagesById,
        messageOrderByConversation: chat.messageOrderByConversation,
        toolExecutionsById: chat.toolExecutionsById,
        toolExecutionOrder: chat.toolExecutionOrder,
        childSessionMessages: withChildMessages(state.childSessionMessages, childSessionId, selectOrderedMessagesFromState(chat, viewKey)),
        // Child views are filtered by task, so they are never paged.
        historyByConversation: withHistoryView(state.historyByConversation, viewKey, { loading: false, error: '', hasMore: false }),
      };
    });
  } catch (error) {
    if (!request.isCurrent()) return;
    patchHistoryView(set, viewKey, { loading: false, error: errorMessage(error, '子会话历史加载失败') });
  }
}

function sessionRowsToConversations(rows: readonly SessionListItem[]): ConversationMeta[] {
  const titleCache = loadTitleCache();
  const seen = new Set<string>();
  const conversations: ConversationMeta[] = [];
  for (const session of rows) {
    // Entry branches are temporary runtime sessions of a parent conversation.
    if (isEntryBranchSessionId(session.session_id)) continue;
    const conversationId = normalizeConversationKey(session.conversation_key) || session.session_id;
    // A bad runtime list can carry the parent and an internal row for the same conversation.
    if (seen.has(conversationId)) continue;
    seen.add(conversationId);
    conversations.push({
      id: conversationId,
      sessionId: session.session_id,
      // Generated titles exist only in this browser; the server only knows the conversation key.
      title: titleCache[conversationId] || titleFromSession(session.conversation_key, session.session_id),
      updatedAt: session.updated_at || session.created_at || new Date().toISOString(),
    });
  }
  return conversations;
}

function mergeSessionPage(set: StoreSetter, rows: readonly SessionListItem[]) {
  const incoming = sessionRowsToConversations(rows);
  set((state) => {
    const known = new Set(state.conversations.map((item) => item.id));
    const added = incoming.filter((item) => !known.has(item.id));
    if (added.length === 0) return state;
    const conversationIdsBySession = { ...state.conversationIdsBySession };
    for (const conversation of added) {
      if (conversation.sessionId) conversationIdsBySession[conversation.sessionId] = conversation.id;
    }
    return { conversations: sortConversationsByRecency([...state.conversations, ...added]), conversationIdsBySession };
  });
}

async function loadStartupSessions(set: StoreSetter, get: StoreGetter, request: RequestTicket) {
  if (startupLoaded) return;
  startupLoaded = true;
  set({ startupError: '', sessionPaging: { hasMore: false, loading: true, error: '' } });

  const rows = await listSessions('web', SESSION_PAGE_SIZE, 0);
  if (!request.isCurrent()) return;
  sessionOffset = rows.length;
  mergeSessionPage(set, rows);
  set((state) => ({
    activeConversationId: state.activeConversationId || state.conversations[0]?.id || null,
    // The server marks the last page by returning fewer rows than asked for.
    sessionPaging: { hasMore: rows.length >= SESSION_PAGE_SIZE, loading: false, error: '' },
  }));

  // Open the stream before reading history so nothing that happens during the read is lost.
  startGlobalWebSocket(set, get);
  const active = getActiveConversation(get());
  if (active?.sessionId) await syncConversation(active.id, set, get);
}

export const useChatStore = create<ChatStoreState>((set, get) => ({
  ...createInitialChatState(),
  ...createStoreBase(),

  resetState: () => {
    stopRealtimeAndHistory();
    startupLoaded = false;
    startupRequests.reset();
    sessionOffset = 0;
    autoApprovedApprovalIds.clear();
    saveAutoApproved(autoApprovedApprovalIds);
    set({
      ...createInitialChatState(),
      ...createStoreBase(),
    });
  },

  selectConversation: commandAction('session.select', (id: string) => {
    if (id !== get().activeConversationId && !confirmNavigation()) return;
    const target = get().conversations.find((conversation) => conversation.id === id);
    set((state) => ({
      // Several sessions can run at once; the composer lock follows the selected one.
      activeConversationId: id,
      viewingChildSessionId: null,
      isGenerating: Boolean(state.sendingByConversation[id]) || (target?.sessionId ? Boolean(state.generatingBySession[target.sessionId]) : false),
    }));
    // Selecting only reads; the global stream is owned by startup, sending and reset.
    if (target?.sessionId) void syncConversation(target.id, set, get);
  }),

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
    if (trimmed !== get().viewingChildSessionId && !confirmNavigation()) return;
    const viewKey = getChildConversationId(trimmed);
    // The task monitor reopens the same child with another task filter; start the view over instead of merging.
    forgetHistory(viewKey);
    childViewTaskIds.set(trimmed, taskId?.trim() || undefined);
    set((state) => ({
      ...removeConversationMessages(state, viewKey),
      viewingChildSessionId: trimmed,
      childSessionMessages: { ...state.childSessionMessages, [trimmed]: [] },
    }));
    void loadChildHistory(trimmed, set, get);
  },

  exitChildSession: commandAction('session.exitChild', () => {
    if (get().viewingChildSessionId && !confirmNavigation()) return;
    // [2026-06-03] Why: returning from child view should reveal the still-selected
    // parent conversation. How: clear only the view marker and leave message caches in
    // place. Purpose: reopening the same child can reuse cached messages while history
    // refreshes in the background on the next view action.
    set({ viewingChildSessionId: null });
  }),

  createConversation: commandAction('session.create', () => {
    if (!confirmNavigation()) return get().activeConversationId || '';
    const conversation = createConversationMeta();
    set((state) => ({
      conversations: [conversation, ...state.conversations],
      activeConversationId: conversation.id,
      viewingChildSessionId: null,
      isGenerating: false,
    }));
    return conversation.id;
  }),

  deleteConversation: commandAction('session.delete', async (id: string) => {
    if (get().sendingByConversation[id]) throw new Error('消息仍在发送，请发送结束后再删除会话');
    if (get().deletingByConversation[id]) throw new Error('会话正在删除，请稍候');
    const conversation = get().conversations.find((item) => item.id === id);
    if (!conversation) return;
    const request = startupRequests.begin();
    set(state => ({ deletingByConversation: { ...state.deletingByConversation, [id]: true } }));
    try {
      if (conversation.sessionId) {
        await deleteSession(conversation.sessionId);
      }

      if (!request.isCurrent() || get().conversations.find(item => item.id === id)?.sessionId !== conversation.sessionId) {
        throw new Error('原会话已删除，但当前会话已变更；新上下文未移除');
      }
      forgetHistory(id);
      set((state) => {
        const conversations = state.conversations.filter((item) => item.id !== id);
        const activeConversationId = state.activeConversationId === id ? conversations[0]?.id || null : state.activeConversationId;
        const nextChatState = removeConversationMessages(state, id);
        // Branch and child sessions route to the parent too; a stale route would revive the deleted conversation.
        const conversationIdsBySession = Object.fromEntries(
          Object.entries(nextChatState.conversationIdsBySession).filter(([, conversationId]) => conversationId !== id),
        );
        const childNodes = Object.fromEntries(
          Object.entries(state.childNodes).filter(([, child]) => child.parentConversationId !== id),
        );

        const generatingBySession = conversation?.sessionId
          ? { ...state.generatingBySession, [conversation.sessionId]: false }
          : state.generatingBySession;
        const sendingByConversation = { ...state.sendingByConversation };
        delete sendingByConversation[id];

        return {
          ...nextChatState,
          conversationIdsBySession,
          conversations,
          activeConversationId,
          viewingChildSessionId: state.viewingChildSessionId && childNodes[state.viewingChildSessionId]
            ? state.viewingChildSessionId
            : null,
          generatingBySession,
          sendingByConversation,
          childNodes,
          historyByConversation: withoutHistoryView(state.historyByConversation, id),
          isGenerating: isConversationGenerating(conversations, activeConversationId, generatingBySession, false, sendingByConversation),
        };
      });
    } finally {
      if (request.isCurrent()) set(state => ({ deletingByConversation: { ...state.deletingByConversation, [id]: false } }));
    }
  }),

  resetConversationView: (id, previousSessionId) => {
    if (get().conversations.find(item => item.id === id)?.sessionId !== previousSessionId) return;
    forgetHistory(id);
    set(state => {
      if (state.conversations.find(item => item.id === id)?.sessionId !== previousSessionId) return {};
      const conversations = state.conversations.map(item => item.id === id ? { ...item, sessionId: '' } : item);
      const childNodes = Object.fromEntries(Object.entries(state.childNodes).filter(([, child]) => child.parentConversationId !== id));
      const generatingBySession = { ...state.generatingBySession, [previousSessionId]: false };
      const sendingByConversation = { ...state.sendingByConversation, [id]: false };
      return {
        ...removeConversationMessages(state, id), conversations, childNodes, generatingBySession, sendingByConversation,
        conversationIdsBySession: Object.fromEntries(Object.entries(state.conversationIdsBySession).filter(([, conversationId]) => conversationId !== id)),
        viewingChildSessionId: state.viewingChildSessionId && state.childNodes[state.viewingChildSessionId]?.parentConversationId === id ? null : state.viewingChildSessionId,
        historyByConversation: withoutHistoryView(state.historyByConversation, id),
        isGenerating: isConversationGenerating(conversations, state.activeConversationId, generatingBySession, false, sendingByConversation),
      };
    });
  },

  renameConversation: commandAction('session.rename', (id: string, newTitle: string) => {
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
  }),

  sendMessage: async (text, attachments, entryNodeId) => {
    const trimmed = text.trim();
    if (!trimmed && (!attachments || attachments.length === 0)) return;

    const state = get();
    let conversationId = state.activeConversationId;
    if (!conversationId) {
      const conversation = createConversationMeta();
      conversationId = conversation.id;
      set(current => ({ conversations: [conversation, ...current.conversations], activeConversationId: conversation.id, viewingChildSessionId: null }));
    }
    if (get().sendingByConversation[conversationId]) throw new Error('这条消息正在发送，请稍候');
    if (get().deletingByConversation[conversationId]) throw new Error('当前会话正在删除，消息未发送');
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
      sendingByConversation: { ...current.sendingByConversation, [conversationId]: true },
      connectionStatus: current.connectionStatus === 'open' ? 'open' : 'connecting',
    }));

    let sessionId = '';
    try {
      // Files picked in this browser are uploaded first; the inbound message carries their workspace paths.
      let uploadedAttachments: { name: string; size: number; type?: string; path?: string; mime_type?: string }[] | undefined;
      if (attachments && attachments.length > 0) {
        const uploaded = await Promise.all(
          attachments.map(async (attachment: Attachment) => {
            if (attachment.file) {
              const result = await uploadAttachment(attachment.file, conversationKey);
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
      // Shown before the inbound_message echo; the id matches the reducer's, so the echo updates it in place.
      const inboundSeq = result.inbound_seq;
      if (inboundSeq != null) {
        const optimisticId = `message:${conversationId}:user:inbound:${inboundSeq}`;
        const now = new Date().toISOString();
        set((current) => {
          if (current.messagesById[optimisticId]) return current;
          bumpRealtimeRevision(conversationId);
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
                attachments: (uploadedAttachments || []).map(({ name, size, type, path, mime_type }) => ({ name, size, type, path, mime_type })),
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
      // A failed send says nothing about the global stream; an open connection stays marked open.
      set((current) => ({
        isGenerating: isConversationGenerating(current.conversations, current.activeConversationId, current.generatingBySession, false, { ...current.sendingByConversation, [conversationId]: false }),
        sendingByConversation: { ...current.sendingByConversation, [conversationId]: false },
        connectionStatus: current.connectionStatus === 'open' ? 'open' : 'closed',
      }));
      throw error;
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
      sendingByConversation: { ...current.sendingByConversation, [conversationId]: false },
      isGenerating: isConversationGenerating(current.conversations.map(item => item.id === conversationId ? { ...item, sessionId } : item), current.activeConversationId, { ...current.generatingBySession, [sessionId]: true }, false, { ...current.sendingByConversation, [conversationId]: false }),
    }));

    // Keeps the one global stream, or opens it if this is the tab's first message.
    startGlobalWebSocket(set, get);
  },

  cancelCurrentTask: async () => {
    const activeConversation = getActiveConversation(get());
    if (!activeConversation?.sessionId) return;

    await cancelActiveTasks(activeConversation.sessionId);

    // [2026-06-03] Why: cancellation should not manage the global WebSocket and
    // should unlock the active composer immediately. How: clear only the active
    // session's local generating flags after the cancel API returns or races with
    // completion. Purpose: resetState remains the only normal path that disconnects
    // realtime transport, while cancel stays a task action.
    set((state) => ({
      isGenerating: isConversationGenerating(state.conversations, state.activeConversationId, { ...state.generatingBySession, [activeConversation.sessionId]: false }, false, state.sendingByConversation),
      generatingBySession: {
        ...state.generatingBySession,
        [activeConversation.sessionId]: false,
      },
    }));
  },

  loadStartup: () => {
    const request = startupRequests.begin();
    void loadStartupSessions(set, get, request).catch(error => {
      if (!request.isCurrent()) return;
      startupLoaded = false;
      set({
        startupError: errorMessage(error, '加载会话失败，请重试'),
        sessionPaging: { hasMore: false, loading: false, error: '' },
      });
    });
  },

  loadMoreConversations: async () => {
    const paging = get().sessionPaging;
    if (paging.loading || !paging.hasMore) return;
    const request = startupRequests.begin();
    set({ sessionPaging: { ...paging, loading: true, error: '' } });
    try {
      const rows = await listSessions('web', SESSION_PAGE_SIZE, sessionOffset);
      if (!request.isCurrent()) return;
      sessionOffset += rows.length;
      mergeSessionPage(set, rows);
      set({ sessionPaging: { hasMore: rows.length >= SESSION_PAGE_SIZE, loading: false, error: '' } });
    } catch (error) {
      if (!request.isCurrent()) return;
      set((state) => ({ sessionPaging: { ...state.sessionPaging, loading: false, error: errorMessage(error, '更多对话加载失败') } }));
    }
  },

  loadOlderHistory: async () => {
    const state = get();
    const conversationId = state.activeConversationId;
    if (!conversationId || state.viewingChildSessionId) return;
    const sessionId = state.conversations.find((item) => item.id === conversationId)?.sessionId;
    const cached = historyCaches.get(conversationId);
    const view = state.historyByConversation[conversationId] || IDLE_HISTORY;
    const before = cached?.window.rows[0]?.cursor;
    if (!sessionId || cached?.sessionId !== sessionId || !cached.window.hasMore || typeof before !== 'number') return;
    if (view.loading || view.loadingOlder) return;

    const request = historyRequests.begin(conversationId);
    patchHistoryView(set, conversationId, { loadingOlder: true, olderError: '' });
    try {
      const page = await getSessionHistory(sessionId, { limit: HISTORY_PAGE_SIZE, before, signal: request.signal });
      if (!request.isCurrent()) return;
      set((current) => {
        if (current.conversations.find((item) => item.id === conversationId)?.sessionId !== sessionId) {
          return { historyByConversation: withHistoryView(current.historyByConversation, conversationId, { loadingOlder: false }) };
        }
        const { chat, hasMore } = applyHistoryPage(current, conversationId, sessionId, page, 'older', true);
        return { ...chat, historyByConversation: withHistoryView(current.historyByConversation, conversationId, { loadingOlder: false, hasMore }) };
      });
    } catch (error) {
      if (!request.isCurrent()) return;
      patchHistoryView(set, conversationId, { loadingOlder: false, olderError: errorMessage(error, '更早的消息加载失败') });
    }
  },

  retryHistory: () => {
    const state = get();
    if (state.viewingChildSessionId) {
      void loadChildHistory(state.viewingChildSessionId, set, get);
      return;
    }
    if (state.activeConversationId) void syncConversation(state.activeConversationId, set, get);
  },
}));

/** History state of whatever the message list currently shows. */
export function selectVisibleHistory(state: ChatStoreState): HistoryView {
  const viewKey = state.viewingChildSessionId ? getChildConversationId(state.viewingChildSessionId) : state.activeConversationId;
  return (viewKey && state.historyByConversation[viewKey]) || IDLE_HISTORY;
}

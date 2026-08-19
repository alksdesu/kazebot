// [2026-05-31] Step 2A tests for the reducer-backed chat store and log-panel layout.
// Why: the new store must coexist with the legacy store while proving that WebSocket
// events flow through eventReducer and that the event log lives in the right rail.
// How: use a fake browser WebSocket plus fetch stubs for Supervisor endpoints, then
// assert selector-derived messages, startup hydration, and rendered log rows. Purpose:
// protect the refactor boundary before later components switch to the new message model.
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EventLogPanel } from '../components/log/EventLogPanel';
import { AppLayout } from '../components/layout/AppLayout';
import { selectMessages } from '../store/eventSelectors';
import { useChatStore } from '../store/chatStore';
import { useClientPrefsStore } from '../store/clientPrefsStore';
import type { SupervisorEvent } from '../types/chat';
import type { WsMessage } from '../types/message';

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
}

function supervisorEvent(seq: number, type: string, payload: Record<string, unknown>, sessionId = 'sess-chat'): SupervisorEvent {
  return {
    seq,
    event_id: `chat-ev-${seq}`,
    ts: `2026-05-31T03:10:${String(seq).padStart(2, '0')}.000Z`,
    session_id: sessionId,
    type,
    payload,
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('chatStore', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    // [2026-06-02] Why: chatStore now writes sidebar titles to browser storage.
    // How: clear storage and restore default client preferences before each case.
    // Purpose: persisted metadata from one test cannot affect another startup path.
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    useChatStore.getState().resetState();
  });

  afterEach(() => {
    useChatStore.getState().resetState();
    // [2026-06-02] Why: title persistence intentionally survives resetState at runtime.
    // How: clear jsdom storage during test cleanup instead of relying on the store reset.
    // Purpose: tests stay isolated while production browser refreshes keep cached titles.
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('routes WebSocket events through eventReducer while reconciling the optimistic sent user message', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/inbound')) {
        return jsonResponse({ session_id: 'sess-chat', inbound_seq: 1, accepted: true });
      }
      return jsonResponse([]);
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('run uname', undefined, 'ereuna_main');

    // [2026-06-03] Why: sendMessage now inserts an optimistic user card before
    // the websocket echo arrives. How: assert the pending visible card exists here
    // and let the later inbound_message reducer path merge it in place. Purpose:
    // this test matches the current no-empty-chat behavior after pressing send.
    expect(selectMessages(useChatStore.getState(), conversationId)).toHaveLength(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain('/v1/ws');

    ws.open();
    expect(ws.sent).toEqual([JSON.stringify({ last_seq: 0 })]);
    // [2026-06-01] Opening the browser WebSocket is enough to mark realtime as healthy.
    // Why: waiting for a non-ping event leaves the right rail stuck in a false
    // connecting or disconnected state when the socket is already usable. How: the
    // onOpen callback updates connectionStatus immediately. Purpose: the UI reflects
    // the actual connection before the first Supervisor event arrives.
    expect(useChatStore.getState().connectionStatus).toBe('open');

    ws.receive(supervisorEvent(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'run uname' }));
    ws.receive(supervisorEvent(2, 'stream_delta', { source_inbound_seq: 1, type: 'text', content: 'working' }));
    ws.receive(supervisorEvent(3, 'outbound_message', { source_inbound_seq: 1, text: 'done', attachments: [] }));
    ws.receive(supervisorEvent(4, 'task_completed', { source_inbound_seq: 1, status: 'completed' }));

    const state = useChatStore.getState();
    const messages = selectMessages(state, conversationId);
    expect(messages.map((message) => message.role)).toEqual(['user', 'assistant']);
    expect(messages[0].blocks[0]).toMatchObject({ kind: 'text', text: 'run uname' });
    // [AutoC 2026-06-04] Why: outbound_message finalizes the same LLM request that
    // produced the stream_delta. How: assert the single assistant card now contains
    // the backend final text rather than a closed stream preview plus a second card.
    // Purpose: the store-level realtime path follows the one-request-one-card rule.
    expect(messages[1].status).toBe('completed');
    expect(messages[1].blocks[0]).toMatchObject({ kind: 'text', text: 'done', delivery: 'final', streaming: false });
    expect(state.lastSeqBySession['sess-chat']).toBe(4);
    expect(state.isGenerating).toBe(false);
    // [2026-06-03] A terminal task event no longer closes realtime transport.
    // Why: the same /v1/ws connection must continue receiving all sessions. How:
    // completion only clears generation and preserves reducer-owned cards. Purpose:
    // successful turns leave the global WebSocket open without rebuilding history.
    expect(state.connectionStatus).toBe('open');
    expect(state.eventLog.map((entry) => entry.type)).toEqual(['inbound_message', 'stream_delta', 'outbound_message', 'task_completed']);
    // [AutoC 2026-06-04] Why: task completion used to fetch /history and replace all
    // visible cards. How: inspect the mocked transport calls after the terminal event.
    // Purpose: this test prevents reintroducing task-completion history rebuilds.
    const fetchMock = fetch as unknown as { mock: { calls: Array<[RequestInfo | URL, RequestInit?]> } };
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/history'))).toBe(false);
  });

  it('tracks transient task activity from global WebSocket events', async () => {
    // [AutoC 2026-06-04] Why: ActiveTasksModal needs live task phases without owning
    // the WebSocket. How: drive the existing global socket path and inspect the new
    // store-owned taskActivities map. Purpose: stream, tool, approval, and terminal
    // events update modal state independently of the 5-second polling fallback.
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    useChatStore.setState((state) => ({
      conversations: [{ id: 'conv-activity', sessionId: 'sess-activity', title: 'Activity', updatedAt: '2026-06-04T02:00:00.000Z' }],
      activeConversationId: 'conv-activity',
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-activity': 'conv-activity' },
    }));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.open();

    ws.receive(supervisorEvent(10, 'stream_delta', { task_id: 'task-live', node_id: 'ereuna_main', type: 'thinking', content: 'reasoning' }, 'sess-activity'));
    expect(useChatStore.getState().taskActivities['task-live']).toMatchObject({ phase: 'thinking', detail: '' });

    ws.receive(supervisorEvent(11, 'stream_delta', { task_id: 'task-live', node_id: 'ereuna_main', type: 'text', content: 'answer' }, 'sess-activity'));
    expect(useChatStore.getState().taskActivities['task-live']).toMatchObject({ phase: 'generating', detail: '' });

    ws.receive(supervisorEvent(12, 'tool_call_start', { task_id: 'task-live', node_id: 'ereuna_main', tool_name: 'read_file' }, 'sess-activity'));
    expect(useChatStore.getState().taskActivities['task-live']).toMatchObject({ phase: 'tool_call', detail: 'read_file' });

    ws.receive(supervisorEvent(13, 'approval_requested', { node_id: 'ereuna_main', operation: 'execute_command' }, 'sess-activity'));
    expect(useChatStore.getState().taskActivities['sess-activity:ereuna_main']).toMatchObject({ phase: 'awaiting_approval', detail: 'execute_command' });

    ws.receive(supervisorEvent(14, 'task_completed', { task_id: 'task-live', node_id: 'ereuna_main' }, 'sess-activity'));
    expect(useChatStore.getState().taskActivities['task-live']).toBeUndefined();
  });

  it('keeps realtime marked open and lets cancelCurrentTask unlock generation', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/history')) return jsonResponse([]);
      return jsonResponse({ ok: true });
    }));

    useChatStore.setState({
      conversations: [{ id: 'conv-cancel', sessionId: 'sess-cancel', title: 'Cancel test', updatedAt: '2026-05-31T03:30:00.000Z' }],
      activeConversationId: 'conv-cancel',
      isGenerating: true,
      connectionStatus: 'open',
    });

    await useChatStore.getState().cancelCurrentTask();

    // [2026-06-03] Why: cancelCurrentTask now keeps the socket open but unlocks the
    // active composer immediately after the cancel API returns. How: assert the local
    // flag clears before the authoritative task_cancelled event arrives. Purpose:
    // the test follows the current cancellation UX without changing transport rules.
    expect(useChatStore.getState()).toMatchObject({ isGenerating: false, connectionStatus: 'open' });
    // [2026-06-03] Why: this setup marks the global connection as already open
    // without constructing a fake socket. How: stop before websocket event replay and
    // assert the local cancel path only. Purpose: the test covers cancelCurrentTask's
    // current responsibility without depending on unrelated startup wiring.
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('persists first-message conversation titles in localStorage', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return jsonResponse({ session_id: 'sess-title', inbound_seq: 1, accepted: true });
      }
      return jsonResponse([]);
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('remember this title', undefined, 'ereuna_main');

    // [2026-06-02] Why: generated sidebar titles used to live only in Zustand memory.
    // How: read the dedicated title cache after the first-message title is upserted.
    // Purpose: a browser refresh can restore the human-readable title instead of the id.
    const cache = JSON.parse(localStorage.getItem('clonoth_conversation_titles') || '{}');
    expect(cache[conversationId]).toBe('remember this title');
  });

  it('prefers locally persisted conversation titles when loading server sessions', async () => {
    // [2026-06-02] Why: startup session metadata from the backend does not include the
    // generated frontend title. How: seed the browser title cache before loadStartup.
    // Purpose: the sidebar chooses the persisted title over the conversation key.
    localStorage.setItem('clonoth_conversation_titles', JSON.stringify({ 'conv-history': 'Cached human title' }));
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-history', conversation_key: 'web:conv-history', channel: 'web', created_at: '2026-05-31T03:00:00.000Z', updated_at: '2026-05-31T03:01:00.000Z' }]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(useChatStore.getState().conversations[0]?.title).toBe('Cached human title'));
  });

  it('filters startup branch sessions out of the sidebar conversation list', async () => {
    // [2026-06-03] Why: active entry branches inherit the web channel while they are
    // running, and a global session list can otherwise expose branch_1 as a chat.
    // How: return a parent web session plus its temporary branch and assert only the
    // parent remains. Purpose: refresh cannot create duplicate branch conversations.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([
          { session_id: 'branch_1', conversation_key: 'web:conv-history', channel: 'web', created_at: '2026-05-31T03:02:00.000Z', updated_at: '2026-05-31T03:02:00.000Z' },
          { session_id: 'sess-history', conversation_key: 'web:conv-history', channel: 'web', created_at: '2026-05-31T03:00:00.000Z', updated_at: '2026-05-31T03:01:00.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(useChatStore.getState().conversations).toHaveLength(1));
    expect(useChatStore.getState().conversations[0]).toMatchObject({ id: 'conv-history', sessionId: 'sess-history' });
  });

  it('routes branch_created and later branch runtime events to the parent conversation', async () => {
    // [2026-06-03] Why: /v1/ws can deliver branch runtime task snapshots even when
    // the sidebar only knows the parent web session. How: branch_created registers
    // branch_1 to the same conversation, then a branch task event updates that chat.
    // Purpose: global realtime cannot create or select a branch_1 conversation.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/history')) return jsonResponse([]);
      return jsonResponse([]);
    }));
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-parent', sessionId: 'sess-parent', title: 'Parent', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-parent',
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-parent': 'conv-parent' },
      connectionStatus: 'idle',
    }));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(supervisorEvent(1, 'branch_created', { parent_session_id: 'sess-parent', branch_session_id: 'branch_1', inbound_seq: 10 }, 'sess-parent'));
    ws.receive(supervisorEvent(2, 'task_created', {
      task_id: 'task-branch',
      status: 'pending',
      source_inbound_seq: 10,
      input: { parent_session_id: 'sess-parent', branch_session_id: 'branch_1' },
    }, 'branch_1'));

    const state = useChatStore.getState();
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(['conv-parent']);
    expect(state.conversationIdsBySession.branch_1).toBe('conv-parent');
    expect(state.eventLog.find((entry) => entry.eventId === 'chat-ev-2')?.conversationId).toBe('conv-parent');
  });

  it('routes agent child events through structured parent conversation metadata', async () => {
    // [2026-06-03] Why: dispatch children use agent: conversation keys that the old
    // frontend rejected before reading route_conversation_key. How: send a child task
    // event with the same structured fallback fields produced by dispatch_origin.
    // Purpose: the store keeps the event under the visible web conversation.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/history')) return jsonResponse([]);
      return jsonResponse([]);
    }));
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-parent', sessionId: 'sess-parent', title: 'Parent', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-parent',
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-parent': 'conv-parent' },
      connectionStatus: 'idle',
    }));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(supervisorEvent(20, 'task_created', {
      task_id: 'task-child',
      session_id: 'child-runtime',
      input: {
        entry_node_id: 'scout',
        parent_session_id: 'sess-parent',
        branch_session_id: 'child-branch',
        task_context: {
          conversation_key: 'agent:scout:web:conv-parent',
          route_conversation_key: 'web:conv-parent',
          session_id: 'child-runtime',
          parent_session_id: 'sess-parent',
          branch_session_id: 'child-branch',
          node_id: 'scout',
        },
        _dispatch_origin: {
          parent_session_id: 'sess-parent',
          parent_conversation_key: 'web:conv-parent',
        },
      },
    }, 'child-runtime'));

    const state = useChatStore.getState();
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(['conv-parent']);
    expect(state.conversationIdsBySession['child-runtime']).toBe('conv-parent');
    expect(state.conversationIdsBySession['child-branch']).toBe('conv-parent');
    expect(state.eventLog.find((entry) => entry.eventId === 'chat-ev-20')?.conversationId).toBe('conv-parent');
  });

  it('rebuilds child node state from the session children endpoint during startup', async () => {
    // [2026-06-03] Why: childNodes is browser memory and disappears on refresh.
    // How: startup now asks the backend for child sessions after parent history is
    // loaded. Purpose: the sidebar and child panel can recover delegated work without
    // replaying old WebSocket events.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-parent', conversation_key: 'web:conv-parent', channel: 'web', created_at: '2026-06-03T12:00:00.000Z', updated_at: '2026-06-03T12:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-parent/children')) {
        return jsonResponse([{
          session_id: 'child-smith',
          parent_session_id: 'sess-parent',
          node_id: 'smith',
          status: 'running',
          task_id: 'task-child',
          started_at: '2026-06-03T12:00:20.000Z',
        }]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(useChatStore.getState().selectChildNodes('conv-parent')).toHaveLength(1));
    expect(useChatStore.getState().selectChildNodes('conv-parent')[0]).toMatchObject({
      sessionId: 'child-smith',
      nodeId: 'smith',
      parentConversationId: 'conv-parent',
      status: 'running',
      taskId: 'task-child',
    });
  });

  it('tracks child node task status and exposes child node selectors', async () => {
    // [2026-06-03] Why: UI work in the next phase needs data-layer child session
    // status without rendering components here. How: replay routed child lifecycle
    // events and read the store selectors. Purpose: selector consumers can detect
    // active child nodes and terminal child nodes by parent conversation.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/history')) return jsonResponse([]);
      return jsonResponse([]);
    }));
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-parent', sessionId: 'sess-parent', title: 'Parent', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-parent',
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-parent': 'conv-parent' },
      connectionStatus: 'idle',
    }));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.open();
    const childPayload = {
      task_id: 'task-child',
      input: {
        entry_node_id: 'smith',
        parent_session_id: 'sess-parent',
        task_context: {
          conversation_key: 'agent:smith:web:conv-parent',
          route_conversation_key: 'web:conv-parent',
          node_id: 'smith',
        },
        _dispatch_origin: {
          parent_session_id: 'sess-parent',
          parent_conversation_key: 'web:conv-parent',
        },
      },
    };

    ws.receive(supervisorEvent(30, 'task_created', childPayload, 'child-smith'));
    expect(useChatStore.getState().selectHasActiveChildNodes('conv-parent')).toBe(true);
    expect(useChatStore.getState().selectChildNodes('conv-parent')[0]).toMatchObject({
      sessionId: 'child-smith',
      nodeId: 'smith',
      parentConversationId: 'conv-parent',
      status: 'running',
      taskId: 'task-child',
    });

    ws.receive(supervisorEvent(31, 'approval_requested', childPayload, 'child-smith'));
    expect(useChatStore.getState().selectChildNodes('conv-parent')[0]?.status).toBe('awaiting_approval');

    ws.receive(supervisorEvent(32, 'approval_decided', childPayload, 'child-smith'));
    expect(useChatStore.getState().selectChildNodes('conv-parent')[0]?.status).toBe('running');

    ws.receive(supervisorEvent(33, 'task_completed', childPayload, 'child-smith'));
    expect(useChatStore.getState().selectHasActiveChildNodes('conv-parent')).toBe(false);
    expect(useChatStore.getState().selectChildNodes('conv-parent')[0]?.status).toBe('completed');
  });

  it('hydrates dispatch result inbound history as a dispatch callback card instead of a user message', async () => {
    // [AutoC 2026-06-04] Why: refreshed dispatch history must use the same pure
    // structured contract as realtime events. How: hydrate a dispatch_result row with
    // raw content plus child_task_id, child_node_id, caller_node_id, summary, and
    // child_session_id. Purpose: the web client builds its own Chinese presentation
    // without carrying backend-localized text in ConversationStore.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-dispatch', conversation_key: 'web:conv-dispatch', channel: 'web', created_at: '2026-06-03T12:00:00.000Z', updated_at: '2026-06-03T12:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-dispatch/history')) {
        return jsonResponse([
          {
            id: 'u-dispatch',
            role: 'user',
            content: 'raw child result',
            message_type: 'dispatch_result',
            child_session_id: 'child-scout',
            child_task_id: 'task-child',
            child_node_id: 'scout',
            caller_node_id: 'smith',
            summary: 'done',
            created_at: '2026-06-03T12:00:10.000Z',
          },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(selectMessages(useChatStore.getState(), 'conv-dispatch')).toHaveLength(1));
    const [message] = selectMessages(useChatStore.getState(), 'conv-dispatch');
    expect(message.role).toBe('dispatch_callback');
    expect(message.source).toMatchObject({
      childSessionId: 'child-scout',
      childTaskId: 'task-child',
      childNodeId: 'scout',
      callerNodeId: 'smith',
      summary: 'done',
    });
    expect(message.blocks[0]).toMatchObject({ kind: 'text', text: 'raw child result' });
  });

  it('does not classify localized dispatch-looking text as a dispatch result', async () => {
    // [2026-06-03] Why: presentation text can change by language and should not be a
    // data contract. How: hydrate a normal user_input row that happens to begin with
    // the old Chinese dispatch notice. Purpose: prevent future regressions from
    // reintroducing frontend text-prefix matching.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-dispatch-text', conversation_key: 'web:conv-dispatch-text', channel: 'web', created_at: '2026-06-03T12:00:00.000Z', updated_at: '2026-06-03T12:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-dispatch-text/history')) {
        return jsonResponse([
          { id: 'u-dispatch-text', role: 'user', content: '[异步子任务完成] this is ordinary user text', message_type: 'user_input', created_at: '2026-06-03T12:00:10.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(selectMessages(useChatStore.getState(), 'conv-dispatch-text')).toHaveLength(1));
    const [message] = selectMessages(useChatStore.getState(), 'conv-dispatch-text');
    expect(message.role).toBe('user');
    expect(message.blocks[0]).toMatchObject({ kind: 'text', text: expect.stringContaining('[异步子任务完成]') });
  });

  it('loads child session history when entering child session view', async () => {
    // [2026-06-03] Why: Phase 3 child navigation renders the child session's own
    // ConversationStore history in the main chat area. How: viewChildSession fetches
    // the existing history endpoint and caches normalized messages by child session id.
    // Purpose: clicking a child node opens its independent chat stream.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/child-smith/history')) {
        return jsonResponse([
          { id: 'u-child', role: 'user', content: 'child input', message_type: 'user_input', created_at: '2026-06-03T12:00:20.000Z' },
          { id: 'a-child', role: 'assistant', content: 'child output', message_type: 'assistant', created_at: '2026-06-03T12:00:30.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().viewChildSession('child-smith');

    expect(useChatStore.getState().viewingChildSessionId).toBe('child-smith');
    await waitFor(() => expect(useChatStore.getState().childSessionMessages['child-smith']).toHaveLength(2));
    expect(useChatStore.getState().childSessionMessages['child-smith'][1].blocks[0]).toMatchObject({ kind: 'text', text: 'child output' });
  });

  it('renders realtime routed child events into the active child session view', async () => {
    // [2026-06-03] Why: child-agent websocket events are intentionally hidden from
    // the parent chat, but the same events should be visible while the user is looking
    // at that child session. How: keep parent routing for childNodes while replaying a
    // child-view event copy into childSessionMessages. Purpose: live child streaming
    // continues after entering the child chat stream.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/history')) return jsonResponse([]);
      return jsonResponse([]);
    }));
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-parent', sessionId: 'sess-parent', title: 'Parent', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-parent',
      viewingChildSessionId: 'child-smith',
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-parent': 'conv-parent' },
      connectionStatus: 'idle',
    }));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(supervisorEvent(34, 'stream_delta', {
      task_id: 'task-child',
      type: 'text',
      content: 'child live text',
      input: {
        child_session_id: 'child-smith',
        entry_node_id: 'smith',
        task_context: {
          conversation_key: 'agent:smith:web:conv-parent',
          route_conversation_key: 'web:conv-parent',
          node_id: 'smith',
        },
        _dispatch_origin: { parent_session_id: 'sess-parent', parent_conversation_key: 'web:conv-parent' },
      },
    }, 'branch_1'));

    const parentMessages = selectMessages(useChatStore.getState(), 'conv-parent');
    expect(parentMessages).toHaveLength(0);
    expect(useChatStore.getState().childSessionMessages['child-smith']?.[0]?.blocks[0]).toMatchObject({ kind: 'text', text: 'child live text' });
  });

  it('loads server sessions and keeps a historical finish call separate from prior tool-only work', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-history', conversation_key: 'web:conv-history', channel: 'web', created_at: '2026-05-31T03:00:00.000Z', updated_at: '2026-05-31T03:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-history/history')) {
        return jsonResponse([
          { id: 'u1', role: 'user', content: 'hello', message_type: 'user_input', created_at: '2026-05-31T03:00:01.000Z' },
          {
            id: 'a-tools',
            role: 'assistant',
            content: '',
            created_at: '2026-05-31T03:00:02.000Z',
            thinking: 'thought',
            thinking_blocks: [{ text: 'thought', started_at: '2026-05-31T03:00:02.100Z', ended_at: '2026-05-31T03:00:03.400Z' }],
            tool_calls: [{ id: 'search-1', name: 'search_in_files', arguments: { query: 'needle' } }],
          },
          { id: 't-tools', role: 'tool', content: 'found one', message_type: 'tool_result', tool_call_id: 'search-1', tool_name: 'search_in_files', created_at: '2026-05-31T03:00:03.000Z' },
          { id: 'a-finish', role: 'assistant', content: '', created_at: '2026-05-31T03:00:04.000Z', tool_calls: [{ id: 'finish-1', name: 'finish', arguments: { text: 'hi there' } }] },
          { id: 't-finish', role: 'tool', content: 'ok', message_type: 'tool_result', tool_call_id: 'finish-1', tool_name: 'finish', created_at: '2026-05-31T03:00:05.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(useChatStore.getState().conversations).toHaveLength(1));
    await waitFor(() => expect(selectMessages(useChatStore.getState(), 'conv-history')).toHaveLength(3));

    const state = useChatStore.getState();
    const messages = selectMessages(state, 'conv-history');
    expect(state.activeConversationId).toBe('conv-history');
    expect(messages[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hello', delivery: 'history' });
    // Why: finish is persisted as its own assistant tool call by the backend guard.
    // How: this history fixture places a visible finish after a tool-only assistant row.
    // Purpose: hydration must render the prior tool card and the final finish card separately.
    expect(messages[1].blocks.map((block) => block.kind)).toEqual(['thinking', 'tool']);
    // [AutoC 2026-06-04] Why: /history now returns structured reasoning blocks with
    // timing metadata. How: assert hydration transfers those fields to ThinkingBlock.
    // Purpose: historical reasoning renders elapsed time instead of a character-count
    // fallback in the collapsed header.
    expect(messages[1].blocks[0]).toMatchObject({
      kind: 'thinking',
      text: 'thought',
      startedAt: '2026-05-31T03:00:02.100Z',
      endedAt: '2026-05-31T03:00:03.400Z',
    });
    expect(messages[2].blocks.map((block) => block.kind)).toEqual(['tool', 'text']);
    expect(messages[2].blocks.at(-1)).toMatchObject({ kind: 'text', text: 'hi there', delivery: 'final' });
  });

  it('hydrates historical reply and finish control text with distinct delivery metadata', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-history-delivery', conversation_key: 'web:conv-history-delivery', channel: 'web', created_at: '2026-05-31T04:00:00.000Z', updated_at: '2026-05-31T04:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-history-delivery/history')) {
        return jsonResponse([
          { id: 'u1', role: 'user', content: 'hello', message_type: 'user_input', created_at: '2026-05-31T04:00:01.000Z' },
          { id: 'a-reply', role: 'assistant', content: '', created_at: '2026-05-31T04:00:02.000Z', tool_calls: [{ id: 'reply-1', name: 'reply', arguments: { text: 'still working' } }] },
          { id: 't-reply', role: 'tool', content: 'ok', message_type: 'tool_result', tool_call_id: 'reply-1', tool_name: 'reply', created_at: '2026-05-31T04:00:03.000Z' },
          { id: 'a-finish', role: 'assistant', content: '', created_at: '2026-05-31T04:00:04.000Z', tool_calls: [{ id: 'finish-1', name: 'finish', arguments: { text: 'all done' } }] },
          { id: 't-finish', role: 'tool', content: 'ok', message_type: 'tool_result', tool_call_id: 'finish-1', tool_name: 'finish', created_at: '2026-05-31T04:00:05.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.getState().loadStartup();

    await waitFor(() => expect(useChatStore.getState().conversations).toHaveLength(1));
    await waitFor(() => expect(selectMessages(useChatStore.getState(), 'conv-history-delivery')).toHaveLength(3));

    const messages = selectMessages(useChatStore.getState(), 'conv-history-delivery');
    const replyText = messages[1].blocks.at(-1);
    const finishText = messages[2].blocks.at(-1);

    // [2026-06-02] Why: history rebuild previously made all text blocks delivery
    // history, which removed the reply border after refresh. How: derive delivery
    // from the hydrated control completion type. Purpose: rebuilt reply and finish
    // messages match live rendering metadata.
    expect(messages[1]).toMatchObject({ completionType: 'reply' });
    expect(messages[2]).toMatchObject({ completionType: 'finish' });
    expect(replyText).toMatchObject({ kind: 'text', text: 'still working', delivery: 'intermediate' });
    expect(finishText).toMatchObject({ kind: 'text', text: 'all done', delivery: 'final' });
  });

  it('does not create sidebar conversations for events from unknown branch sessions', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/inbound')) {
        return jsonResponse({ session_id: 'sess-chat', inbound_seq: 1, accepted: true });
      }
      return jsonResponse([]);
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('start work', undefined, 'ereuna_main');
    expect(useChatStore.getState().conversations.map((conversation) => conversation.id)).toEqual([conversationId]);

    // Why: child agents and branch sessions may stream events on session ids that the
    // web sidebar did not create. How: replay one event that resolves to its own session
    // and one that resolves to the parent turn through source_inbound_seq. Purpose: the
    // sidebar neither gains a child conversation nor replaces the parent session id.
    FakeWebSocket.instances[0].receive(supervisorEvent(2, 'stream_delta', { type: 'text', content: 'branch work' }, 'branch-sess'));
    FakeWebSocket.instances[0].receive(supervisorEvent(3, 'stream_delta', { source_inbound_seq: 1, type: 'text', content: 'parent work' }, 'child-sess'));

    expect(useChatStore.getState().conversations.map((conversation) => conversation.id)).toEqual([conversationId]);
    expect(useChatStore.getState().conversations[0].sessionId).toBe('sess-chat');
  });

  it('preserves live messages when history loading races with an active generation', async () => {
    const liveMessage: WsMessage = {
      id: 'message:conv-race:live-assistant',
      conversationId: 'conv-race',
      sessionId: 'sess-race',
      role: 'assistant',
      status: 'streaming',
      createdAt: '2026-05-31T03:05:00.000Z',
      updatedAt: '2026-05-31T03:05:01.000Z',
      source: { inboundSeq: 1 },
      blocks: [{
        id: 'message:conv-race:live-assistant|block:text:live',
        kind: 'text',
        text: 'live stream',
        delivery: 'stream',
        streaming: true,
        createdAt: '2026-05-31T03:05:00.000Z',
        updatedAt: '2026-05-31T03:05:01.000Z',
        eventIds: ['live-ev-1'],
      }],
      eventIds: ['live-ev-1'],
    };

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-race/history')) {
        return jsonResponse([
          { id: 'old-user', role: 'user', content: 'old message', message_type: 'user_input', created_at: '2026-05-31T03:00:00.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-race', sessionId: 'sess-race', title: 'Race test', updatedAt: '2026-05-31T03:05:00.000Z' }],
      activeConversationId: 'conv-race',
      isGenerating: true,
      generatingBySession: { ...state.generatingBySession, 'sess-race': true },
      messagesById: { ...state.messagesById, [liveMessage.id]: liveMessage },
      messageOrderByConversation: { ...state.messageOrderByConversation, 'conv-race': [liveMessage.id] },
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-race': 'conv-race' },
    }));

    useChatStore.getState().selectConversation('conv-race');

    // Why: selecting a generating conversation can finish history loading after
    // WebSocket stream blocks already exist. How: this assertion requires hydration
    // to keep the live message while adding history. Purpose: avoid losing active
    // output when an older history response arrives late.
    await waitFor(() => expect(selectMessages(useChatStore.getState(), 'conv-race')).toHaveLength(2));
    expect(useChatStore.getState().messagesById[liveMessage.id]).toBeDefined();
    expect(selectMessages(useChatStore.getState(), 'conv-race').some((message) => message.id === liveMessage.id)).toBe(true);
  });

  it('skips duplicate history rows when preserving live streamed messages', async () => {
    const liveUser: WsMessage = {
      id: 'message:conv-dupe:user:inbound:1',
      conversationId: 'conv-dupe',
      sessionId: 'sess-dupe',
      role: 'user',
      status: 'completed',
      createdAt: '2026-05-31T03:06:00.000Z',
      updatedAt: '2026-05-31T03:06:00.000Z',
      source: { inboundSeq: 1 },
      blocks: [{
        id: 'message:conv-dupe:user:inbound:1|block:text:live',
        kind: 'text',
        text: 'question',
        delivery: 'final',
        streaming: false,
        createdAt: '2026-05-31T03:06:00.000Z',
        updatedAt: '2026-05-31T03:06:00.000Z',
        eventIds: ['live-inbound-1'],
      }],
      eventIds: ['live-inbound-1'],
    };
    const liveAssistant: WsMessage = {
      id: 'message:conv-dupe:assistant:inbound:1',
      conversationId: 'conv-dupe',
      sessionId: 'sess-dupe',
      role: 'assistant',
      status: 'streaming',
      createdAt: '2026-05-31T03:06:01.000Z',
      updatedAt: '2026-05-31T03:06:02.000Z',
      source: { inboundSeq: 1 },
      blocks: [{
        id: 'message:conv-dupe:assistant:inbound:1|block:text:live',
        kind: 'text',
        text: 'same answer',
        delivery: 'stream',
        streaming: true,
        createdAt: '2026-05-31T03:06:01.000Z',
        updatedAt: '2026-05-31T03:06:02.000Z',
        eventIds: ['live-stream-1'],
      }],
      eventIds: ['live-stream-1'],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-dupe/history')) {
        return jsonResponse([
          { id: 'hist-user-1', role: 'user', content: 'question', message_type: 'user_input', created_at: '2026-05-31T03:06:00.000Z' },
          { id: 'hist-assistant-1', role: 'assistant', content: 'same answer', message_type: 'assistant', created_at: '2026-05-31T03:06:02.000Z' },
        ]);
      }
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const initialUpdatedAt = '2026-05-31T03:06:00.000Z';
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-dupe', sessionId: 'sess-dupe', title: 'Dupe test', updatedAt: initialUpdatedAt }],
      activeConversationId: 'conv-dupe',
      isGenerating: true,
      generatingBySession: { ...state.generatingBySession, 'sess-dupe': true },
      messagesById: { ...state.messagesById, [liveUser.id]: liveUser, [liveAssistant.id]: liveAssistant },
      messageOrderByConversation: { ...state.messageOrderByConversation, 'conv-dupe': [liveUser.id, liveAssistant.id] },
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-dupe': 'conv-dupe' },
    }));

    useChatStore.getState().selectConversation('conv-dupe');

    await waitFor(() => {
      expect(useChatStore.getState().conversations.find((conversation) => conversation.id === 'conv-dupe')?.updatedAt).not.toBe(initialUpdatedAt);
    });

    // Why: active history hydration preserves live stream cards instead of clearing
    // the conversation. How: this regression fixture returns persisted rows that
    // mirror existing live messages. Purpose: selecting back into a running session
    // must not show both delivery=stream and delivery=history copies.
    // [2026-06-03] Why: history loading now also asks the child-session registry to
    // rebuild childNodes after refresh. How: assert the specific history endpoint was
    // still called exactly once and allow the additional /children request. Purpose:
    // this duplicate-history regression test remains focused on message dedupe.
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/v1/sessions/sess-dupe/history'))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/v1/sessions/sess-dupe/children'))).toHaveLength(1);
    const messages = selectMessages(useChatStore.getState(), 'conv-dupe');
    expect(messages).toHaveLength(2);
    expect(messages.map((message) => message.id)).toEqual([liveUser.id, liveAssistant.id]);
  });
});

describe('Step 2A layout and event log panel', () => {
  beforeEach(() => {
    useChatStore.getState().resetState();
  });

  afterEach(() => {
    useChatStore.getState().resetState();
  });

  it('keeps AppLayout compatible while rendering the optional log slot in the right rail', () => {
    render(
      <AppLayout
        composer={<div>composer</div>}
        header={<div>header</div>}
        logPanel={<div>log body</div>}
        sidebar={<div>sidebar</div>}
      >
        <div>chat body</div>
      </AppLayout>,
    );

    expect(screen.getByText('chat body')).toBeInTheDocument();
    expect(screen.getByText('log body')).toBeInTheDocument();
    // Why: the event log moved from a bottom strip into the desktop right rail.
    // How: AppLayout exposes the rail with an accessible label instead of the old
    // placeholder. Purpose: tests protect the layout contract that carries EventLogPanel.
    expect(screen.getByLabelText('事件日志面板')).toBeInTheDocument();
    expect(screen.queryByLabelText('状态面板占位')).not.toBeInTheDocument();
  });

  it('renders the active session event log as timestamped monospace rows', () => {
    useChatStore.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-log', sessionId: 'sess-log', title: 'Log test', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-log',
      eventLog: [
        {
          id: 'log-1',
          eventId: 'event-1',
          seq: 1,
          ts: '2026-05-31T03:20:30.000Z',
          sessionId: 'sess-log',
          conversationId: 'conv-log',
          type: 'stream_delta',
          payload: { content: 'hello' },
          summary: 'hello',
        },
      ],
    }));

    render(<EventLogPanel />);

    expect(screen.getByText(/\[stream_delta\]/)).toBeInTheDocument();
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /折叠|展开/ })).not.toBeInTheDocument();
  });
});

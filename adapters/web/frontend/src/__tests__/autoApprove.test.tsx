// [2026-06-01] Auto-approval tests for browser-local tool rules.
// Why: frontend builds can allow selected low-risk tools without changing backend policy.
// How: replay an approval event through chatStore and assert decideApproval is called.
// Purpose: approval automation remains tied to clientPrefsStore and visible tool cards.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToolCallCard } from '../components/chat/v2';
import * as supervisorClient from '../api/supervisorClient';
import { useClientPrefsStore } from '../store/clientPrefsStore';
import { useChatStore } from '../store/chatStore';
import type { SupervisorEvent } from '../types/chat';
import type { ToolExecution } from '../types/message';

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

const now = '2026-06-01T14:10:00.000Z';

function event(seq: number, type: string, payload: Record<string, unknown>): SupervisorEvent {
  return { seq, event_id: `auto-${seq}`, ts: now, session_id: 'sess-auto', type, payload };
}

function baseTool(overrides: Partial<ToolExecution> = {}): ToolExecution {
  return {
    stableId: 'tool-auto',
    messageId: 'msg-auto',
    blockId: 'block-tool',
    id: 'call-auto',
    name: 'read_file',
    status: 'awaiting_approval',
    arguments: { path: 'README.md' },
    argumentsText: '{"path":"README.md"}',
    approvalId: 'approval-auto',
    approvalStatus: 'pending',
    approvalDetails: { operation: 'read_file', details: { path: 'README.md' } },
    createdAt: now,
    updatedAt: now,
    eventIds: ['ev-tool'],
    ...overrides,
  };
}

describe('client auto-approval', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useChatStore.getState().resetState();
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    useChatStore.getState().resetState();
    useClientPrefsStore.getState().resetClientPrefs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('automatically allows configured approval requests from the WebSocket stream', async () => {
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('read file', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'read file' }));
    ws.receive(event(2, 'tool_call_start', { source_inbound_seq: 1, tool_call_id: 'call-auto', tool_name: 'read_file', arguments: { path: 'README.md' } }));
    ws.receive(event(3, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-auto', tool_call_id: 'call-auto', operation: 'read_file', details: { path: 'README.md' }, status: 'pending' }));

    await waitFor(() => expect(approvalSpy).toHaveBeenCalledWith('', 'approval-auto', 'allow', 'auto-approved by client preference'));

    // [2026-06-02] Why: auto-approved ids used to be module-only state and were lost
    // after a browser refresh. How: assert the dedicated localStorage cache receives
    // the id when the store submits an automatic approval. Purpose: refreshed clients
    // keep treating the same pending request as already handled locally.
    expect(JSON.parse(localStorage.getItem('clonoth_auto_approved_ids') || '[]')).toContain('approval-auto');
  });

  it('removes a failed automatic approval id from localStorage', async () => {
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockRejectedValue(new Error('network down'));
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('read file', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'read file' }));
    ws.receive(event(2, 'tool_call_start', { source_inbound_seq: 1, tool_call_id: 'call-auto', tool_name: 'read_file', arguments: { path: 'README.md' } }));
    ws.receive(event(3, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-auto', tool_call_id: 'call-auto', operation: 'read_file', details: { path: 'README.md' }, status: 'pending' }));

    await waitFor(() => expect(approvalSpy).toHaveBeenCalledWith('', 'approval-auto', 'allow', 'auto-approved by client preference'));
    await waitFor(() => expect(JSON.parse(localStorage.getItem('clonoth_auto_approved_ids') || '[]')).not.toContain('approval-auto'));
  });

  it('shows auto-approved pending tool approvals as a badge instead of manual buttons', () => {
    useClientPrefsStore.getState().setAutoApproveTool('read_file', true);

    render(<ToolCallCard tool={baseTool()} />);

    expect(screen.getByText('已自动放行')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /允许/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /拒绝/ })).not.toBeInTheDocument();
  });

  it('keeps manual buttons for tools not configured for auto approval', () => {
    render(<ToolCallCard tool={baseTool({ name: 'execute_command', approvalDetails: { operation: 'execute_command', details: {} } })} />);

    expect(screen.getByRole('button', { name: /允许/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /拒绝/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /允许/ }));
  });

  it('does not auto-approve another tool that reports the same policy operation', async () => {
    // apply_diff 送审时报的 op 就是 write_file，勾 write_file 不该把它一起放掉。
    useClientPrefsStore.getState().setAutoApproveTool('write_file', true);
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('patch file', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'patch file' }));
    ws.receive(event(2, 'tool_call_start', { source_inbound_seq: 1, tool_call_id: 'call-diff', tool_name: 'apply_diff', arguments: { path: 'a.ts' } }));
    ws.receive(event(3, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-diff', tool_call_id: 'call-diff', operation: 'write_file', details: { path: 'a.ts' }, status: 'pending' }));

    await Promise.resolve();
    expect(approvalSpy).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem('clonoth_auto_approved_ids') || '[]')).not.toContain('approval-diff');
  });

  it('does not auto-approve a read-only tool that switches to a write operation', async () => {
    // search_in_files 的 replace 模式报 write_file，而 search_in_files 是默认勾上的。
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('replace', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'replace' }));
    ws.receive(event(2, 'tool_call_start', { source_inbound_seq: 1, tool_call_id: 'call-replace', tool_name: 'search_in_files', arguments: { query: 'a', replace: 'b' } }));
    ws.receive(event(3, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-replace', tool_call_id: 'call-replace', operation: 'write_file', details: { path: '(search_replace)' }, status: 'pending' }));

    await Promise.resolve();
    expect(approvalSpy).not.toHaveBeenCalled();
  });

  it('auto-approves a tool whose policy operation is not its own name', async () => {
    // list_dir 报的是 read_file，改用工具名匹配之后这个默认勾选才第一次真正生效。
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('list', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'list' }));
    ws.receive(event(2, 'tool_call_start', { source_inbound_seq: 1, tool_call_id: 'call-list', tool_name: 'list_dir', arguments: { path: 'src' } }));
    ws.receive(event(3, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-list', tool_call_id: 'call-list', operation: 'read_file', details: { path: 'src' }, status: 'pending' }));

    await waitFor(() => expect(approvalSpy).toHaveBeenCalledWith('', 'approval-list', 'allow', 'auto-approved by client preference'));
  });

  it('stays manual when the approval cannot be traced back to a tool card', async () => {
    const approvalSpy = vi.spyOn(supervisorClient, 'decideApproval').mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/inbound')) {
        return new Response(JSON.stringify({ session_id: 'sess-auto', inbound_seq: 1, accepted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));

    const conversationId = useChatStore.getState().createConversation();
    await useChatStore.getState().sendMessage('read file', undefined, 'ereuna_main');
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.receive(event(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'read file' }));
    ws.receive(event(2, 'approval_requested', { source_inbound_seq: 1, approval_id: 'approval-orphan', operation: 'read_file', details: { path: 'README.md' }, status: 'pending' }));

    await Promise.resolve();
    expect(approvalSpy).not.toHaveBeenCalled();
  });

  it('renders the auto-approved badge only when the store would also allow it', () => {
    useClientPrefsStore.getState().setAutoApproveTool('write_file', true);

    // 卡片上的灰显判断跟真正发 allow 的判断必须落在同一个键上。
    render(<ToolCallCard tool={baseTool({ name: 'apply_diff', approvalDetails: { operation: 'write_file', details: {} } })} />);

    expect(screen.queryByText('已自动放行')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /允许/ })).toBeInTheDocument();
  });
});

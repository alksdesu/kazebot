// [2026-06-03] Child-node UI tests are added before the rendering implementation.
// Why: Phase 2 must expose child sessions in both the left tree and the chat-side
// floating panel without changing backend data flow. How: seed chatStore.childNodes
// directly and render the public components. Purpose: lock the visual contract before
// implementing the Sidebar tree rows, shared status dot, and ChildNodePanel timer.
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChildNodePanel } from '../components/chat/v2/ChildNodePanel';
import { Sidebar } from '../components/layout/Sidebar';
import { useChatStore, type ChildNodeState, type ConversationMeta } from '../store/chatStore';

const conversation: ConversationMeta = {
  id: 'conv-1',
  sessionId: 'parent-session-1',
  title: '父对话',
  updatedAt: '2026-06-03T12:00:00.000Z',
};

const otherConversation: ConversationMeta = {
  id: 'conv-2',
  sessionId: 'parent-session-2',
  title: '其他对话',
  updatedAt: '2026-06-03T12:05:00.000Z',
};

function seedChildNodes(children: ChildNodeState[]) {
  // [2026-06-03] Tests write childNodes through Zustand setState instead of websocket
  // fixtures. Why: Phase 2 is only a renderer and must not depend on backend event
  // routing. How: key each child by sessionId, matching chatStore's normalized map.
  // Purpose: component tests stay focused on grouping, status labels, and timing.
  useChatStore.setState({
    childNodes: Object.fromEntries(children.map((child) => [child.sessionId, child])),
  });
}

describe('child node UI', () => {
  beforeEach(() => {
    useChatStore.getState().resetState();
    vi.setSystemTime(new Date('2026-06-03T12:02:15.000Z'));
  });

  afterEach(() => {
    useChatStore.getState().resetState();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('renders child sessions under their parent conversation in the Sidebar', () => {
    seedChildNodes([
      {
        sessionId: 'child-session-1',
        nodeId: 'scout',
        parentConversationId: 'conv-1',
        status: 'running',
        startedAt: '2026-06-03T12:00:00.000Z',
      },
      {
        sessionId: 'child-session-2',
        nodeId: 'smith',
        parentConversationId: 'conv-2',
        status: 'completed',
        startedAt: '2026-06-03T12:01:00.000Z',
      },
    ]);

    render(
      <Sidebar
        activeConversationId="conv-1"
        conversations={[conversation]}
        onCreateConversation={() => undefined}
        onDeleteConversation={() => undefined}
        onSelectConversation={() => undefined}
      />,
    );

    expect(screen.getByText('父对话')).toBeInTheDocument();
    expect(screen.getByText('scout')).toBeInTheDocument();
    expect(screen.queryByText('smith')).not.toBeInTheDocument();
    expect(screen.getByLabelText('子节点 scout 状态：运行中')).toHaveClass('bg-green-500', 'animate-pulse');
  });

  it('shows the current conversation child nodes in a floating panel with live runtime labels and opens child session view', () => {
    // [2026-06-03] Why: Phase 3 turns the Phase 2 passive row into real navigation.
    // How: mock the child history endpoint and assert the store enters child-session
    // view after clicking a panel row. Purpose: future changes cannot regress the
    // child chat stream entry point back to a console-only placeholder.
    vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })));
    seedChildNodes([
      {
        sessionId: 'child-session-1',
        nodeId: 'scout',
        parentConversationId: 'conv-1',
        status: 'running',
        startedAt: '2026-06-03T12:00:00.000Z',
      },
      {
        sessionId: 'child-session-2',
        nodeId: 'approver',
        parentConversationId: 'conv-1',
        status: 'awaiting_approval',
        startedAt: '2026-06-03T12:01:15.000Z',
      },
      {
        sessionId: 'child-session-3',
        nodeId: 'other',
        parentConversationId: 'conv-2',
        status: 'running',
        startedAt: '2026-06-03T12:00:30.000Z',
      },
    ]);

    render(<ChildNodePanel conversationId="conv-1" />);

    expect(screen.getByText('子节点')).toBeInTheDocument();
    expect(screen.getByText(/scout · 2m 15s/)).toBeInTheDocument();
    expect(screen.getByText(/approver · 1m 0s/)).toBeInTheDocument();
    expect(screen.getByText('运行中')).toBeInTheDocument();
    expect(screen.getByText('等待审批')).toBeInTheDocument();
    expect(screen.queryByText('other')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /打开子节点 scout/i }));
    expect(useChatStore.getState().viewingChildSessionId).toBe('child-session-1');
  });

  it('opens child session view from the Sidebar child row', () => {
    // [2026-06-03] Why: the left conversation tree is the second Phase 3 navigation
    // entry. How: render a child row and click it with a mocked history response.
    // Purpose: the sidebar and floating panel share the same store-backed navigation.
    vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })));
    seedChildNodes([{
      sessionId: 'child-session-1',
      nodeId: 'scout',
      parentConversationId: 'conv-1',
      status: 'running',
      startedAt: '2026-06-03T12:00:00.000Z',
    }]);

    render(
      <Sidebar
        activeConversationId="conv-1"
        conversations={[conversation]}
        onCreateConversation={() => undefined}
        onDeleteConversation={() => undefined}
        onSelectConversation={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '子节点 scout' }));

    expect(useChatStore.getState().viewingChildSessionId).toBe('child-session-1');
  });

  it('does not render the floating panel when the conversation has no child nodes', () => {
    seedChildNodes([
      {
        sessionId: 'child-session-3',
        nodeId: 'other',
        parentConversationId: 'conv-2',
        status: 'running',
        startedAt: '2026-06-03T12:00:30.000Z',
      },
    ]);

    const { container } = render(<ChildNodePanel conversationId="conv-1" />);

    expect(container).toBeEmptyDOMElement();
  });
});

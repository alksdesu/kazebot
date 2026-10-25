// 全局事件流带着所有会话的流式片段：别的会话来事件时，打开着的这个会话用到的那几片状态不换对象，跟着它的组件不重渲染。
import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useShallow } from 'zustand/react/shallow';

import { useChatStore } from '../store/chatStore';
import { createInitialChatState, reduceChatEvent } from '../store/eventReducer';
import { selectMessages, selectToolsForMessages } from '../store/eventSelectors';
import type { SupervisorEvent } from '../types/chat';
import type { ChatState, ToolExecution, WsMessage } from '../types/message';

const AT = '2026-05-31T03:00:00.000Z';

const tool = (stableId: string, messageId: string, status: ToolExecution['status'] = 'running'): ToolExecution => ({
  stableId,
  messageId,
  name: 'read_file',
  status,
  createdAt: AT,
  updatedAt: AT,
  eventIds: [`${stableId}-ev`],
});

const withTool = (id: string, conversationId: string, toolId: string): WsMessage => ({
  id,
  conversationId,
  sessionId: `sess-${conversationId}`,
  role: 'assistant',
  status: 'running_tools',
  createdAt: AT,
  updatedAt: AT,
  source: {},
  blocks: [{ id: `${id}|block:tool`, kind: 'tool', toolIds: [toolId], createdAt: AT, updatedAt: AT, eventIds: [`${id}-ev`] }],
  eventIds: [`${id}-ev`],
});

function twoConversations(): ChatState {
  const a = withTool('msg-a', 'conv-a', 'tool-a');
  const b = withTool('msg-b', 'conv-b', 'tool-b');
  return {
    ...createInitialChatState(),
    messagesById: { [a.id]: a, [b.id]: b },
    messageOrderByConversation: { 'conv-a': [a.id], 'conv-b': [b.id] },
    toolExecutionsById: { 'tool-a': tool('tool-a', a.id), 'tool-b': tool('tool-b', b.id) },
    toolExecutionOrder: ['tool-a', 'tool-b'],
    conversationIdsBySession: { 'sess-conv-a': 'conv-a', 'sess-conv-b': 'conv-b' },
  };
}

const streamDelta = (sessionId: string, seq: number): SupervisorEvent => ({
  seq,
  event_id: `iso-ev-${seq}`,
  ts: AT,
  session_id: sessionId,
  type: 'stream_delta',
  payload: { source_inbound_seq: 1, type: 'text', content: `piece ${seq}` },
});

afterEach(() => {
  useChatStore.getState().resetState();
});

describe('别的会话的事件不碰打开着的会话', () => {
  it('工具只取这几张卡片指着的，别的会话里工具更新时结果逐项不变', () => {
    const state = twoConversations();
    const messages = selectMessages(state, 'conv-a');
    const before = selectToolsForMessages(state, messages);

    const next = { ...state, toolExecutionsById: { ...state.toolExecutionsById, 'tool-b': tool('tool-b', 'msg-b', 'success') } };
    const after = selectToolsForMessages(next, selectMessages(next, 'conv-a'));

    expect(Object.keys(before)).toEqual(['tool-a']);
    expect(after['tool-a']).toBe(before['tool-a']);
    expect(Object.keys(after)).toEqual(['tool-a']);
  });

  it('另一个会话的流式片段不重建这个会话的消息和顺序', () => {
    const state = twoConversations();

    const next = reduceChatEvent(state, streamDelta('sess-conv-b', 1));

    expect(next).not.toBe(state);
    expect(next.messageOrderByConversation['conv-a']).toBe(state.messageOrderByConversation['conv-a']);
    expect(next.messagesById['msg-a']).toBe(state.messagesById['msg-a']);
    expect(next.toolExecutionsById['tool-a']).toBe(state.toolExecutionsById['tool-a']);
  });

  it('跟着这个会话工具卡的组件，别的会话来事件时不重渲染', () => {
    useChatStore.setState(twoConversations());
    const messages = selectMessages(useChatStore.getState(), 'conv-a');
    let renders = 0;
    const Probe = () => {
      useChatStore(useShallow((state) => selectToolsForMessages(state, messages)));
      renders += 1;
      return null;
    };
    render(<Probe />);
    const settled = renders;

    act(() => {
      useChatStore.setState((state) => ({ toolExecutionsById: { ...state.toolExecutionsById, 'tool-b': tool('tool-b', 'msg-b', 'success') } }));
    });
    expect(renders).toBe(settled);

    act(() => {
      useChatStore.setState((state) => ({ toolExecutionsById: { ...state.toolExecutionsById, 'tool-a': tool('tool-a', 'msg-a', 'success') } }));
    });
    expect(renders).toBe(settled + 1);
  });
});

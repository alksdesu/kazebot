// 历史和会话列表的分页、失败提示与重试：读失败要说出来并能重试，往前翻接在最前面，会话列表能接着往下加载。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MessageListV2 } from '../components/chat/v2';
import { useChatStore, type HistoryView } from '../store/chatStore';
import { selectMessages } from '../store/eventSelectors';
import { useSettingsStore } from '../store/settingsStore';
import type { WsMessage } from '../types/message';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send() {}

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

const json = (value: unknown, status = 200): Response => new Response(JSON.stringify(value), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const IDLE: HistoryView = { loading: false, error: '', hasMore: false, loadingOlder: false, olderError: '', childrenError: '' };

const message = (id: string, role: WsMessage['role'], text: string, createdAt = '2026-05-31T03:00:00.000Z'): WsMessage => ({
  id,
  conversationId: 'conv-p',
  sessionId: 'sess-p',
  role,
  status: 'completed',
  createdAt,
  updatedAt: createdAt,
  source: {},
  blocks: [{ id: `${id}|block:text`, kind: 'text', text, delivery: 'final', streaming: false, createdAt, updatedAt: createdAt, eventIds: [`${id}-ev`] }],
  eventIds: [`${id}-ev`],
});

const textOf = (item: WsMessage): string => item.blocks.map((block) => (block.kind === 'text' ? block.text : '')).join('');

const historyRow = (cursor: number, role: 'user' | 'assistant', content: string) => ({
  id: `row-${cursor}`,
  cursor,
  role,
  content,
  message_type: role === 'user' ? 'user_input' : 'assistant',
  created_at: `2026-05-31T03:${String(Math.floor(cursor / 60) % 60).padStart(2, '0')}:${String(cursor % 60).padStart(2, '0')}.000Z`,
});

describe('消息列表的历史提示', () => {
  it('还没有消息时先说正在加载', () => {
    render(<MessageListV2 messages={[]} toolsById={{}} history={{ ...IDLE, loading: true }} />);

    expect(screen.getByRole('status')).toHaveTextContent('正在加载历史消息…');
  });

  it('一条都没读到又失败了，说出原因并能重试', () => {
    const retry = vi.fn();
    render(<MessageListV2 messages={[]} toolsById={{}} history={{ ...IDLE, error: '503 历史挂了' }} onRetryHistory={retry} />);

    expect(screen.getByRole('alert')).toHaveTextContent('历史消息加载失败：503 历史挂了');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it('已有消息时刷新失败，消息留着，上面说一声', () => {
    render(<MessageListV2 messages={[message('m1', 'user', '还在这里')]} toolsById={{}} history={{ ...IDLE, error: '503 历史挂了' }} onRetryHistory={() => {}} />);

    expect(screen.getByRole('alert')).toHaveTextContent('历史消息刷新失败：503 历史挂了');
    expect(screen.getByText('还在这里')).toBeInTheDocument();
  });

  it('还有更早的就给一个往前翻的按钮', () => {
    const older = vi.fn();
    render(<MessageListV2 messages={[message('m1', 'user', 'hi')]} toolsById={{}} history={{ ...IDLE, hasMore: true }} onLoadOlder={older} />);

    fireEvent.click(screen.getByRole('button', { name: '加载更早的消息' }));
    expect(older).toHaveBeenCalledOnce();
  });

  it('往前翻失败了说原因，重试还是往前翻', () => {
    const older = vi.fn();
    render(<MessageListV2 messages={[message('m1', 'user', 'hi')]} toolsById={{}} history={{ ...IDLE, hasMore: true, olderError: '503 翻不动' }} onLoadOlder={older} />);

    expect(screen.getByRole('alert')).toHaveTextContent('503 翻不动');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(older).toHaveBeenCalledOnce();
  });

  it('翻到头了就什么都不摆', () => {
    render(<MessageListV2 messages={[message('m1', 'user', 'hi')]} toolsById={{}} history={IDLE} onLoadOlder={() => {}} />);

    expect(screen.queryByRole('button', { name: '加载更早的消息' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  // 子节点面板摆的是上一次读成的那份：不说一声，读失败看着就像这个会话没派出过子节点。
  it('子会话列表没读到时消息照常摆着，另起一行说原因，重试就重读整个会话', () => {
    const retry = vi.fn();
    render(<MessageListV2 messages={[message('m1', 'user', '还在这里')]} toolsById={{}} history={{ ...IDLE, childrenError: '503 子会话挂了' }} onRetryHistory={retry} />);

    expect(screen.getByRole('alert')).toHaveTextContent('子节点列表没读到：503 子会话挂了');
    expect(screen.getByText('还在这里')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it('还没有消息时子会话列表没读到也说出来，不只摆一句开始对话', () => {
    render(<MessageListV2 messages={[]} toolsById={{}} history={{ ...IDLE, childrenError: '503 子会话挂了' }} onRetryHistory={() => {}} />);

    expect(screen.getByRole('alert')).toHaveTextContent('子节点列表没读到：503 子会话挂了');
    expect(screen.getByText(/发送消息，开始对话/)).toBeInTheDocument();
  });
});

describe('会话历史的读取、往前翻与重试', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    useSettingsStore.setState({ adminToken: '' });
    useChatStore.getState().resetState();
  });

  afterEach(() => {
    useChatStore.getState().resetState();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const seed = (...messages: WsMessage[]) => useChatStore.setState((state) => ({
    ...state,
    conversations: [{ id: 'conv-p', sessionId: 'sess-p', title: 'Paging', updatedAt: '2026-05-31T03:00:00.000Z' }],
    activeConversationId: 'conv-p',
    messagesById: { ...state.messagesById, ...Object.fromEntries(messages.map((item) => [item.id, item])) },
    messageOrderByConversation: { ...state.messageOrderByConversation, 'conv-p': messages.map((item) => item.id) },
    conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-p': 'conv-p' },
  }));
  const view = () => useChatStore.getState().historyByConversation['conv-p'];
  const texts = () => selectMessages(useChatStore.getState(), 'conv-p').map(textOf);

  it('历史读失败说出原因，已有消息不动；重试读到了就换成历史', async () => {
    let fail = true;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-p/history')) {
        return fail ? json({ detail: '历史挂了' }, 503) : json([historyRow(0, 'user', 'question'), historyRow(1, 'assistant', 'answer')]);
      }
      if (url.includes('/running_tasks')) return json({ tasks: [] });
      return json([]);
    }));
    seed(message('local', 'user', 'question'));

    useChatStore.getState().selectConversation('conv-p');
    await waitFor(() => expect(view()).toMatchObject({ loading: false, error: '503 历史挂了' }));
    expect(texts()).toEqual(['question']);

    fail = false;
    useChatStore.getState().retryHistory();
    await waitFor(() => expect(view()).toMatchObject({ loading: false, error: '' }));
    expect(texts()).toEqual(['question', 'answer']);
  });

  it('子会话列表读失败记在这个会话的历史状态上，历史照常换上；重试读成了就清掉', async () => {
    let fail = true;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-p/history')) return json([historyRow(0, 'user', 'question')]);
      if (url.includes('/v1/sessions/sess-p/children')) return fail ? json({ detail: '子会话挂了' }, 503) : json([]);
      if (url.includes('/running_tasks')) return json({ tasks: [] });
      return json([]);
    }));
    seed();

    useChatStore.getState().selectConversation('conv-p');
    await waitFor(() => expect(view()).toMatchObject({ loading: false, error: '', childrenError: '503 子会话挂了' }));
    expect(texts()).toEqual(['question']);

    fail = false;
    useChatStore.getState().retryHistory();
    await waitFor(() => expect(view()).toMatchObject({ loading: false, error: '', childrenError: '' }));
  });

  it('子会话列表对不上形状也算没读成，不当成一个子会话都没有', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-p/history')) return json([]);
      if (url.includes('/v1/sessions/sess-p/children')) return json({ children: [] });
      if (url.includes('/running_tasks')) return json({ tasks: [] });
      return json([]);
    }));
    seed();

    useChatStore.getState().selectConversation('conv-p');

    await waitFor(() => expect(view()).toMatchObject({ loading: false, childrenError: '子会话列表格式不对：返回的不是数组' }));
  });

  it('往前翻带着最早一条的位置去读，读到的接在最前面，翻到头就不再有按钮', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-p/history')) {
        return url.includes('before=200')
          ? json([historyRow(0, 'user', 'q0'), historyRow(1, 'assistant', 'a1')])
          : json([historyRow(200, 'user', 'q200'), historyRow(201, 'assistant', 'a201')]);
      }
      if (url.includes('/running_tasks')) return json({ tasks: [] });
      return json([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    seed();

    useChatStore.getState().selectConversation('conv-p');
    await waitFor(() => expect(view()).toMatchObject({ loading: false, hasMore: true }));
    expect(texts()).toEqual(['q200', 'a201']);

    await useChatStore.getState().loadOlderHistory();

    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('before=200'))).toBe(true);
    expect(texts()).toEqual(['q0', 'a1', 'q200', 'a201']);
    expect(view()).toMatchObject({ loadingOlder: false, hasMore: false, olderError: '' });
  });

  it('往前翻失败说出原因，已经显示的消息不动', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-p/history')) {
        return url.includes('before=') ? json({ detail: '翻不动' }, 503) : json([historyRow(200, 'user', 'q200')]);
      }
      if (url.includes('/running_tasks')) return json({ tasks: [] });
      return json([]);
    }));
    seed();
    useChatStore.getState().selectConversation('conv-p');
    await waitFor(() => expect(view()).toMatchObject({ loading: false, hasMore: true }));

    await useChatStore.getState().loadOlderHistory();

    expect(view()).toMatchObject({ loadingOlder: false, hasMore: true, olderError: '503 翻不动' });
    expect(texts()).toEqual(['q200']);
  });
});

describe('会话列表分页', () => {
  const sessionRow = (index: number) => ({
    session_id: `s${index}`,
    conversation_key: `web:c${index}`,
    channel: 'web',
    created_at: '2026-05-31T00:00:00.000Z',
    updated_at: `2026-05-31T${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}:00.000Z`,
  });
  const page = (from: number, count: number) => Array.from({ length: count }, (_, offset) => sessionRow(from + offset));

  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    useSettingsStore.setState({ adminToken: '' });
    useChatStore.getState().resetState();
  });

  afterEach(() => {
    useChatStore.getState().resetState();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const serve = (more: () => Response) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?')) return url.includes('offset=50') ? more() : json(page(0, 50));
      return json([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  };

  it('第一页满了就还能接着加载，加载到的接在后面，不满一页就到头了', async () => {
    serve(() => json(page(50, 3)));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(useChatStore.getState().sessionPaging).toEqual({ hasMore: true, loading: false, error: '' }));
    expect(useChatStore.getState().conversations).toHaveLength(50);

    await useChatStore.getState().loadMoreConversations();

    expect(useChatStore.getState().conversations).toHaveLength(53);
    expect(useChatStore.getState().sessionPaging).toEqual({ hasMore: false, loading: false, error: '' });
  });

  it('加载更多失败时说原因，还能再点', async () => {
    serve(() => json({ detail: '列表挂了' }, 503));

    useChatStore.getState().loadStartup();
    await waitFor(() => expect(useChatStore.getState().sessionPaging.hasMore).toBe(true));

    await useChatStore.getState().loadMoreConversations();

    expect(useChatStore.getState().conversations).toHaveLength(50);
    expect(useChatStore.getState().sessionPaging).toEqual({ hasMore: true, loading: false, error: '503 列表挂了' });
  });
});

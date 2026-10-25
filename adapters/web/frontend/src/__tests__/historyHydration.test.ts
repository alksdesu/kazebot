// 历史和实时消息怎么拼：翻页窗口怎么接，实时卡片怎么认领它落盘后的那条记录，什么都没变时不换对象。
import { describe, expect, it } from 'vitest';

import type { StructuredMessage } from '../api/supervisorClient';
import { createInitialChatState } from '../store/eventReducer';
import {
  buildHistoryEntries,
  historyWindowOf,
  mergeHistoryEntries,
  mergeLatestHistoryPage,
  mergeOlderHistoryPage,
} from '../store/historyHydration';
import type { ChatState, WsMessage } from '../types/message';

const CONV = 'conv-h';
const SESSION = 'sess-h';

const row = (cursor: number, role: string, content: string, at = `2026-05-31T03:00:${String(cursor).padStart(2, '0')}.000Z`): StructuredMessage => ({
  id: `row-${cursor}`,
  cursor,
  role,
  content,
  message_type: role === 'user' ? 'user_input' : 'assistant',
  created_at: at,
});

const ids = (rows: readonly StructuredMessage[]): string[] => rows.map((item) => item.id);
const entriesOf = (rows: StructuredMessage[]) => buildHistoryEntries(rows, SESSION, CONV);
const allFresh = (rows: StructuredMessage[]) => new Set(ids(rows));

const live = (id: string, role: 'user' | 'assistant', text: string, createdAt: string): WsMessage => ({
  id,
  conversationId: CONV,
  sessionId: SESSION,
  role,
  status: 'completed',
  createdAt,
  updatedAt: createdAt,
  source: {},
  blocks: [{ id: `${id}|block:text`, kind: 'text', text, delivery: 'final', streaming: false, createdAt, updatedAt: createdAt, eventIds: [`${id}-ev`] }],
  eventIds: [`${id}-ev`],
});

function withLive(state: ChatState, ...messages: WsMessage[]): ChatState {
  return {
    ...state,
    messagesById: { ...state.messagesById, ...Object.fromEntries(messages.map((message) => [message.id, message])) },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [CONV]: [...(state.messageOrderByConversation[CONV] || []), ...messages.map((message) => message.id)],
    },
  };
}

describe('历史窗口', () => {
  it('第一条不在最开头就还有更早的', () => {
    expect(historyWindowOf([row(5, 'user', 'a')]).hasMore).toBe(true);
    expect(historyWindowOf([row(0, 'user', 'a')]).hasMore).toBe(false);
    // 老后端不给位置，没法往前翻。
    expect(historyWindowOf([{ id: 'old', role: 'user', content: 'a' }]).hasMore).toBe(false);
  });

  it('最新一页接得上已载入的更早记录就接上', () => {
    const previous = historyWindowOf([row(3, 'user', 'a'), row(4, 'assistant', 'b'), row(5, 'user', 'c')]);

    const merged = mergeLatestHistoryPage(previous, [row(5, 'user', 'c'), row(6, 'assistant', 'd')]);

    expect(ids(merged.rows)).toEqual(['row-3', 'row-4', 'row-5', 'row-6']);
    expect(merged.hasMore).toBe(true);
  });

  it('中间缺了一段就只留最新一页', () => {
    const previous = historyWindowOf([row(1, 'user', 'a'), row(2, 'assistant', 'b')]);

    expect(ids(mergeLatestHistoryPage(previous, [row(5, 'user', 'c')]).rows)).toEqual(['row-5']);
  });

  it('同一位置换了记录（历史被压缩重排过）也只留最新一页', () => {
    const previous = historyWindowOf([row(3, 'user', 'a'), row(4, 'assistant', 'b')]);
    const replaced = { ...row(4, 'assistant', 'b2'), id: 'other-4' };

    expect(ids(mergeLatestHistoryPage(previous, [replaced, row(5, 'user', 'c')]).rows)).toEqual(['other-4', 'row-5']);
  });

  it('往前翻到的一页接在最前面，已有的不重复', () => {
    const previous = historyWindowOf([row(4, 'user', 'c'), row(5, 'assistant', 'd')]);

    const merged = mergeOlderHistoryPage(previous, [row(2, 'user', 'a'), row(3, 'assistant', 'b'), row(4, 'user', 'c')]);

    expect(ids(merged.rows)).toEqual(['row-2', 'row-3', 'row-4', 'row-5']);
    expect(merged.hasMore).toBe(true);
  });

  it('翻到头了就不再显示还有更早的', () => {
    const previous = historyWindowOf([row(2, 'user', 'c')]);

    expect(mergeOlderHistoryPage(previous, [row(0, 'user', 'a'), row(1, 'assistant', 'b')]).hasMore).toBe(false);
  });

  it('往前翻却没拿到更早的，别再让人翻', () => {
    const previous = historyWindowOf([row(4, 'user', 'c')]);

    expect(mergeOlderHistoryPage(previous, [row(4, 'user', 'c')])).toEqual({ rows: previous.rows, hasMore: false });
  });
});

describe('历史与实时消息合并', () => {
  it('同样的历史再合一次，什么都没变就不换对象', () => {
    const rows = [row(0, 'user', 'question'), row(1, 'assistant', 'answer')];
    const first = mergeHistoryEntries(createInitialChatState(), CONV, entriesOf(rows), { keepLive: false, freshRowIds: allFresh(rows), anchors: new Map() });

    const again = mergeHistoryEntries(first.state, CONV, entriesOf(rows), { keepLive: false, freshRowIds: new Set(), anchors: first.anchors });

    expect(again.state).toBe(first.state);
  });

  it('不保留实时消息时，历史整体替换这个会话', () => {
    const stale = live('live-a', 'assistant', 'half', '2026-05-31T03:00:05.000Z');
    const rows = [row(0, 'user', 'question'), row(1, 'assistant', 'answer')];

    const { state } = mergeHistoryEntries(withLive(createInitialChatState(), stale), CONV, entriesOf(rows), { keepLive: false, freshRowIds: allFresh(rows), anchors: new Map() });

    expect(state.messagesById['live-a']).toBeUndefined();
    expect(state.messageOrderByConversation[CONV]).toEqual(entriesOf(rows).map((entry) => entry.message.id));
  });

  it('保留实时消息时，落盘的那几条由实时卡片认领，不重复显示', () => {
    const user = live('live-user', 'user', 'question', '2026-05-31T03:00:00.000Z');
    const answer = live('live-answer', 'assistant', 'answer', '2026-05-31T03:00:01.000Z');
    const rows = [row(0, 'user', 'older'), row(1, 'user', 'question'), row(2, 'assistant', 'answer')];
    const entries = entriesOf(rows);

    const { state, anchors } = mergeHistoryEntries(withLive(createInitialChatState(), user, answer), CONV, entries, { keepLive: true, freshRowIds: allFresh(rows), anchors: new Map() });

    expect(state.messageOrderByConversation[CONV]).toEqual([entries[0].message.id, 'live-user', 'live-answer']);
    expect(anchors).toEqual(new Map([['live-user', entries[1].message.id], ['live-answer', entries[2].message.id]]));
  });

  it('文字一样但比实时消息早得多，不算同一条', () => {
    const answer = live('live-answer', 'assistant', 'ok', '2026-05-31T04:00:00.000Z');
    const rows = [row(0, 'assistant', 'ok', '2026-05-31T03:00:00.000Z')];
    const entries = entriesOf(rows);

    const { state } = mergeHistoryEntries(withLive(createInitialChatState(), answer), CONV, entries, { keepLive: true, freshRowIds: allFresh(rows), anchors: new Map() });

    expect(state.messageOrderByConversation[CONV]).toEqual([entries[0].message.id, 'live-answer']);
  });

  it('认领关系跨次合并保留，旧记录不会被另一张同样文字的实时卡片抢走', () => {
    const first = live('live-1', 'assistant', 'ok', '2026-05-31T03:00:01.000Z');
    const rows = [row(0, 'user', 'q'), row(1, 'assistant', 'ok')];
    const entries = entriesOf(rows);
    const merged = mergeHistoryEntries(withLive(createInitialChatState(), first), CONV, entries, { keepLive: true, freshRowIds: allFresh(rows), anchors: new Map() });
    const second = live('live-2', 'assistant', 'ok', '2026-05-31T03:00:09.000Z');

    const next = mergeHistoryEntries(withLive(merged.state, second), CONV, entries, { keepLive: true, freshRowIds: new Set(), anchors: merged.anchors });

    expect(next.state.messageOrderByConversation[CONV]).toEqual([entries[0].message.id, 'live-1', 'live-2']);
  });
});

import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import * as api from '../api/supervisorClient';
import { ReceiptUnreadable } from '../api/supervisorClient';
import { useChatStore } from '../store/chatStore';
import { scopedKey } from '../store/storageKey';

vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(), connectGlobalWS: vi.fn(), disconnectGlobalWS: vi.fn(),
}));
vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(), postInbound: vi.fn(), uploadAttachment: vi.fn(),
  deleteSession: vi.fn(), cancelActiveTasks: vi.fn(), listSessions: vi.fn(), getSessionHistory: vi.fn(), getSessionChildren: vi.fn(),
}));
function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
const conversation = (id: string) => ({ id, sessionId: `session-${id}`, title: id, updatedAt: '2026-09-25T00:00:00Z' });
beforeEach(() => {
  useChatStore.getState().resetState(); vi.resetAllMocks();
  vi.mocked(api.getSessionHistory).mockResolvedValue([]); vi.mocked(api.getSessionChildren).mockResolvedValue([]);
  useChatStore.setState({ conversations: [conversation('a'), conversation('b')], activeConversationId: 'a', conversationIdsBySession: { 'session-a': 'a', 'session-b': 'b' } });
});
afterEach(() => { useChatStore.getState().resetState(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('rejects real HTTP and payload failures instead of treating them as an empty list or deleted session', async () => {
  const actual = await vi.importActual<typeof import('../api/supervisorClient')>('../api/supervisorClient');
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response('{"detail":"offline"}', { status: 503 }))
    .mockRejectedValueOnce(new Error('network down'))
    .mockResolvedValueOnce(new Response('{"not":"a list"}'))
    .mockResolvedValueOnce(new Response('{"ok":false}'))
    .mockResolvedValueOnce(new Response('{"detail":"denied"}', { status: 403 }))
    .mockResolvedValueOnce(new Response('{"ok":true}'));
  vi.stubGlobal('fetch', fetch);
  await expect(actual.listSessions()).rejects.toThrow('503');
  await expect(actual.listSessions()).rejects.toThrow('network down');
  await expect(actual.listSessions()).rejects.toThrow('会话列表格式不对：返回的不是数组');
  await expect(actual.deleteSession('a')).rejects.toThrow('未确认');
  await expect(actual.deleteSession('a')).rejects.toThrow('403');
  await expect(actual.deleteSession('a/b')).resolves.toEqual({ ok: true });
  expect(fetch.mock.calls.at(-1)?.[0]).toContain('/sessions/a%2Fb');
});

it('propagates inbound failure for the composer to keep its draft and clears only the originating sending flag', async () => {
  const request = deferred<Awaited<ReturnType<typeof api.postInbound>>>();
  vi.mocked(api.postInbound).mockReturnValue(request.promise);
  const send = useChatStore.getState().sendMessage('text');
  const rejected = expect(send).rejects.toThrow('offline');
  expect(useChatStore.getState().sendingByConversation.a).toBe(true);
  useChatStore.setState({ activeConversationId: 'b', generatingBySession: { 'session-b': true }, isGenerating: true });
  request.reject(new Error('offline')); await rejected;
  expect(useChatStore.getState().sendingByConversation.a).toBe(false);
  expect(useChatStore.getState().isGenerating).toBe(true);
});
it('does not send inbound after a failed upload and does not swallow upload error', async () => {
  vi.mocked(api.uploadAttachment).mockRejectedValue(new Error('upload unavailable'));
  await expect(useChatStore.getState().sendMessage('text', [{ name: 'x', file: new File(['x'], 'x') }])).rejects.toThrow('upload unavailable');
  expect(api.postInbound).not.toHaveBeenCalled(); expect(useChatStore.getState().isGenerating).toBe(false);
});
it('rejects duplicate sends and deletion while an HTTP send is pending', async () => {
  const request = deferred<Awaited<ReturnType<typeof api.postInbound>>>();
  vi.mocked(api.postInbound).mockReturnValue(request.promise);
  const first = useChatStore.getState().sendMessage('first');
  const rejected = expect(first).rejects.toThrow('offline');
  await expect(useChatStore.getState().sendMessage('duplicate')).rejects.toThrow('正在发送');
  await expect(useChatStore.getState().deleteConversation('a')).rejects.toThrow('发送结束');
  expect(api.postInbound).toHaveBeenCalledTimes(1); expect(api.deleteSession).not.toHaveBeenCalled();
  request.reject(new Error('offline')); await rejected;
});
it('keeps local messages and selected conversation when deletion is rejected', async () => {
  vi.mocked(api.deleteSession).mockRejectedValue(new Error('403'));
  await expect(useChatStore.getState().deleteConversation('a')).rejects.toThrow('403');
  expect(useChatStore.getState().conversations).toHaveLength(2);
  expect(useChatStore.getState().activeConversationId).toBe('a');
  expect(useChatStore.getState().conversationIdsBySession['session-a']).toBe('a');
});
it('blocks a send during deletion and keeps a newer session if the old deletion response arrives late', async () => {
  const request = deferred<{ ok: boolean }>();
  vi.mocked(api.deleteSession).mockReturnValue(request.promise);
  const deletion = useChatStore.getState().deleteConversation('a');
  const rejected = expect(deletion).rejects.toThrow('当前会话已变更');
  await expect(useChatStore.getState().sendMessage('keep this draft')).rejects.toThrow('正在删除');
  expect(api.postInbound).not.toHaveBeenCalled();
  useChatStore.setState(state => ({ conversations: state.conversations.map(item => item.id === 'a' ? { ...item, sessionId: 'new-session' } : item) }));
  request.resolve({ ok: true }); await rejected;
  expect(useChatStore.getState().conversations.find(item => item.id === 'a')?.sessionId).toBe('new-session');
  expect(useChatStore.getState().deletingByConversation.a).toBe(false);
});
it('does not remove a new identity conversation on an old delete response', async () => {
  const request = deferred<{ ok: boolean }>(); vi.mocked(api.deleteSession).mockReturnValue(request.promise);
  const deletion = useChatStore.getState().deleteConversation('a');
  const rejected = expect(deletion).rejects.toThrow('当前会话已变更');
  useChatStore.getState().resetState();
  useChatStore.setState({ conversations: [conversation('a')], activeConversationId: 'a' });
  request.resolve({ ok: true }); await rejected;
  expect(useChatStore.getState().conversations).toHaveLength(1);
});
it('keeps generating state on failed cancellation and never unlocks a different selected session', async () => {
  useChatStore.setState({ generatingBySession: { 'session-a': true, 'session-b': true }, isGenerating: true });
  vi.mocked(api.cancelActiveTasks).mockRejectedValueOnce(new Error('503'));
  await expect(useChatStore.getState().cancelCurrentTask()).rejects.toThrow('503');
  expect(useChatStore.getState().isGenerating).toBe(true);
  const request = deferred<api.CancelResult>(); vi.mocked(api.cancelActiveTasks).mockReturnValue(request.promise);
  const cancel = useChatStore.getState().cancelCurrentTask();
  useChatStore.setState({ activeConversationId: 'b' }); request.resolve({ ok: true, cancelled_count: 1 }); await cancel;
  expect(useChatStore.getState().generatingBySession['session-a']).toBe(false);
  expect(useChatStore.getState().isGenerating).toBe(true);
});
// 回执读不懂时服务端已经做完了：当成失败，人会照着再发、再删、再取消一次。
it('treats a sent message with an unreadable receipt as sent and hands the problem back', async () => {
  vi.mocked(api.postInbound).mockRejectedValue(new ReceiptUnreadable('消息已经发出，可回执格式不对：缺 session_id'));
  await expect(useChatStore.getState().sendMessage('text')).resolves.toBe('消息已经发出，可回执格式不对：缺 session_id');
  expect(useChatStore.getState().sendingByConversation.a).toBe(false);
  expect(useChatStore.getState().conversations.find(item => item.id === 'a')?.sessionId).toBe('session-a');
});
it('stops a send whose attachment upload receipt is unreadable and says the file is already on the server', async () => {
  vi.mocked(api.uploadAttachment).mockRejectedValue(new ReceiptUnreadable('附件 x 已经传上，可回执格式不对：缺 path'));
  await expect(useChatStore.getState().sendMessage('text', [{ name: 'x', file: new File(['x'], 'x') }]))
    .rejects.toThrow('附件 x 已经传上，可回执格式不对：缺 path。消息没有发出，重发会把附件再传一份');
  expect(api.postInbound).not.toHaveBeenCalled();
  expect(useChatStore.getState().sendingByConversation.a).toBe(false);
});
it('removes a deleted conversation even when the delete receipt is unreadable', async () => {
  vi.mocked(api.deleteSession).mockRejectedValue(new ReceiptUnreadable('会话已经删掉，可回执格式不对：缺 ok'));
  await expect(useChatStore.getState().deleteConversation('a')).resolves.toBe('会话已经删掉，可回执格式不对：缺 ok');
  expect(useChatStore.getState().conversations.map(item => item.id)).toEqual(['b']);
  expect(useChatStore.getState().conversationIdsBySession['session-a']).toBeUndefined();
});
it('unlocks the composer after a cancel request whose receipt is unreadable', async () => {
  useChatStore.setState({ generatingBySession: { 'session-a': true }, isGenerating: true });
  vi.mocked(api.cancelActiveTasks).mockRejectedValue(new ReceiptUnreadable('取消请求已经交上，可回执不是 JSON'));
  await expect(useChatStore.getState().cancelCurrentTask()).resolves.toBe('取消请求已经交上，可回执不是 JSON');
  expect(useChatStore.getState().generatingBySession['session-a']).toBe(false);
  expect(useChatStore.getState().isGenerating).toBe(false);
});
it('keeps only string titles from a damaged title cache', async () => {
  useChatStore.getState().resetState();
  localStorage.setItem(scopedKey('clonoth_conversation_titles'), JSON.stringify({ kept: 'Kept title', broken: 42 }));
  vi.mocked(api.listSessions).mockResolvedValue([
    { session_id: 'session-kept', conversation_key: 'web:kept', created_at: '', updated_at: '', channel: 'web' },
    { session_id: 'session-broken', conversation_key: 'web:broken', created_at: '', updated_at: '', channel: 'web' },
  ]);
  useChatStore.getState().loadStartup();
  await waitFor(() => expect(useChatStore.getState().conversations).toHaveLength(2));
  const titles = Object.fromEntries(useChatStore.getState().conversations.map(item => [item.id, item.title]));
  expect(titles.kept).toBe('Kept title');
  expect(titles.broken).not.toBe(42);
  expect(typeof titles.broken).toBe('string');
  localStorage.removeItem(scopedKey('clonoth_conversation_titles'));
});
it('resets only the exact confirmed conversation and session revision', () => {
  useChatStore.setState({ generatingBySession: { 'session-a': true, 'session-b': true }, isGenerating: true });
  useChatStore.getState().resetConversationView('a', 'obsolete-session');
  expect(useChatStore.getState().conversations[0].sessionId).toBe('session-a');
  useChatStore.getState().resetConversationView('a', 'session-a');
  expect(useChatStore.getState().conversations[0].sessionId).toBe('');
  expect(useChatStore.getState().conversations[1].sessionId).toBe('session-b');
  expect(useChatStore.getState().conversationIdsBySession).toEqual({ 'session-b': 'b' });
});
it('shows startup failure, allows retry, and retains a local conversation created during loading', async () => {
  useChatStore.getState().resetState();
  vi.mocked(api.listSessions).mockRejectedValueOnce(new Error('startup offline'));
  useChatStore.getState().loadStartup();
  await waitFor(() => expect(useChatStore.getState().startupError).toBe('startup offline'));
  const request = deferred<Awaited<ReturnType<typeof api.listSessions>>>();
  vi.mocked(api.listSessions).mockReturnValue(request.promise);
  useChatStore.getState().loadStartup(); useChatStore.getState().loadStartup();
  expect(api.listSessions).toHaveBeenCalledTimes(2);
  const id = useChatStore.getState().createConversation();
  request.resolve([{ session_id: 'restored-session', conversation_key: 'web:restored', created_at: '', updated_at: '', channel: 'web' }]);
  await waitFor(() => expect(useChatStore.getState().conversations).toHaveLength(2));
  expect(useChatStore.getState().activeConversationId).toBe(id);
  expect(useChatStore.getState().startupError).toBe('');
});
it('ignores a startup result from before reset or identity change', async () => {
  useChatStore.getState().resetState();
  const request = deferred<Awaited<ReturnType<typeof api.listSessions>>>();
  vi.mocked(api.listSessions).mockReturnValue(request.promise);
  useChatStore.getState().loadStartup(); useChatStore.getState().resetState();
  request.resolve([{ session_id: 'old', conversation_key: 'web:old', created_at: '', updated_at: '', channel: 'web' }]);
  await request.promise; await Promise.resolve();
  expect(useChatStore.getState().conversations).toEqual([]);
});

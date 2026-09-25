import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import * as api from '../api/supervisorClient';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import type { AppViewContext } from '../views/viewRegistry';

let context: AppViewContext;
vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  checkHealth: vi.fn(), getActiveNode: vi.fn(), resetConversation: vi.fn(),
}));
vi.mock('../views/viewRegistry', () => ({
  viewRegistry: { chat: {
    header: (ctx: AppViewContext) => { context = ctx; return <><span>{ctx.title}</span>{ctx.onReset && <button onClick={ctx.onReset}>打开重置</button>}<button onClick={ctx.onCancel} disabled={ctx.isCancelling}>停止任务</button></>; },
    sidebar: () => <button>导航</button>, main: () => <div>消息区域</div>,
  } },
}));

const originalActions = { loadStartup: useChatStore.getState().loadStartup, sendMessage: useChatStore.getState().sendMessage, cancelCurrentTask: useChatStore.getState().cancelCurrentTask, resetConversationView: useChatStore.getState().resetConversationView };
const send = vi.fn(); const cancel = vi.fn(); const clear = vi.fn();
beforeEach(() => {
  useChatStore.getState().resetState();
  useChatStore.setState({ conversations: [{ id: 'A', title: '对话A', sessionId: 'sid-A', updatedAt: '2026-09-25T00:00:00Z' }, { id: 'B', title: '对话B', sessionId: 'sid-B', updatedAt: '2026-09-25T00:00:00Z' }], activeConversationId: 'A',
    loadStartup: vi.fn().mockResolvedValue(undefined), sendMessage: send.mockReset().mockResolvedValue(undefined), cancelCurrentTask: cancel.mockReset().mockResolvedValue(undefined), resetConversationView: clear.mockReset() });
  useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
  useSettingsStore.setState({ isAuthenticated: true, storageWarning: '', activeNodeId: 'wrong-B-node', activeNodeSessionId: 'sid-B', entryNodeId: 'entry' });
  vi.mocked(api.checkHealth).mockResolvedValue({} as never);
  vi.mocked(api.getActiveNode).mockResolvedValue({ node_id: 'node-A', is_override: true, default_node_id: 'main' });
  vi.mocked(api.resetConversation).mockResolvedValue({} as never);
});
afterEach(() => { useChatStore.setState(originalActions); useChatStore.getState().resetState(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe('应用动作的目标与失败反馈', () => {
  it('健康检查超时取消且允许后续重试，卸载中止当前探测', async () => {
    vi.useFakeTimers(); const signals: AbortSignal[] = [];
    vi.mocked(api.checkHealth).mockImplementation(signal => new Promise((_, reject) => {
      signals.push(signal!); signal!.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    }));
    const view = render(<App />); expect(signals).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(signals[0].aborted).toBe(true); expect(signals.length).toBeGreaterThan(1);
    const last = signals[signals.length - 1]; view.unmount(); expect(last.aborted).toBe(true);
  });
  it('发送时读取当前会话的节点，绝不消费其他会话的缓存', async () => {
    render(<App />); await act(async () => { await context.onSendMessage('hello'); });
    expect(api.getActiveNode).toHaveBeenCalledWith('sid-A'); expect(send).toHaveBeenCalledWith('hello', undefined, 'node-A');
    expect(useSettingsStore.getState().activeNodeSessionId).toBe('sid-A');
  });
  it('等待节点期间切换会话，拒绝发送并保留调用方草稿', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.getActiveNode>>) => void;
    vi.mocked(api.getActiveNode).mockImplementation(() => new Promise(done => { resolve = done; }));
    render(<App />); const attempt = Promise.resolve(context.onSendMessage('keep-draft'));
    act(() => useChatStore.setState({ activeConversationId: 'B' }));
    await act(async () => { resolve({ node_id: 'node-A', is_override: true, default_node_id: 'main' }); await expect(attempt).rejects.toThrow('草稿已保留'); });
    expect(send).not.toHaveBeenCalled(); expect(useSettingsStore.getState().activeNodeSessionId).toBe('sid-B');
  });
  it('发送失败沿调用链抛出，不伪装成功', async () => {
    send.mockRejectedValue(new Error('synthetic send failure')); render(<App />);
    await act(async () => { await expect(context.onSendMessage('keep')).rejects.toThrow('synthetic send failure'); });
  });
  it('重置需要确认，失败不清空，重试成功只清捕获的原会话', async () => {
    vi.mocked(api.resetConversation).mockRejectedValueOnce(new Error('synthetic reset failure')); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '打开重置' })); expect(api.resetConversation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认重置' }));
    await waitFor(() => expect(screen.getAllByRole('alert').some(item => item.textContent?.includes('synthetic reset failure'))).toBe(true));
    expect(clear).not.toHaveBeenCalled(); expect(screen.getByRole('dialog', { name: '重置对话' })).toBeInTheDocument();
    act(() => useChatStore.setState({ activeConversationId: 'B' }));
    fireEvent.click(screen.getByRole('button', { name: '确认重置' }));
    await waitFor(() => expect(clear).toHaveBeenCalledWith('A', 'sid-A'));
    expect(api.resetConversation).toHaveBeenLastCalledWith('web:A'); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('确认前原会话已被替换，不重置同ID的新上下文', async () => {
    render(<App />); fireEvent.click(screen.getByRole('button', { name: '打开重置' }));
    act(() => useChatStore.setState({ conversations: [{ id: 'A', title: '新上下文', sessionId: 'new-sid', updatedAt: '2026-09-25T00:00:00Z' }] }));
    fireEvent.click(screen.getByRole('button', { name: '确认重置' }));
    await waitFor(() => expect(screen.getAllByRole('alert').some(item => item.textContent?.includes('会话已变化'))).toBe(true));
    expect(api.resetConversation).not.toHaveBeenCalled(); expect(clear).not.toHaveBeenCalled();
  });
  it('子会话只读且不提供父会话重置或取消', async () => {
    useChatStore.setState({ viewingChildSessionId: 'child' }); render(<App />);
    expect(context.onReset).toBeUndefined(); await expect(context.onSendMessage('not sent')).rejects.toThrow('查看模式');
    await act(async () => context.onCancel()); expect(cancel).not.toHaveBeenCalled();
  });
  it('取消中禁止重复操作，失败显示错误而不声称成功', async () => {
    let reject!: (reason: Error) => void; cancel.mockImplementation(() => new Promise((_, fail) => { reject = fail; })); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '停止任务' })); expect(screen.getByRole('button', { name: '停止任务' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '停止任务' })); expect(cancel).toHaveBeenCalledOnce();
    await act(async () => reject(new Error('synthetic cancel failure')));
    expect(screen.getByRole('alert')).toHaveTextContent('synthetic cancel failure'); expect(screen.queryByText('已提交取消请求。')).not.toBeInTheDocument();
  });
});

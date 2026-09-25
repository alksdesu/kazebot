import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Sidebar } from '../components/layout/Sidebar';
import { useChatStore } from '../store/chatStore';

const originalLoad = useChatStore.getState().loadStartup;
afterEach(() => { useChatStore.setState({ startupError: '', loadStartup: originalLoad }); });

describe('会话列表加载失败', () => {
  it('明确显示加载失败而不是空列表，点击重试使用现有启动动作', () => {
    const load = vi.fn(() => useChatStore.setState({ startupError: '' }));
    useChatStore.setState({ startupError: 'synthetic offline', loadStartup: load });
    render(<Sidebar conversations={[]} activeConversationId={null} onCreateConversation={() => {}} onSelectConversation={() => {}} onDeleteConversation={() => {}} />);
    expect(screen.getByRole('alert')).toHaveTextContent('会话列表未加载：synthetic offline');
    expect(screen.queryByText('暂无对话')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试加载' })); expect(load).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: '重试加载' })).not.toBeInTheDocument();
    act(() => useChatStore.setState({ startupError: 'synthetic retry failed' }));
    expect(screen.getByRole('alert')).toHaveTextContent('synthetic retry failed');
    expect(screen.getByRole('button', { name: '重试加载' })).toBeEnabled();
  });
  it('加载失败不遮蔽已存在的本地对话或新建入口', () => {
    useChatStore.setState({ startupError: 'synthetic offline' });
    const create = vi.fn(); const select = vi.fn();
    render(<Sidebar conversations={[{ id: 'local', title: '本地草稿会话', sessionId: '', updatedAt: '2026-09-25T00:00:00Z' }]} activeConversationId="local" onCreateConversation={create} onSelectConversation={select} onDeleteConversation={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /本地草稿会话 暂无会话/ })); expect(select).toHaveBeenCalledWith('local');
    fireEvent.click(screen.getByRole('button', { name: '新对话' })); expect(create).toHaveBeenCalledOnce();
    expect(screen.getByRole('alert')).toHaveTextContent('synthetic offline');
  });
});

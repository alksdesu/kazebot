import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ conversations: vi.fn(), request: vi.fn() }));
vi.mock('../../api/supervisorClient', () => ({ getConversations: mocks.conversations }));
vi.mock('../../store/settingsStore', () => ({ useSettingsStore: (select: (value: unknown) => unknown) => select({ adminToken: 'fixture-token' }) }));
vi.mock('../client', () => ({ featureRequest: mocks.request }));
vi.mock('../ui', () => ({ actionClass: '' }));
vi.mock('./ExecutionProgressCard', () => ({ ExecutionProgressCard: () => null }));
import { ConversationPlans } from './ConversationPlans';

beforeEach(() => { mocks.conversations.mockReset(); mocks.request.mockReset(); });
afterEach(() => cleanup());

describe('会话计划读取反馈', () => {
  it('首次读取失败不把未知状态显示成零项', async () => {
    mocks.conversations.mockRejectedValue(new Error('offline'));
    render(<ConversationPlans sessionId="fixture-session" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('计划状态读取失败');
    expect(screen.getByText(/暂时无法读取/)).toBeVisible();
    expect(screen.queryByText(/0项待处理/)).toBeNull();
  });

  it('连接恢复后可手动重读，真实空列表保持安静', async () => {
    mocks.conversations.mockRejectedValueOnce(new Error('offline')).mockResolvedValue([{ session_id: 'fixture-session', conversation_key: 'web:fixture' }]);
    mocks.request.mockResolvedValue({ plans: [] });
    render(<ConversationPlans sessionId="fixture-session" />);
    fireEvent.click(await screen.findByRole('button', { name: '重新读取计划' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(mocks.request).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/本会话的计划/)).toBeNull();
  });
});

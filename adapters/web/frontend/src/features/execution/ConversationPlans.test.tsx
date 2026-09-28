import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

const conversation = [{ session_id: 'fixture-session', conversation_key: 'web:fixture' }];
const plan = {
  id: 'P123456789abc', goal: '保存报表', work_scope: '仅写入指定文件', scope: 'web:fixture',
  revision: 1, version: 1, status: 'draft', inputs: [], steps: [], artifacts: [], items: [],
};

describe('会话计划轮询', () => {
  afterEach(() => vi.useRealTimers());

  it('上一轮读取没回来就跳过这几个间隔，放行后接着按 5 秒轮询', async () => {
    vi.useFakeTimers();
    mocks.conversations.mockResolvedValue(conversation);
    let release!: (value: unknown) => void;
    mocks.request.mockImplementationOnce(() => new Promise(resolve => { release = resolve; })).mockResolvedValue({ plans: [] });
    render(<ConversationPlans sessionId="fixture-session" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(mocks.request).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(mocks.request).toHaveBeenCalledTimes(1);

    await act(async () => { release({ plans: [plan] }); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('本会话的计划 · 1项待处理')).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(mocks.request).toHaveBeenCalledTimes(2);
    // 会话键读到一次就一直用，跳过的几轮里也没有重查
    expect(mocks.conversations).toHaveBeenCalledTimes(1);
  });

  it('首轮落地后每轮都慢过间隔，同一时间仍只有一个读在途', async () => {
    vi.useFakeTimers();
    mocks.conversations.mockResolvedValue(conversation);
    let round = 0;
    mocks.request.mockImplementation(async () => {
      const size = ++round;
      if (size > 1) await new Promise(resolve => { setTimeout(resolve, 12000); });
      return { plans: Array.from({ length: size }, (_, index) => ({ ...plan, id: `P${index}23456789ab` })) };
    });
    render(<ConversationPlans sessionId="fixture-session" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('本会话的计划 · 1项待处理')).toBeVisible();
    expect(mocks.request).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(mocks.request).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(11000); });
    expect(mocks.request).toHaveBeenCalledTimes(2);
    expect(screen.getByText('本会话的计划 · 1项待处理')).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText('本会话的计划 · 2项待处理')).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(mocks.request).toHaveBeenCalledTimes(3);

    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    expect(screen.getByText('本会话的计划 · 3项待处理')).toBeVisible();
    expect(mocks.request).toHaveBeenCalledTimes(3);
  });

  it('轮询在途时收到执行更新事件，仍然立刻重读', async () => {
    vi.useFakeTimers();
    mocks.conversations.mockResolvedValue(conversation);
    mocks.request.mockReturnValue(new Promise(() => undefined));
    render(<ConversationPlans sessionId="fixture-session" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(mocks.request).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(mocks.request).toHaveBeenCalledTimes(1);

    await act(async () => { window.dispatchEvent(new Event('clonoth:execution-updated')); await vi.advanceTimersByTimeAsync(0); });
    expect(mocks.request).toHaveBeenCalledTimes(2);
  });
});

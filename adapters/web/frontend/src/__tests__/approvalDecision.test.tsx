// 审批决定的唯一提交口：同一条同时只发一次；服务端回的状态和本次决定对不上就是已被别处处理，照实说；换了人或卸载后不再报结果。
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { approvalStatusLabel, staleApprovalMessage, submitApprovalDecision, useApprovalDecision } from '../hooks/useApprovalDecision';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  decideApproval: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

const approval = (status: string): api.AdminApproval => ({ approval_id: 'ap-1', status } as api.AdminApproval);

beforeEach(() => {
  vi.mocked(api.decideApproval).mockReset();
  useSettingsStore.setState({ adminToken: 'token-a' });
});

afterEach(() => {
  useSettingsStore.setState({ adminToken: '' });
});

describe('审批状态的说法', () => {
  it('认得的状态说中文，认不得的照原样，空的说未知', () => {
    expect(approvalStatusLabel('denied')).toBe('已拒绝');
    expect(approvalStatusLabel('expired')).toBe('expired');
    expect(approvalStatusLabel('')).toBe('未知');
    expect(staleApprovalMessage('allowed')).toBe('该审批已被处理，当前状态：已允许');
  });
});

describe('submitApprovalDecision', () => {
  it('回执状态和这次的决定对得上才算生效', async () => {
    vi.mocked(api.decideApproval).mockResolvedValueOnce(approval('allowed')).mockResolvedValueOnce(approval('denied'));

    await expect(submitApprovalDecision('ap-1', 'allow', '', 'token-a')).resolves.toEqual({ applied: true, status: 'allowed' });
    // 允许的请求到之前，别处已经拒了：后端回的是它现在的状态。
    await expect(submitApprovalDecision('ap-1', 'allow', '', 'token-a')).resolves.toEqual({ applied: false, status: 'denied' });
  });

  it('同一条审批同时只发一次，各自拿自己的决定去比', async () => {
    const request = deferred<api.AdminApproval>();
    vi.mocked(api.decideApproval).mockReturnValue(request.promise);

    const allow = submitApprovalDecision('ap-1', 'allow', '', 'token-a');
    const deny = submitApprovalDecision('ap-1', 'deny', '', 'token-a');
    request.resolve(approval('denied'));

    await expect(allow).resolves.toEqual({ applied: false, status: 'denied' });
    await expect(deny).resolves.toEqual({ applied: true, status: 'denied' });
    expect(api.decideApproval).toHaveBeenCalledOnce();
    expect(api.decideApproval).toHaveBeenCalledWith('token-a', 'ap-1', 'allow', '');
  });

  it('上一次提交结束后可以再提交', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new api.ApiError(503, '审批服务不可用')).mockResolvedValueOnce(approval('allowed'));

    await expect(submitApprovalDecision('ap-1', 'allow', '')).rejects.toThrow('503 审批服务不可用');
    await expect(submitApprovalDecision('ap-1', 'allow', '')).resolves.toEqual({ applied: true, status: 'allowed' });
    expect(api.decideApproval).toHaveBeenCalledTimes(2);
  });
});

describe('useApprovalDecision', () => {
  it('提交中这一条是忙的，重复点不会再发', async () => {
    const request = deferred<api.AdminApproval>();
    vi.mocked(api.decideApproval).mockReturnValue(request.promise);
    const { result } = renderHook(() => useApprovalDecision());

    let first!: Promise<unknown>;
    act(() => { first = result.current.decide('ap-1', 'allow'); });
    expect(result.current.isBusy('ap-1')).toBe(true);
    let second: unknown;
    await act(async () => { second = await result.current.decide('ap-1', 'allow'); });
    expect(second).toBeNull();

    await act(async () => { request.resolve(approval('allowed')); await first; });
    await expect(first).resolves.toEqual({ applied: true, status: 'allowed', failed: false, message: '' });
    expect(result.current.isBusy('ap-1')).toBe(false);
    expect(result.current.messageOf('ap-1')).toBe('');
    expect(api.decideApproval).toHaveBeenCalledOnce();
    expect(api.decideApproval).toHaveBeenCalledWith('token-a', 'ap-1', 'allow', '');
  });

  it('已被别处处理时说清它现在的状态', async () => {
    vi.mocked(api.decideApproval).mockResolvedValue(approval('denied'));
    const { result } = renderHook(() => useApprovalDecision());

    let outcome: unknown;
    await act(async () => { outcome = await result.current.decide('ap-1', 'allow'); });

    expect(outcome).toEqual({ applied: false, status: 'denied', failed: false, message: '该审批已被处理，当前状态：已拒绝' });
    expect(result.current.messageOf('ap-1')).toBe('该审批已被处理，当前状态：已拒绝');
  });

  it('提交失败把原因留下，下次提交前清掉', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new api.ApiError(503, '审批服务不可用'));
    const { result } = renderHook(() => useApprovalDecision());

    await act(async () => { await result.current.decide('ap-1', 'deny'); });
    expect(result.current.messageOf('ap-1')).toBe('503 审批服务不可用');

    const retry = deferred<api.AdminApproval>();
    vi.mocked(api.decideApproval).mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.decide('ap-1', 'deny'); });
    expect(result.current.messageOf('ap-1')).toBe('');
    await act(async () => { retry.resolve(approval('denied')); await pending; });
  });

  it('结果回来时已换人登录，不报这一趟的结果', async () => {
    const request = deferred<api.AdminApproval>();
    vi.mocked(api.decideApproval).mockReturnValue(request.promise);
    const { result } = renderHook(() => useApprovalDecision());

    let decision!: Promise<unknown>;
    act(() => { decision = result.current.decide('ap-1', 'allow'); });
    act(() => { useSettingsStore.setState({ adminToken: 'token-b' }); });
    await act(async () => { request.resolve(approval('denied')); await decision; });

    await expect(decision).resolves.toBeNull();
    expect(result.current.messageOf('ap-1')).toBe('');
    expect(result.current.isBusy('ap-1')).toBe(false);
  });

  it('卸载之后回来的结果也不报', async () => {
    const request = deferred<api.AdminApproval>();
    vi.mocked(api.decideApproval).mockReturnValue(request.promise);
    const { result, unmount } = renderHook(() => useApprovalDecision());

    let decision!: Promise<unknown>;
    act(() => { decision = result.current.decide('ap-1', 'allow'); });
    unmount();
    request.resolve(approval('allowed'));

    await expect(decision).resolves.toBeNull();
  });
});

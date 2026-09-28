// 请求时限的合同：读请求到点中止报超时、写请求不设时限、调用方 signal 与定时器的收尾。

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { READ_TIMEOUT_MS, RequestTimeoutError, fetchWithDeadline } from '../api/requestDeadline';
import { MOUNT } from '../api/supervisorClient';
import { featureRequest } from '../features/client';

const TEN_MINUTES = 10 * 60_000;

interface FetchCall { input: string; init: RequestInit; abortedAtCall: boolean }
interface Pending { resolve: (response: Response) => void; reject: (error: unknown) => void }

let calls: FetchCall[];
let pending: Pending[];

function stubFetch(): void {
  calls = [];
  pending = [];
  vi.stubGlobal('fetch', vi.fn((input: string, init: RequestInit = {}) => {
    calls.push({ input, init, abortedAtCall: init.signal?.aborted === true });
    return new Promise<Response>((resolve, reject) => {
      pending.push({ resolve, reject });
      const signal = init.signal;
      if (!signal) return;
      // 真 fetch 中止时拒绝的就是 signal.reason，照搬才能分清 AbortError 和超时错误
      const fail = () => reject(signal.reason);
      if (signal.aborted) fail();
      else signal.addEventListener('abort', fail, { once: true });
    });
  }));
}

interface Captured<T> { settled: boolean; value?: T; error?: unknown }

function capture<T>(promise: Promise<T>): Captured<T> {
  const state: Captured<T> = { settled: false };
  promise.then(
    value => { state.settled = true; state.value = value; },
    error => { state.settled = true; state.error = error; },
  );
  return state;
}

const jsonResponse = (): Response => ({ ok: true, json: async () => ({}) }) as unknown as Response;

beforeEach(() => { vi.useFakeTimers(); stubFetch(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('读请求的默认时限', () => {
  it('GET 到点中止并抛超时错误', async () => {
    const result = capture(fetchWithDeadline('/v1/ping'));
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1);
    expect(result.settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const error = result.error as RequestTimeoutError;
    expect(error).toBeInstanceOf(RequestTimeoutError);
    expect(error.name).toBe('RequestTimeoutError');
    expect(error.message).toBe('请求超时：60 秒内服务器没有应答');
    expect(error.timeoutMs).toBe(60_000);
    expect(READ_TIMEOUT_MS).toBe(60_000);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([undefined, 'GET', 'get', 'HEAD', 'head'])('method 为 %s 时按读请求计时', async method => {
    const result = capture(fetchWithDeadline('/v1/ping', method === undefined ? {} : { method }));
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1);
    expect(result.settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(result.error).toBeInstanceOf(RequestTimeoutError);
    expect((result.error as RequestTimeoutError).timeoutMs).toBe(60_000);
  });

  it('设了时限就给 fetch 一个新建的 signal，其余 init 原样带过去', async () => {
    const headers = { 'X-Probe': '1' };
    const init: RequestInit = { headers, cache: 'no-store' };
    capture(fetchWithDeadline('/v1/ping', init));
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toHaveLength(1);
    expect(calls[0].input).toBe('/v1/ping');
    expect(calls[0].init).not.toBe(init);
    expect(calls[0].init.headers).toBe(headers);
    expect(calls[0].init.cache).toBe('no-store');
    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal);
    expect(calls[0].init.signal!.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(1);
  });

  it('自定义毫秒数到点才中止', async () => {
    const result = capture(fetchWithDeadline('/v1/ping', {}, 1_000));
    await vi.advanceTimersByTimeAsync(999);
    expect(result.settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const error = result.error as RequestTimeoutError;
    expect(error).toBeInstanceOf(RequestTimeoutError);
    expect(error.message).toBe('请求超时：1 秒内服务器没有应答');
    expect(error.timeoutMs).toBe(1_000);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('不设时限的请求', () => {
  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'delete'])('%s 推过十分钟也不超时，init 原样透传', async method => {
    const init: RequestInit = { method, body: '{}' };
    const result = capture(fetchWithDeadline('/v1/write', init));
    await vi.advanceTimersByTimeAsync(TEN_MINUTES);
    expect(result.settled).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    expect(calls[0].init).toBe(init);
    expect(calls[0].init.signal).toBeUndefined();
  });

  it('显式传 null 让读请求也不设时限', async () => {
    const init: RequestInit = { method: 'GET' };
    const result = capture(fetchWithDeadline('/v1/ping', init, null));
    await vi.advanceTimersByTimeAsync(TEN_MINUTES);
    expect(result.settled).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    expect(calls[0].init).toBe(init);
    expect(calls[0].init.signal).toBeUndefined();
  });
});

describe('调用方传进来的 signal', () => {
  it('外部中止时抛原来的 AbortError，不是超时错误', async () => {
    const outer = new AbortController();
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(1_000);
    outer.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(result.error).not.toBeInstanceOf(RequestTimeoutError);
    expect((result.error as DOMException).name).toBe('AbortError');
    expect(result.error).toBe(outer.signal.reason);
    expect(calls[0].init.signal!.reason).toBe(outer.signal.reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('外部 signal 已中止就当场中止，不等到时限', async () => {
    const outer = new AbortController();
    outer.abort();
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    expect(calls[0].abortedAtCall).toBe(true);
    expect(calls[0].init.signal!.reason).toBe(outer.signal.reason);
    expect(result.error).toBe(outer.signal.reason);
    expect(result.error).not.toBeInstanceOf(RequestTimeoutError);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('拿到响应头后转发还留着，调用方仍取消得了正文', async () => {
    const outer = new AbortController();
    const added = vi.spyOn(outer.signal, 'addEventListener');
    const removed = vi.spyOn(outer.signal, 'removeEventListener');
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    const inner = calls[0].init.signal!;
    pending[0].resolve(jsonResponse());
    await vi.advanceTimersByTimeAsync(0);
    expect(result.settled).toBe(true);
    expect(added).toHaveBeenCalledTimes(1);
    expect(removed).not.toHaveBeenCalled();
    outer.abort();
    expect(inner.aborted).toBe(true);
    expect(inner.reason).toBe(outer.signal.reason);
  });

  it('超时收尾摘掉外部 signal 上的监听', async () => {
    const outer = new AbortController();
    const added = vi.spyOn(outer.signal, 'addEventListener');
    const removed = vi.spyOn(outer.signal, 'removeEventListener');
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS);
    expect(result.error).toBeInstanceOf(RequestTimeoutError);
    expect(added).toHaveBeenCalledTimes(1);
    expect(removed).toHaveBeenCalledWith('abort', added.mock.calls[0][1]);
  });

  it('网络错误收尾摘掉外部 signal 上的监听', async () => {
    const outer = new AbortController();
    const added = vi.spyOn(outer.signal, 'addEventListener');
    const removed = vi.spyOn(outer.signal, 'removeEventListener');
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    const failure = new TypeError('Failed to fetch');
    pending[0].reject(failure);
    await vi.advanceTimersByTimeAsync(0);
    expect(result.error).toBe(failure);
    expect(added).toHaveBeenCalledTimes(1);
    expect(removed).toHaveBeenCalledWith('abort', added.mock.calls[0][1]);
  });

  it('调用方取消收尾摘掉外部 signal 上的监听', async () => {
    const outer = new AbortController();
    const added = vi.spyOn(outer.signal, 'addEventListener');
    const removed = vi.spyOn(outer.signal, 'removeEventListener');
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    outer.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect((result.error as DOMException).name).toBe('AbortError');
    expect(added).toHaveBeenCalledTimes(1);
    expect(removed).toHaveBeenCalledWith('abort', added.mock.calls[0][1]);
  });

  it('调用方先取消、时限随后到点，报的还是 AbortError', async () => {
    const outer = new AbortController();
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    outer.abort();
    // 同步推时限，抢在 catch 判定之前让两边都中止过，才验得到"按先中止的那一方报"
    vi.advanceTimersByTime(READ_TIMEOUT_MS);
    await vi.advanceTimersByTimeAsync(0);
    expect(result.error).not.toBeInstanceOf(RequestTimeoutError);
    expect((result.error as DOMException).name).toBe('AbortError');
    expect(result.error).toBe(outer.signal.reason);
    expect(calls[0].init.signal!.reason).toBe(outer.signal.reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('时限先到点、调用方随后取消，报的还是超时错误', async () => {
    const outer = new AbortController();
    const result = capture(fetchWithDeadline('/v1/ping', { signal: outer.signal }));
    await vi.advanceTimersByTimeAsync(0);
    vi.advanceTimersByTime(READ_TIMEOUT_MS);
    outer.abort();
    await vi.advanceTimersByTimeAsync(0);
    const error = result.error as RequestTimeoutError;
    expect(error).toBeInstanceOf(RequestTimeoutError);
    expect(error.name).toBe('RequestTimeoutError');
    expect(error.message).toBe('请求超时：60 秒内服务器没有应答');
    expect(error.timeoutMs).toBe(60_000);
    expect(calls[0].init.signal!.reason).toBe(error);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('收尾不留定时器', () => {
  it('成功返回后清掉定时器', async () => {
    const result = capture(fetchWithDeadline('/v1/ping'));
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    const response = jsonResponse();
    pending[0].resolve(response);
    await vi.advanceTimersByTimeAsync(0);
    expect(result.value).toBe(response);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('非超时失败后也清掉定时器，原错误照旧抛出', async () => {
    const result = capture(fetchWithDeadline('/v1/ping'));
    await vi.advanceTimersByTimeAsync(0);
    const failure = new TypeError('Failed to fetch');
    pending[0].reject(failure);
    await vi.advanceTimersByTimeAsync(0);
    expect(result.error).toBe(failure);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('响应头到手后读正文再慢也不受限', async () => {
    let release: (text: string) => void = () => {};
    const body = new Promise<string>(resolve => { release = resolve; });
    const result = capture(fetchWithDeadline('/v1/slow'));
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1);
    pending[0].resolve({ ok: true, text: () => body } as unknown as Response);
    await vi.advanceTimersByTimeAsync(0);
    expect(result.error).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
    const text = capture(result.value!.text());
    await vi.advanceTimersByTimeAsync(TEN_MINUTES);
    expect(text.settled).toBe(false);
    expect(result.error).toBeUndefined();
    release('{"ok":true}');
    await vi.advanceTimersByTimeAsync(0);
    expect(text.value).toBe('{"ok":true}');
    expect(text.error).toBeUndefined();
  });
});

describe('featureRequest 沿用同一套时限', () => {
  it('默认 GET 到点报超时', async () => {
    const result = capture(featureRequest('/v1/community/state', { scope: 'qq_group:probe' }));
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1);
    expect(result.settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const error = result.error as RequestTimeoutError;
    expect(error).toBeInstanceOf(RequestTimeoutError);
    expect(error.message).toBe('请求超时：60 秒内服务器没有应答');
    expect(error.timeoutMs).toBe(60_000);
    expect(calls[0].input).toBe(`${MOUNT}/v1/community/state?scope=qq_group%3Aprobe`);
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('POST 推过十分钟也不超时', async () => {
    const result = capture(featureRequest('/v1/community/rules', { method: 'POST', body: { name: '规则' } }));
    await vi.advanceTimersByTimeAsync(TEN_MINUTES);
    expect(result.settled).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    expect(calls[0].input).toBe(`${MOUNT}/v1/community/rules`);
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.signal).toBeUndefined();
    expect(calls[0].init.body).toBe('{"name":"规则"}');
    pending[0].resolve(jsonResponse());
    await vi.advanceTimersByTimeAsync(0);
    expect(result.settled).toBe(true);
  });
});

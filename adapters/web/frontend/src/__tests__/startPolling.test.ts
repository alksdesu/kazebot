// 轮询的两条闩：上一轮没回来就跳过这一轮，stop 之后一次都不再发。first 决定头一轮是立刻还是等一个间隔。
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';

import { createLatch, startPolling } from '../utils/polling';

const INTERVAL_MS = 5000;

type RejectionListener = (...args: unknown[]) => void;

const nodeProcess = (globalThis as unknown as {
  process: {
    listeners(event: string): RejectionListener[];
    removeAllListeners(event: string): void;
    on(event: string, listener: RejectionListener): void;
    off(event: string, listener: RejectionListener): void;
  };
}).process;

// createLatch 把 pending.finally(...) 的派生 promise 丢了，job 失败时那条 rejection 没人接，runner 会把整轮判失败。
const muteUnhandledRejection = (): void => {
  const saved = nodeProcess.listeners('unhandledRejection');
  const swallow: RejectionListener = () => undefined;
  nodeProcess.removeAllListeners('unhandledRejection');
  nodeProcess.on('unhandledRejection', swallow);
  onTestFinished(() => {
    nodeProcess.off('unhandledRejection', swallow);
    saved.forEach((listener) => { nodeProcess.on('unhandledRejection', listener); });
  });
};

// running 闩在 job 的 promise settle 之后的微任务里才松开；再垫一个宏任务，Node 判定 rejection 没人接也要等到那时候。
const settle = async (): Promise<void> => {
  await Promise.resolve();
  await new Promise<void>((resolve) => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => { channel.port1.close(); resolve(); };
    channel.port2.postMessage(0);
  });
};

const defer = (): { promise: Promise<void>; resolve: () => void; reject: (reason: unknown) => void } => {
  let resolve!: () => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

type PollOptions = Parameters<typeof startPolling>[2];

const LAZY_OPTIONS: Array<[string, PollOptions]> = [
  ['整个选项缺省', undefined],
  ['选项在但 first 是 undefined', { first: undefined }],
];

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('createLatch', () => {
  it('第一次放行，上一个没回来时后面全挡掉，回来之后又放行', async () => {
    const inflight = defer();
    const job = vi.fn(() => inflight.promise);
    const run = createLatch();

    expect(run(job)).toBe(true);
    expect(run(job)).toBe(false);
    expect(run(job)).toBe(false);
    expect(job).toHaveBeenCalledTimes(1);

    inflight.resolve();
    await settle();

    expect(run(job)).toBe(true);
    expect(job).toHaveBeenCalledTimes(2);
  });

  it('job 同步抛时错不冒出调用处，闩照样放开让下一个 job 跑', async () => {
    muteUnhandledRejection();
    const boom = new Error('同步就炸了');
    const explode = vi.fn((): Promise<unknown> => { throw boom; });
    const next = vi.fn(async () => undefined);
    const run = createLatch();

    let started: boolean | undefined;
    expect(() => { started = run(explode); }).not.toThrow();
    expect(started).toBe(true);
    expect(explode).toHaveBeenCalledTimes(1);

    // 同步抛被包成 rejected promise，闩要等它 settle 之后的微任务才松开。
    expect(run(next)).toBe(false);
    await settle();

    expect(run(next)).toBe(true);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('job 返回的 promise 失败时闩也放开', async () => {
    muteUnhandledRejection();
    const inflight = defer();
    const failing = vi.fn(() => inflight.promise);
    const next = vi.fn(async () => undefined);
    const run = createLatch();

    expect(run(failing)).toBe(true);
    expect(run(next)).toBe(false);
    expect(next).not.toHaveBeenCalled();

    inflight.reject(new Error('503 读不回来'));
    await settle();

    expect(run(next)).toBe(true);
    expect(next).toHaveBeenCalledTimes(1);
  });
});

describe('startPolling', () => {
  it('给了 first 就先跑一次，再按间隔跑', async () => {
    const task = vi.fn(async () => undefined);
    const stop = startPolling(task, INTERVAL_MS, { first: task });

    expect(task).toHaveBeenCalledTimes(1);
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);

    stop();
  });

  it.each(LAZY_OPTIONS)('没给 first（%s）时要等第一个间隔到了才跑', (_label, options) => {
    const task = vi.fn(async () => undefined);
    const stop = startPolling(task, INTERVAL_MS, options);

    expect(task).not.toHaveBeenCalled();

    vi.advanceTimersByTime(INTERVAL_MS - 1);
    expect(task).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(task).toHaveBeenCalledTimes(1);

    stop();
  });

  it('first 还在途时，中间几个间隔的 task 一次都不发，first 回来后下一个间隔才发', async () => {
    const inflight = defer();
    const first = vi.fn(() => inflight.promise);
    const task = vi.fn(async () => undefined);
    const stop = startPolling(task, INTERVAL_MS, { first });

    expect(first).toHaveBeenCalledTimes(1);
    expect(task).not.toHaveBeenCalled();

    vi.advanceTimersByTime(INTERVAL_MS * 4);
    expect(task).not.toHaveBeenCalled();
    expect(first).toHaveBeenCalledTimes(1);

    inflight.resolve();
    await settle();
    expect(task).not.toHaveBeenCalled();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(1);
    expect(first).toHaveBeenCalledTimes(1);

    stop();
  });

  it('上一轮还没回来时，中间几个间隔全跳过，放行后下一个间隔又跑', async () => {
    const inflight = defer();
    const task = vi.fn(() => inflight.promise);
    const stop = startPolling(task, INTERVAL_MS, { first: task });
    expect(task).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(INTERVAL_MS * 4);
    expect(task).toHaveBeenCalledTimes(1);

    inflight.resolve();
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);

    stop();
  });

  it('上一轮失败了照样放开下一轮', async () => {
    muteUnhandledRejection();
    const inflight = defer();
    // 失败也要松闩，挂在 then 上的话一次读失败就把轮询永久停住了。
    const task = vi.fn((): Promise<void> => Promise.resolve());
    task.mockReturnValueOnce(inflight.promise);
    const stop = startPolling(task, INTERVAL_MS, { first: task });
    expect(task).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(1);

    inflight.reject(new Error('503 读不回来'));
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);

    stop();
  });

  it('stop 之后推多少个间隔都不再跑', async () => {
    const task = vi.fn(async () => undefined);
    const stop = startPolling(task, INTERVAL_MS, { first: task });
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);
    await settle();

    stop();

    vi.advanceTimersByTime(INTERVAL_MS * 5);
    expect(task).toHaveBeenCalledTimes(2);
  });

  it('first 同步抛时异常不冒出 startPolling，调用方照样拿到停止函数', async () => {
    muteUnhandledRejection();
    const boom = new Error('同步就炸了');
    const task = vi.fn((): Promise<unknown> => { throw boom; });

    let stop!: () => void;
    expect(() => { stop = startPolling(task, INTERVAL_MS, { first: task }); }).not.toThrow();
    expect(task).toHaveBeenCalledTimes(1);
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);

    stop();
    vi.advanceTimersByTime(INTERVAL_MS * 5);
    expect(task).toHaveBeenCalledTimes(2);
    // 末轮的 rejection 要在静音还生效时判定完，否则漏给 runner 变成整轮失败。
    await settle();
  });

  it('间隔里同步抛之后闩照样放开，后面几轮接着跑', async () => {
    const boom = new Error('同步就炸了');
    muteUnhandledRejection();
    const task = vi.fn((): Promise<unknown> => { throw boom; });
    const stop = startPolling(task, INTERVAL_MS);

    expect(() => vi.advanceTimersByTime(INTERVAL_MS)).not.toThrow();
    expect(task).toHaveBeenCalledTimes(1);
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(2);
    await settle();

    vi.advanceTimersByTime(INTERVAL_MS);
    expect(task).toHaveBeenCalledTimes(3);

    stop();
    // 末轮的 rejection 要在静音还生效时判定完，否则漏给 runner 变成整轮失败。
    await settle();
  });
});

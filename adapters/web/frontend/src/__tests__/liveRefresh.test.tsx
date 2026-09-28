// 生效状态只能拉：进页拉一次、每 15 秒一次、切回页面补一次；藏在后台、没登录、上一次还没回来时一次都不拉，被在途挡掉的那次不占去重间隔。
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useLiveRefresh } from '../console/liveRefresh';

const REFRESH_MS = 15_000;
const RERUN_GAP_MS = 1000;

// jsdom 的 document.hidden 是原型上的只读 getter，只能在实例上盖一个同名的。
const setHidden = (hidden: boolean): void => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
};

const focus = () => act(() => { window.dispatchEvent(new Event('focus')); });
const visibilityChange = () => act(() => { document.dispatchEvent(new Event('visibilitychange')); });
const tick = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });

// 在途闩要等 refresh 的 promise settle 之后的微任务才松开，不放干队列后面每一次都会被挡掉。
const settle = async (): Promise<void> => { await Promise.resolve(); await Promise.resolve(); };

let refresh: ReturnType<typeof vi.fn>;

const pendingRefresh = (): { release: () => void } => {
  let resolve!: () => void;
  refresh.mockImplementation(() => new Promise<void>((res) => { resolve = res; }));
  return { release: () => resolve() };
};

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
  refresh = vi.fn(async () => undefined);
});

afterEach(() => {
  vi.useRealTimers();
  Reflect.deleteProperty(document, 'hidden');
});

describe('useLiveRefresh', () => {
  it('挂载就拉一次，带上 token', () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledWith('admin-token');
  });

  it('之后每 15 秒拉一次', async () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    await settle();

    tick(REFRESH_MS - 1);
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(1);
    expect(refresh).toHaveBeenCalledTimes(2);
    await settle();

    tick(REFRESH_MS);
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it('切回标签页就补一次，focus 和 visibilitychange 都算', async () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    await settle();

    tick(RERUN_GAP_MS);
    focus();
    expect(refresh).toHaveBeenCalledTimes(2);
    await settle();

    tick(RERUN_GAP_MS);
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it('隔不到一秒的第二次跳过 —— 切回标签页时两个事件前后脚都会来', async () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    await settle();

    tick(RERUN_GAP_MS - 1);
    focus();
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(1);
    focus();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('上一次还没回来时，定时、focus、visibilitychange 都不再拉，回来之后照常', async () => {
    const inflight = pendingRefresh();
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(REFRESH_MS * 3);
    focus();
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);

    inflight.release();
    await settle();

    tick(REFRESH_MS);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('在途挡掉的 focus 不占去重间隔，读回来之后紧接着的 focus 立刻拉', async () => {
    const inflight = pendingRefresh();
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(RERUN_GAP_MS);
    focus();
    expect(refresh).toHaveBeenCalledTimes(1);

    inflight.release();
    await settle();

    // 时间一毫秒都没走，挡掉那次要是记了账，这里就被去重间隔盖住了。
    focus();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('在途挡掉的定时那次不占去重间隔，读回来之后紧接着的 visibilitychange 立刻拉', async () => {
    const inflight = pendingRefresh();
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(REFRESH_MS);
    expect(refresh).toHaveBeenCalledTimes(1);

    inflight.release();
    await settle();

    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('页面藏在后台时一次都不拉，切回前台才补上', () => {
    setHidden(true);
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).not.toHaveBeenCalled();

    tick(REFRESH_MS);
    focus();
    expect(refresh).not.toHaveBeenCalled();

    setHidden(false);
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['空串', ''],
    ['null', null],
    ['undefined', undefined],
  ])('没 token（%s）时什么都不做', (_label: string, token: string | null | undefined) => {
    renderHook(() => useLiveRefresh(refresh, token));

    tick(REFRESH_MS * 2);
    focus();
    visibilityChange();

    expect(refresh).not.toHaveBeenCalled();
  });

  it('登录之后才开始拉', () => {
    const { rerender } = renderHook(
      ({ token }: { token: string | null }) => useLiveRefresh(refresh, token),
      { initialProps: { token: null as string | null } },
    );
    expect(refresh).not.toHaveBeenCalled();

    rerender({ token: 'admin-token' });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledWith('admin-token');
  });

  it('卸载之后定时器和监听都撤掉', async () => {
    const { unmount } = renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).toHaveBeenCalledTimes(1);
    // 不放行头一次的话，后面不拉是在途闩挡的，看不出定时器和监听有没有真撤掉。
    await settle();

    unmount();

    tick(REFRESH_MS * 3);
    focus();
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

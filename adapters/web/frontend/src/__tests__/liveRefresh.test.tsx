// 生效状态只能拉：进页拉一次、每 15 秒一次、切回页面补一次，藏在后台和没登录时一次都不拉。
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

let refresh: ReturnType<typeof vi.fn>;

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

  it('之后每 15 秒拉一次', () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));

    tick(REFRESH_MS - 1);
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(1);
    expect(refresh).toHaveBeenCalledTimes(2);

    tick(REFRESH_MS);
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it('切回标签页就补一次，focus 和 visibilitychange 都算', () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));

    tick(RERUN_GAP_MS);
    focus();
    expect(refresh).toHaveBeenCalledTimes(2);

    tick(RERUN_GAP_MS);
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it('隔不到一秒的第二次跳过 —— 切回标签页时两个事件前后脚都会来', () => {
    renderHook(() => useLiveRefresh(refresh, 'admin-token'));

    tick(RERUN_GAP_MS - 1);
    focus();
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);

    tick(1);
    focus();
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

  it('卸载之后定时器和监听都撤掉', () => {
    const { unmount } = renderHook(() => useLiveRefresh(refresh, 'admin-token'));
    expect(refresh).toHaveBeenCalledTimes(1);

    unmount();

    tick(REFRESH_MS * 3);
    focus();
    visibilityChange();
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

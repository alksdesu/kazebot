// 请求作废的合同：卸载、范围变化、换登录、同一路被后发的请求顶掉，四种情况下迟到的响应都不落地。
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { RequestScope, STALE_TICKET, SupersededRequest } from '../api/requestGuard';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useSettingsStore } from '../store/settingsStore';

beforeEach(() => { useSettingsStore.setState({ adminToken: 'token-a', isAuthenticated: false }); });
afterEach(() => { useSettingsStore.setState({ adminToken: null, isAuthenticated: false }); });

describe('RequestScope', () => {
  it('同一路后发的顶掉先发的，并中止它', () => {
    const scope = new RequestScope();
    const first = scope.begin('load');
    const second = scope.begin('load');

    expect(first.isCurrent()).toBe(false);
    expect(first.signal.aborted).toBe(true);
    expect(first.signal.reason).toBeInstanceOf(SupersededRequest);
    expect(second.isCurrent()).toBe(true);
  });

  it('不同路、不给路的请求互不顶替', () => {
    const scope = new RequestScope();
    const free = scope.begin();
    const load = scope.begin('load');
    scope.begin('artifact');
    scope.begin();

    expect(free.isCurrent()).toBe(true);
    expect(load.isCurrent()).toBe(true);
  });

  it('drop 只作废那一路还没回来的请求', () => {
    const scope = new RequestScope();
    const load = scope.begin('load');
    const save = scope.begin();

    scope.drop('load');
    scope.drop('never-started');

    expect(load.isCurrent()).toBe(false);
    expect(save.isCurrent()).toBe(true);
    expect(scope.begin('load').isCurrent()).toBe(true);
  });

  it('reset 作废全部在途请求，之后发的照常有效', () => {
    const scope = new RequestScope();
    const free = scope.begin();
    const load = scope.begin('load');

    scope.reset();

    expect([free.isCurrent(), load.isCurrent()]).toEqual([false, false]);
    expect([free.signal.aborted, load.signal.aborted]).toEqual([true, true]);
    expect(scope.begin().isCurrent()).toBe(true);
    expect(scope.begin('load').isCurrent()).toBe(true);
  });

  it('换了登录，之前发出的请求作废；公开端点的不受影响', () => {
    const scope = new RequestScope();
    const anonymous = new RequestScope({ anonymous: true });
    const signedIn = scope.begin('load');
    const open = anonymous.begin('load');

    useSettingsStore.setState({ adminToken: 'token-b' });

    expect(signedIn.isCurrent()).toBe(false);
    expect(open.isCurrent()).toBe(true);
    expect(scope.begin('load').isCurrent()).toBe(true);
  });

  it('令牌没变、核验状态变了，也算换了登录', () => {
    const scope = new RequestScope();
    const ticket = scope.begin();

    useSettingsStore.setState({ isAuthenticated: true });

    expect(ticket.isCurrent()).toBe(false);
    expect(scope.begin().isCurrent()).toBe(true);
  });

  it('登录换走又换回同一个令牌，之前发出的请求照样作废', () => {
    const scope = new RequestScope();
    const ticket = scope.begin();

    useSettingsStore.setState({ adminToken: 'token-b' });
    useSettingsStore.setState({ adminToken: 'token-a' });

    expect(ticket.isCurrent()).toBe(false);
  });

  it('check 在作废后抛 SupersededRequest', () => {
    const scope = new RequestScope();
    const ticket = scope.begin('load');
    expect(() => ticket.check()).not.toThrow();

    scope.drop('load');

    expect(() => ticket.check()).toThrow(SupersededRequest);
  });

  it('STALE_TICKET 从一开始就作废', () => {
    expect(STALE_TICKET.isCurrent()).toBe(false);
    expect(STALE_TICKET.signal.aborted).toBe(true);
  });
});

describe('useRequestGuard', () => {
  it('挂载期间有效，卸载后在途请求作废、再发的也作废', () => {
    const { result, unmount } = renderHook(() => useRequestGuard('scope'));
    const guard = result.current;
    const ticket = guard.begin('load');
    expect(guard.isCurrent()).toBe(true);
    expect(ticket.isCurrent()).toBe(true);

    unmount();

    expect(guard.isCurrent()).toBe(false);
    expect(ticket.signal.aborted).toBe(true);
    expect(guard.begin('load').isCurrent()).toBe(false);
  });

  it('范围变了，旧范围发出的请求作废，旧那一版页面再发的也作废', () => {
    const { result, rerender } = renderHook(({ scope }) => useRequestGuard(scope), { initialProps: { scope: 'group:a' } });
    const previous = result.current;
    const ticket = previous.begin();

    rerender({ scope: 'group:b' });

    expect(ticket.isCurrent()).toBe(false);
    expect(ticket.signal.aborted).toBe(true);
    expect(previous.isCurrent()).toBe(false);
    expect(previous.begin().isCurrent()).toBe(false);
    expect(result.current.key).not.toBe(previous.key);
    expect(result.current.begin().isCurrent()).toBe(true);
  });

  it('范围换走又换回原值，旧那一版发出的请求不会重新生效', () => {
    const { result, rerender } = renderHook(({ scope }) => useRequestGuard(scope), { initialProps: { scope: 'web:materials' } });
    const first = result.current;
    const ticket = first.begin();

    rerender({ scope: 'qq_group:b' });
    rerender({ scope: 'web:materials' });

    expect(result.current.key).toBe(first.key);
    expect(result.current).not.toBe(first);
    expect(first.isCurrent()).toBe(false);
    expect(ticket.isCurrent()).toBe(false);
    expect(() => first.check()).toThrow(SupersededRequest);
  });

  it('范围没变时守卫保持同一个，可以放进依赖', () => {
    const { result, rerender } = renderHook(({ scope }) => useRequestGuard(scope), { initialProps: { scope: ['web:console', 'bot'] } });
    const previous = result.current;

    rerender({ scope: ['web:console', 'bot'] });

    expect(result.current).toBe(previous);
  });

  it('换登录立刻作废在途请求，key 跟着换，但不含令牌本身', () => {
    const { result } = renderHook(() => useRequestGuard('scope'));
    const previous = result.current;
    const ticket = previous.begin('load');

    act(() => { useSettingsStore.setState({ adminToken: 'token-b' }); });

    expect(ticket.isCurrent()).toBe(false);
    expect(previous.isCurrent()).toBe(false);
    expect(result.current.key).not.toBe(previous.key);
    expect(result.current.key).not.toContain('token-b');
    expect(result.current.begin('load').isCurrent()).toBe(true);
  });

  it('换登录后、页面重渲染之前，旧守卫已经不认了', () => {
    const { result } = renderHook(() => useRequestGuard('scope'));
    const guard = result.current;

    useSettingsStore.setState({ adminToken: 'token-b' });

    expect(guard.isCurrent()).toBe(false);
    expect(() => guard.check()).toThrow(SupersededRequest);
  });

  it('登录换走又换回，key 也不回到原来那个', () => {
    const { result } = renderHook(() => useRequestGuard('scope'));
    const previous = result.current;

    act(() => { useSettingsStore.setState({ adminToken: 'token-b' }); });
    act(() => { useSettingsStore.setState({ adminToken: 'token-a' }); });

    expect(result.current.key).not.toBe(previous.key);
    expect(previous.isCurrent()).toBe(false);
    expect(result.current.isCurrent()).toBe(true);
  });

  it('公开端点的守卫不随登录换代', () => {
    const { result } = renderHook(() => useRequestGuard('directory', { anonymous: true }));
    const previous = result.current;
    const ticket = previous.begin('load');

    act(() => { useSettingsStore.setState({ adminToken: 'token-b' }); });

    expect(result.current).toBe(previous);
    expect(ticket.isCurrent()).toBe(true);
  });

  it('旧那一版页面的 drop 碰不到新范围的请求', () => {
    const { result, rerender } = renderHook(({ scope }) => useRequestGuard(scope), { initialProps: { scope: 'a' } });
    const previous = result.current;
    rerender({ scope: 'b' });
    const ticket = result.current.begin('load');

    previous.drop('load');

    expect(ticket.isCurrent()).toBe(true);
  });
});

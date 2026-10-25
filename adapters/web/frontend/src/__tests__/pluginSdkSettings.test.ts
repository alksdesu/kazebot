// 插件 SDK 给的设置里没有管理员令牌：调本实例接口走 sdk.http.request，令牌由宿主在最内层补上。
import { afterEach, describe, expect, it } from 'vitest';

import { extensions } from '../plugins/registry';
import { createPluginSdk, type PluginResources } from '../plugins/sdk';
import { useSettingsStore } from '../store/settingsStore';

const resources = (): PluginResources => ({
  signal: new AbortController().signal,
  own: (dispose) => dispose,
  release: (dispose) => dispose,
  style: () => () => undefined,
  asset: async () => new Blob([]),
  request: async () => new Response(null, { status: 204 }),
});

const transactions: Array<ReturnType<typeof extensions.transaction>> = [];

function sdkFor() {
  const transaction = extensions.transaction('settings-probe', String(transactions.length));
  transactions.push(transaction);
  return createPluginSdk({ id: 'settings-probe', generation: '1', config: {}, metadata: {} }, transaction, resources());
}

afterEach(() => {
  for (const transaction of transactions.splice(0)) transaction.dispose();
  useSettingsStore.setState({ adminToken: '', isAuthenticated: false });
});

describe('插件看到的设置', () => {
  it('读不到管理员令牌，其余设置照常', () => {
    useSettingsStore.setState({ adminToken: 'secret-token', isAuthenticated: true });

    const settings = sdkFor().state.settings.getState();

    expect(Object.keys(settings)).not.toContain('adminToken');
    expect(settings.isAuthenticated).toBe(true);
    expect(JSON.stringify(settings)).not.toContain('secret-token');
  });

  it('订阅收到的新旧两份里也没有令牌', () => {
    const seen: object[] = [];
    const unsubscribe = sdkFor().state.settings.subscribe((state: object, previous: object) => { seen.push(state, previous); });

    useSettingsStore.setState({ adminToken: 'rotated-token' });
    unsubscribe();

    expect(seen).toHaveLength(2);
    for (const snapshot of seen) expect(Object.keys(snapshot)).not.toContain('adminToken');
  });

  // useSyncExternalStore 每次都拿到新对象会一直重渲染。
  it('设置没变时每次拿到的是同一个对象', () => {
    const settings = sdkFor().state.settings;

    expect(settings.getState()).toBe(settings.getState());
  });
});

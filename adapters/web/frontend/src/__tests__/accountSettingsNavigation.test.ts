import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { window.history.replaceState(null, '', '/'); });

describe('账号设置的初始地址', () => {
  it.each([
    '/web/?view=settings&tab=qq-account',
    '/web/?view=console&domain=account',
    '/i/second/web/?view=settings&tab=platform-qq&section=account',
  ])('%s 刷新后仍定位通用的账号分区', async (url) => {
    vi.resetModules();
    window.history.replaceState(null, '', `${url}&keep=1#details`);
    const { useViewStore } = await import('../store/viewStore');

    expect(useViewStore.getState()).toMatchObject({
      viewMode: 'settings', activeSettingsTab: 'general', activeSettingsSection: 'account',
    });
    const params = new URLSearchParams(window.location.search);
    expect(params.get('tab')).toBe('general');
    expect(params.get('section')).toBe('account');
    expect(params.has('domain')).toBe(false);
    expect(params.get('keep')).toBe('1');
    expect(window.location.pathname).toBe(url.split('?')[0]);
    expect(window.location.hash).toBe('#details');
  });

  it('旧账号地址显式指名其他 QQ 分区时保持该目标', async () => {
    vi.resetModules();
    window.history.replaceState(null, '', '/web/?view=settings&tab=qq-account&section=link');
    const { useViewStore } = await import('../store/viewStore');

    expect(useViewStore.getState()).toMatchObject({ activeSettingsTab: 'platform-qq', activeSettingsSection: 'link' });
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('platform-qq');
    expect(new URLSearchParams(window.location.search).get('section')).toBe('link');
  });
});

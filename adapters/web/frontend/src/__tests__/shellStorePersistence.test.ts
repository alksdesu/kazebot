import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

beforeEach(() => { localStorage.clear(); vi.resetModules(); });
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('壳的本地偏好容错', () => {
  it.each([{ version: 2, rightPanelWidth: 420 }, { version: 2, titleGeneration: 'bad', thinkingDefaultCollapsed: 'false', rightPanelWidth: null }])('残缺或字段损坏的偏好保留明确默认值：%j', async stored => {
    const { scopedKey } = await import('../store/storageKey');
    localStorage.setItem(scopedKey('clonoth_client_prefs'), JSON.stringify(stored));
    const { useClientPrefsStore } = await import('../store/clientPrefsStore');
    expect(useClientPrefsStore.getState()).toMatchObject({ titleGeneration: 'first-message', thinkingDefaultCollapsed: true, toolResultsDefaultCollapsed: true, rightPanelWidth: stored.rightPanelWidth || 288 });
  });
  it('存储写入拒绝时保留当前偏好与登录状态并显示警告', async () => {
    const { useClientPrefsStore } = await import('../store/clientPrefsStore');
    const { useSettingsStore } = await import('../store/settingsStore');
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    expect(() => useClientPrefsStore.getState().setRightPanelWidth(500)).not.toThrow();
    expect(() => useSettingsStore.getState().setAdminToken('synthetic-test-token')).not.toThrow();
    expect(() => useSettingsStore.getState().setEntryNodeId('entry')).not.toThrow();
    expect(useClientPrefsStore.getState()).toMatchObject({ rightPanelWidth: 500, storageWarning: expect.stringContaining('无法保存') });
    expect(useSettingsStore.getState()).toMatchObject({ adminToken: 'synthetic-test-token', entryNodeId: 'entry', storageWarning: expect.stringContaining('存储不可用') });
  });
  it('禁止读取存储不阻断模块启动', async () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    const { useSettingsStore } = await import('../store/settingsStore');
    const { useClientPrefsStore } = await import('../store/clientPrefsStore');
    expect(useSettingsStore.getState()).toMatchObject({ adminToken: null, entryNodeId: '', storageWarning: expect.stringContaining('存储不可用') });
    expect(useClientPrefsStore.getState()).toMatchObject({ rightPanelWidth: 288, thinkingDefaultCollapsed: true });
  });
});
